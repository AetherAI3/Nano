"""Opt-in native service adapter (#61 slice 3): existing auth/store/lifecycle, local or fake runtime only."""
import copy
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request

import pytest

from nano.quantum import build_plan, prepare_bits, sample
from nano.quantum.canonical import canonical_digest
from nano_qsearch.native_artifact import prepare_native
from nano_qsearch.native_policy import SEAL_PREFIX, parse_native_policy, verify_native_seal
from nano_qsearch.native_request import build_native_request, request_digest
from nano_qsearch.native_service import NATIVE_REQUEST_PREFIX, NATIVE_SUBMIT_PATH
from nano_qsearch.service import QSearchService, ServiceConfig, serve_in_thread
from native_bridge_support import (SYNTHETIC_RECEIPT_KEY, Clock, FakeRuntime, body_bytes, headers, ideal_observations,
                                   native_policy, plan, profile_payload, read, service_config, target_payload)

qiskit_test = pytest.mark.skipif(importlib.util.find_spec("qiskit") is None, reason="requires the pinned quantum extra")
JOB_STATE_KEYS = {"schema", "request", "lineage", "state", "provider", "execution", "accounting", "usage",
                  "cancellation", "deadlines", "next_action"}


def known_bits_request(request_id="native-known-bits"):
    return build_native_request(read("native-artifact-known-bits"), request_id=request_id)


def request_for(value, request_id="native-request"):
    return build_native_request(prepare_native(value, profile_payload(), target_payload()), request_id=request_id)


def two_pub_plan(shots=2, wall=5, width=2):
    bits = ("1" + "0" * (width - 1), "0" * (width - 1) + "1")
    return build_plan([prepare_bits(bits[0], operation_id="a"), prepare_bits(bits[1], operation_id="b"),
                       *(sample(p, operation_id=f"s{p}", expected_width=width, shots=shots, max_circuits=2,
                                max_wall_s=wall, max_qpu_s=0, pub_index=i) for i, p in enumerate("ab"))])


def make(tmp_path, runtime=None, clock=None, **config):
    runtime = runtime or FakeRuntime()
    svc = QSearchService(service_config(tmp_path, **config), clock=clock or Clock(),
                         native_runtime_factory=lambda: runtime)
    return svc, runtime


def submit(svc, body, *, now=1000, nonce=None, raw=None):
    raw = body_bytes(body) if raw is None else raw
    return svc.native.submit("POST", NATIVE_SUBMIT_PATH, raw, headers("POST", NATIVE_SUBMIT_PATH, raw, now=now,
                                                                      nonce=nonce), now=now)


def status(svc, digest, *, now=1000):
    path = NATIVE_REQUEST_PREFIX + digest
    return svc.native.status("GET", path, b"", headers("GET", path, now=now), now=now)


def cancel(svc, digest, *, now=1000):
    path = NATIVE_REQUEST_PREFIX + digest + "/cancel"
    return svc.native.cancel("POST", path, b"", headers("POST", path, now=now), now=now)


def test_native_routes_are_not_enabled_by_default_before_auth(tmp_path):
    config = service_config(tmp_path, native_policy=None)
    assert ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path, receipt_key=SYNTHETIC_RECEIPT_KEY,
                         request_key=b"another-synthetic-key-0001").native_policy is None
    svc = QSearchService(config, native_runtime_factory=lambda: pytest.fail("runtime constructed"))
    try:
        raw = body_bytes(known_bits_request())
        auth = headers("POST", NATIVE_SUBMIT_PATH, raw, nonce="ab" * 16)
        assert svc.native.submit("POST", NATIVE_SUBMIT_PATH, raw, auth) == (404, {"error": "not found",
                                                                                "code": "NOT_ENABLED"})
        digest = "sha256:" + "c" * 64
        assert status(svc, digest)[1]["code"] == "NOT_ENABLED" and cancel(svc, digest)[1]["code"] == "NOT_ENABLED"
        assert svc.store.remember("ab" * 16, 1000) is True        # no nonce was burned
        assert svc.store.pending_requests() == []
    finally:
        svc.close()


