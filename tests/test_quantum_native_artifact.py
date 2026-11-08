"""Native prepared artifact (#61 slice 2): pinned local Qiskit build/load, no IBM or network."""
import base64
import copy
import hashlib
import importlib.util
import json
import sys

import pytest

from nano.quantum import QuantumValidationError, build_plan, prepare_bits, sample
from nano.quantum.canonical import canonical_digest
from nano.quantum.native import parse_native_plan, pub_map_digest
from nano_qsearch.native_artifact import (METADATA_KEY, build_circuit, load_native_circuits,
                                          parse_native_artifact, prepare_native, qpy_bytes)
from nano_qsearch.native_request import build_native_request, parse_native_request, request_digest
from native_bridge_support import (PLAN_FIXTURES, SCHEMAS, plan, profile_payload, read, target_payload)

qiskit_test = pytest.mark.skipif(importlib.util.find_spec("qiskit") is None, reason="requires the pinned quantum extra")
FORBIDDEN_KEYS = {"qubo", "method", "gammas", "betas", "p", "samples", "optimization_verdict", "delivery_verdict",
                  "seal", "usage", "receipt", "hardware"}


def code(exc_info):
    return exc_info.value.code


def keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


def reseal(payload):
    body = {k: v for k, v in payload.items() if k != "artifact_digest"}
    return {**body, "artifact_digest": canonical_digest(body)}


def with_qpy(payload, index, binary):
    payload["pubs"][index]["qpy_base64"] = base64.b64encode(binary).decode("ascii")
    payload["pubs"][index]["isa_digest"] = "sha256:" + hashlib.sha256(binary).hexdigest()
    return reseal(payload)


def two_pub_plan():
    return build_plan([prepare_bits("10", operation_id="a"), prepare_bits("01", operation_id="b"),
                       sample("a", operation_id="sa", expected_width=2, shots=2, max_circuits=2, max_wall_s=5,
                              max_qpu_s=0, pub_index=0),
                       sample("b", operation_id="sb", expected_width=2, shots=2, max_circuits=2, max_wall_s=5,
                              max_qpu_s=0, pub_index=1)])


def artifact(name="known-bits"):
    return prepare_native(plan(name), profile_payload(), target_payload())


def refuse(payload, expected_code, **kwargs):
    with pytest.raises(QuantumValidationError) as exc:
        parse_native_artifact(payload, **kwargs)
    assert code(exc) == expected_code
    return exc


def refuse_load(payload, expected_code):
    with pytest.raises(QuantumValidationError) as exc:
        load_native_circuits(payload, expected_digest=payload["artifact_digest"])
    assert code(exc) == expected_code


def test_frozen_known_bits_artifact_parses_without_an_sdk_and_matches_its_schema():
    frozen = read("native-artifact-known-bits")
    golden = read("native-goldens")["plans"]["known-bits"]
    parsed = parse_native_artifact(frozen, expected_digest=golden["artifact_digest"])
    assert parsed.plan_digest == golden["plan_digest"] and parsed.pub_map_digest == golden["pub_map_digest"]
    assert parsed.profile_digest == read("native-goldens")["profile_digest"]
    assert request_digest(build_native_request(parsed, request_id=golden["request_id"])) == golden["request_digest"]
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator(json.loads((SCHEMAS / "native-artifact-v1.schema.json").read_text())).validate(frozen)
    request = build_native_request(parsed, request_id="native-known-bits")
    jsonschema.Draft202012Validator(json.loads((SCHEMAS / "native-request-v1.schema.json").read_text())).validate(request)


def test_artifact_has_no_fabricated_optimization_samples_verdicts_or_execution_claims():
    frozen = read("native-artifact-known-bits")
    assert not FORBIDDEN_KEYS & keys({k: v for k, v in frozen.items() if k != "plan"})
    assert not {"qubo", "method", "gammas", "betas", "audit"} & keys(frozen["plan"])
    assert frozen["effect"] == "local_compile"


@qiskit_test
@pytest.mark.parametrize("name", [*PLAN_FIXTURES, "large-mapping"])
def test_pinned_build_is_deterministic_and_matches_frozen_goldens(name):
    golden = read("native-goldens")["plans"][name]
    first = artifact(name)
    assert first.artifact_digest == golden["artifact_digest"]
    assert request_digest(build_native_request(first, request_id=golden["request_id"])) == golden["request_digest"]
    if name == "known-bits":
        assert first.to_payload() == read("native-artifact-known-bits")
        assert artifact(name).to_payload() == first.to_payload()


