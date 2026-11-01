"""Frozen quantum vocabulary, admission, provenance and market isolation."""
import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from nano.intent.quantum import (QuantumCommandIR, compile_quantum, structured_reply,
                                 validate_catalog, validate_quantum_document)
from nano.intent.tokens import IntentInputError
from nano.quantum import QuantumValidationError

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "nano/intent/fixtures/quantum_commands_v1.json"
SCHEMA = ROOT / "nano/intent/schemas/quantum-command-v1.schema.json"
BUNDLE = json.loads(FIXTURE.read_text(encoding="utf-8"))


def catalog():
    return copy.deepcopy(BUNDLE["catalog"])


@pytest.mark.parametrize("fixture", BUNDLE["fixtures"], ids=lambda f: f["id"])
def test_frozen_quantum_command_corpus(fixture):
    ir = compile_quantum(fixture["source"], catalog=catalog())
    assert ir.operation == fixture["operation"]
    assert dict(ir.args) == fixture["args"]
    assert [quantity.to_dict() for quantity in ir.quantities] == fixture["quantities"]
    assert ir.source_hash == "sha256:" + hashlib.sha256(fixture["source"].encode()).hexdigest()
    assert [d.code for d in ir.diagnostics] == ([fixture["diagnostic"]] if fixture["diagnostic"] else [])
    if fixture["diagnostic"]:
        assert not ir.steps and not ir.effects and not ir.args and not ir.entities
    else:
        assert len(ir.steps) == 1
        assert ir.steps[0].capability == fixture["capability"]
        assert dict(ir.steps[0].args) == fixture["args"]
    assert all(entity.kind not in ("security", "ticker", "topic") for entity in ir.entities)
    assert all(step.capability.startswith("quantum.") for step in ir.steps)
    assert not {"llm.call", "data.read", "external.write", "trade.request"} & set(ir.effects)
    for node in (*ir.entities, *ir.quantities):
        assert fixture["source"][node.span[0]:node.span[1]] == node.raw


def test_schema_and_same_validator_admit_every_frozen_result():
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator(json.loads(SCHEMA.read_text()))
    validator.check_schema(validator.schema)
    for fixture in BUNDLE["fixtures"]:
        ir = compile_quantum(fixture["source"], catalog=catalog())
        document = ir.to_dict()
        validator.validate(document)
        assert QuantumCommandIR.from_dict(document, catalog=catalog()).to_dict() == document


def test_equivalent_phrases_share_semantics_but_keep_distinct_provenance():
    groups = {f["equivalenceGroup"] for f in BUNDLE["fixtures"]} - {None}
    for group in groups:
        plans = [compile_quantum(f["source"], catalog=catalog()) for f in BUNDLE["fixtures"] if f["equivalenceGroup"] == group]
        assert len({p.plan_digest for p in plans}) == 1
        assert len({p.source_hash for p in plans}) == len(plans)
    canonical = compile_quantum("simulate plan demo-plan on local with 100 shots 1 circuit 0 qpu seconds", catalog=catalog())
    source = "  SIMULATE plan DEMO-PLAN on LOCAL with 0.0 QPU seconds 1 circuit 100 shots  "
    varied = compile_quantum(source, catalog=catalog())
    assert varied.plan_digest == canonical.plan_digest
    assert varied.source_hash != canonical.source_hash
    assert [source[q.span[0]:q.span[1]] for q in varied.quantities] == [q.raw for q in varied.quantities]


