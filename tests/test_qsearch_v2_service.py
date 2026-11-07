"""Lane HW0 N4: ``POST /qsearch/v2/submit_request`` behind ``NANO_QSEARCH_V2``, durable and recoverable.

The IBM runtime is real (``IbmRuntime``) with an injected request delegate that
counts submissions, so these tests prove the service submits each request at
most once across repeats and restarts, without ATLAS, Aer or IBM.
"""
import json
import secrets
import threading
import time
import urllib.error
import urllib.request

import pytest

from nano_qsearch.auth import AUTH_HEADER, sign_request
from nano_qsearch.receipt_v2 import HARDWARE_SEAL_PREFIX_V2, SEAL_PREFIX_V2, verify_seal_v2
from nano_qsearch.request import parse_request
from nano_qsearch.runtime_ibm import IbmRuntime
from nano_qsearch.service import (
    NOT_ENABLED, REQUEST_PATH, SUBMIT_PATH, QSearchService, ServiceConfig, serve_in_thread,
)
from nano_qsearch.store import JobStore
from qsearch_v2_support import atlas_request_receipt, request_body

RECEIPT_KEY = b"receipt-key-" + b"0" * 20
REQUEST_KEY = b"request-key-" + b"1" * 20


class Delegate:
    """Counts ATLAS request submissions; returns a matching request receipt (or raises ``fail``)."""

    def __init__(self, fail=None):
        self.submits, self.recovers, self.fail = 0, 0, fail
        self.recovered = None
        self.gate = threading.Event()
        self.gate.set()

    def submit(self, request, compiled, **kw):
        self.submits += 1
        self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return atlas_request_receipt(request)

    def recover(self, request, **kw):
        self.recovers += 1
        return self.recovered


def _config(tmp_path, **over):
    base = dict(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path, receipt_key=RECEIPT_KEY,
                request_key=REQUEST_KEY, v2_enabled=True)
    return ServiceConfig(**{**base, **over})


def _service(tmp_path, delegate, *, clock=None, **over):
    runtime = IbmRuntime(compile_fn=lambda job: None, request_submit_fn=delegate.submit,
                         request_recover_fn=delegate.recover, environ={})
    options = {} if clock is None else {"clock": clock}
    return QSearchService(_config(tmp_path, **over), runtime_factory=lambda kind: runtime, **options)


def _bytes(body):
    return json.dumps(body, sort_keys=True).encode("utf-8")


def _post(svc, raw, *, ts=1000, nonce=None, path=REQUEST_PATH):
    nonce = nonce or secrets.token_hex(16)
    headers = {AUTH_HEADER: sign_request(REQUEST_KEY, "POST", path, raw, ts=ts, nonce=nonce)}
    return svc.submit_request("POST", path, raw, headers, now=ts)


@pytest.fixture
def served(tmp_path):
    delegate = Delegate()
    svc = _service(tmp_path, delegate)
    yield svc, delegate
    svc.close()


def test_disabled_by_default_and_answers_404_not_enabled_before_auth(tmp_path):
    delegate = Delegate()
    svc = _service(tmp_path, delegate, v2_enabled=False)
    try:
        raw = _bytes(request_body(3))
        status, payload = _post(svc, raw, nonce="ab" * 16)
        assert (status, payload) == (404, NOT_ENABLED) and payload["code"] == "NOT_ENABLED"
        assert svc.store.remember("ab" * 16, 1000) is True              # the nonce was never burned
        assert delegate.submits == 0 and svc.store.pending_requests() == []
    finally:
        svc.close()
    assert ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path, receipt_key=RECEIPT_KEY,
                         request_key=REQUEST_KEY).v2_enabled is False


def test_from_env_reads_the_flag(tmp_path, monkeypatch):
    (tmp_path / "k1").write_bytes(RECEIPT_KEY)
    (tmp_path / "k2").write_bytes(REQUEST_KEY)
    monkeypatch.setenv("NANO_QSEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NANO_QSEARCH_KEY_PATH", str(tmp_path / "k1"))
    monkeypatch.setenv("NANO_QSEARCH_REQUEST_KEY_PATH", str(tmp_path / "k2"))
    monkeypatch.delenv("NANO_QSEARCH_V2", raising=False)
    assert ServiceConfig.from_env().v2_enabled is False
    for value in ("0", "true", " 1"):
        monkeypatch.setenv("NANO_QSEARCH_V2", value)
        assert ServiceConfig.from_env().v2_enabled is False
    monkeypatch.setenv("NANO_QSEARCH_V2", "1")
    assert ServiceConfig.from_env().v2_enabled is True