def test_env_opt_in_requires_an_explicit_policy_file(tmp_path, monkeypatch):
    keys = {name: tmp_path / name for name in ("receipt.key", "request.key")}
    keys["receipt.key"].write_bytes(SYNTHETIC_RECEIPT_KEY)
    keys["request.key"].write_bytes(b"synthetic-test-key-native-other-0001")
    for var, value in (("NANO_QSEARCH_DATA_DIR", str(tmp_path / "data")),
                       ("NANO_QSEARCH_KEY_PATH", str(keys["receipt.key"])),
                       ("NANO_QSEARCH_REQUEST_KEY_PATH", str(keys["request.key"]))):
        monkeypatch.setenv(var, value)
    monkeypatch.delenv("NANO_QSEARCH_NATIVE", raising=False)
    assert ServiceConfig.from_env().native_policy is None
    monkeypatch.setenv("NANO_QSEARCH_NATIVE", "1")
    monkeypatch.delenv("NANO_QSEARCH_NATIVE_POLICY", raising=False)
    with pytest.raises(ValueError):
        ServiceConfig.from_env()
    goldens = read("native-goldens")
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({"schema": "nano.qsearch.native-host-policy.v1",
                                       "admitted_profiles": [goldens["profile_digest"]],
                                       "admitted_targets": [goldens["target_digest"]], "max_pubs": 16,
                                       "max_total_shots": 100000, "max_qubits": 16, "max_deadline_s": 60,
                                       "evidence_banks": None}), encoding="utf-8")
    monkeypatch.setenv("NANO_QSEARCH_NATIVE_POLICY", str(policy_file))
    policy = ServiceConfig.from_env().native_policy
    assert policy.max_pubs == 16 and policy.admitted_targets == {goldens["target_digest"]}
    with pytest.raises(ValueError):
        parse_native_policy({**json.loads(policy_file.read_text()), "max_qubits": 21})
    with pytest.raises(ValueError):
        parse_native_policy({**json.loads(policy_file.read_text()), "max_pubs": 4097})


def test_fake_runtime_round_trip_status_and_replay_never_run_twice(tmp_path):
    svc, runtime = make(tmp_path)
    try:
        body = known_bits_request()
        code, record = submit(svc, body, nonce="11" * 16)
        assert code == 200 and record["status"] == "OK" and record["schema"] == "nano.quantum.native-result.v1"
        assert record["backend"] == {"kind": "fake_local", "dry_run": True, "qiskit_version": None}
        assert record["hardware"] is None and record["accounting"] == {"status": "not_applicable",
                                                                       "provider_usage": None}
        assert record["decoding"]["delivery_verdict"] == "unverified" and record["source_verification"] == "admitted"
        assert record["seal"].startswith(SEAL_PREFIX) and "hardware" not in record["seal"]
        verify_native_seal(record, SYNTHETIC_RECEIPT_KEY)
        assert record["identity"]["artifact_digest"] == read("native-goldens")["plans"]["known-bits"]["artifact_digest"]
        digest = request_digest(body)
        code, state = status(svc, digest)
        assert code == 200 and set(state) == JOB_STATE_KEYS and state["schema"] == "nano.qsearch.job-state.v1"
        assert state["execution"] == {"status": "SUCCEEDED", "outcome": "SUCCEEDED", "terminal": True}
        assert state["accounting"]["status"] == "NOT_APPLICABLE"
        assert state["usage"] == {"reservation_status": "NOT_RESERVED", "held_quantum_seconds": None,
                                  "settled_quantum_seconds": None}
        assert state["request"]["target"] == "local_ideal"
        assert submit(svc, body) == (200, record)
        assert submit(svc, body, nonce="11" * 16)[0] == 401          # repeated nonce
        assert runtime.runs == 1 and svc.store.get_request(digest).attempts == 1
    finally:
        svc.close()


def test_wrong_authentication_stores_nothing(tmp_path):
    svc, runtime = make(tmp_path)
    try:
        raw = body_bytes(known_bits_request())
        for auth in ({}, headers("POST", NATIVE_SUBMIT_PATH, raw, key=b"synthetic-test-key-wrong-0000001"),
                     headers("POST", "/qsearch/v2/submit_request", raw), headers("POST", NATIVE_SUBMIT_PATH, b"{}"),
                     headers("POST", NATIVE_SUBMIT_PATH, raw, now=1000 - 500)):
            code, error = svc.native.submit("POST", NATIVE_SUBMIT_PATH, raw, auth, now=1000)
            assert code == 401 and error["code"] == "UNAUTHORIZED"
        assert svc.store.pending_requests() == [] and runtime.preflights == runtime.runs == 0
    finally:
        svc.close()