def test_reference_snapshot_binds_plan_operation_and_caps_without_alias_identity():
    phrase = "compile plan demo-plan on local"
    original = compile_quantum(phrase, catalog=catalog())
    for key, value in (("operation_ref", "operation:different"), ("max_shots", 999),
                       ("operation_digest", "sha256:" + "1" * 64)):
        changed = catalog()
        changed["plans"]["demo-plan"][key] = value
        assert compile_quantum(phrase, catalog=changed).plan_digest != original.plan_digest
    aliases = catalog()
    aliases["plans"]["another-name"] = copy.deepcopy(aliases["plans"]["demo-plan"])
    assert compile_quantum("compile plan another-name on local", catalog=aliases).plan_digest == original.plan_digest
    changed = catalog()
    changed["banks"]["demo-bank"]["digest"] = "sha256:" + "2" * 64
    assert compile_quantum(phrase, catalog=changed).plan_digest == original.plan_digest
    bank_phrase = "check plan demo-plan using evidence bank demo-bank"
    assert compile_quantum(bank_phrase, catalog=changed).plan_digest != compile_quantum(bank_phrase, catalog=catalog()).plan_digest


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(extra=True),
    lambda c: c.pop("plans"),
    lambda c: c.update(version=True),
    lambda c: c["plans"]["demo-plan"].update(max_shots=True),
    lambda c: c["plans"]["demo-plan"].update(max_circuits=3.0),
    lambda c: c["plans"]["demo-plan"].update(operation_ref="\ud800"),
    lambda c: c["plans"]["demo-plan"].update(operation_kind="arbitrary_circuit"),
    lambda c: c["backends"]["simulator"].update(kind="hardware"),
    lambda c: c["banks"]["demo-bank"].update(digest="wrong"),
    lambda c: c["jobs"]["demo-job"].update(plan_id="absent"),
    lambda c: c["jobs"]["demo-job"].update(state=True),
    lambda c: c["receipts"]["demo-receipt"].update(job_id="absent"),
])
def test_catalog_is_strict_and_names_do_not_supply_defaults(mutate):
    changed = catalog()
    mutate(changed)
    with pytest.raises(QuantumValidationError, match="CATALOG"):
        compile_quantum("check plan demo-plan", catalog=changed)


def test_absent_catalog_and_unknown_resource_abstain_without_authority():
    ir = compile_quantum("check plan demo-plan")
    assert ir.diagnostics[0].code == "MISSING_CATALOG" and not ir.steps
    empty = {"version": "quantum-catalog/1", **{key: {} for key in ("backends", "plans", "banks", "jobs", "receipts")}}
    ir = compile_quantum("check plan demo-plan", catalog=empty)
    assert ir.diagnostics[0].code == "UNKNOWN_REFERENCE" and not ir.steps
    admitted = validate_catalog(catalog())
    assert compile_quantum("check plan demo-plan", catalog=admitted).operation == "check"
    with pytest.raises(TypeError):
        admitted.payload["plans"]["demo-plan"]["max_shots"] = 999


def test_receipt_and_referenced_job_must_identify_same_canonical_plan():
    changed = catalog()
    changed["plans"]["second-plan"] = {**changed["plans"]["demo-plan"], "id": "plan:second"}
    changed["receipts"]["demo-receipt"]["plan_id"] = "plan:second"
    with pytest.raises(QuantumValidationError, match="CATALOG: receipt and referenced job"):
        compile_quantum("explain receipt demo-receipt", catalog=changed)
    changed["jobs"]["demo-job"]["plan_id"] = "plan:second"
    ir = compile_quantum("explain receipt demo-receipt", catalog=changed)
    reply = structured_reply(ir, catalog=changed)
    assert ir.args["receipt_id"] == "receipt:demo"
    assert reply["references"] == {"receipt_id": "receipt:demo", "job_id": "job:demo", "plan_id": "plan:second"}
    job = compile_quantum("inspect status job demo-job", catalog=changed)
    assert job.args["job_id"] == "job:demo"
    assert structured_reply(job, catalog=changed)["references"]["plan_id"] == "plan:second"


@pytest.mark.parametrize("source", [
    "compile plan demo-plan on local; submit it",
    "compile plan demo-plan on local\nsubmit it",
    "simulate plan demo-plan on local with 100 shots 1 circuit 0 qpu seconds submit",
    "simulate plan demo-plan on local with nan shots 1 circuit 0 qpu seconds",
    "simulate plan demo-plan on local with 1e2 shots 1 circuit 0 qpu seconds",
    "simulate plan demo-plan on local with 1001 shots 1 circuit 0 qpu seconds",
    "simulate plan demo-plan on local with 100 shots 4 circuits 0 qpu seconds",
    "prepare submission plan demo-plan on local with 100 shots 1 circuit 0 qpu seconds",
    "check plan demo-plan using evidence bank demo-bank extra",
    "compile arbitrary bell circuit on local",
])
def test_unconsumed_text_unsupported_effects_and_bounds_abstain(source):
    ir = compile_quantum(source, catalog=catalog())
    assert ir.diagnostics and not ir.steps and not ir.effects


def test_typo_suggestion_is_bounded_and_never_corrects_source():
    source = "simlate plan demo-plan on local with 100 shots 1 circuit 0 qpu seconds"
    ir = compile_quantum(source, catalog=catalog())
    assert ir.diagnostics[0].suggestions == ("simulate",)
    assert ir.source == source and not ir.steps
    assert not compile_quantum("x" * 100, catalog=catalog()).diagnostics[0].suggestions


