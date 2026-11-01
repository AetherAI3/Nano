"""Accounting promotion gate: a live v2 receipt is sealed only once provider usage is final.

A live (non-dry) request whose ATLAS result is DONE / FAILED but whose usage is still pending is
held ACCEPTED (503 ``ACCOUNTING_PENDING``; ``ACCOUNTING_TIMEOUT`` after the window), nothing is
resubmitted, and bounded retrieval drives recovery until the accounting deadline. Explicit
reconciliation can still obtain final usage afterwards; the OK or FAILED receipt is then sealed
once with ``usage_pending`` false. A receipt an older version sealed
while usage was pending is never served as final. Stand-in delegates only (no ATLAS, no IBM);
the Predator parity test runs when Predator's verifier is importable.
"""
import json
import secrets
import threading

import pytest

from nano_qsearch.auth import AUTH_HEADER, sign_request
from nano_qsearch.receipt_v2 import seal_receipt_v2, verify_seal_v2
from nano_qsearch.request import parse_request
from nano_qsearch.runtime_ibm import IbmRuntime, with_settled_usage
from nano_qsearch.service import REQUEST_PATH, QSearchService, ServiceConfig
from nano_qsearch.store import JobStore
from qsearch_v2_support import atlas_request_receipt, request_body

RECEIPT_KEY = b"receipt-key-" + b"0" * 20
REQUEST_KEY = b"request-key-" + b"1" * 20
LIVE_ENV = {"AETHER_AQRC_HW_GO": "1", "AETHER_AQRC_QSEARCH_LIVE": "1", "AETHER_AQRC_QSEARCH_PACKED": "1",
            "AETHER_AQRC_AUTHORITY_PATH": __file__}
PENDING = (503, "ACCOUNTING_PENDING")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _advance(svc, seconds=30):
    svc.test_clock.now += seconds


def _code(response):
    status, payload = response
    return status, payload.get("code")


class QSearchSubmittedPending(Exception):
    """Stands in for ATLAS's exception of the same name."""


class Provider:
    """What ATLAS returns: the submit result once, then one recovery result per retrieval (the last repeats)."""

    def __init__(self, submit, recoveries=()):
        self.submit_result, self.recoveries = submit, list(recoveries)
        self.submits = self.recovers = 0
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()

    def submit(self, request, compiled, **kw):
        self.submits += 1
        self.started.set()
        self.gate.wait()
        if isinstance(self.submit_result, Exception):
            raise self.submit_result
        return self.submit_result(request)

    def recover(self, request, **kw):
        self.recovers += 1
        if not self.recoveries:
            return None
        value = self.recoveries.pop(0) if len(self.recoveries) > 1 else self.recoveries[0]
        return value(request)


def _pending(request):
    return atlas_request_receipt(request, usage=0)


def _settled(seconds=2.0):
    return lambda request: atlas_request_receipt(request, usage=seconds)


def _failed(seconds):
    def build(request):
        receipt = atlas_request_receipt(request, usage=seconds)
        receipt.update(status="FAILED", pubs=[], provider_status="ERROR", reason="provider job ended ERROR")
        return receipt
    return build


def _service(tmp_path, provider, *, clock=None, **over):
    clock = clock or Clock()
    runtime = IbmRuntime(compile_fn=lambda job: None, environ=dict(LIVE_ENV), context_fn=None,
                         request_submit_fn=provider.submit, request_recover_fn=provider.recover,
                         request_observe_fn=lambda request, **kw: {
                             "provider_status": "DONE", "provider_job_id": "fake-job-1"})
    cfg = ServiceConfig(**{"bind": "127.0.0.1:0", "backend": "auto", "data_dir": tmp_path, "receipt_key": RECEIPT_KEY,
                           "request_key": REQUEST_KEY, "v2_enabled": True, **over})
    svc = QSearchService(cfg, runtime_factory=lambda kind: runtime, clock=clock)
    svc.test_clock = clock
    return svc


def _post(svc, body):
    raw = json.dumps(body, sort_keys=True).encode("utf-8")
    headers = {AUTH_HEADER: sign_request(REQUEST_KEY, "POST", REQUEST_PATH, raw, ts=1000, nonce=secrets.token_hex(16))}
    return svc.submit_request("POST", REQUEST_PATH, raw, headers, now=1000)