@qiskit_test
@pytest.mark.parametrize("field,cap", [("pub_count", "max_pubs"), ("total_shots", "max_total_shots"),
                                       ("max_width", "max_qubits"), ("deadline_s", "max_deadline_s")])
def test_resource_boundary_and_boundary_plus_one(tmp_path, field, cap):
    body = request_for(two_pub_plan(shots=3, wall=7, width=3))
    exact = body["descriptor"]["resources"][field]
    over, runtime = make(tmp_path / "over", native_policy=native_policy(**{cap: exact - 1}))
    try:
        code, error = submit(over, body)
        assert (code, error["code"], error["field"]) == (400, "RESOURCE_LIMIT", f"descriptor.resources.{field}")
        garbage = copy.deepcopy(body)
        garbage["artifact"] = {"not": "an artifact"}
        assert submit(over, garbage)[1]["code"] == "RESOURCE_LIMIT"       # refused before artifact parsing
        assert over.store.pending_requests() == [] and runtime.preflights == 0
    finally:
        over.close()
    at, runtime = make(tmp_path / "at", native_policy=native_policy(**{cap: exact}))
    try:
        assert submit(at, body)[0] == 200 and runtime.runs == 1
    finally:
        at.close()


def test_unadmitted_profile_or_target_is_refused_before_artifact_parsing(tmp_path):
    body = known_bits_request()
    body["artifact"] = {"not": "parsed"}
    other = "sha256:" + "d" * 64
    for policy, field in ((native_policy(admitted_profiles={other}), "descriptor.profile_digest"),
                          (native_policy(admitted_targets={other}), "descriptor.target_digest")):
        svc, runtime = make(tmp_path / field, native_policy=policy)
        try:
            code, error = submit(svc, body)
            assert (code, error["code"], error["field"]) == (403, "NOT_ADMITTED", field)
            assert svc.store.pending_requests() == [] and runtime.preflights == 0
        finally:
            svc.close()


def test_substituted_artifact_lying_descriptor_and_unsupported_documents_fail(tmp_path):
    svc, runtime = make(tmp_path)
    try:
        body = known_bits_request()
        substituted = copy.deepcopy(body)
        substituted["descriptor"]["artifact_digest"] = "sha256:" + "e" * 64
        assert submit(svc, substituted)[1]["code"] == "IDENTITY"
        lying = copy.deepcopy(body)
        lying["descriptor"]["resources"]["total_shots"] = 1
        assert submit(svc, lying)[1]["code"] == "IDENTITY"
        assert submit(svc, {**body, "schema": "nano.quantum.native-request.v2"})[1]["code"] == "UNSUPPORTED_VERSION"
        optimize = copy.deepcopy(body)
        optimize["artifact"]["plan"] = read("optimize-qubo")
        sealed = {k: v for k, v in optimize["artifact"].items() if k != "artifact_digest"}
        optimize["artifact"]["artifact_digest"] = optimize["descriptor"]["artifact_digest"] = canonical_digest(sealed)
        assert submit(svc, optimize)[1]["code"] == "UNSUPPORTED_OPERATION"
        raw = body_bytes(body).replace(b'{"artifact":', b'{"schema":"x","artifact":', 1)
        assert submit(svc, None, raw=raw)[1]["code"] == "JSON"            # duplicate key
        assert svc.store.pending_requests() == [] and runtime.runs == 0
    finally:
        svc.close()


@qiskit_test
def test_stale_evidence_bank_is_refused_by_the_host_registry(tmp_path):
    body = request_for(plan("one-record"))
    reference = "fixture:records"
    stale, runtime = make(tmp_path / "stale", native_policy=native_policy(evidence_banks={reference: "sha256:" + "0" * 64}))
    try:
        code, error = submit(stale, body)
        assert (code, error["code"]) == (409, "STALE_EVIDENCE")
        assert runtime.preflights == 0
    finally:
        stale.close()
    current = canonical_digest(read("evidence-bank"))
    fresh, runtime = make(tmp_path / "fresh", native_policy=native_policy(evidence_banks={reference: current}))
    try:
        assert submit(fresh, body)[0] == 200
    finally:
        fresh.close()


