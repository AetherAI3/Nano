"""Actual local circuits and pinned legacy migration; no IBM or paid calls."""
import copy
import importlib.util
import json
import sys
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from nano.quantum import (QuantumValidationError, build_plan, parse_plan, prepare_bits, sample)
from nano.quantum.conformance import large_mapping_fixture
from nano_qsearch.canonical import canonical_digest
from nano_qsearch.job import JobValidationError, QSearchJob
from nano_qsearch.quantum_adapter import (BRIDGE_SOURCE_SHA, QISKIT_VERSION, LegacyCompileProfile, LocalHostPolicy,
    decode_verified, lower_local, read_legacy, run_local, to_legacy_job)

FIXTURES = Path(__file__).resolve().parents[1] / "nano" / "quantum" / "fixtures"
qiskit_test = pytest.mark.skipif(importlib.util.find_spec("qiskit") is None,
                                reason="requires the pinned quantum extra")


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def plan(name):
    return parse_plan(fixture(name))


def observed(value, samples):
    executions = [o for o in value.operations if o.op != "prepare_bits"]
    return {"schema": "nano.quantum.fixture-result.v1", "plan_digest": value.plan_digest,
            "pubs": [{"operation_id": o.id, "pub_index": o.result["pub_index"],
                      "register": o.result["register"], "bit_order": "msb_first", "samples": raw}
                     for o, raw in zip(executions, samples)],
            "optimization_verdict": "pass", "delivery_verdict": "pass"}


@qiskit_test
def test_direct_known_bits_circuit_sampler_and_verified_result(monkeypatch):
    import qiskit
    assert qiskit.__version__ == QISKIT_VERSION == "2.3.1"
    monkeypatch.setitem(sys.modules, "qiskit_ibm_runtime", None)
    monkeypatch.setitem(sys.modules, "aether_atlas", None)
    value = plan("known-bits")
    compiled = lower_local(value)
    assert compiled.effect == "local_compile" and compiled.deadline_owner == "run_local_subprocess"
    assert compiled.pubs[0].execution_limits["max_wall_s"] == 5
    pub = compiled.pubs[0]
    assert pub.variables == ("x0", "x1", "x2") and pub.circuit.cregs[0].name == "meas"
    assert pub.circuit.count_ops() == {"x": 1, "measure": 3}
    assert [pub.circuit.find_bit(i.qubits[0]).index for i in pub.circuit.data
            if i.operation.name == "x"] == [0]
    assert [(pub.circuit.find_bit(i.qubits[0]).index, pub.circuit.find_bit(i.clbits[0]).index)
            for i in pub.circuit.data if i.operation.name == "measure"] == [(0, 0), (1, 1), (2, 2)]
    result = run_local(compiled)
    assert result["pubs"][0]["samples"] == ["001", "001"]
    assert run_local(value) == result
    decoded = decode_verified(value, result, {})
    assert decoded.state == "verified" and decoded.delivery_verdict == "not_applicable"
    assert decoded.optimization_verdict == "not_applicable"


@qiskit_test
@pytest.mark.parametrize("name,expected", [("one-record", ["record-1"]),
                                           ("packed-records", ["record-1", "record-2"])])
def test_one_and_packed_record_roundtrips_include_authentication_overhead(name, expected):
    value = plan(name)
    compiled = lower_local(value)
    assert compiled.pubs[0].packets == value.operations[0].packets
    result = run_local(compiled)
    calls = []
    def verifier(packet, payload, auth):
        calls.append((packet.recipient_id, packet.slot, payload, auth))
        return auth == "1"
    decoded = decode_verified(value, result, {"fixture:records": fixture("evidence-bank")}, authenticate=verifier)
    assert [r.record["id"] for r in decoded.records] == expected
    assert [r.slot for r in decoded.records] == list(range(len(expected)))
    assert len(calls) == len(expected) and all(c[-1] == "1" for c in calls)
    assert [r.packet for r in decoded.records] == list(value.operations[0].packets)
    assert decoded.delivery_verdict == "pass" and decoded.optimization_verdict == "not_applicable"
    assert all(r.authentication_verdict == "pass" and r.authentication_bits == "1" for r in decoded.records)


