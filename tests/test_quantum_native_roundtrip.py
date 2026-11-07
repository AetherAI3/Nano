"""Cross-boundary native evidence (#61 slice 4): builder -> plan -> artifact -> authenticated local
service -> local ideal samples -> caller-verified typed records. No IBM, network or paid call."""
import copy
import hashlib
import hmac
import importlib.util
import json
import subprocess
import sys
import urllib.request

import pytest

from nano.quantum import QuantumValidationError, build_plan, prepare_bits, sample
from nano.quantum.canonical import canonical_json
from nano.quantum.conformance import large_mapping_fixture
from nano_qsearch.native_artifact import prepare_native
from nano_qsearch.native_policy import seal_native_result
from nano_qsearch.native_request import build_native_request
from nano_qsearch.native_result import retain_identity, verify_native_result
from nano_qsearch.native_service import NATIVE_SUBMIT_PATH
from nano_qsearch.service import QSearchService, serve_in_thread
from native_bridge_support import (ROOT, SYNTHETIC_RECEIPT_KEY, Clock, body_bytes, headers, native_policy, plan,
                                   profile_payload, read, service_config, target_payload)

qiskit_test = pytest.mark.skipif(importlib.util.find_spec("qiskit") is None, reason="requires the pinned quantum extra")
TEST_MAX_BODY_BYTES = 8 * 1024 * 1024        # explicit host test cap; the production default stays 1 MiB
QUBO_KEYS = {"qubo", "method", "gammas", "betas", "audit", "p"}


def keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


def catalog(name):
    if name == "large-mapping":
        return {"fixture:large-bank": {"records": [f"record-{i}" for i in range(1200)]}}
    return {"fixture:records": read("evidence-bank")} if name != "known-bits" else {}


def forge(record, **change):
    """What a compromised service holding the receipt key could return."""
    from nano_qsearch.native_policy import SEAL_DOMAIN, SEAL_PREFIX
    body = {k: v for k, v in {**record, **change}.items() if k != "seal"}
    mac = hmac.new(SYNTHETIC_RECEIPT_KEY, SEAL_DOMAIN + canonical_json(body).encode("utf-8"), hashlib.sha256)
    return {**body, "seal": SEAL_PREFIX + mac.hexdigest()}


def auth_bit_is_one(packet, payload, auth):
    return auth == "1"


def caller_side(value, request_id):
    """The caller builds and retains everything before any service call."""
    artifact = prepare_native(value, profile_payload(), target_payload())
    body = build_native_request(artifact, request_id=request_id)
    return body, retain_identity(value, body)


def submit(svc, body, *, now=1000):
    raw = body_bytes(body)
    return svc.native.submit("POST", NATIVE_SUBMIT_PATH, raw, headers("POST", NATIVE_SUBMIT_PATH, raw, now=now), now=now)


def builder_plan(name):
    if name == "known-bits":
        return build_plan([prepare_bits("100"), sample("prepare", expected_width=3, shots=2, max_circuits=1,
                                                       max_wall_s=5, max_qpu_s=0)])
    return plan(name)


@qiskit_test
@pytest.mark.parametrize("name,expected", [("known-bits", []), ("one-record", ["record-1"]),
                                           ("packed-records", ["record-1", "record-2"])])
def test_native_source_to_verified_records_through_artifact_and_local_service(tmp_path, monkeypatch, name, expected):
    monkeypatch.setitem(sys.modules, "qiskit_ibm_runtime", None)
    monkeypatch.setitem(sys.modules, "aether_atlas", None)
    value = builder_plan(name)
    assert value.plan_digest == read("native-goldens")["plans"][name]["plan_digest"]
    body, expected_identity = caller_side(value, f"native-{name}")
    assert not QUBO_KEYS & keys(body["artifact"]["plan"]) and not QUBO_KEYS & keys(body["descriptor"])
    svc = QSearchService(service_config(tmp_path), clock=Clock())
    try:
        code, record = submit(svc, body)
        assert code == 200 and record["status"] == "OK" and record["backend"]["kind"] == "local_ideal"
        verified = verify_native_result(expected_identity, record, catalog(name), receipt_key=SYNTHETIC_RECEIPT_KEY,
                                        authenticate=auth_bit_is_one)
        assert verified.source_verification == "pass" and verified.execution_status == "OK"
        assert verified.accounting_status == "not_applicable" and verified.hardware_qualification == "none"
        assert verified.delivery_verdict == ("pass" if expected else "not_applicable")
        assert [r.record["id"] for r in verified.decoded.records] == expected
        assert verified.decoded.optimization_verdict == "not_applicable"
    finally:
        svc.close()