@qiskit_test
def test_preparation_only_artifact_is_never_executed(tmp_path):
    svc, runtime = make(tmp_path)
    try:
        code, error = submit(svc, request_for(build_plan([prepare_bits("101")])))
        assert (code, error["code"]) == (400, "PREPARATION_ONLY")
        assert svc.store.pending_requests() == [] and runtime.runs == 0
    finally:
        svc.close()


@qiskit_test
def test_local_runtime_preflight_refuses_corrupt_metadata_wrong_or_missing_sdk(tmp_path, monkeypatch):
    import base64
    import hashlib
    import qiskit
    from nano_qsearch.native_artifact import build_circuit, qpy_bytes
    svc = QSearchService(service_config(tmp_path), clock=Clock())
    try:
        good = request_for(plan("known-bits"))
        corrupt = copy.deepcopy(good)
        artifact = corrupt["artifact"]
        identity = {"plan_digest": artifact["plan_digest"], "profile_digest": artifact["profile_digest"],
                    "target_digest": artifact["target_digest"], "operation_id": "sample", "preparation_id": "prepare",
                    "pub_index": 9}
        binary = qpy_bytes(build_circuit(qiskit, plan("known-bits").operations[0], "meas", identity))
        artifact["pubs"][0].update(qpy_base64=base64.b64encode(binary).decode(),
                                   isa_digest="sha256:" + hashlib.sha256(binary).hexdigest())
        artifact["artifact_digest"] = canonical_digest({k: v for k, v in artifact.items() if k != "artifact_digest"})
        corrupt["descriptor"]["artifact_digest"] = artifact["artifact_digest"]
        assert submit(svc, corrupt)[1]["code"] == "METADATA"
        monkeypatch.setattr(qiskit, "__version__", "2.3.0")
        assert submit(svc, good)[1]["code"] == "UNSUPPORTED_SDK"
        monkeypatch.setitem(sys.modules, "qiskit", None)
        code, error = submit(svc, good)
        assert (code, error["code"]) == (501, "UNAVAILABLE_DEPENDENCY")
        assert svc.store.pending_requests() == []
    finally:
        svc.close()


def test_hardware_or_unknown_runtime_kind_is_refused(tmp_path):
    class Hardware(FakeRuntime):
        kind = "ibm"
    svc, runtime = make(tmp_path, runtime=Hardware())
    try:
        assert submit(svc, known_bits_request())[1]["code"] == "UNSUPPORTED_RUNTIME"
        assert runtime.runs == 0 and svc.store.pending_requests() == []
    finally:
        svc.close()


@pytest.mark.parametrize("mode,reason", [("partial", "result differs from the admitted PUB inventory"),
                                         ("claims_pass", "result differs from the admitted PUB inventory"),
                                         ("raise", "local runtime: RuntimeError")])
def test_partial_malformed_or_failed_local_runs_are_sealed_failed_without_observations(tmp_path, mode, reason):
    svc, runtime = make(tmp_path, runtime=FakeRuntime(mode))
    try:
        body = known_bits_request()
        code, record = submit(svc, body)
        assert code == 200 and record["status"] == "FAILED" and record["reason"] == reason
        assert record["observations"] is None and record["hardware"] is None
        state = status(svc, request_digest(body))[1]
        assert state["execution"]["status"] == "FAILED" and state["accounting"]["status"] == "NOT_APPLICABLE"
        assert submit(svc, body)[1] == record and runtime.runs == 1
    finally:
        svc.close()


def test_lost_acknowledgement_stays_held_and_restart_reattaches_without_rerun(tmp_path):
    runtime = FakeRuntime("unknown")
    svc, _ = make(tmp_path, runtime=runtime)
    body = known_bits_request()
    digest = request_digest(body)
    try:
        code, error = submit(svc, body)
        assert (code, error["code"]) == (503, "OUTCOME_UNKNOWN")
        state = status(svc, digest)[1]
        assert state["execution"]["status"] == "SUBMITTED_UNKNOWN" and state["accounting"]["status"] == "PENDING"
        assert state["usage"]["reservation_status"] == "HELD" and state["usage"]["held_quantum_seconds"] is None
        assert state["usage"]["settled_quantum_seconds"] is None
        assert submit(svc, body)[1]["code"] == "OUTCOME_UNKNOWN"
        assert (runtime.runs, runtime.recovers) == (1, 1)
    finally:
        svc.close()
    runtime.recovered = ideal_observations
    svc, _ = make(tmp_path, runtime=runtime)
    try:
        assert svc.recover() == [digest]
        assert status(svc, digest)[1]["execution"]["status"] == "SUCCEEDED"
        assert submit(svc, body)[1]["status"] == "OK" and runtime.runs == 1
    finally:
        svc.close()