@qiskit_test
def test_load_checks_layout_register_and_bit_order_without_ibm_or_atlas(monkeypatch):
    monkeypatch.setitem(sys.modules, "qiskit_ibm_runtime", None)
    monkeypatch.setitem(sys.modules, "aether_atlas", None)
    value = artifact("packed-records")
    loaded, circuits = load_native_circuits(value.to_payload(), expected_digest=value.artifact_digest)
    assert loaded.artifact_digest == value.artifact_digest and len(circuits) == 1
    circuit = circuits[0]
    bits = plan("packed-records").operations[0].bits
    assert circuit.cregs[0].name == "meas" and circuit.num_qubits == len(bits)
    assert [circuit.find_bit(i.qubits[0]).index for i in circuit.data if i.operation.name == "x"] == \
        [i for i, b in enumerate(bits) if b == "1"]
    assert [(circuit.find_bit(i.qubits[0]).index, circuit.find_bit(i.clbits[0]).index)
            for i in circuit.data if i.operation.name == "measure"] == [(i, i) for i in range(len(bits))]
    assert circuit.metadata[METADATA_KEY]["plan_digest"] == value.plan_digest


@qiskit_test
def test_preparation_only_artifact_carries_no_pub_or_execution_claim():
    only = prepare_native(build_plan([prepare_bits("101")]), profile_payload(), target_payload())
    payload = only.to_payload()
    assert payload["pubs"] == [] and payload["resources"]["pub_count"] == 0
    assert payload["preparations"] == [{"preparation_id": "prepare", "width": 3, "x_qubits": [0, 2]}]
    assert not FORBIDDEN_KEYS & keys({k: v for k, v in payload.items() if k != "plan"})
    assert load_native_circuits(payload, expected_digest=only.artifact_digest)[1] == []


@qiskit_test
def test_substituted_or_tampered_artifact_is_refused_by_retained_digest_and_contents():
    value = artifact("one-record").to_payload()
    refuse(value, "IDENTITY", expected_digest=read("native-goldens")["plans"]["known-bits"]["artifact_digest"])
    tampered = copy.deepcopy(value)
    tampered["pubs"][0]["shots"] = 3
    refuse(tampered, "DIGEST")
    refuse(reseal(tampered), "IDENTITY")
    bad_resources = copy.deepcopy(value)
    bad_resources["resources"]["total_shots"] = 1
    refuse(reseal(bad_resources), "LIMITS")
    refuse({**value, "schema": "nano.quantum.native-artifact.v2"}, "UNSUPPORTED_VERSION")
    refuse({**value, "schema": "nano.qsearch.prepared-artifact.v1"}, "SCHEMA")


@qiskit_test
def test_missing_duplicate_and_reordered_pubs_fail():
    value = prepare_native(two_pub_plan(), profile_payload(), target_payload()).to_payload()
    missing = copy.deepcopy(value)
    missing["pubs"].pop()
    refuse(reseal(missing), "PUB")
    duplicate = copy.deepcopy(value)
    duplicate["pubs"][1] = copy.deepcopy(duplicate["pubs"][0])
    refuse(reseal(duplicate), "PUB")
    reordered = copy.deepcopy(value)
    reordered["pubs"].reverse()
    refuse(reseal(reordered), "PUB")


@qiskit_test
def test_wrong_width_layout_and_target_width_fail():
    value = artifact("known-bits").to_payload()
    wide = copy.deepcopy(value)
    wide["pubs"][0]["width"] = 4
    refuse(reseal(wide), "WIDTH")
    shuffled = copy.deepcopy(value)
    shuffled["pubs"][0]["layout"] = [1, 0, 2]
    refuse(reseal(shuffled), "WIDTH")
    narrow_target = {**target_payload(), "num_qubits": 2}
    from nano.quantum.native import native_profile, parse_native_target
    with pytest.raises(QuantumValidationError) as exc:
        prepare_native(plan("known-bits"), native_profile(parse_native_target(narrow_target).digest), narrow_target)
    assert code(exc) == "WIDTH"


@qiskit_test
def test_substituted_profile_target_and_plan_are_refused():
    value = artifact("one-record").to_payload()
    other_target = {**target_payload(), "num_qubits": 20}
    swapped = copy.deepcopy(value)
    swapped["target"] = other_target
    refuse(reseal(swapped), "IDENTITY")
    from nano.quantum.native import native_profile, parse_native_profile, parse_native_target
    rebound = copy.deepcopy(swapped)
    rebound["target_digest"] = parse_native_target(other_target).digest
    rebound["profile"] = native_profile(rebound["target_digest"])
    rebound["profile_digest"] = parse_native_profile(rebound["profile"]).digest
    refuse_load(reseal(rebound), "METADATA")
    renamed = copy.deepcopy(value)
    renamed["profile"]["id"] = "another-profile"
    refuse(reseal(renamed), "IDENTITY")
    stale = copy.deepcopy(value)
    stale["plan"]["operations"][0]["packets"][0]["evidence_bank"]["digest"] = "sha256:" + "0" * 64
    refuse(reseal(stale), "IDENTITY")
    replanned = parse_native_plan(stale["plan"])
    stale.update(plan_digest=replanned.plan_digest, pub_map_digest=pub_map_digest(replanned))
    refuse_load(reseal(stale), "METADATA")


