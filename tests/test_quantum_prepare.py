"""Offline protocol and real pinned compiler coverage; no IBM services."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nano import __version__
from nano.cli.main import main
from nano_qsearch.canonical import canonical_digest
from nano_qsearch.prepare import (ARTIFACT_SCHEMA, PINNED_QISKIT, PROFILE_SCHEMA, SNAPSHOT_SCHEMA,
    PreparationError, _plan_digest, atlas_source_digest, atlas_source_manifest, check_request, load_prepared_plans, parse_compile_profile,
    parse_prepared_artifact, parse_target_snapshot, prepare_request, samples_from_pub_result, snapshot_from_backend)
from qsearch_v2_support import request_body, resign


def profile():
    return {"schema": PROFILE_SCHEMA, "id": "offline-basis-v1",
            "operation": {"schema": "nano.quantum.operations.v1", "op": "optimize_qubo"}, "basis_prep": True,
            "layout": "none", "seed": 11, "optimization_level": 3, "atlas_source_digest": None,
            "compiler": {"name": "nano-qsearch", "version": __version__, "qiskit_version": PINNED_QISKIT,
                         "qiskit_ibm_runtime_version": None}}


def snapshot(n=6):
    instructions = {name: [{"qargs": [i], "error": 0.01, "duration": 0.000001} for i in range(n)]
                    for name in ("x", "sx", "rz", "measure", "reset")}
    instructions["cz"] = [{"qargs": [i, i + 1], "error": 0.02, "duration": 0.00001} for i in range(n - 1)]
    return {"schema": SNAPSHOT_SCHEMA, "name": "ibm_fez", "num_qubits": n, "instructions": instructions,
            "timing_properties": {"dt": None, "granularity": 1, "min_length": 1, "pulse_alignment": 1, "acquire_alignment": 1},
            "qubit_properties": [{"t1": 0.0001, "t2": 0.0001, "frequency": 5e9} for _ in range(n)]}


def body(n=2):
    value = request_body(n, target="ibm_fez")
    for i, job in enumerate(value["jobs"]):
        width = 4 - i
        job["variables"] = [f"x{j}" for j in range(width)]
        job["qubo"]["q"] = [[-1.0 if a == b else 0.0 for b in range(width)] for a in range(width)]
        job["compile"]["variant"] = "ordinary"
        job["audit"]["classical_min_energy"] = -float(width)
        value["descriptor"]["blocks"][i].update(n=width, classical_min_energy=-float(width))
    return resign(value)


def fake_artifact():
    """A structurally valid fixture with opaque QPY; loading still needs the SDK."""
    request, declared, target = body(), profile(), snapshot()
    checked = check_request(request, declared, target)
    pubs = []
    for i, job in enumerate(request["jobs"]):
        n = len(job["variables"])
        binary = b"offline-fixture-not-loadable-QPY" + bytes([i])
        metrics = {"logical_2q": 0, "native_2q": 0, "native_depth": 0}
        record = {"target": "ibm_fez", "backend_name": "ibm_fez", "coupling_map": "target:ibm_fez",
                  "physical_qubits": 6, "basis": ["x"], "opt_level": 3, "seed": 11,
                  "target_digest": parse_target_snapshot(target).structural_digest,
                  "candidates": {"ordinary": metrics, "joint5": None},
                  "rewrites": {"basis_prep": "applied", "layout": {"mode": "off"}}}
        pub = {"pub_index": i, "job_digest": job["job_digest"], "isa_digest": "sha256:" + hashlib.sha256(binary).hexdigest(),
               "layout": list(range(n)), "qpy_base64": base64.b64encode(binary).decode(), "variant": "ordinary",
               "nuclei": [], "mapping": list(range(n)), "metrics": metrics, "compile_record": record,
               "inspection": {"ops": {"x": n, "measure": n}, **metrics,
                              "measurement_map": [[q, q] for q in range(n)], "initial_layout": list(range(n))}}
        pub["compiled_plan_digest"] = _plan_digest(pub)
        pubs.append(pub)
    resources = copy.deepcopy(checked["resources"])
    resources.update(gate_counts=[p["inspection"]["ops"] for p in pubs], gate_counts_status="compiled")
    value = {"schema": ARTIFACT_SCHEMA, "request": request, "profile": declared, "target_snapshot": target,
             "logical_identity": checked["logical_identity"], "profile_digest": checked["profile_digest"],
             "target_snapshot_digest": checked["target_snapshot_digest"], "pubs": pubs, "resources": resources}
    return {**value, "artifact_digest": canonical_digest(value)}


def rehash(value):
    value["artifact_digest"] = canonical_digest({k: v for k, v in value.items() if k != "artifact_digest"})
    return value


def test_profiles_and_snapshots_are_deeply_immutable_detached_values():
    source = profile()
    parsed = parse_compile_profile(source)
    source["compiler"]["version"] = "changed"
    assert parsed.to_payload()["compiler"]["version"] == __version__
    detached = parsed.to_payload()
    detached["id"] = "changed"
    assert parsed.reference["id"] == "offline-basis-v1"
    target = parse_target_snapshot(snapshot())
    assert target == parse_target_snapshot(target.to_payload())
    assert target.digest != target.calibration_digest != target.structural_digest


def test_versioned_local_fixture_contract_is_checked_without_an_sdk():
    directory = Path(__file__).parent / "qsearch_fixtures" / "prepare_v1"
    request, declared, target = [json.loads((directory / name).read_text(encoding="utf-8"))
                                for name in ("request.json", "profile.json", "target.json")]
    assert request == body() and declared == profile() and target == snapshot()
    assert check_request(request, declared, target)["ok"]


@pytest.mark.parametrize("edit", [lambda p: p.update(schema="nano.qsearch.compile-profile.v0"),
    lambda p: p.update(extra=True), lambda p: p.update(seed=True), lambda p: p.update(optimization_level=4),
    lambda p: p.update(basis_prep=1), lambda p: p["compiler"].update(qiskit_version="latest")])
def test_malformed_or_stale_profiles_fail(edit):
    value = profile()
    edit(value)
    with pytest.raises(PreparationError):
        parse_compile_profile(value)


@pytest.mark.parametrize("edit", [lambda s: s.update(schema="nano.qsearch.target-snapshot.v0"),
    lambda s: s["instructions"]["x"].append(s["instructions"]["x"][0]),
    lambda s: s["instructions"]["x"][0].update(qargs=[99]),
    lambda s: s["instructions"]["x"][0].update(error=True),
    lambda s: s["qubit_properties"][0].update(t1=float("inf"))])
def test_malformed_or_stale_snapshots_fail(edit):
    value = snapshot()
    edit(value)
    with pytest.raises(PreparationError):
        parse_target_snapshot(value)


def test_check_has_no_sdk_credential_or_network_path(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("SDK import forbidden")
    monkeypatch.setattr("nano_qsearch.prepare.importlib.import_module", forbidden)
    monkeypatch.setenv("IBM_QUANTUM_TOKEN", "not-used")
    first = check_request(body(), profile(), snapshot())
    monkeypatch.setenv("NANO_QSEARCH_BASIS_PREP", "0")
    monkeypatch.setenv("NANO_QSEARCH_LAYOUT", "calibration:missing.json")
    monkeypatch.setenv("NANO_QSEARCH_LAYOUT_BACKEND", "ibm_other")
    assert check_request(body(), profile(), snapshot()) == first
    assert first["ok"] and first["resources"]["widths"] == [4, 3]
    assert first["resources"]["usage"]["observed"] is None
    assert first["resources"]["usage"]["settled"] is None
    assert first["resources"]["gate_counts_status"] == "requires_preparation"


def test_diagnostic_categories_and_profile_disagreement(monkeypatch):
    assert check_request(body(), profile())["diagnostics"][0]["code"] == "missing_target"
    unsupported = profile()
    unsupported["layout"] = "random"
    assert check_request(body(), unsupported, snapshot())["diagnostics"][0]["code"] == "unsupported_capability"
    assert check_request({}, profile(), snapshot())["diagnostics"][0]["code"] == "invalid_plan"
    declared = profile()
    declared["seed"] = 12
    assert check_request(body(), declared, snapshot())["diagnostics"][0]["code"] == "invalid_plan"
    def absent(name):
        raise ImportError(name)
    monkeypatch.setattr("nano_qsearch.prepare.importlib.import_module", absent)
    with pytest.raises(PreparationError) as error:
        prepare_request(body(), profile(), snapshot())
    assert error.value.code == "unavailable_dependency"


@pytest.mark.parametrize("edit", [lambda a: a["jobs"][0]["backend"].update(shots=2**31),
    lambda a: a["jobs"][0]["backend"].update(seed=2**53),
    lambda a: a["jobs"][0]["backend"].update(chi_max=2**53),
    lambda a: a["jobs"][0]["limits"].update(max_wall_s=2**53),
    lambda a: a["descriptor"]["blocks"][0]["recipients"][0].update(chunk=2**53),
    lambda a: a["descriptor"]["blocks"][0]["recipients"][0].update(slot=2**53),
    lambda a: a["descriptor"].update(batch_index=2**53)])
def test_check_rejects_requests_outside_the_shared_provenance_wire_limits(edit):
    value = body()
    edit(value)
    # Keep source and descriptor consistent: a rejected check must exercise
    # provenance bounds rather than merely a stale request assertion.
    value["descriptor"]["blocks"][0]["shots"] = value["jobs"][0]["backend"]["shots"]
    value["descriptor"]["blocks"][0]["seed"] = value["jobs"][0]["backend"]["seed"]
    result = check_request(resign(value), profile(), snapshot())
    assert not result["ok"] and result["diagnostics"][0]["code"] == "invalid_plan"


def test_profile_and_target_bounds_match_the_shared_provenance_wire_limits():
    declared = profile()
    declared["seed"] = 2**53
    with pytest.raises(PreparationError):
        parse_compile_profile(declared)
    target = snapshot()
    target["num_qubits"] = 4097
    with pytest.raises(PreparationError):
        parse_target_snapshot(target)
    declared["seed"] = 2**53 - 1
    assert parse_compile_profile(declared).to_payload()["seed"] == 2**53 - 1


@pytest.fixture
def atlas_sources(tmp_path, monkeypatch):
    package = tmp_path / "aether_atlas"
    root = package / "qcompiler"
    root.mkdir(parents=True)
    # If source discovery executes package code, the test fails immediately.
    (package / "__init__.py").write_text("raise AssertionError('ATLAS initializer must not execute')\n", encoding="utf-8")
    for name in ("__init__.py", "nuclei.py", "nucleus_program.py", "compiled_plan.py", "nucleus_phase.py", "qaoa_select.py"):
        (root / name).write_text("import qiskit_ibm_runtime\nraise AssertionError('source discovery must not import')\n", encoding="utf-8")
    nested = root / "helpers"
    nested.mkdir()
    (nested / "angles.py").write_text("ANGLE = 0.5\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return root


def test_atlas_source_pin_covers_transitive_nested_helpers_without_imports(atlas_sources, monkeypatch):
    before = set(sys.modules)
    def no_import(*args, **kwargs):
        pytest.fail("source pinning must not execute package imports")
    monkeypatch.setattr("nano_qsearch.prepare.importlib.import_module", no_import)
    manifest = atlas_source_manifest()
    first = atlas_source_digest()
    assert manifest["schema"] == "nano.qsearch.atlas-source-manifest.v1"
    paths = [file["path"] for file in manifest["files"]]
    assert paths == sorted(paths) and "helpers/angles.py" in paths
    assert canonical_digest(manifest) == first
    (atlas_sources / "nucleus_phase.py").write_text("PHASE = 0.25\n", encoding="utf-8")
    second = atlas_source_digest()
    assert first != second
    (atlas_sources / "helpers" / "angles.py").write_text("ANGLE = 0.75\n", encoding="utf-8")
    assert atlas_source_digest() != second
    assert not any(name.startswith(("aether_atlas", "qiskit_ibm_runtime")) for name in set(sys.modules) - before)


def test_atlas_source_manifest_is_independent_of_file_enumeration_order(atlas_sources, monkeypatch):
    expected = atlas_source_manifest()
    original = Path.rglob
    monkeypatch.setattr(Path, "rglob", lambda path, pattern: iter(reversed(list(original(path, pattern)))))
    assert atlas_source_manifest() == expected
    assert atlas_source_digest() == canonical_digest(expected)


def test_atlas_source_pin_accepts_one_root_namespace_packages(atlas_sources):
    (atlas_sources / "__init__.py").unlink()
    (atlas_sources.parent / "__init__.py").unlink()
    manifest = atlas_source_manifest()
    assert "nucleus_phase.py" in [file["path"] for file in manifest["files"]]
    assert atlas_source_digest() == canonical_digest(manifest)


def test_atlas_source_pin_rejects_missing_or_unreadable_source(atlas_sources, monkeypatch):
    required = atlas_sources / "qaoa_select.py"
    required.unlink()
    with pytest.raises(PreparationError) as error:
        atlas_source_digest()
    assert error.value.code == "unavailable_dependency"
    required.write_text("def qaoa_circuit(): pass\n", encoding="utf-8")
    original = Path.read_bytes
    def unreadable(path):
        if path == required:
            raise OSError("source unavailable")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(PreparationError) as error:
        atlas_source_digest()
    assert error.value.code == "unavailable_dependency"


@pytest.mark.parametrize("operation", [{"schema": "nano.quantum.operations.v0", "op": "optimize_qubo"},
    {"schema": "nano.quantum.operations.v1", "op": "prepare_bits"},
    {"schema": "nano.quantum.operations.v1", "op": "sample"}, {"schema": "nano.quantum.operations.v1", "op": "invented"}])
def test_legacy_qubo_adapter_binds_only_the_coordinated_operation(operation):
    declared = profile()
    declared["operation"] = operation
    checked = check_request(body(), declared, snapshot())
    assert not checked["ok"] and checked["diagnostics"][0]["code"] == "unsupported_capability"


def test_logical_identity_separate_from_target_profile_and_full_request():
    first = check_request(body(), profile(), snapshot())
    target = snapshot()
    target["instructions"]["x"][0]["error"] = 0.03
    second = check_request(body(), profile(), target)
    assert second["logical_identity"] == first["logical_identity"]
    assert second["request_digest"] == first["request_digest"]
    assert second["target_snapshot_digest"] != first["target_snapshot_digest"]
    assert second["calibration_digest"] != first["calibration_digest"]
    declared = profile()
    declared["basis_prep"] = False
    assert check_request(body(), declared, snapshot())["profile_digest"] != first["profile_digest"]


def test_artifact_round_trip_expected_digest_and_content_validation():
    payload = fake_artifact()
    parsed = parse_prepared_artifact(payload, expected_digest=payload["artifact_digest"])
    assert parsed.to_payload() == payload
    assert parsed.request.block_count == 2
    assert len(parsed.compiled_plan_evidence) == 2
    assert parsed.snapshot_digest == payload["target_snapshot_digest"]
    assert parsed.compiler == profile()["compiler"]
    with pytest.raises(PreparationError, match="independently expected"):
        parse_prepared_artifact(payload, expected_digest="sha256:" + "a" * 64)
    payload["pubs"][0]["layout"][0] = 99
    with pytest.raises(PreparationError):
        parse_prepared_artifact(payload)


@pytest.mark.parametrize("edit", [lambda a: a.update(schema="nano.qsearch.prepared-artifact.v0"),
    lambda a: a["pubs"].pop(), lambda a: a["pubs"].reverse(), lambda a: a["pubs"].append(a["pubs"][0]),
    lambda a: a["pubs"][0].update(layout=[0, 0, 1, 2]), lambda a: a["pubs"][0].update(qpy_base64="!bad!"),
    lambda a: a["pubs"][0]["inspection"].update(measurement_map=[[0, 0]]),
    lambda a: a["pubs"][0]["compile_record"].update(seed=12),
    lambda a: a["pubs"][0].update(mapping=[0, 1, 2, 5]),
    lambda a: a["pubs"][0]["inspection"].update(native_2q=False),
    lambda a: a["pubs"][0]["inspection"].update(initial_layout=[False, True, 2, 3]),
    lambda a: a["resources"]["usage"].update(settled=0),
    lambda a: a["pubs"][0].update(compiled_plan_digest="sha256:" + "f" * 64)])
def test_rehashed_malformed_artifacts_still_rejected(edit):
    value = fake_artifact()
    edit(value)
    with pytest.raises(PreparationError):
        parse_prepared_artifact(rehash(value))


@pytest.mark.parametrize("change", [lambda s: s["instructions"]["x"][0].update(error=0.02),
    lambda s: s["instructions"]["x"][0].update(duration=0.002),
    lambda s: s["qubit_properties"][0].update(t1=0.0002),
    lambda s: s["instructions"]["cz"].pop()])
def test_target_or_calibration_drift_fails_before_sdk_load(change, monkeypatch):
    value = fake_artifact()
    current = snapshot()
    change(current)
    monkeypatch.setattr("nano_qsearch.prepare._qiskit", lambda: pytest.fail("SDK load before drift check"))
    with pytest.raises(PreparationError) as error:
        load_prepared_plans(value, current_snapshot=current, expected_digest=value["artifact_digest"])
    assert error.value.code == "target_drift"


@pytest.mark.parametrize("mapping_data", [False, True], ids=["attribute-DataBin", "mapping-DataBin"])
def test_public_sdk_result_adapters_preserve_display_order_and_pub_width(mapping_data):
    output = []
    for width, values in [(4, ["1101", "0000"]), (3, ["101", "000"])]:
        register = SimpleNamespace(get_bitstrings=lambda values=values: values)
        pub = {"data": {"meas": register}} if mapping_data else SimpleNamespace(data=SimpleNamespace(meas=register))
        samples = samples_from_pub_result(pub, register="meas", width=width, shots=2)
        output.append(samples)
        assert samples == values
    assert [int(bit) for bit in reversed(output[0][0])] == [1, 0, 1, 1]
    assert output[1][0] == "101"
    with pytest.raises(PreparationError):
        samples_from_pub_result(pub, register="meas", width=3, shots=3)


def test_snapshot_capture_old_and_new_public_target_shapes(monkeypatch):
    # This isolates property/name shape coverage; real instruction definitions
    # and fixed-angle rejection are covered by the pinned compiler test below.
    monkeypatch.setattr("nano_qsearch.prepare._validate_target_definitions", lambda _: None)
    expected = snapshot(2)
    class Target:
        num_qubits = 2
        operation_names = expected["instructions"].keys()
        qubit_properties = [SimpleNamespace(**p) for p in expected["qubit_properties"]]
        def qargs_for_operation_name(self, name):
            return {tuple(row["qargs"]) for row in expected["instructions"][name]}
        def __getitem__(self, name):
            return {tuple(row["qargs"]): SimpleNamespace(error=row["error"], duration=row["duration"])
                    for row in expected["instructions"][name]}
    for name in ("ibm_fez", lambda: "ibm_fez"):
        assert snapshot_from_backend(SimpleNamespace(name=name, target=Target())).to_payload() == expected


def test_cli_check_prepare_missing_and_artifact_json(tmp_path, capsys):
    request_file, profile_file, snapshot_file = [tmp_path / name for name in ("request.json", "profile.json", "target.json")]
    for path, value in ((request_file, body()), (profile_file, profile()), (snapshot_file, snapshot())):
        path.write_text(json.dumps(value), encoding="utf-8")
    assert main(["quantum", "check", str(request_file), "--profile", str(profile_file), "--target-snapshot", str(snapshot_file)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert main(["quantum", "prepare", str(request_file), "--profile", str(profile_file)]) == 1
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "missing_target"
    artifact = fake_artifact()
    artifact_file = tmp_path / "artifact.json"
    artifact_file.write_text(json.dumps(artifact), encoding="utf-8")
    assert main(["quantum", "check", str(artifact_file), "--artifact", "--expected-digest", artifact["artifact_digest"]]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["artifact_digest"] == artifact["artifact_digest"]
    assert checked["validation_scope"] == "structure_and_digests"
    assert checked["isa_validation"] == "not_loaded"
    artifact_file.write_text('{"schema":1,"schema":2}', encoding="utf-8")
    assert main(["quantum", "check", str(artifact_file), "--artifact"]) == 1
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "invalid_plan"


@pytest.mark.parametrize("arguments", [["--expected-digest", "sha256:" + "0" * 64],
    ["--artifact", "--profile", "ignored.json"], ["--artifact", "--target-snapshot", "ignored.json"]])
def test_cli_rejects_ignored_approval_or_override_flags(tmp_path, capsys, arguments):
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    assert main(["quantum", "check", str(source), *arguments]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["diagnostics"][0]["code"] == "invalid_plan"


def test_fresh_cli_import_check_never_imports_quantum_sdks():
    root = str(Path(__file__).resolve().parents[1])
    code = "import sys;sys.path.insert(0,sys.argv[1]);from nano.cli.main import build_parser;build_parser();assert not any(m.split('.')[0] in ('qiskit','qiskit_ibm_runtime','numpy','aether_atlas') for m in sys.modules)"
    result = subprocess.run([sys.executable, "-I", "-c", code, root], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.engine
def test_real_pinned_qiskit_prepare_repeats_roundtrips_and_loads_without_atlas_or_ibm(monkeypatch):
    qiskit = pytest.importorskip("qiskit")
    if qiskit.__version__ != PINNED_QISKIT:
        pytest.skip("requires the declared pinned Qiskit 2.3.1; no package upgrades performed")
    import nano_qsearch.compile as compile_module
    monkeypatch.setattr(compile_module.CompileOptions, "from_env", lambda: pytest.fail("ambient compilation profile"))
    before = set(sys.modules)
    first = prepare_request(body(), profile(), snapshot())
    second = prepare_request(body(), profile(), snapshot())
    assert first.to_payload() == second.to_payload()
    assert first.artifact_digest == second.artifact_digest
    assert len(first.compiled_plan_evidence) == 2
    plans = load_prepared_plans(first, current_snapshot=snapshot(), expected_digest=first.artifact_digest)
    assert [plan.isa.num_clbits for plan in plans] == [4, 3]
    assert [plan.compiled_plan_digest for plan in plans] == [pub["compiled_plan_digest"] for pub in first.to_payload()["pubs"]]
    assert first.to_payload()["resources"]["gate_counts_status"] == "compiled"
    assert not any(name.startswith(("qiskit_ibm_runtime", "aether_atlas")) for name in set(sys.modules) - before)
    target = snapshot()
    target["instructions"]["x"][0]["error"] = 0.03
    changed = prepare_request(body(), profile(), target)
    assert changed.artifact_digest != first.artifact_digest
    assert changed.snapshot_digest != first.snapshot_digest
    renamed = profile()
    renamed["id"] = "separately-reviewed-profile"
    reprofiled = prepare_request(body(), renamed, snapshot())
    assert reprofiled.artifact_digest != first.artifact_digest
    assert reprofiled.to_payload()["logical_identity"] == first.to_payload()["logical_identity"]
    assert reprofiled.compiled_plan_evidence == first.compiled_plan_evidence
    backend = parse_target_snapshot(snapshot()).to_backend()
    assert snapshot_from_backend(backend).to_payload() == snapshot()
    from qiskit.circuit.library import RZGate
    from qiskit.transpiler import Target
    fixed = Target(num_qubits=2)
    fixed.add_instruction(RZGate(0.5), {(0,): None, (1,): None})
    with pytest.raises(PreparationError) as error:
        snapshot_from_backend(SimpleNamespace(name="ibm_fez", target=fixed))
    assert error.value.code == "unsupported_capability"