def accepted_unclaimed(svc, body):
    """An ACCEPTED row whose single attempt was never claimed (e.g. the process died before the claim)."""
    svc.store.begin_request(request_digest(body), body["descriptor"]["request_id"], body_bytes(body).decode())


def test_preflight_holds_a_concurrency_slot_and_busy_refusal_stores_nothing(tmp_path):
    svc, runtime = make(tmp_path, max_concurrent=1, poll_interval_s=0.01)
    try:
        assert svc._slots.acquire(timeout=1)
        code, error = submit(svc, known_bits_request())
        assert (code, error["code"]) == (503, "SERVICE_BUSY")
        assert svc.store.pending_requests() == [] and runtime.preflights == runtime.runs == 0
        svc._slots.release()
        assert submit(svc, known_bits_request())[0] == 200 and runtime.runs == 1
    finally:
        svc.close()


def test_restart_never_starts_an_unclaimed_row_and_resubmission_runs_once(tmp_path):
    runtime = FakeRuntime()
    svc, _ = make(tmp_path, runtime=runtime, max_concurrent=1, poll_interval_s=0.01)
    body = known_bits_request()
    try:
        accepted_unclaimed(svc, body)
        assert svc.store.get_request(request_digest(body)).attempts == 0
    finally:
        svc.close()
    svc, _ = make(tmp_path, runtime=runtime, max_concurrent=1, poll_interval_s=0.01)
    try:
        assert svc.recover() == [] and runtime.runs == 0
        assert submit(svc, body)[0] == 200 and runtime.runs == 1
    finally:
        svc.close()


def test_cancel_before_claim_seals_not_run_and_never_executes(tmp_path):
    svc, runtime = make(tmp_path, max_concurrent=1, poll_interval_s=0.01)
    body = known_bits_request()
    digest = request_digest(body)
    try:
        accepted_unclaimed(svc, body)
        code, state = cancel(svc, digest)
        assert code == 200 and state["execution"]["status"] == "NOT_RUN" and state["cancellation"]["requested"]
        assert state["accounting"]["status"] == "NOT_APPLICABLE"
        code, record = submit(svc, body)
        assert code == 200 and record["status"] == "NOT_RUN" and record["effect"] == "none" and runtime.runs == 0
    finally:
        svc.close()


def test_cancel_racing_a_running_execution_keeps_fence_and_real_result(tmp_path):
    runtime = FakeRuntime("block")
    svc, _ = make(tmp_path, runtime=runtime)
    body = known_bits_request()
    digest = request_digest(body)
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.setdefault("submit", submit(svc, body)))
    try:
        worker.start()
        assert runtime.started.wait(5)
        code, error = cancel(svc, digest)
        assert (code, error["code"]) == (409, "REQUEST_BUSY")
        assert "cancellation_requested_at" in json.loads(svc.store.request_record(digest))
        runtime.release.set()
        worker.join(10)
        assert outcome["submit"][0] == 200 and outcome["submit"][1]["status"] == "OK"
        state = status(svc, digest)[1]
        assert state["execution"]["status"] == "SUCCEEDED"
        assert state["cancellation"]["requested"] is True and state["cancellation"]["confirmed"] is False
        before = svc.store.get_request(digest)
        assert cancel(svc, digest)[1]["execution"]["status"] == "SUCCEEDED"     # cancel after done: no change
        assert svc.store.get_request(digest).receipt_json == before.receipt_json and runtime.runs == 1
    finally:
        runtime.release.set()
        worker.join(10)
        svc.close()