@qiskit_test
def test_recipient_collision_and_unsupported_operations_inside_artifact_fail():
    value = artifact("packed-records").to_payload()
    collision = copy.deepcopy(value)
    packets = collision["plan"]["operations"][0]["packets"]
    packets[1].update(recipient_id=packets[0]["recipient_id"], chunk=packets[0]["chunk"])
    refuse(reseal(collision), "RECIPIENT")
    optimize = copy.deepcopy(value)
    optimize["plan"] = read("optimize-qubo")
    refuse(reseal(optimize), "UNSUPPORTED_OPERATION")
    for field, wrong, expected in (("version", "0.0.1", "UNSUPPORTED_COMPILER"),
                                   ("qiskit_version", "2.3.0", "UNSUPPORTED_SDK")):
        compiler = copy.deepcopy(value)
        compiler["profile"]["compiler"][field] = wrong
        refuse(reseal(compiler), expected)


@qiskit_test
def test_corrupted_qpy_metadata_isa_and_drift_fail_before_execution():
    import qiskit
    value = artifact("known-bits")
    payload = value.to_payload()
    raw = base64.b64decode(payload["pubs"][0]["qpy_base64"])
    unsealed = copy.deepcopy(payload)
    unsealed["pubs"][0]["qpy_base64"] = base64.b64encode(raw[:-7]).decode("ascii")
    refuse(reseal(unsealed), "QPY")
    not_base64 = copy.deepcopy(payload)
    not_base64["pubs"][0]["qpy_base64"] = "!!!!"
    refuse(reseal(not_base64), "QPY")
    refuse_load(with_qpy(copy.deepcopy(payload), 0, raw[:-7]), "QPY")
    preparation = value.plan.operations[0]
    identity = load_native_circuits(payload, expected_digest=value.artifact_digest)[1][0].metadata[METADATA_KEY]
    wrong_identity = {**identity, "pub_index": 7}
    refuse_load(with_qpy(copy.deepcopy(payload), 0,
                         qpy_bytes(build_circuit(qiskit, preparation, "meas", wrong_identity))), "METADATA")
    extra = build_circuit(qiskit, preparation, "meas", identity)
    extra.barrier()
    refuse_load(with_qpy(copy.deepcopy(payload), 0, qpy_bytes(extra)), "ISA")
    drifted = build_circuit(qiskit, preparation, "meas", identity)
    drifted.global_phase = 0.5
    refuse_load(with_qpy(copy.deepcopy(payload), 0, qpy_bytes(drifted)), "DRIFT")


@qiskit_test
def test_missing_or_wrong_sdk_has_stable_diagnostics(monkeypatch):
    value = artifact("known-bits")
    import qiskit
    monkeypatch.setattr(qiskit, "__version__", "2.3.0")
    with pytest.raises(QuantumValidationError) as exc:
        load_native_circuits(value.to_payload(), expected_digest=value.artifact_digest)
    assert code(exc) == "UNSUPPORTED_SDK"
    monkeypatch.setitem(sys.modules, "qiskit", None)
    for call in (lambda: prepare_native(plan("known-bits"), profile_payload(), target_payload()),
                 lambda: load_native_circuits(value.to_payload(), expected_digest=value.artifact_digest)):
        with pytest.raises(QuantumValidationError) as exc:
            call()
        assert code(exc) == "UNAVAILABLE_DEPENDENCY"
    assert parse_native_artifact(value.to_payload()).artifact_digest == value.artifact_digest


@qiskit_test
def test_request_descriptor_must_equal_its_artifact():
    value = artifact("one-record")
    body = build_native_request(value, request_id="native-one")
    assert parse_native_request(body).request_digest == request_digest(body)
    lying = copy.deepcopy(body)
    lying["descriptor"]["resources"]["total_shots"] = 1
    with pytest.raises(QuantumValidationError) as exc:
        parse_native_request(lying)
    assert code(exc) == "IDENTITY"
    substituted = copy.deepcopy(body)
    substituted["artifact"] = artifact("packed-records").to_payload()
    with pytest.raises(QuantumValidationError) as exc:
        parse_native_request(substituted)
    assert code(exc) == "IDENTITY"
    with pytest.raises(QuantumValidationError) as exc:
        parse_native_request({**body, "schema": "nano.quantum.native-request.v9"})
    assert code(exc) == "UNSUPPORTED_VERSION"
    calls = []
    parse_native_request(body, admit=calls.append)
    assert calls and calls[0]["request_id"] == "native-one"