def _live():
    return request_body(3, target="ibm_fez", max_qpu_s=5, max_execution_time=10)


def _record(svc, body):
    return json.loads(svc.store.request_record(parse_request(body).request_digest) or "{}")


# --- pending -> final -------------------------------------------------------------------------------


def test_pending_usage_is_held_then_sealed_once_with_the_settled_usage(tmp_path):
    body = _live()
    provider = Provider(_pending, recoveries=[_pending, _settled(2.0)])
    svc = _service(tmp_path, provider)
    try:
        status, held = _post(svc, body)
        assert held == {"error": "job not completed", "code": "ACCOUNTING_PENDING", "job_id": "fake-job-1",
                        "result": "OK", "provider_status": "DONE", "terminal": False, "usage_pending": True}
        digest = parse_request(body).request_digest
        row = svc.store.get_request(digest)
        assert row.state == "ACCEPTED" and row.attempts == 1 and _record(svc, body)["accounting"] == "pending"
        assert _code(_post(svc, body)) == PENDING                                    # recovery: still not final
        _advance(svc)
        status, receipt = _post(svc, body)
        assert status == 200 and receipt["status"] == "OK" and len(receipt["blocks"]) == 3
        assert receipt["usage"]["quantum_seconds"] == 2.0 and receipt["usage"]["usage_pending"] is False
        verify_seal_v2(receipt, RECEIPT_KEY)
        record = _record(svc, body)
        assert record["accounting"] == "settled" and record["quantum_seconds"] == 2.0
        assert provider.submits == 1 and provider.recovers == 2
        # Repeated retrieval: the same sealed receipt, no further recovery, no resubmission.
        for _ in range(3):
            assert _post(svc, body) == (200, receipt)
        assert provider.submits == 1 and provider.recovers == 2
    finally:
        svc.close()


def test_settlement_survives_a_restart(tmp_path):
    body = _live()
    provider = Provider(_pending, recoveries=[_pending])
    clock = Clock()
    svc = _service(tmp_path, provider, clock=clock)
    assert _code(_post(svc, body)) == PENDING
    svc.close()
    svc = _service(tmp_path, provider, clock=clock)
    try:
        assert svc.recover() == []                                            # still pending after restart
        provider.recoveries = [_settled(1.5)]
        _advance(svc)
        assert svc.recover() == [parse_request(body).request_digest]
        status, receipt = _post(svc, body)
        assert status == 200 and receipt["usage"]["quantum_seconds"] == 1.5 and provider.submits == 1
    finally:
        svc.close()


def test_a_terminal_failure_with_late_usage_is_failed_with_the_settled_usage(tmp_path):
    body = _live()
    provider = Provider(QSearchSubmittedPending("req-hw0", "job-1"), recoveries=[_failed(0), _failed(0.7)])
    svc = _service(tmp_path, provider)
    try:
        assert _post(svc, body) == (503, {"error": "job not completed"})     # outcome not known yet
        status, held = _post(svc, body)                                       # FAILED, usage not final
        assert (status, held["code"], held["terminal"], held["provider_status"]) == \
            (503, "ACCOUNTING_PENDING_TERMINAL", True, "ERROR")
        _advance(svc)
        status, receipt = _post(svc, body)
        assert status == 200 and receipt["status"] == "FAILED" and receipt["blocks"] == []
        assert receipt["usage"]["quantum_seconds"] == 0.7 and receipt["usage"]["usage_pending"] is False
        verify_seal_v2(receipt, RECEIPT_KEY)
        assert provider.submits == 1
    finally:
        svc.close()


def test_concurrent_duplicates_of_a_pending_request_submit_once(tmp_path):
    body = _live()
    provider = Provider(_pending, recoveries=[_pending])
    provider.gate.clear()
    svc = _service(tmp_path, provider)
    try:
        results = []
        threads = [threading.Thread(target=lambda: results.append(_post(svc, body))) for _ in range(4)]
        for t in threads:
            t.start()
        assert provider.started.wait(timeout=3)
        provider.gate.set()
        for t in threads:
            t.join()
        assert provider.submits == 1 and len(results) == 4 and all(status == 503 for status, _ in results)
    finally:
        svc.close()