@qiskit_test
def test_local_boundary_rejects_mutable_circuit_or_forged_typed_plan():
    value = plan("known-bits")
    compiled = lower_local(value)
    compiled.pubs[0].circuit.x(1)
    with pytest.raises(QuantumValidationError, match="CIRCUIT"):
        run_local(compiled)
    forged = replace(value, plan_digest="sha256:" + "0" * 64)
    with pytest.raises(QuantumValidationError, match="DIGEST"):
        lower_local(forged)


def test_local_boundary_refuses_optimization_and_nonlocal_effects_before_qiskit_import():
    with pytest.raises(QuantumValidationError, match="UNSUPPORTED"):
        lower_local(plan("optimize-qubo"))
    body = fixture("known-bits")
    body["operations"][1]["backend"] = "ibm_fez"
    with pytest.raises(QuantumValidationError, match="EFFECT"):
        lower_local(parse_plan(body))


@pytest.mark.parametrize("kind", ["qubits", "shots", "pubs"])
def test_host_allocation_caps_fail_before_qiskit_allocation(monkeypatch, kind):
    import nano_qsearch.quantum_adapter as adapter
    def forbidden_import():
        raise AssertionError("Qiskit allocation must follow host preflight")
    monkeypatch.setattr(adapter, "_qiskit", forbidden_import)
    if kind == "qubits":
        value = build_plan([prepare_bits("0" * 60), sample("prepare", expected_width=60, shots=1,
            max_circuits=1, max_wall_s=5, max_qpu_s=0)])
        policy = None
    else:
        value = plan("known-bits")
        policy = LocalHostPolicy(max_total_shots=1) if kind == "shots" else LocalHostPolicy(max_pubs=1)
        if kind == "pubs":
            body = value.to_payload()
            second = copy.deepcopy(body["operations"][1])
            second["id"] = "second-sample"
            second["result"]["pub_index"] = 1
            second["limits"]["max_circuits"] = 2
            body["operations"][1]["limits"]["max_circuits"] = 2
            body["operations"].append(second)
            value = parse_plan(body)
    with pytest.raises(QuantumValidationError, match="HOST_LIMIT"):
        lower_local(value, policy=policy)


@pytest.mark.parametrize("value", [True, 0, -1, 1.0, "16"])
def test_host_policy_overrides_require_positive_exact_integers(value):
    with pytest.raises(QuantumValidationError, match="HOST_POLICY"):
        LocalHostPolicy(max_qubits=value)


@pytest.mark.parametrize("mutate,code", [
    (lambda b: b["pubs"].clear(), "PUB"),
    (lambda b: b["pubs"].append(copy.deepcopy(b["pubs"][0])), "PUB"),
    (lambda b: b["pubs"][0].update(pub_index=True), "PUB"),
    (lambda b: b["pubs"][0].update(register="other"), "PUB"),
    (lambda b: b["pubs"][0].update(operation_id="other"), "PUB"),
    (lambda b: b["pubs"][0].update(bit_order="x0_first"), "PUB"),
    (lambda b: b["pubs"][0].update(samples=["01", "01"]), "WIDTH"),
    (lambda b: b["pubs"][0].update(samples=["001"]), "WIDTH"),
    (lambda b: b["pubs"][0].update(samples=["100", "100"]), "PAYLOAD"),
])
def test_result_failures_ignore_forged_pass_verdicts(mutate, code):
    value = plan("known-bits")
    body = observed(value, [["001", "001"]])
    mutate(body)
    with pytest.raises(QuantumValidationError, match=code):
        decode_verified(value, body, {})