@qiskit_test
def test_400_recipients_1200_pubs_survive_artifact_load_and_http_local_service(tmp_path):
    value = large_mapping_fixture()
    assert value.plan_digest == read("large-mapping")["plan_digest"]
    body, expected_identity = caller_side(value, "native-large-mapping")
    assert expected_identity.request_digest == read("native-goldens")["plans"]["large-mapping"]["request_digest"]
    defaults = QSearchService(service_config(tmp_path / "defaults"), clock=Clock())
    try:
        raw = body_bytes(body)
        assert len(raw) > defaults.config.max_body_bytes == 1_048_576
        assert submit(defaults, body)[0] == 413                       # production body cap refuses it
    finally:
        defaults.close()
    small = QSearchService(service_config(tmp_path / "pubs", max_body_bytes=TEST_MAX_BODY_BYTES,
                                          native_policy=native_policy(max_pubs=1199)), clock=Clock())
    try:
        code, error = submit(small, body)
        assert (code, error["code"], error["field"]) == (400, "RESOURCE_LIMIT", "descriptor.resources.pub_count")
    finally:
        small.close()
    svc = QSearchService(service_config(tmp_path / "caps", max_body_bytes=TEST_MAX_BODY_BYTES,
                                        native_policy=native_policy(max_pubs=1200, max_total_shots=2400)),
                         clock=Clock())
    server, thread = serve_in_thread(svc)
    try:
        raw = body_bytes(body)
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{NATIVE_SUBMIT_PATH}", data=raw,
                                     method="POST", headers=headers("POST", NATIVE_SUBMIT_PATH, raw))
        with urllib.request.urlopen(req, timeout=300) as response:
            record = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        svc.close()
    assert record["status"] == "OK" and len(record["observations"]["pubs"]) == 1200
    verified = verify_native_result(expected_identity, record, catalog("large-mapping"),
                                    receipt_key=SYNTHETIC_RECEIPT_KEY)
    rows = [(r.recipient_id, r.chunk, r.slot, r.pub_index, r.record) for r in verified.decoded.records]
    assert rows == [(f"recipient-{i // 3:03d}", i % 3, 0, i, f"record-{i}") for i in range(1200)]
    assert verified.delivery_verdict == "pass" and verified.accounting_status == "not_applicable"


@qiskit_test
def test_caller_retained_identity_rejects_substitution_even_when_service_records_agree(tmp_path):
    body_a, expected_a = caller_side(plan("one-record"), "native-a")
    body_b, expected_b = caller_side(plan("packed-records"), "native-b")
    svc = QSearchService(service_config(tmp_path), clock=Clock())
    try:
        record_a, record_b = submit(svc, body_a)[1], submit(svc, body_b)[1]
    finally:
        svc.close()
    evidence = catalog("one-record")
    with pytest.raises(QuantumValidationError) as exc:
        verify_native_result(expected_a, record_b, evidence, receipt_key=SYNTHETIC_RECEIPT_KEY)
    assert exc.value.code == "IDENTITY"
    # A compromised service rewrites its own manifest to name A while returning B's observations.
    forged = {**copy.deepcopy(record_b), "request": record_a["request"], "identity": record_a["identity"]}
    forged = seal_native_result(forged, SYNTHETIC_RECEIPT_KEY)
    with pytest.raises(QuantumValidationError) as exc:
        verify_native_result(expected_a, forged, evidence, receipt_key=SYNTHETIC_RECEIPT_KEY,
                             authenticate=auth_bit_is_one)
    assert exc.value.code == "DIGEST"
    retargeted = seal_native_result({**copy.deepcopy(record_a), "identity": {**record_a["identity"],
                                     "target_digest": "sha256:" + "1" * 64}}, SYNTHETIC_RECEIPT_KEY)
    for changed, code in ((retargeted, "IDENTITY"), ({**record_a, "status": "FAILED"}, "SEAL")):
        with pytest.raises(QuantumValidationError) as exc:
            verify_native_result(expected_a, changed, evidence, receipt_key=SYNTHETIC_RECEIPT_KEY)
        assert exc.value.code == code
    with pytest.raises(QuantumValidationError) as exc:
        retain_identity(plan("packed-records"), body_a)
    assert exc.value.code == "IDENTITY"