def test_the_window_stops_automatic_waiting_until_explicit_reconciliation(tmp_path):
    body = _live()
    provider = Provider(_pending, recoveries=[_pending])
    svc = _service(tmp_path, provider, accounting_window_s=1.0)
    try:
        assert _code(_post(svc, body)) == PENDING
        _advance(svc, 1.2)
        assert _code(_post(svc, body)) == (503, "ACCOUNTING_TIMEOUT")
        assert svc.store.get_request(parse_request(body).request_digest).state == "ACCEPTED"
        provider.recoveries = [_settled(3.0)]
        for _ in range(3):
            assert _code(_post(svc, body)) == (503, "ACCOUNTING_TIMEOUT")
        assert provider.recovers == 0
        path = "/qsearch/v2/requests/" + parse_request(body).request_digest + "/refresh"
        headers = {AUTH_HEADER: sign_request(REQUEST_KEY, "POST", path, b"", ts=1001,
                                             nonce=secrets.token_hex(16))}
        status, state = svc.control_request("POST", path, b"", headers, now=1001)
        assert status == 200 and state["accounting"]["status"] == "SETTLED"
        assert state["usage"]["settled_quantum_seconds"] == 3.0 and provider.recovers == 1
        status, receipt = _post(svc, body)                                    # settled receipt now replays offline
        assert status == 200 and receipt["usage"]["quantum_seconds"] == 3.0 and provider.submits == 1
    finally:
        svc.close()


def test_dry_runs_and_already_settled_results_are_sealed_at_once(tmp_path):
    provider = Provider(lambda request: atlas_request_receipt(request))
    svc = _service(tmp_path / "dry", provider)
    try:
        status, receipt = _post(svc, request_body(3))
        assert status == 200 and receipt["usage"]["usage_pending"] is True and receipt["backend"]["dry_run"] is True
    finally:
        svc.close()
    provider = Provider(_settled(2.0))
    svc = _service(tmp_path / "live", provider)
    try:
        status, receipt = _post(svc, _live())
        assert status == 200 and receipt["usage"]["quantum_seconds"] == 2.0 and provider.recovers == 0
    finally:
        svc.close()


# --- receipts an older version sealed while usage was pending -----------------------------------


def _seed_sealed_pending(tmp_path, body):
    request = parse_request(body)
    unsigned = IbmRuntime(environ=dict(LIVE_ENV))._request_body(request, _pending(request))   # what an older Nano sealed
    stale = seal_receipt_v2(unsigned, RECEIPT_KEY)
    assert stale["usage"]["usage_pending"] is True
    store = JobStore(tmp_path / "qsearch.sqlite3")
    store.begin_request(request.request_digest, request.request_id, json.dumps(body))
    store.mark_request_attempt(request.request_digest)
    store.complete_request(request.request_digest, json.dumps(stale, sort_keys=True))
    store.close()
    return stale


def test_a_receipt_sealed_pending_is_resealed_from_the_settled_row_and_versioned(tmp_path):
    body = _live()
    stale = _seed_sealed_pending(tmp_path, body)
    provider = Provider(_pending, recoveries=[_settled(2.0)])
    svc = _service(tmp_path, provider)
    try:
        status, receipt = _post(svc, body)
        assert status == 200 and receipt["usage"]["quantum_seconds"] == 2.0
        assert receipt["usage"]["usage_pending"] is False
        assert {k: v for k, v in receipt.items() if k not in ("usage", "seal")} == \
            {k: v for k, v in stale.items() if k not in ("usage", "seal")}                 # results unchanged
        verify_seal_v2(receipt, RECEIPT_KEY)
        [reseal] = _record(svc, body)["reseals"]
        assert reseal["previous_seal"] == stale["seal"] and reseal["previous_usage"]["usage_pending"] is True
        assert _post(svc, body) == (200, receipt) and provider.recovers == 1 and provider.submits == 0
    finally:
        svc.close()


