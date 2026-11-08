"""Native prepare/sample transport contract (#61 slice 1): stdlib only, no SDK or provider."""
import copy
import json

import pytest

from nano.quantum import QuantumValidationError, build_plan, prepare_bits, sample
from nano.quantum.canonical import canonical_digest
from nano.quantum.native import (DESCRIPTOR_KEYS, REQUEST_SCHEMA, capability_matrix, native_plan,
                                 native_resources, parse_native_plan, parse_native_profile, parse_native_target,
                                 parse_request_descriptor, parse_resources, pub_map, pub_map_digest)
from native_bridge_support import (PLAN_FIXTURES, SCHEMAS, contract_golden, plan, profile_payload, read,
                                   target_payload)


def code(exc_info):
    return exc_info.value.code


def descriptor(name="known-bits", **changes):
    golden = read("native-goldens")
    value = {"schema": REQUEST_SCHEMA, "request_id": "native-known-bits", "target": "local_ideal",
             "artifact_digest": "sha256:" + "a" * 64, "plan_digest": golden["plans"][name]["plan_digest"],
             "pub_map_digest": golden["plans"][name]["pub_map_digest"], "profile_digest": golden["profile_digest"],
             "target_digest": golden["target_digest"], "resources": golden["plans"][name]["resources"]}
    return {**copy.deepcopy(value), **changes}


@pytest.mark.parametrize("name,schema", [("native-target", "native-target-v1"), ("native-profile", "native-profile-v1"),
                                         ("native-capabilities", "native-capabilities-v1")])
def test_frozen_native_fixtures_satisfy_their_structural_schemas(name, schema):
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json.loads((SCHEMAS / f"{schema}.schema.json").read_text()))
    validator.validate(read(name))


def test_target_profile_and_capability_goldens_are_frozen():
    goldens = read("native-goldens")
    assert read("native-target") == target_payload()
    assert read("native-profile") == profile_payload()
    assert parse_native_target(read("native-target")).digest == goldens["target_digest"]
    assert parse_native_profile(read("native-profile")).digest == goldens["profile_digest"]
    assert read("native-profile")["target_digest"] == goldens["target_digest"]
    assert read("native-capabilities") == capability_matrix()


@pytest.mark.parametrize("name", [*PLAN_FIXTURES, "large-mapping"])
def test_plan_pub_map_and_resource_goldens_are_frozen(name):
    expected = read("native-goldens")["plans"][name]
    actual = contract_golden(name)
    assert {k: expected[k] for k in actual} == actual


def test_large_fixture_plan_digest_matches_existing_mapping_golden():
    assert read("native-goldens")["plans"]["large-mapping"]["plan_digest"] == read("large-mapping")["plan_digest"]


def test_pub_map_freezes_register_bit_offsets_and_packet_order():
    rows = pub_map(plan("packed-records"))
    assert len(rows) == 1 and rows[0]["register"] == "meas" and rows[0]["bit_order"] == "msb_first"
    assert [(p["recipient_id"], p["chunk"], p["slot"], p["offset"], p["payload_width"],
             p["authentication_width"], p["bit_significance"]) for p in rows[0]["packets"]] == [
        (p["recipient_id"], p["chunk"], p["slot"], off, p["payload_width"], p["authentication_width"],
         p["bit_significance"])
        for p, off in zip(read("packed-records")["operations"][0]["packets"], (0, 3))]
    assert rows[0]["variables"] == read("packed-records")["operations"][0]["variables"]


def test_large_pub_map_binds_every_recipient_chunk_to_its_pub():
    rows = pub_map(plan("large-mapping"))
    assert len(rows) == 1200
    seen = set()
    for index, row in enumerate(rows):
        assert row["pub_index"] == index and len(row["packets"]) == 1
        packet = row["packets"][0]
        assert (packet["recipient_id"], packet["chunk"]) == (f"recipient-{index // 3:03d}", index % 3)
        seen.add((packet["recipient_id"], packet["chunk"]))
    assert len(seen) == 1200