@pytest.mark.parametrize("verifier", [None, lambda *_: False, lambda *_: 1])
def test_authentication_overhead_requires_explicit_true(verifier):
    value = plan("one-record")
    with pytest.raises(QuantumValidationError, match="AUTHENTICATION"):
        decode_verified(value, observed(value, [["101", "101"]]),
                        {"fixture:records": fixture("evidence-bank")}, authenticate=verifier)


def test_authentication_errors_are_bounded_and_do_not_expose_exception_text():
    value = plan("one-record")
    def verifier(*_):
        raise RuntimeError("SECRET")
    with pytest.raises(QuantumValidationError) as err:
        decode_verified(value, observed(value, [["101", "101"]]),
                        {"fixture:records": fixture("evidence-bank")}, authenticate=verifier)
    assert "SECRET" not in str(err.value) and err.value.code == "AUTHENTICATION"


def test_evidence_missing_wrong_digest_bad_codec_and_index_fail_explicitly():
    value = plan("one-record")
    body = observed(value, [["101", "101"]])
    for banks, code in [({}, "EVIDENCE"), ({"fixture:records": {"records": []}}, "DIGEST")]:
        with pytest.raises(QuantumValidationError, match=code):
            decode_verified(value, body, banks, authenticate=lambda *_: True)
    altered = fixture("one-record")
    altered["operations"][0]["packets"][0]["codec"]["name"] = "unknown"
    with pytest.raises(QuantumValidationError):
        parse_plan(altered)
    altered = fixture("one-record")
    bank = {"records": ["only-zero"]}
    altered["operations"][0]["packets"][0]["evidence_bank"]["digest"] = canonical_digest(bank)
    value = parse_plan(altered)
    with pytest.raises(QuantumValidationError, match="CODEC"):
        decode_verified(value, observed(value, [["101", "101"]]), {"fixture:records": bank},
                        authenticate=lambda *_: True)


def test_duplicate_delivery_across_sampling_pubs_and_bad_catalog_fail():
    body = fixture("one-record")
    second = copy.deepcopy(body["operations"][1])
    second["id"] = "second-sample"
    second["result"]["pub_index"] = 1
    body["operations"][1]["limits"]["max_circuits"] = 2
    second["limits"]["max_circuits"] = 2
    body["operations"].append(second)
    value = parse_plan(body)
    with pytest.raises(QuantumValidationError, match="RECIPIENT"):
        decode_verified(value, observed(value, [["101", "101"], ["101", "101"]]),
                        {"fixture:records": fixture("evidence-bank")}, authenticate=lambda *_: True)
    with pytest.raises(QuantumValidationError, match="TYPE"):
        decode_verified(value, observed(value, [["101", "101"], ["101", "101"]]), None)


@qiskit_test
def test_raw_bits_and_msb_packet_significance():
    body = fixture("one-record")
    packet = body["operations"][0]["packets"][0]
    packet["codec"]["name"] = "raw_bits"
    value = parse_plan(body)
    assert decode_verified(value, run_local(value), {"fixture:records": fixture("evidence-bank")},
                           authenticate=lambda *_: True).records[0].record == "10"
    packet["codec"]["name"] = "evidence_index"
    packet["bit_significance"] = "msb_first"
    value = parse_plan(body)
    assert decode_verified(value, run_local(value), {"fixture:records": fixture("evidence-bank")},
                           authenticate=lambda *_: True).records[0].record["id"] == "record-2"


@qiskit_test
def test_400_recipients_1200_actual_sampler_pubs_preserve_exact_mapping():
    value = large_mapping_fixture()
    compiled = lower_local(value)
    observations = run_local(compiled)
    assert len(compiled.pubs) == len(observations["pubs"]) == 1200
    bank = {"records": [f"record-{i}" for i in range(1200)]}
    decoded = decode_verified(value, observations, {"fixture:large-bank": bank})
    assert len({r.recipient_id for r in decoded.records}) == 400
    assert [(r.recipient_id, r.chunk, r.slot, r.pub_index, r.record) for r in decoded.records] == [
        (f"recipient-{i // 3:03d}", i % 3, 0, i, f"record-{i}") for i in range(1200)]
    swapped = copy.deepcopy(observations)
    swapped["pubs"][0], swapped["pubs"][1199] = swapped["pubs"][1199], swapped["pubs"][0]
    with pytest.raises(QuantumValidationError, match="PUB"):
        decode_verified(value, swapped, {"fixture:large-bank": bank})