@qiskit_test
def test_hardware_seal_or_accounting_claims_and_partial_records_are_rejected(tmp_path):
    body, expected = caller_side(plan("one-record"), "native-claims")
    svc = QSearchService(service_config(tmp_path), clock=Clock())
    try:
        record = submit(svc, body)[1]
    finally:
        svc.close()
    evidence = catalog("one-record")
    claims = [({"hardware": {"provider_job_id": "x"}}, "HARDWARE_CLAIM"),
              ({"backend": {**record["backend"], "dry_run": False}}, "HARDWARE_CLAIM"),
              ({"backend": {**record["backend"], "kind": "ibm"}}, "HARDWARE_CLAIM"),
              ({"accounting": {"status": "settled", "provider_usage": {"quantum_seconds": 0}}}, "ACCOUNTING"),
              ({"decoding": {"delivery_verdict": "pass", "optimization_verdict": "not_applicable"}}, "DECODING"),
              ({"seal": "hmac-sha256+hardware-v2:" + "0" * 64}, "SEAL")]
    claims += [({"effect": "submitted"}, "EFFECT"), ({"reason": "explained success"}, "TYPE")]
    for change, code in claims:
        changed = {**record, **change} if "seal" in change else forge(record, **change)
        with pytest.raises(QuantumValidationError) as exc:
            verify_native_result(expected, changed, evidence, receipt_key=SYNTHETIC_RECEIPT_KEY)
        assert exc.value.code == code, change
    for key in (None, b""):
        with pytest.raises(QuantumValidationError) as exc:
            verify_native_result(expected, record, evidence, receipt_key=key)
        assert exc.value.code == "SEAL"
    partial = copy.deepcopy(record)
    partial["observations"]["pubs"][0]["samples"].pop()
    failed_with_samples = forge(copy.deepcopy(record), status="FAILED")
    stale = {"fixture:records": {**read("evidence-bank"), "records": read("evidence-bank")["records"][::-1]}}
    wide = copy.deepcopy(record)
    wide["observations"]["pubs"][0]["samples"] = ["1010", "1010"]
    missing = copy.deepcopy(record)
    missing["observations"]["pubs"] = []
    for changed, catalog_value, code in ((partial, evidence, "WIDTH"), (failed_with_samples, evidence, "PARTIAL"),
                                         (record, stale, "DIGEST"), (wide, evidence, "WIDTH"),
                                         (missing, evidence, "PUB")):
        with pytest.raises(QuantumValidationError) as exc:
            verify_native_result(expected, forge(changed), catalog_value, receipt_key=SYNTHETIC_RECEIPT_KEY,
                                 authenticate=auth_bit_is_one)
        assert exc.value.code == code


@qiskit_test
def test_reordered_pubs_are_rejected_by_the_caller(tmp_path):
    value = build_plan([prepare_bits("10", operation_id="a"), prepare_bits("01", operation_id="b"),
                        *(sample(p, operation_id=f"s{p}", expected_width=2, shots=1, max_circuits=2, max_wall_s=5,
                                 max_qpu_s=0, pub_index=i) for i, p in enumerate("ab"))])
    body, expected = caller_side(value, "native-reorder")
    svc = QSearchService(service_config(tmp_path), clock=Clock())
    try:
        record = submit(svc, body)[1]
    finally:
        svc.close()
    assert record["observations"]["pubs"][0]["samples"] == ["01"] and verify_native_result(expected, record, {}, receipt_key=SYNTHETIC_RECEIPT_KEY)
    reordered = copy.deepcopy(record)
    reordered["observations"]["pubs"].reverse()
    with pytest.raises(QuantumValidationError) as exc:
        verify_native_result(expected, forge(reordered), {}, receipt_key=SYNTHETIC_RECEIPT_KEY)
    assert exc.value.code == "PUB"


_NO_QISKIT = r"""
import importlib.abc, json, sys
BLOCKED = ("qiskit", "qiskit_aer", "qiskit_ibm_runtime", "aether_atlas")
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked optional dependency: " + name)
sys.meta_path.insert(0, Block())
import nano, nano.quantum.native
from nano.quantum import QuantumValidationError, parse_plan
from nano_qsearch import service, native_service
from nano_qsearch.native_artifact import parse_native_artifact, prepare_native
from nano_qsearch.native_request import build_native_request
from nano_qsearch.native_result import retain_identity, verify_native_result
fixtures = sys.argv[1]
read = lambda name: json.load(open(f"{fixtures}/{name}.json", encoding="utf-8"))
artifact = parse_native_artifact(read("native-artifact-known-bits"))
body = build_native_request(artifact, request_id="native-known-bits")
expected = retain_identity(parse_plan(read("known-bits")), body)
result = verify_native_result(expected, read("native-result-known-bits"), {},
                              receipt_key=b"synthetic-test-key-native-receipt-0001")
try:
    prepare_native(artifact.plan, read("native-profile"), read("native-target"))
    raise SystemExit("prepare_native ran without qiskit")
except QuantumValidationError as exc:
    code = exc.code
loaded = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
print(json.dumps({"status": result.execution_status, "delivery": result.delivery_verdict,
                  "diagnostic": code, "loaded": loaded}))
"""


def test_core_checking_and_caller_verification_work_without_optional_dependencies():
    out = subprocess.run([sys.executable, "-c", _NO_QISKIT, str(ROOT / "nano" / "quantum" / "fixtures")],
                         capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout) == {"status": "OK", "delivery": "not_applicable",
                                      "diagnostic": "UNAVAILABLE_DEPENDENCY", "loaded": []}


def test_frozen_native_result_is_local_only_and_matches_its_schema():
    record = read("native-result-known-bits")
    assert record["hardware"] is None and record["backend"] == {"kind": "local_ideal", "dry_run": True,
                                                                "qiskit_version": "2.3.1"}
    assert record["seal"].startswith("hmac-sha256+native-local-v1:")
    assert record["accounting"] == {"status": "not_applicable", "provider_usage": None}
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "nano/quantum/schemas/native-result-v1.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(record)