def test_identity_changes_with_plan_packet_map_and_resource_bounds():
    base = read("one-record")
    digests = {pub_map_digest(parse_native_plan(base))}
    changed_bits = copy.deepcopy(base)
    changed_bits["operations"][0]["bits"] = "001"
    changed_packet = copy.deepcopy(base)
    changed_packet["operations"][0]["packets"][0]["recipient_id"] = "recipient-b"
    changed_bound = copy.deepcopy(base)
    changed_bound["operations"][1]["limits"]["max_wall_s"] = 6
    for variant in (changed_bits, changed_packet, changed_bound):
        digests.add(pub_map_digest(parse_native_plan(variant)) if variant is not changed_bound
                    else parse_native_plan(variant).plan_digest)
    assert len(digests) == 4
    assert native_resources(parse_native_plan(changed_bound))["deadline_s"] == 6


def test_preparation_only_plan_is_admitted_without_execution_resources():
    value = native_plan(build_plan([prepare_bits("10")]))
    assert pub_map(value) == []
    assert native_resources(value) == {"preparations": 1, "pub_count": 0, "total_shots": 0,
                                       "max_shots_per_pub": 0, "max_width": 2, "deadline_s": None,
                                       "qpu_seconds": 0}


def test_optimize_and_unknown_operations_are_unsupported_not_relabelled():
    with pytest.raises(QuantumValidationError) as exc:
        parse_native_plan(read("optimize-qubo"))
    assert code(exc) == "UNSUPPORTED_OPERATION"
    unknown = copy.deepcopy(read("known-bits"))
    unknown["operations"][0]["op"] = "superpose"
    with pytest.raises(QuantumValidationError) as exc:
        parse_native_plan(unknown)
    assert code(exc) == "UNSUPPORTED_OPERATION"
    future = {**read("known-bits"), "schema": "nano.quantum.operations.v2"}
    with pytest.raises(QuantumValidationError) as exc:
        parse_native_plan(future)
    assert code(exc) == "UNSUPPORTED_VERSION"


def test_native_capability_cannot_name_hardware():
    hardware = copy.deepcopy(read("known-bits"))
    hardware["operations"][1]["backend"] = "ibm_fez"
    with pytest.raises(QuantumValidationError) as exc:
        parse_native_plan(hardware)
    assert code(exc) == "UNSUPPORTED_TARGET"
    for change in ({"name": "ibm_fez"}, {"kind": "hardware"}, {"qpu_seconds": 1}, {"basis": ["measure", "x", "cz"]}):
        with pytest.raises(QuantumValidationError) as exc:
            parse_native_target({**target_payload(), **change})
        assert code(exc) == "UNSUPPORTED_TARGET"
    with pytest.raises(QuantumValidationError) as exc:
        parse_native_target({**target_payload(), "num_qubits": 21})
    assert code(exc) == "LIMITS"
    with pytest.raises(QuantumValidationError) as exc:
        parse_resources({**native_resources(plan("known-bits")), "qpu_seconds": 1})
    assert code(exc) == "UNSUPPORTED_TARGET"


@pytest.mark.parametrize("change,expected", [
    ({"compiler": {"name": "other", "version": "1.1.0", "qiskit_version": "2.3.1", "qiskit_ibm_runtime_version": None}},
     "UNSUPPORTED_COMPILER"),
    ({"compiler": {"name": "nano-qsearch", "version": "0.0.1", "qiskit_version": "2.3.1",
                   "qiskit_ibm_runtime_version": None}}, "UNSUPPORTED_COMPILER"),
    ({"compiler": {"name": "nano-qsearch", "version": "1.1.0", "qiskit_version": "2.3.0",
                   "qiskit_ibm_runtime_version": None}}, "UNSUPPORTED_SDK"),
    ({"compiler": {"name": "nano-qsearch", "version": "1.1.0", "qiskit_version": "2.3.1",
                   "qiskit_ibm_runtime_version": "0.40.0"}}, "UNSUPPORTED_SDK"),
    ({"operations": ["prepare_bits", "sample", "optimize_qubo"]}, "UNSUPPORTED_OPERATION"),
    ({"layout": "target"}, "UNSUPPORTED_CAPABILITY"),
    ({"optimization_level": 1}, "UNSUPPORTED_CAPABILITY"),
    ({"schema": "nano.quantum.native-profile.v2"}, "UNSUPPORTED_VERSION"),
    ({"schema": "nano.qsearch.compile-profile.v1"}, "SCHEMA"),
])
def test_profile_refuses_wrong_compiler_sdk_operation_and_version(change, expected):
    body = {**profile_payload(), **change}
    if "compiler" in change:
        body["compiler"]["version"] = change["compiler"]["version"]
    with pytest.raises(QuantumValidationError) as exc:
        parse_native_profile(body)
    assert code(exc) == expected