def test_a_receipt_sealed_pending_is_never_served_while_unsettled_or_inconsistent(tmp_path):
    body = _live()
    stale = _seed_sealed_pending(tmp_path, body)
    digest = parse_request(body).request_digest
    provider = Provider(_pending, recoveries=[_pending])
    svc = _service(tmp_path, provider)
    try:
        assert _code(_post(svc, body)) == PENDING
        assert json.loads(svc.store.get_request(digest).receipt_json) == stale and provider.submits == 0

        def different(request):
            receipt = atlas_request_receipt(request, usage=2.0)
            receipt["pubs"][0]["raw_samples"] = ["0000"] * len(receipt["pubs"][0]["raw_samples"])
            return receipt
        provider.recoveries = [different]
        _advance(svc)
        assert _post(svc, body) == (503, {"error": "job not completed", "code": "RESEAL_MISMATCH"})
        assert json.loads(svc.store.get_request(digest).receipt_json) == stale
    finally:
        svc.close()


# --- the merge itself -------------------------------------------------------------------------------


def test_settled_usage_merges_only_for_the_same_job_and_a_positive_amount():
    request = parse_request(_live())
    pending = _pending(request)
    assert with_settled_usage(pending, {"job_id": "fake-job-1", "usage": 2.5})["provider_final_usage_seconds"] == 2.5
    for row in ({"job_id": "fake-job-1", "usage": None}, {"job_id": "fake-job-1", "usage": 0},
                {"job_id": "other-job", "usage": 2.5}, None):
        assert with_settled_usage(pending, row) == pending                      # zero / unknown / other job: pending
    settled = _settled(1.0)(request)
    assert with_settled_usage(settled, {"job_id": "fake-job-1", "usage": 9.9}) is settled


def test_atlas_settled_receipt_helper_is_used_and_its_extra_fields_stay_out_of_the_sealed_usage():
    from types import SimpleNamespace
    from nano_qsearch.runtime_ibm import SETTLED_RECEIPT_HELPER, settled_request_receipt
    request = parse_request(_live())
    pending = _pending(request)
    completed = {**_settled(2.0)(request), "usage_settled_at": "2026-11-01T00:00:00Z",
                 "provider_usage_at_receipt": 0}                      # ATLAS #184's completion fields
    ledger = SimpleNamespace(get=lambda request_id: {"job_id": "fake-job-1", "usage": 9.9})
    runtime = SimpleNamespace(**{SETTLED_RECEIPT_HELPER: lambda ledger, request_id: completed})
    merged = settled_request_receipt(runtime, ledger, "req-hw0", pending)
    assert merged is completed                                           # the helper wins over the row
    body = IbmRuntime(environ=dict(LIVE_ENV))._request_body(request, merged)
    assert set(body["usage"]) == {"quantum_seconds", "usage_pending", "provider_sub_jobs", "provider_metrics"}
    assert body["usage"]["quantum_seconds"] == 2.0 and body["usage"]["usage_pending"] is False
    assert "usage_settled_at" not in json.dumps(body)
    for helper_result in (None, {**completed, "job_id": "other-job"}, {"request_id": "req-hw0", "receipt": None}):
        runtime = SimpleNamespace(**{SETTLED_RECEIPT_HELPER: lambda ledger, request_id, r=helper_result: r})
        assert settled_request_receipt(runtime, ledger, "req-hw0", pending) is pending      # stays pending
    # An ATLAS pin without the helper: the row's settled usage (same job, positive) is the fallback.
    fallback = settled_request_receipt(SimpleNamespace(), ledger, "req-hw0", pending)
    assert fallback["provider_final_usage_seconds"] == 9.9 and fallback["usage_pending"] is False


# --- Predator parity ---------------------------------------------------------------------------------


def test_settled_and_resealed_receipts_verify_under_predator(tmp_path):
    receipt_v2 = pytest.importorskip("predator.drive.qsearch_receipt_v2")
    body = _live()
    expected = receipt_v2.ExpectedRequestV2.from_descriptor(body["descriptor"])
    svc = _service(tmp_path / "a", Provider(_pending, recoveries=[_settled(2.0)]))
    try:
        assert _code(_post(svc, body)) == PENDING
        status, receipt = _post(svc, body)
    finally:
        svc.close()
    verified = receipt_v2.verify_qsearch_receipt(receipt, key=RECEIPT_KEY, expected=expected)
    assert verified.status == "OK" and verified.usage["usage_pending"] is False
    assert verified.usage["quantum_seconds"] == 2.0
    (tmp_path / "b").mkdir()
    _seed_sealed_pending(tmp_path / "b", body)
    svc = _service(tmp_path / "b", Provider(_pending, recoveries=[_settled(2.0)]))
    try:
        status, resealed = _post(svc, body)
    finally:
        svc.close()
    verified = receipt_v2.verify_qsearch_receipt(resealed, key=RECEIPT_KEY, expected=expected)
    assert verified.usage["usage_pending"] is False and verified.usage["quantum_seconds"] == 2.0