@pytest.mark.parametrize("source", [None, True, 100, "\ud800", "x" * 4097, " ".join(["x"] * 257)])
def test_bounded_input_diagnostics(source):
    with pytest.raises(IntentInputError):
        compile_quantum(source, catalog=catalog())


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(extra=True),
    lambda d: d.update(operation="prepare_submission"),
    lambda d: d["args"].update(shots=True),
    lambda d: d["args"].update(shots=100.0),
    lambda d: d["steps"][0].update(capability="market.quote"),
    lambda d: d["steps"][0].update(kind="external.write"),
    lambda d: d.update(effects=["external.write"]),
    lambda d: d.update(quantities=tuple(d["quantities"])),
    lambda d: d.update(plan_digest="sha256:" + "0" * 64),
    lambda d: d["catalog_snapshot"]["resources"]["plan"].update(max_shots=999),
])
def test_model_or_manual_proposals_use_identical_admission(mutate):
    document = compile_quantum("simulate plan demo-plan on local with 100 shots 1 circuit 0 qpu seconds", catalog=catalog()).to_dict()
    mutate(document)
    with pytest.raises(QuantumValidationError, match="COMMAND"):
        validate_quantum_document(document, catalog=catalog())


def test_diagnostic_cannot_be_forged_into_executable_plan():
    document = compile_quantum("do not compile plan demo-plan on local", catalog=catalog()).to_dict()
    document["steps"] = [{"kind": "local_compile", "capability": "quantum.compile", "args": {"plan_id": "plan:demo"}}]
    with pytest.raises(QuantumValidationError, match="COMMAND"):
        validate_quantum_document(document, catalog=catalog())


def test_structured_replies_report_only_supplied_observation_or_proposal():
    for alias, state in (("demo-job", "queued"), ("cancelled-job", "cancelled"), ("accounting-job", "accounting-pending")):
        ir = compile_quantum(f"inspect status job {alias}", catalog=catalog())
        reply = structured_reply(ir, catalog=catalog())
        assert reply["state"] == state
        assert reply["references"] == {"job_id": ir.args["job_id"], "plan_id": "plan:demo"}
        assert reply["observed_from"] == "caller_catalog" and not reply["attestation_verified"]
    receipt = structured_reply(compile_quantum("explain receipt demo-receipt", catalog=catalog()), catalog=catalog())
    assert receipt["state"] == "verified" and receipt["references"]["receipt_id"] == "receipt:demo"
    proposal = compile_quantum("compile plan demo-plan on local", catalog=catalog())
    assert structured_reply(proposal, catalog=catalog())["state"] == "proposed"
    with pytest.raises(QuantumValidationError):
        structured_reply(replace(proposal, effects=("external.write",)), catalog=catalog())


def test_fractional_hardware_budget_remains_exact_and_proposal_only():
    source = "prepare submission plan demo-plan on ibm-fez with 100 shots 1 circuit 2.5 qpu seconds"
    ir = compile_quantum(source, catalog=catalog())
    assert ir.operation == "prepare_submission" and ir.args["qpu_seconds"] == 2.5
    assert ir.effects == ("proposal_only",)
    assert ir.quantities[-1].raw == "2.5 qpu seconds"
    assert structured_reply(ir, catalog=catalog())["state"] == "proposed"
    assert validate_quantum_document(ir.to_dict(), catalog=catalog()).args["qpu_seconds"] == 2.5


def test_ats_fixture_bytes_remain_pinned():
    baseline = BUNDLE["marketBaseline"]
    path = FIXTURE.with_name(baseline["fixture"])
    assert baseline["lineEndings"] == "LF"
    assert hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == baseline["sha256"]


def test_public_intent_api_dispatches_quantum_before_market_binding():
    from nano.intent import (QuantumCommandIR as ExportedIR, compile_intent, compile_quantum as exported_compile,
                             compile_to_dict, explain_intent, parse_intent, structured_reply as exported_reply,
                             validate_quantum_document as exported_validate)
    assert ExportedIR is QuantumCommandIR and exported_compile is compile_quantum
    assert exported_reply is structured_reply and exported_validate is validate_quantum_document
    source = "compile plan demo-plan on local"
    options = {"profile": "quantum", "quantum_catalog": catalog()}
    parsed, compiled = parse_intent(source, **options), compile_intent(source, **options)
    assert isinstance(parsed, QuantumCommandIR) and isinstance(compiled, QuantumCommandIR)
    assert parsed.to_dict() == compiled.to_dict() == compile_to_dict(source, **options)
    assert compiled.canonical_bytes() == compiled.canonical_json().encode("ascii")
    assert json.loads(compiled.canonical_json()) == compiled.to_dict()
    assert json.loads(explain_intent(source, **options))["state"] == "proposed"
    for gap in ("explain quantum circuit", "show quantum job status", "simulate bell pair with 100 shots", "compile quantum search"):
        ir = compile_intent(gap, **options)
        assert ir.diagnostics and not ir.steps and not ir.entities