def profile_from_payload(body):
    return LegacyCompileProfile(target=body["compile"]["target"], backend_kind=body["backend"]["kind"],
        chi_max=body["backend"]["chi_max"], opt_level=body["compile"]["opt_level"],
        compile_seed=body["compile"]["seed"], max_variables=body["limits"]["max_variables"],
        compiled_search_digest=body["compiled_search_digest"])


def test_exact_pinned_legacy_job_read_and_explicit_optimizer_lowering_roundtrip():
    frozen = fixture("legacy-job-v1")
    assert frozen["source"]["commit"] == BRIDGE_SOURCE_SHA
    body = frozen["payload"]
    imported = read_legacy(body)
    job = to_legacy_job(imported.plan, "legacy-optimize-0", profile_from_payload(body))
    assert isinstance(job, QSearchJob) and job.job_digest == body["job_digest"]
    assert imported.to_legacy_payload() == body
    assert imported.plan.operations[0].method["gammas"] == tuple(body["compile"]["gammas"])
    assert imported.metadata["job_digests"] == (body["job_digest"],)
    body["compile"]["seed"] = 999
    assert imported.to_legacy_payload()["compile"]["seed"] != 999
    with pytest.raises(QuantumValidationError, match="UNSUPPORTED"):
        to_legacy_job(plan("known-bits"), "prepare", profile_from_payload(frozen["payload"]))


def test_explicit_optimizer_never_converts_local_effect_to_ibm():
    value = plan("optimize-qubo")
    profile = LegacyCompileProfile("heron_heavy_hex", "statevector", 64, 1, 0, 2, value.plan_digest)
    job = to_legacy_job(value, "optimize", profile)
    assert job.q == ((-1.0, 0.0), (0.0, 1.0)) and job.p == 1
    with pytest.raises(QuantumValidationError, match="EFFECT"):
        to_legacy_job(value, "optimize", replace(profile, target="ibm_fez", backend_kind="ibm"))
    with pytest.raises(QuantumValidationError, match="EFFECT"):
        to_legacy_job(value, "optimize", replace(profile, target="ibm_fez", backend_kind="auto"))
    with pytest.raises(QuantumValidationError, match="EFFECT"):
        to_legacy_job(value, "optimize", replace(profile, target="ibm_fez"))
    with pytest.raises(QuantumValidationError, match="EFFECT"):
        to_legacy_job(value, "optimize", replace(profile, backend_kind="auto"))
    with pytest.raises(JobValidationError):
        to_legacy_job(value, "optimize", replace(profile, opt_level=True))


def test_optimizer_audit_and_packet_delivery_are_independent_derived_verdicts():
    value = plan("optimize-qubo")
    passed = decode_verified(value, observed(value, [["01", "01"]]), {})
    failed = decode_verified(value, observed(value, [["00", "00"]]), {})
    assert passed.optimization_verdict == "pass" and passed.delivery_verdict == "not_applicable"
    assert failed.optimization_verdict == "fail" and failed.delivery_verdict == "not_applicable"
    assert failed.state == "failed"