# --- review #54: what a held request tells the client; settlement without live flags ------------


class _Submitted(Exception):
    """ATLAS's QSearchSubmittedPending shape: names the provider job."""

    def __init__(self, request_id, job_id):
        super().__init__(f"{request_id} submitted as {job_id}")
        self.request_id, self.job_id = request_id, job_id


_Submitted.__name__ = "QSearchSubmittedPending"


def test_a_cancelled_job_with_zero_usage_is_held_and_reported_as_terminal(tmp_path):
    """Reviewer's reproduction: cancelled, usage 0, four POSTs. Never sealed (never usage final at 0),
    but every 503 names the provider job, its terminal status and the reason, so the client can record it."""
    body = _live()
    provider = Provider(_Submitted("req-hw0", "fake-job-1"),
                        recoveries=[lambda r: {**_failed(0)(r), "provider_status": "CANCELLED"}])
    svc = _service(tmp_path, provider)
    try:
        assert _post(svc, body) == (503, {"error": "job not completed", "code": "SUBMITTED_PENDING",
                                          "job_id": "fake-job-1"})
        for _ in range(3):
            _advance(svc)
            assert _post(svc, body) == (503, {"error": "job not completed", "code": "ACCOUNTING_PENDING_TERMINAL",
                                              "job_id": "fake-job-1", "result": "FAILED",
                                              "provider_status": "CANCELLED", "terminal": True,
                                              "usage_pending": True, "reason": "provider: CANCELLED"})
        row = svc.store.get_request(parse_request(body).request_digest)
        assert row.state == "ACCEPTED" and row.receipt_json is None and provider.submits == 1
    finally:
        svc.close()


def test_a_settled_historical_receipt_is_resealed_from_the_ledger_without_live_flags(tmp_path):
    body = _live()
    stale = _seed_sealed_pending(tmp_path, body)
    request = parse_request(body)
    calls = []
    no_flags = {"AETHER_AQRC_AUTHORITY_PATH": __file__}                       # GO / LIVE absent
    settled_view = {"value": _settled(2.0)(request)}

    def settled_fn(req, **k):
        calls.append("ledger")
        return settled_view["value"]
    # ATLAS's own recovery (the default) is the production path: without GO / LIVE it must not be reached.
    runtime = IbmRuntime(compile_fn=lambda job: None, environ=no_flags, context_fn=None, request_settled_fn=settled_fn)
    cfg = ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path, receipt_key=RECEIPT_KEY,
                        request_key=REQUEST_KEY, v2_enabled=True)
    svc = QSearchService(cfg, runtime_factory=lambda kind: runtime)
    try:
        status, receipt = _post(svc, body)
        assert status == 200 and receipt["usage"]["quantum_seconds"] == 2.0 and calls == ["ledger"]
        record = _record(svc, body)
        assert receipt["blocks"] == stale["blocks"] and record["reseals"][0]["previous_seal"] == stale["seal"]
        assert "provider_metrics" in record["usage_note"]                      # 0 at completion vs settled 2.0
    finally:
        svc.close()
    (tmp_path / "unsettled").mkdir()
    _seed_sealed_pending(tmp_path / "unsettled", body)
    settled_view["value"] = _pending(request)                                  # the row is not settled yet
    cfg = ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path / "unsettled", receipt_key=RECEIPT_KEY,
                        request_key=REQUEST_KEY, v2_enabled=True)
    svc = QSearchService(cfg, runtime_factory=lambda kind: runtime)
    try:
        status, held = _post(svc, body)
        assert (status, held["code"], held["job_id"]) == (503, "ACCOUNTING_NEEDS_LIVE_CONTEXT", "fake-job-1")
        assert calls == ["ledger", "ledger"]                                  # ledger read; no provider path
    finally:
        svc.close()