def test_round_trip_seals_a_v2_receipt_and_a_repeat_returns_it_without_resubmitting(served):
    svc, delegate = served
    body = request_body(3)
    raw = _bytes(body)
    status, receipt = _post(svc, raw)
    assert status == 200 and receipt["status"] == "OK" and receipt["seal"].startswith(HARDWARE_SEAL_PREFIX_V2)
    verify_seal_v2(receipt, RECEIPT_KEY)
    assert [b["job_digest"] for b in receipt["blocks"]] == [j["job_digest"] for j in body["jobs"]]
    assert receipt["request"]["request_digest"] == parse_request(body).request_digest
    row = svc.store.get_request(receipt["request"]["request_digest"])
    assert row.state == "DONE" and row.attempts == 1 and row.request_id == "req-hw0"
    again = _post(svc, raw)
    assert again == (200, receipt) and delegate.submits == 1


def test_auth_and_validation_failures_store_nothing(served):
    svc, delegate = served
    raw = _bytes(request_body(2))
    assert svc.submit_request("POST", REQUEST_PATH, raw, {}, now=1000)[0] == 401
    v1_signed = {AUTH_HEADER: sign_request(REQUEST_KEY, "POST", SUBMIT_PATH, raw, ts=1000, nonce="cd" * 16)}
    assert svc.submit_request("POST", REQUEST_PATH, raw, v1_signed, now=1000)[0] == 401    # path is in the MAC
    bad = request_body(2)
    bad["jobs"].reverse()
    assert _post(svc, _bytes(bad)) == (400, {"error": "invalid request", "code": "MISMATCH"})
    assert _post(svc, b"{not json") == (400, {"error": "invalid request", "code": "JSON"})
    assert delegate.submits == 0 and svc.store.pending_requests() == []


def test_a_v1_job_body_on_the_v2_path_and_a_v2_body_on_the_v1_path_are_refused(served, fixtures_dir):
    svc, delegate = served
    job = (fixtures_dir / "job_4var.json").read_bytes()
    assert _post(svc, job) == (400, {"error": "invalid request", "code": "SCHEMA"})
    raw = _bytes(request_body(2))
    headers = {AUTH_HEADER: sign_request(REQUEST_KEY, "POST", SUBMIT_PATH, raw, ts=1000, nonce="ef" * 16)}
    status, payload = svc.submit("POST", SUBMIT_PATH, raw, headers, now=1000)
    assert status == 400 and payload["code"] == "SCHEMA" and delegate.submits == 0


def test_request_id_reuse_with_another_digest_is_a_conflict(served):
    svc, delegate = served
    assert _post(svc, _bytes(request_body(2)))[0] == 200
    assert _post(svc, _bytes(request_body(3)))[0] == 409 and delegate.submits == 1


def test_resource_failure_is_a_stored_not_run_with_no_blocks(tmp_path):
    delegate = Delegate(fail=MemoryError())
    svc = _service(tmp_path, delegate)
    try:
        status, receipt = _post(svc, _bytes(request_body(3)))
        assert status == 200 and receipt["status"] == "NOT_RUN" and receipt["blocks"] == []
        assert receipt["reason"] == "ibm: MemoryError" and receipt["seal"].startswith(SEAL_PREFIX_V2)
        assert receipt["request"]["block_count"] == 0
        verify_seal_v2(receipt, RECEIPT_KEY)
        assert _post(svc, _bytes(request_body(3)))[1] == receipt and delegate.submits == 1
    finally:
        svc.close()


def test_service_caps_refuse_before_any_submission(tmp_path):
    delegate = Delegate()
    svc = _service(tmp_path, delegate, max_shots=4)
    try:
        status, receipt = _post(svc, _bytes(request_body(2, shots=8)))
        assert status == 200 and receipt["status"] == "NOT_RUN" and receipt["reason"].startswith("refused: limits:")
        assert delegate.submits == 0 and svc.store.get_request(receipt["request"]["request_digest"]).attempts == 0
    finally:
        svc.close()


