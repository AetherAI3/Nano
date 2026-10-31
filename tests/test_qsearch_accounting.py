"""Accounting promotion gate: a live v2 receipt is sealed only once provider usage is final.

A live (non-dry) request whose ATLAS result is DONE / FAILED but whose usage is still pending is
held ACCEPTED (503 ``ACCOUNTING_PENDING``; ``ACCOUNTING_TIMEOUT`` after the window), nothing is
resubmitted, and every retrieval drives recovery until the settled usage arrives; then the OK or
FAILED receipt is sealed once with ``usage_pending`` false. A receipt an older version sealed
while usage was pending is never served as final. Stand-in delegates only (no ATLAS, no IBM);
the Predator parity test runs when Predator's verifier is importable.
"""
import json
import secrets
import threading
import time

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
PENDING = (503, {"error": "job not completed", "code": "ACCOUNTING_PENDING"})


class QSearchSubmittedPending(Exception):
    """Stands in for ATLAS's exception of the same name."""


class Provider:
    """What ATLAS returns: the submit result once, then one recovery result per retrieval (the last repeats)."""

    def __init__(self, submit, recoveries=()):
        self.submit_result, self.recoveries = submit, list(recoveries)
        self.submits = self.recovers = 0
        self.gate = threading.Event()
        self.gate.set()

    def submit(self, request, compiled, **kw):
        self.submits += 1
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


def _service(tmp_path, provider, **over):
    runtime = IbmRuntime(compile_fn=lambda job: None, environ=dict(LIVE_ENV), context_fn=None,
                         request_submit_fn=provider.submit, request_recover_fn=provider.recover)
    cfg = ServiceConfig(**{"bind": "127.0.0.1:0", "backend": "auto", "data_dir": tmp_path, "receipt_key": RECEIPT_KEY,
                           "request_key": REQUEST_KEY, "v2_enabled": True, **over})
    return QSearchService(cfg, runtime_factory=lambda kind: runtime)


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
        assert _post(svc, body) == PENDING
        digest = parse_request(body).request_digest
        row = svc.store.get_request(digest)
        assert row.state == "ACCEPTED" and row.attempts == 1 and _record(svc, body)["accounting"] == "pending"
        assert _post(svc, body) == PENDING                                    # recovery: still not final
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
    svc = _service(tmp_path, provider)
    assert _post(svc, body) == PENDING
    svc.close()
    svc = _service(tmp_path, provider)
    try:
        assert svc.recover() == []                                            # still pending after restart
        provider.recoveries = [_settled(1.5)]
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
        assert _post(svc, body) == PENDING                                    # FAILED, usage not final
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
        time.sleep(0.2)
        provider.gate.set()
        for t in threads:
            t.join()
        assert provider.submits == 1 and len(results) == 4 and all(status == 503 for status, _ in results)
    finally:
        svc.close()


def test_the_window_bounds_waiting_but_never_seals_or_gives_up(tmp_path):
    body = _live()
    provider = Provider(_pending, recoveries=[_pending])
    svc = _service(tmp_path, provider, accounting_window_s=1.0)
    try:
        assert _post(svc, body) == PENDING
        time.sleep(1.2)
        assert _post(svc, body) == (503, {"error": "job not completed", "code": "ACCOUNTING_TIMEOUT"})
        assert svc.store.get_request(parse_request(body).request_digest).state == "ACCEPTED"
        provider.recoveries = [_settled(3.0)]
        status, receipt = _post(svc, body)                                    # late accounting still lands
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
        assert _post(svc, body) == PENDING
        assert json.loads(svc.store.get_request(digest).receipt_json) == stale and provider.submits == 0

        def different(request):
            receipt = atlas_request_receipt(request, usage=2.0)
            receipt["pubs"][0]["raw_samples"] = ["0000"] * len(receipt["pubs"][0]["raw_samples"])
            return receipt
        provider.recoveries = [different]
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


# --- Predator parity ---------------------------------------------------------------------------------


def test_settled_and_resealed_receipts_verify_under_predator(tmp_path):
    receipt_v2 = pytest.importorskip("predator.drive.qsearch_receipt_v2")
    body = _live()
    expected = receipt_v2.ExpectedRequestV2.from_descriptor(body["descriptor"])
    svc = _service(tmp_path / "a", Provider(_pending, recoveries=[_settled(2.0)]))
    try:
        assert _post(svc, body) == PENDING
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