def test_pinned_v2_request_reader_preserves_all_legacy_recipient_metadata():
    frozen = fixture("legacy-request-v2")
    assert frozen["source"]["commit"] == BRIDGE_SOURCE_SHA
    body = frozen["payload"]
    imported = read_legacy(body)
    assert imported.to_legacy_payload() == body
    assert imported.metadata["request_digest"] == canonical_digest(body["descriptor"])
    assert [o.result["pub_index"] for o in imported.plan.operations] == [0, 1, 2]
    assert [(b["pub_index"], r["agent_id"], r["chunk"], r["slot"])
            for b in imported.metadata["descriptor"]["blocks"] for r in b["recipients"]] == [
        (0, "agent-007", 0, 0), (1, "agent-007", 1, 0), (2, "agent-007", 2, 0)]
    for operation, original in zip(imported.plan.operations, body["jobs"]):
        job = to_legacy_job(imported.plan, operation.id, profile_from_payload(original))
        assert job.job_digest == original["job_digest"]


@pytest.mark.parametrize("mutate,code", [
    (lambda b: b["jobs"].pop(), "PUB"),
    (lambda b: b["descriptor"]["blocks"][0].update(pub_index=True), "LEGACY_MISMATCH"),
    (lambda b: b["descriptor"]["blocks"][0].update(shots=True), "LEGACY_MISMATCH"),
    (lambda b: b["descriptor"]["blocks"][0].update(classical_min_energy=True), "LEGACY_MISMATCH"),
    (lambda b: b["descriptor"]["blocks"][1]["recipients"][0].update(chunk=0), "RECIPIENT"),
    (lambda b: b["descriptor"]["blocks"].reverse(), "LEGACY_MISMATCH"),
    (lambda b: b["descriptor"].update(extra="unknown"), "LEGACY_FIELDS"),
    (lambda b: b["descriptor"].update(manifest_digest="wrong"), "DIGEST"),
])
def test_legacy_request_negative_cross_checks(mutate, code):
    body = fixture("legacy-request-v2")["payload"]
    mutate(body)
    with pytest.raises(QuantumValidationError, match=code):
        read_legacy(body)


def test_legacy_unknown_schema_and_pub_bound_are_not_guessed():
    with pytest.raises(QuantumValidationError, match="LEGACY_SCHEMA"):
        read_legacy({"schema": "predator.qsearch.request.v1"})
    with pytest.raises(QuantumValidationError, match="PUB"):
        read_legacy(fixture("legacy-request-v2")["payload"], max_pubs=2)
    with pytest.raises(QuantumValidationError, match="LIMITS"):
        read_legacy(fixture("legacy-request-v2")["payload"], max_pubs=True)


@qiskit_test
def test_local_worker_hard_timeout_and_failure_fail_closed(monkeypatch):
    import nano_qsearch.quantum_adapter as adapter
    value = plan("known-bits")
    calls = []
    def timed_out(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], stderr="SECRET")
    monkeypatch.setattr(adapter.subprocess, "run", timed_out)
    untrusted_bundle = lower_local(value, policy=LocalHostPolicy(max_qubits=99))
    with pytest.raises(QuantumValidationError) as err:
        run_local(untrusted_bundle)
    assert err.value.code == "TIMEOUT" and "SECRET" not in str(err.value)
    assert calls[0][0] == [sys.executable, "-m", "nano_qsearch.quantum_adapter", "--local-worker"]
    assert calls[0][1]["timeout"] == 5
    assert json.loads(calls[0][1]["input"])["plan"] == value.to_payload()
    assert json.loads(calls[0][1]["input"])["policy"]["max_qubits"] == 16
    monkeypatch.setattr(adapter.subprocess, "run", lambda *_a, **_k:
                        subprocess.CompletedProcess([], 1, stdout="", stderr="SECRET"))
    with pytest.raises(QuantumValidationError, match="LOCAL_WORKER") as err:
        run_local(value)
    assert "SECRET" not in str(err.value)


def test_legacy_zero_wall_limit_requires_explicit_migration_policy():
    body = fixture("legacy-job-v1")["payload"]
    body["limits"]["max_wall_s"] = 0
    body["job_digest"] = canonical_digest({k: v for k, v in body.items() if k != "job_digest"})
    with pytest.raises(QuantumValidationError, match="LEGACY_LIMIT"):
        read_legacy(body)