def test_queue_deadline_on_fake_clock_prevents_late_execution(tmp_path):
    clock = Clock()
    svc, runtime = make(tmp_path, clock=clock, max_concurrent=1, poll_interval_s=0.01, queue_deadline_s=60)
    body = known_bits_request()
    digest = request_digest(body)
    try:
        accepted_unclaimed(svc, body)
        clock.now += 60
        assert submit(svc, body, now=int(clock.now))[1]["code"] == "NOT_STARTED"
        state = status(svc, digest, now=int(clock.now))[1]
        assert state["state"] == "QUEUE_EXPIRED" and state["next_action"] == "REQUEST_CANCEL" and runtime.runs == 0
    finally:
        svc.close()


def test_status_is_read_only_and_never_constructs_a_runtime(tmp_path):
    svc = QSearchService(service_config(tmp_path), clock=Clock(),
                         native_runtime_factory=lambda: pytest.fail("status constructed a runtime"))
    try:
        body = known_bits_request()
        digest = request_digest(body)
        svc.store.begin_request(digest, "native-known-bits", json.dumps(body))
        before = (svc.store.get_request(digest), svc.store.request_record(digest))
        for _ in range(3):
            code, state = status(svc, digest)
            assert code == 200 and state["execution"]["status"] == "ACCEPTED"
        assert (svc.store.get_request(digest), svc.store.request_record(digest)) == before
        assert status(svc, "sha256:" + "f" * 64)[1]["code"] == "REQUEST_NOT_FOUND"
    finally:
        svc.close()


def test_v2_and_native_routes_do_not_serve_each_others_rows_and_ids_stay_unique(tmp_path):
    from nano_qsearch.request import parse_request
    from qsearch_v2_support import request_body
    svc, runtime = make(tmp_path, v2_enabled=True)
    try:
        v2_body = request_body(2, target="ibm_fez")
        v2 = parse_request(v2_body)
        svc.store.begin_request(v2.request_digest, v2.request_id, json.dumps(v2_body))
        assert status(svc, v2.request_digest)[1]["code"] == "REQUEST_NOT_FOUND"
        clash = known_bits_request(request_id=v2.request_id)
        code, error = submit(svc, clash)
        assert (code, error["code"]) == (409, "REQUEST_ID_CONFLICT") and runtime.runs == 0
        body = known_bits_request()
        assert submit(svc, body)[0] == 200
        path = "/qsearch/v2/requests/" + request_digest(body)
        code, error = svc.status_request("GET", path, b"", headers("GET", path), now=1000)
        assert (code, error["code"]) == (404, "REQUEST_NOT_FOUND")
        control = path + "/cancel"
        code, error = svc.control_request("POST", control, b"", headers("POST", control), now=1000)
        assert (code, error["code"]) == (404, "REQUEST_NOT_FOUND")
    finally:
        svc.close()


@qiskit_test
def test_http_routes_with_the_actual_local_runtime(tmp_path):
    svc = QSearchService(service_config(tmp_path), clock=Clock())
    disabled = QSearchService(service_config(tmp_path / "off", native_policy=None))
    servers = [serve_in_thread(svc), serve_in_thread(disabled)]
    try:
        body = request_for(plan("one-record"), request_id="native-http")
        raw = body_bytes(body)
        url = f"http://127.0.0.1:{servers[0][0].server_port}"
        req = urllib.request.Request(url + NATIVE_SUBMIT_PATH, data=raw, method="POST",
                                     headers=headers("POST", NATIVE_SUBMIT_PATH, raw))
        with urllib.request.urlopen(req, timeout=60) as response:
            record = json.loads(response.read())
        assert record["status"] == "OK" and record["backend"]["kind"] == "local_ideal"
        assert record["observations"]["pubs"][0]["samples"] == ["101", "101"]
        path = NATIVE_REQUEST_PREFIX + request_digest(body)
        with urllib.request.urlopen(urllib.request.Request(url + path, headers=headers("GET", path)), timeout=10) as r:
            assert json.loads(r.read())["execution"]["status"] == "SUCCEEDED"
        off = f"http://127.0.0.1:{servers[1][0].server_port}{NATIVE_SUBMIT_PATH}"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(urllib.request.Request(off, data=raw, method="POST"), timeout=10)
        assert exc.value.code == 404 and json.loads(exc.value.read())["code"] == "NOT_ENABLED"
    finally:
        for server, thread in servers:
            server.shutdown()
            server.server_close()
            thread.join()
        svc.close()
        disabled.close()