def test_concurrent_identical_requests_submit_once(served):
    svc, delegate = served
    raw = _bytes(request_body(3))
    delegate.gate.clear()
    results = []
    threads = [threading.Thread(target=lambda: results.append(_post(svc, raw))) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    delegate.gate.set()
    for t in threads:
        t.join()
    assert delegate.submits == 1 and len({json.dumps(r, sort_keys=True) for r in results}) == 1


# --- durable store and restart recovery ---------------------------------------------------------


def _seed_pending(tmp_path, body, attempts):
    """A previous process accepted ``body`` and (attempts=1) handed it to ATLAS, then died."""
    request = parse_request(body)
    store = JobStore(tmp_path / "qsearch.sqlite3")
    store.begin_request(request.request_digest, request.request_id, json.dumps(body))
    for _ in range(attempts):
        store.mark_request_attempt(request.request_digest)
    store.close()
    return request


def test_restart_reattaches_a_submitted_request_and_never_resubmits(tmp_path):
    from test_qsearch_lifecycle_service import Clock

    body = request_body(3, target="ibm_fez")
    request = _seed_pending(tmp_path, body, attempts=1)
    delegate = Delegate()
    clock = Clock()
    svc = _service(tmp_path, delegate, clock=clock)
    try:
        assert svc.recover() == []                                           # provider job not done yet
        assert svc.store.get_request(request.request_digest).state == "ACCEPTED"
        assert _post(svc, _bytes(body)) == (503, {"error": "job not completed"})
        assert delegate.recovers == 1                                       # same instant is throttled
        delegate.recovered = atlas_request_receipt(request, usage=4.0)
        clock.now += svc.config.poll_interval_s
        assert svc.recover() == [request.request_digest]
        status, receipt = _post(svc, _bytes(body))
        assert status == 200 and receipt["status"] == "OK" and receipt["usage"]["quantum_seconds"] == 4.0
        verify_seal_v2(receipt, RECEIPT_KEY)
        assert delegate.submits == 0 and delegate.recovers == 2
    finally:
        svc.close()


def test_restart_completes_a_never_submitted_fake_fez_request_as_not_run(tmp_path):
    body = request_body(2)
    request = _seed_pending(tmp_path, body, attempts=0)
    delegate = Delegate()
    svc = _service(tmp_path, delegate)
    try:
        assert svc.recover() == [request.request_digest]
        status, receipt = _post(svc, _bytes(body))
        assert receipt["status"] == "NOT_RUN" and receipt["reason"].startswith("recovery:") and delegate.submits == 0
    finally:
        svc.close()


def test_receipt_survives_a_restart_byte_for_byte(tmp_path):
    body = request_body(3)
    delegate = Delegate()
    svc = _service(tmp_path, delegate)
    status, first = _post(svc, _bytes(body))
    svc.close()
    svc = _service(tmp_path, delegate)
    try:
        assert svc.recover() == []
        assert _post(svc, _bytes(body)) == (200, first) and delegate.submits == 1
    finally:
        svc.close()


# --- HTTP ----------------------------------------------------------------------------------------


def _http(port, path, raw):
    headers = {"Content-Type": "application/json",
               AUTH_HEADER: sign_request(REQUEST_KEY, "POST", path, raw, ts=int(time.time()),
                                         nonce=secrets.token_hex(16))}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=raw, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


@pytest.mark.parametrize("enabled", [False, True])
def test_http_route(tmp_path, enabled):
    delegate = Delegate()
    svc = _service(tmp_path, delegate, v2_enabled=enabled)
    server, thread = serve_in_thread(svc)
    try:
        raw = _bytes(request_body(3))
        status, payload = _http(server.server_port, REQUEST_PATH, raw)
        if enabled:
            assert status == 200 and payload["status"] == "OK" and len(payload["blocks"]) == 3
        else:
            assert (status, payload) == (404, NOT_ENABLED) and delegate.submits == 0
        assert _http(server.server_port, REQUEST_PATH + "/", raw)[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        svc.close()
