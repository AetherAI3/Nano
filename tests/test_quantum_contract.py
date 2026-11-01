"""RFC conformance is offline: these tests do not import or call a provider."""
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from nano.quantum import (PrepareBits, QuantumValidationError, Sample, build_plan, check_evidence_bank,
                          checked_plan, optimize_qubo, parse_plan, parse_result, prepare_bits, sample)
from nano.quantum.conformance import large_mapping_fixture
from nano_qsearch.canonical import canonical_digest
from nano_qsearch.job import parse_job

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "nano" / "quantum" / "fixtures"
SCHEMAS = ROOT / "nano" / "quantum" / "schemas"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["known-bits", "one-record", "packed-records", "optimize-qubo"])
def test_frozen_fixture_schema_and_semantic_conformance(name):
    jsonschema = pytest.importorskip("jsonschema")
    body = fixture(name)
    jsonschema.Draft202012Validator(json.loads((SCHEMAS / "operations-v1.schema.json").read_text())).validate(body)
    plan = parse_plan(body)
    assert plan.to_payload() == body
    assert parse_plan(json.loads(json.dumps(body))).plan_digest == plan.plan_digest


def test_prepare_bits_has_no_fabricated_optimization():
    plan = parse_plan(fixture("known-bits"))
    preparation, sampling = plan.operations
    assert isinstance(preparation, PrepareBits) and preparation.bits == "100"
    assert isinstance(sampling, Sample) and sampling.result["width"] == 3
    assert not {"qubo", "p", "gammas", "betas"} & set(plan.to_payload()["operations"][0])
    with pytest.raises(TypeError):
        plan.payload["operations"][1]["limits"]["max_qpu_s"] = 10
    changed = plan.to_payload()
    changed["operations"][0]["bits"] = "001"
    assert plan.operations[0].bits == "100" and parse_plan(changed).plan_digest != plan.plan_digest


def test_builder_is_same_validator_and_semantic_payload():
    plan = build_plan([prepare_bits("100"), sample("prepare", expected_width=3, shots=2,
                     max_circuits=1, max_wall_s=5, max_qpu_s=0)])
    assert plan.to_payload() == fixture("known-bits")
    with pytest.raises(QuantumValidationError):
        build_plan([prepare_bits("100"), sample("prepare", expected_width=3, shots=True,
                   max_circuits=1, max_wall_s=5, max_qpu_s=0)])


def test_public_records_are_revalidated_at_boundaries():
    plan = parse_plan(fixture("known-bits"))
    for forged in (replace(plan, plan_digest="sha256:" + "0" * 64),
                   replace(plan, operations=(replace(plan.operations[0], bits="001"), plan.operations[1]))):
        with pytest.raises(QuantumValidationError, match="DIGEST"):
            checked_plan(forged)
        with pytest.raises(QuantumValidationError, match="DIGEST"):
            parse_result(forged, observation(plan))


def test_input_mutation_does_not_change_admitted_records():
    body = fixture("known-bits")
    plan = parse_plan(body)
    body["operations"][0]["bits"] = "001"
    assert plan.operations[0].bits == "100"
    operations = fixture("known-bits")["operations"]
    built = build_plan(operations)
    operations[1]["limits"]["max_wall_s"] = 99
    assert built.operations[1].limits["max_wall_s"] == 5


@pytest.mark.parametrize("field", ["id", "backend"])
@pytest.mark.parametrize("value", ["\ud800", "internal\nnewline", "internal\rcarriage"])
def test_non_utf8_or_multiline_text_fails_with_typed_diagnostic(field, value):
    body = fixture("known-bits")
    body["operations"][1][field] = value
    with pytest.raises(QuantumValidationError, match="TYPE"):
        parse_plan(body)


@pytest.mark.parametrize("path,value,code", [
    (("extra",), 1, "FIELDS"),
    (("schema",), "unknown", "SCHEMA"),
    (("operations", 0, "op"), "superposition", "VALUE"),
    (("operations", 0, "bits"), "10", "WIDTH"),
    (("operations", 0, "variables"), ["x0", "x0", "x2"], "VARIABLES"),
    (("operations", 1, "shots"), True, "TYPE"),
    (("operations", 1, "shots"), 2.0, "TYPE"),
    (("operations", 1, "limits", "max_circuits"), False, "TYPE"),
    (("operations", 1, "limits", "max_qpu_s"), -1, "TYPE"),
    (("operations", 1, "limits", "max_qpu_s"), 1, "LIMITS"),
    (("operations", 1, "result", "pub_index"), True, "TYPE"),
    (("operations", 1, "result", "pub_index"), 2, "PUB"),
    (("operations", 1, "result", "width"), 2, "WIDTH"),
    (("operations", 1, "result", "bit_order"), "x0_first", "VALUE"),
    (("operations", 1, "preparation_id"), "absent", "REFERENCE"),
])
def test_rejected_plan_mutations(path, value, code):
    body = fixture("known-bits")
    target = body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(QuantumValidationError) as error:
        parse_plan(body)
    assert error.value.code == code


@pytest.mark.parametrize("field,value", [("payload_width", True), ("authentication_width", -1),
                                        ("expected_records", True), ("slot", 1)])
def test_packet_width_and_type_mutations(field, value):
    body = fixture("one-record")
    body["operations"][0]["packets"][0][field] = value
    with pytest.raises(QuantumValidationError):
        parse_plan(body)


def test_evidence_pointer_is_not_evidence_and_content_digest_is_checked():
    packet = parse_plan(fixture("one-record")).operations[0].packets[0]
    bank = fixture("evidence-bank")
    assert check_evidence_bank(packet, {"fixture:records": bank}) == bank
    for catalog in ({}, {"fixture:records": {"records": ["wrong"]}}):
        with pytest.raises(QuantumValidationError):
            check_evidence_bank(packet, catalog)