def test_one_packet_bearing_preparation_cannot_be_sampled_twice():
    body = copy.deepcopy(read("one-record"))
    second = copy.deepcopy(body["operations"][1])
    second.update(id="sample-again", result={**second["result"], "pub_index": 1})
    body["operations"].append(second)
    for operation in body["operations"][1:]:
        operation["limits"]["max_circuits"] = 2
    with pytest.raises(QuantumValidationError) as exc:
        parse_native_plan(body)
    assert code(exc) == "RECIPIENT"
    repeated = build_plan([prepare_bits("1"), *(sample("prepare", operation_id=f"s{i}", expected_width=1, shots=1,
                                                      max_circuits=2, max_wall_s=5, max_qpu_s=0, pub_index=i)
                                               for i in range(2))])
    assert len(pub_map(repeated)) == 2


def test_recipient_chunk_collision_is_refused_by_the_shared_admission():
    body = copy.deepcopy(read("packed-records"))
    body["operations"][0]["packets"][1]["recipient_id"] = body["operations"][0]["packets"][0]["recipient_id"]
    body["operations"][0]["packets"][1]["chunk"] = body["operations"][0]["packets"][0]["chunk"]
    with pytest.raises(QuantumValidationError) as exc:
        parse_native_plan(body)
    assert code(exc) == "RECIPIENT"


def test_descriptor_header_is_strict_and_versioned():
    assert parse_request_descriptor(descriptor())["request_id"] == "native-known-bits"
    assert set(descriptor()) == DESCRIPTOR_KEYS
    cases = [({"schema": "nano.quantum.native-request.v2"}, "UNSUPPORTED_VERSION"),
             ({"schema": "predator.qsearch.request.v2"}, "SCHEMA"),
             ({"target": "ibm_fez"}, "UNSUPPORTED_TARGET"),
             ({"request_id": "bad id"}, "TYPE"),
             ({"artifact_digest": "sha256:XYZ"}, "DIGEST"),
             ({"extra": 1}, "FIELDS")]
    for change, expected in cases:
        with pytest.raises(QuantumValidationError) as exc:
            parse_request_descriptor(descriptor(**change))
        assert code(exc) == expected, change
    bad = descriptor()
    bad["resources"] = {**bad["resources"], "deadline_s": None}
    with pytest.raises(QuantumValidationError) as exc:
        parse_request_descriptor(bad)
    assert code(exc) == "LIMITS"
    bad["resources"] = {**descriptor()["resources"], "pub_count": True}
    with pytest.raises(QuantumValidationError) as exc:
        parse_request_descriptor(bad)
    assert code(exc) == "TYPE"


def test_capability_matrix_matches_actual_legacy_and_direct_surfaces():
    from nano_qsearch import prepare
    from nano_qsearch.quantum_adapter import LegacyCompileProfile, lower_local, to_legacy_job
    matrix = capability_matrix()
    assert prepare.SUPPORTED_OPERATION == "optimize_qubo"
    assert matrix["operations"]["prepare_bits"]["legacy_prepared_artifact"] == "unsupported"
    profile = LegacyCompileProfile("fake_fez", "statevector", 1, 0, 0, 3, "sha256:" + "0" * 64)
    with pytest.raises(QuantumValidationError) as exc:
        to_legacy_job(plan("known-bits"), "prepare", profile)
    assert code(exc) == "UNSUPPORTED" and matrix["operations"]["prepare_bits"]["legacy_job_adapter"] == "unsupported"
    with pytest.raises(QuantumValidationError) as exc:
        lower_local(plan("optimize-qubo"))
    assert matrix["operations"]["optimize_qubo"]["direct_local_adapter"] == "unsupported"
    assert matrix["defaults"]["native_service_enabled"] is False
    assert matrix["defaults"]["native_enables_hardware"] is False
    assert canonical_digest(matrix) == canonical_digest(read("native-capabilities"))