def test_public_explicit_market_profile_keeps_frozen_canonical_bytes():
    from nano.intent import canonical_bytes, compile_intent
    golden = json.loads(FIXTURE.with_name(BUNDLE["marketBaseline"]["fixture"]).read_text())
    for fixture in golden["fixtures"]:
        default = compile_intent(fixture["source"])
        explicit = compile_intent(fixture["source"], profile="market")
        assert canonical_bytes(default) == canonical_bytes(explicit) == canonical_bytes(fixture["ir"])


@pytest.mark.parametrize("options", [
    {"profile": "unknown"},
    {"profile": True},
    {"profile": "market", "quantum_catalog": BUNDLE["catalog"]},
    {"profile": "quantum", "catalogs": []},
])
def test_public_api_rejects_invalid_profile_and_cross_domain_catalogs(options):
    from nano.intent import compile_intent, compile_to_dict, explain_intent, parse_intent
    for function in (parse_intent, compile_intent, compile_to_dict, explain_intent):
        with pytest.raises(ValueError):
            function("check plan demo-plan", **options)


@pytest.mark.parametrize("action", ["parse", "compile", "explain"])
def test_quantum_cli_routes_to_its_own_ir_and_reply(action, tmp_path, capsys):
    from nano.cli.main import main
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog()), encoding="utf-8")
    source = "inspect status job demo-job"
    argv = ["intent", action, source, "--profile", "quantum", "--catalog", str(path)]
    if action == "compile":
        argv.append("--json")
    assert main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    document = json.loads(captured.out)
    if action == "explain":
        assert document["state"] == "queued" and document["references"]["job_id"] == "job:demo"
    else:
        assert document == compile_quantum(source, catalog=catalog()).to_dict()
        assert document["schema"] == "nano.quantum.command.v1" and "nanoIntentVersion" not in document
    if action == "compile":
        assert captured.out == compile_quantum(source, catalog=catalog()).canonical_json() + "\n"


def test_quantum_cli_rejects_bad_catalog_and_market_catalog_mixing(tmp_path, capsys):
    from nano.cli.main import main
    path = tmp_path / "catalog.json"
    path.write_text("{invalid", encoding="utf-8")
    assert main(["intent", "compile", "check plan demo-plan", "--profile", "quantum", "--catalog", str(path)]) == 1
    captured = capsys.readouterr()
    assert not captured.out and "error:" in captured.err and "Traceback" not in captured.err
    path.write_text(json.dumps(catalog()), encoding="utf-8")
    assert main(["intent", "compile", "check plan demo-plan", "--catalog", str(path)]) == 1
    captured = capsys.readouterr()
    assert not captured.out and "--catalog requires --profile quantum" in captured.err
    assert main(["intent", "compile", "check plan demo-plan", "--profile", "quantum", "--json"]) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["diagnostics"][0]["code"] == "MISSING_CATALOG" and document["steps"] == []


def test_unpaired_surrogate_has_typed_public_and_cli_diagnostics(tmp_path, capsys):
    from nano.intent import compile_intent, compile_to_dict, explain_intent, parse_intent
    from nano.cli.main import main
    for function in (parse_intent, compile_intent, compile_to_dict, explain_intent):
        with pytest.raises(IntentInputError, match="invalid Unicode"):
            function("check plan \ud800", profile="quantum", quantum_catalog=catalog())
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog()), encoding="utf-8")
    assert main(["intent", "compile", "check plan \ud800", "--profile", "quantum", "--catalog", str(path)]) == 1
    captured = capsys.readouterr()
    assert not captured.out and "invalid Unicode" in captured.err and "Traceback" not in captured.err


@pytest.mark.parametrize("amount", ["0.10000000000000000001", "2.5000000000000000001", "10000000000000000.5"])
def test_quantum_budget_never_silently_rounds_a_source_quantity(amount):
    source = f"prepare submission quantum plan demo-plan on ibm-fez with 100 shots 1 circuit {amount} qpu seconds"
    ir = compile_quantum(source, catalog=catalog())
    assert [d.code for d in ir.diagnostics] == ["INVALID_BOUNDS"]
    assert not ir.steps and not ir.effects
    assert validate_quantum_document(ir.to_dict(), catalog=catalog()).to_dict() == ir.to_dict()