def test_unknown_codec_and_wrong_digest_fail_explicitly():
    for mutate in (lambda p: p["codec"].update(name="unknown"),
                   lambda p: p["codec"].update(version=True),
                   lambda p: p["evidence_bank"].update(digest="sha256:bad")):
        body = fixture("one-record")
        mutate(body["operations"][0]["packets"][0])
        with pytest.raises(QuantumValidationError):
            parse_plan(body)


def test_duplicate_recipient_chunk_rejected_distinct_chunks_permitted():
    body = fixture("packed-records")
    first, second = body["operations"][0]["packets"]
    second["recipient_id"] = first["recipient_id"]
    with pytest.raises(QuantumValidationError, match="RECIPIENT"):
        parse_plan(body)
    second["chunk"] = 1
    assert len(parse_plan(body).operations[0].packets) == 2


def test_duplicate_operations_and_underbounded_plan_fail():
    body = fixture("known-bits")
    body["operations"].append(copy.deepcopy(body["operations"][1]))
    with pytest.raises(QuantumValidationError, match="ID"):
        parse_plan(body)
    body["operations"][-1]["id"] = "second-sample"
    body["operations"][-1]["result"]["pub_index"] = 1
    with pytest.raises(QuantumValidationError, match="LIMITS"):
        parse_plan(body)


def test_optimizer_parameters_are_genuine_and_audit_is_separate():
    body = fixture("optimize-qubo")
    for field, value in (("p", True), ("p", 0), ("gammas", [float("nan")]), ("betas", [])):
        bad = copy.deepcopy(body)
        bad["operations"][0]["method"][field] = value
        with pytest.raises(QuantumValidationError):
            parse_plan(bad)
    bad = copy.deepcopy(body)
    bad["operations"][0]["qubo"]["q"][0][1] = 0.5
    with pytest.raises(QuantumValidationError, match="QUBO"):
        parse_plan(bad)
    op = body["operations"][0]
    built = optimize_qubo(op["variables"], op["qubo"]["q"], offset=0, variant="ordinary", p=1,
                          gammas=[0.2], betas=[0.4], classical_min_energy=-1,
                          shots=2, max_circuits=1, max_wall_s=5, max_qpu_s=0)
    assert build_plan([built]).to_payload() == body


def observation(plan):
    return {"schema": "nano.quantum.fixture-result.v1", "plan_digest": plan.plan_digest,
            "pubs": [{"operation_id": o.id, "pub_index": o.result["pub_index"],
                      "register": o.result["register"], "bit_order": "msb_first",
                      "samples": ["001"] * o.shots} for o in plan.operations if isinstance(o, Sample)],
            "optimization_verdict": "not_applicable", "delivery_verdict": "unverified"}


def test_typed_result_pins_asymmetric_order_without_claiming_delivery():
    jsonschema = pytest.importorskip("jsonschema")
    plan = parse_plan(fixture("known-bits"))
    body = observation(plan)
    jsonschema.Draft202012Validator(json.loads((SCHEMAS / "fixture-result-v1.schema.json").read_text())).validate(body)
    result = parse_result(plan, body)
    assert result.pubs[0].samples == ("001", "001")
    assert result.pubs[0].samples[0][::-1] == plan.operations[0].bits
    assert result.delivery_verdict == "unverified"
    # The two verdicts are intentionally independent observations.
    body.update(optimization_verdict="pass", delivery_verdict="fail")
    result = parse_result(plan, body)
    assert result.optimization_verdict == "pass" and result.delivery_verdict == "fail"


@pytest.mark.parametrize("mutation", ["missing", "extra", "width", "register", "bool_pub", "digest", "unknown"])
def test_result_shape_rejects_missing_extra_or_wrong_pubs(mutation):
    plan = parse_plan(fixture("known-bits"))
    body = observation(plan)
    if mutation == "missing": body["pubs"].clear()
    elif mutation == "extra": body["pubs"].append(copy.deepcopy(body["pubs"][0]))
    elif mutation == "width": body["pubs"][0]["samples"][0] = "01"
    elif mutation == "register": body["pubs"][0]["register"] = "other"
    elif mutation == "bool_pub": body["pubs"][0]["pub_index"] = False
    elif mutation == "digest": body["plan_digest"] = "sha256:" + "0" * 64
    else: body["pubs"][0]["extra"] = 0
    with pytest.raises(QuantumValidationError):
        parse_result(plan, body)


def test_400_recipients_1200_pub_fixture_mapping_and_digest():
    recipe = fixture("large-mapping")
    plan = large_mapping_fixture()
    assert plan.plan_digest == recipe["plan_digest"]
    preparations = [o for o in plan.operations if isinstance(o, PrepareBits)]
    samples = [o for o in plan.operations if isinstance(o, Sample)]
    assert len(preparations) == len(samples) == 1200
    mapping = []
    for index, (preparation, sampling) in enumerate(zip(preparations, samples)):
        packet = preparation.packets[0]
        assert packet.recipient_id == f"recipient-{index // 3:03d}" and packet.chunk == index % 3
        assert sampling.preparation_id == preparation.id and sampling.result["pub_index"] == index
        assert int(preparation.bits[::-1], 2) == index
        mapping.append([packet.recipient_id, packet.chunk, packet.slot, index])
    assert canonical_digest(mapping) == recipe["mapping_digest"]
    assert len({row[0] for row in mapping}) == 400


def test_legacy_request_job_schemas_remain_readable():
    for path in (ROOT / "tests" / "qsearch_fixtures").glob("job_*.json"):
        body = json.loads(path.read_text())
        assert parse_job(body).job_digest == body["job_digest"]
