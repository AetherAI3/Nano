"""Bounded host controls use a nonblocking fake provider and injected clock."""
import json
import threading
import time

import pytest

from nano_qsearch.receipt_v2 import failed_v2
from nano_qsearch.request import parse_request
from nano_qsearch.runtime_ibm import LifecycleAdapterError
from nano_qsearch.service import QSearchService, REQUEST_PATH
from qsearch_v2_support import atlas_request_receipt, request_body
from test_qsearch_lifecycle_service import Clock, _config, _get, _headers, _seed


class Provider:
    def __init__(self):
        self.submits = self.observes = self.recovers = self.cancels = 0
        self.status = "QUEUED"
        self.cancel_ack_lost = False
        self.cancel_status = "QUEUED"
        self.usage = None
        self.cancel_authorized = True
        self.svc = None
        self.digest = None

    def _record(self, record):
        record.update(provider_status=self.status, provider_job_id="one-job", held_qpu_s=10.0)

    def run_request(self, request, *, max_pubs, record):
        self.submits += 1
        self._record(record)
        return None

    def observe_request(self, request, *, record):
        self.observes += 1
        self._record(record)

    def cancel_request(self, request, *, record, before_cancel):
        if not self.cancel_authorized:
            raise LifecycleAdapterError("CANCELLATION_NOT_AUTHORIZED", next_action="MANUAL_RECONCILIATION")
        if self.status in ("DONE", "CANCELLED", "ERROR"):
            self._record(record)
            return
        before_cancel()
        # The durable fence must precede the first external cancellation.
        persisted = json.loads(self.svc.store.request_record(request.request_digest))
        assert persisted["cancellation_requested_at"] >= 0
        assert persisted["cancellation_dispatched_at"] >= persisted["cancellation_requested_at"]
        self.cancels += 1
        self.status = self.cancel_status
        self._record(record)
        if self.cancel_ack_lost:
            raise TimeoutError("secret-provider-token")

    def recover_request(self, request, *, record):
        self.recovers += 1
        self._record(record)
        if self.status in ("DONE", "CANCELLED", "ERROR"):
            record.update(accounting="pending", result="OK" if self.status == "DONE" else "FAILED")
            if self.usage is not None:
                # Minimal sealed-compatible terminal failed response. This fake
                # never pretends a cancelled run produced useful computation.
                atlas = atlas_request_receipt(request, usage=self.usage, job_id="one-job")
                atlas.update(status="FAILED", provider_status=self.status, pubs=[],
                             reason="provider job ended " + self.status)
                record.update(accounting="settled", result="FAILED", quantum_seconds=self.usage)
                return failed_v2(request, atlas, purpose=request.purpose,
                                 reason="provider job ended " + self.status)
        return None


def _service(tmp_path, provider, clock, **config):
    svc = QSearchService(_config(tmp_path, **config), clock=clock, runtime_factory=lambda kind: provider)
    provider.svc = svc
    return svc


def _control(svc, path, action, clock):
    route = path + "/" + action
    return svc.control_request("POST", route, b"", _headers("POST", route, now=clock()), now=int(clock()))


def test_eleven_hour_queue_stops_automatic_provider_polling_and_preserves_hold(tmp_path):
    provider, clock = Provider(), Clock()
    svc = _service(tmp_path, provider, clock, queue_deadline_s=3600, poll_interval_s=30)
    try:
        request, path = _seed(svc, {"provider_status": "QUEUED", "provider_job_id": "one-job",
                                    "held_qpu_s": 10, "queue_started_at": clock()})
        # One explicit refresh, then repeated reads never poll at all.
        assert _control(svc, path, "refresh", clock)[0] == 200
        before = provider.observes
        for _ in range(20):
            assert _get(svc, path, now=clock())[0] == 200
        assert provider.observes == before
        started = clock()
        for interval in range(1, 11 * 120 + 1):
            clock.now = started + interval * 30
            svc.recover()
        # One hour at a 30-second interval allows at most 120 status or
        # recovery reads, even though the host is driven for eleven hours.
        assert provider.observes + provider.recovers <= 120
        state = _get(svc, path, now=clock())[1]
        assert state["state"] == "QUEUE_EXPIRED"
        assert state["next_action"] == "REQUEST_CANCEL"
        reads_after_expiry = (provider.observes, provider.recovers)
        for _ in range(20):
            svc.recover()
        assert (provider.observes, provider.recovers) == reads_after_expiry
        assert provider.submits == provider.cancels == 0
        assert svc.store.get_request(request.request_digest).attempts == 1
        assert state["usage"]["reservation_status"] == "HELD"
    finally:
        svc.close()


def test_repeated_cancel_and_restart_after_lost_ack_dispatch_only_once(tmp_path):
    provider, clock = Provider(), Clock()
    provider.cancel_ack_lost = True
    svc = _service(tmp_path, provider, clock)
    request, path = _seed(svc, {"provider_status": "QUEUED", "provider_job_id": "one-job", "held_qpu_s": 10})
    try:
        status, error = _control(svc, path, "cancel", clock)
        assert status in (200, 503)
        assert "secret-provider-token" not in json.dumps(error)
        assert provider.cancels == 1
        assert _get(svc, path, now=clock())[1]["execution"]["terminal"] is False
        for _ in range(4):
            _control(svc, path, "cancel", clock)
        assert provider.cancels == 1
    finally:
        svc.close()
    svc = _service(tmp_path, provider, clock)
    try:
        svc.recover()
        provider.status = "CANCELLED"
        clock.now += 31
        status, state = _control(svc, path, "cancel", clock)
        assert status == 200
        assert state["execution"]["outcome"] == "CANCELLED"
        assert state["cancellation"]["confirmed"] is True
        assert state["usage"]["reservation_status"] == "HELD"
        assert state["usage"]["settled_quantum_seconds"] is None
        assert provider.cancels == 1 and provider.submits == 0
        assert svc.store.get_request(request.request_digest).attempts == 1
        kinds = [event["kind"] for event in svc.store.request_events(request.request_digest)]
        assert kinds.count("cancellation_requested") == 1
        assert kinds.count("cancellation_dispatched") == 1
        assert kinds.count("cancellation_confirmed") == 1
    finally:
        svc.close()


@pytest.mark.parametrize("actual", ["RUNNING", "DONE"])
def test_queue_expiry_refresh_reconciles_actual_job_and_never_replaces(tmp_path, actual):
    provider, clock = Provider(), Clock()
    svc = _service(tmp_path, provider, clock)
    try:
        _, path = _seed(svc, {"provider_status": "QUEUED", "provider_job_id": "one-job",
                             "queue_started_at": clock()})
        clock.now += 11 * 3600
        assert _get(svc, path, now=clock())[1]["state"] == "QUEUE_EXPIRED"
        provider.status = actual
        status, state = _control(svc, path, "refresh", clock)
        assert status == 200
        assert state["provider"]["status"] == actual
        assert state["deadlines"]["queue_expired"] is False
        assert state["execution"]["terminal"] is (actual == "DONE")
        assert provider.submits == provider.cancels == 0
        assert provider.observes == 1
    finally:
        svc.close()


def test_repeated_refresh_throttles_provider_reads(tmp_path):
    provider, clock = Provider(), Clock()
    svc = _service(tmp_path, provider, clock, poll_interval_s=30)
    try:
        _, path = _seed(svc, {"provider_status": "QUEUED", "provider_job_id": "one-job"})
        for _ in range(20):
            assert _control(svc, path, "refresh", clock)[0] in (200, 409, 429)
        assert provider.observes == 1
        clock.now += 30
        assert _control(svc, path, "refresh", clock)[0] == 200
        assert provider.observes == 2
        assert provider.submits == 0
    finally:
        svc.close()


def test_rejected_cancel_preflight_can_be_authorized_later_without_resubmission(tmp_path):
    provider, clock = Provider(), Clock()
    provider.cancel_authorized = False
    svc = _service(tmp_path, provider, clock)
    try:
        request, path = _seed(svc, {"provider_status": "QUEUED", "provider_job_id": "one-job"})
        status, error = _control(svc, path, "cancel", clock)
        assert status == 503 and error["code"] == "CANCELLATION_NOT_AUTHORIZED"
        kinds = [event["kind"] for event in svc.store.request_events(request.request_digest)]
        assert kinds == ["cancellation_requested"]
        assert provider.cancels == provider.submits == 0
        provider.cancel_authorized = True
        clock.now += svc.config.poll_interval_s
        assert _control(svc, path, "cancel", clock)[0] == 200
        kinds = [event["kind"] for event in svc.store.request_events(request.request_digest)]
        assert kinds.count("cancellation_requested") == kinds.count("cancellation_dispatched") == 1
        assert provider.cancels == 1 and provider.submits == 0
    finally:
        svc.close()


def test_job_completed_before_cancel_records_real_outcome_without_dispatch_event(tmp_path):
    provider, clock = Provider(), Clock()
    provider.status = "DONE"
    svc = _service(tmp_path, provider, clock)
    try:
        request, path = _seed(svc, {"provider_status": "QUEUED", "provider_job_id": "one-job"})
        status, state = _control(svc, path, "cancel", clock)
        assert status == 200 and state["execution"]["terminal"] is True
        assert state["provider"]["status"] == "DONE"
        assert state["cancellation"]["confirmed"] is False
        assert [event["kind"] for event in svc.store.request_events(request.request_digest)] == ["cancellation_requested"]
        assert provider.cancels == provider.submits == 0
        assert provider.recovers == 1
        for _ in range(10):
            assert _control(svc, path, "cancel", clock)[0] == 200
        assert provider.recovers == 1  # A no-RPC terminal race still obeys accounting poll bounds.
    finally:
        svc.close()


def test_real_initial_submit_repeat_and_restart_never_submit_twice(tmp_path):
    provider, clock = Provider(), Clock()
    body = request_body(2, target="ibm_fez")
    raw = json.dumps(body).encode()
    request = parse_request(body)
    svc = _service(tmp_path, provider, clock)
    try:
        for _ in range(6):
            headers = _headers("POST", REQUEST_PATH, raw, now=clock())
            assert svc.submit_request("POST", REQUEST_PATH, raw, headers, now=int(clock()))[0] == 503
        assert provider.submits == 1
    finally:
        svc.close()
    svc = _service(tmp_path, provider, clock)
    try:
        svc.recover()
        assert provider.submits == 1
        assert svc.store.get_request(request.request_digest).attempts == 1
    finally:
        svc.close()


def _replace(svc, path, body, clock):
    route = path + "/replace"
    raw = json.dumps(body).encode()
    return svc.control_request("POST", route, raw, _headers("POST", route, raw, now=clock()), now=int(clock()))


def test_replacement_rejects_outstanding_job_without_accepting_new_request(tmp_path):
    provider, clock = Provider(), Clock()
    svc = _service(tmp_path, provider, clock)
    try:
        _, path = _seed(svc, {"provider_status": "RUNNING", "provider_job_id": "one-job"})
        replacement = request_body(2, target="ibm_kingston", request_id="replacement-one")
        status, error = _replace(svc, path, replacement, clock)
        assert status == 409
        assert {"code", "field", "retryable", "next_action"} <= error.keys()
        assert svc.store.get_request(parse_request(replacement).request_digest) is None
        assert provider.submits == provider.cancels == 0
    finally:
        svc.close()


def test_settled_replacement_has_new_target_and_identity_and_submits_once(tmp_path):
    provider, clock = Provider(), Clock()
    svc = _service(tmp_path, provider, clock)
    request, path = _seed(svc, {"provider_status": "CANCELLED", "provider_job_id": "one-job"})
    replacement = request_body(2, target="ibm_kingston", request_id="replacement-one")
    replacement_request = parse_request(replacement)
    try:
        provider.status, provider.usage = "CANCELLED", 0.7
        assert _control(svc, path, "refresh", clock)[0] == 200
        settled = _get(svc, path, now=clock())[1]
        assert settled["state"] == "SETTLED"
        assert settled["execution"]["outcome"] == "CANCELLED"
        assert settled["usage"]["settled_quantum_seconds"] == 0.7
        provider.status, provider.usage = "QUEUED", None
        assert _replace(svc, path, replacement, clock)[0] in (200, 503)
        assert provider.submits == 1
        assert replacement_request.request_id != request.request_id
        assert replacement_request.request_digest != request.request_digest
        assert replacement_request.target != request.target
        for _ in range(5):
            assert _replace(svc, path, replacement, clock)[0] in (200, 503)
        assert provider.submits == 1
        record = json.loads(svc.store.request_record(replacement_request.request_digest))
        assert record["replaces_request_digest"] == request.request_digest
        another = request_body(2, target="ibm_marrakesh", request_id="replacement-two")
        assert _replace(svc, path, another, clock)[0] == 409
        assert svc.store.get_request(parse_request(another).request_digest) is None
    finally:
        svc.close()
    svc = _service(tmp_path, provider, clock)
    try:
        assert _replace(svc, path, replacement, clock)[0] in (200, 503)
        assert provider.submits == 1
        assert svc.store.get_request(replacement_request.request_digest).attempts == 1
    finally:
        svc.close()


def test_cancel_fence_while_waiting_for_host_slot_prevents_initial_submission(tmp_path):
    provider, clock = Provider(), Clock()
    svc = _service(tmp_path, provider, clock, max_concurrent=1)
    body = request_body(2, target="ibm_fez")
    request = parse_request(body)
    raw, results = json.dumps(body).encode(), []
    svc._slots.acquire()

    def submit():
        results.append(svc.submit_request("POST", REQUEST_PATH, raw,
                                         _headers("POST", REQUEST_PATH, raw, now=clock()), now=int(clock())))

    thread = threading.Thread(target=submit)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while svc.store.get_request(request.request_digest) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert svc.store.get_request(request.request_digest) is not None
        assert _control(svc, "/qsearch/v2/requests/" + request.request_digest, "cancel", clock)[0] == 409
        svc._slots.release()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert results
        assert provider.submits == 0
        assert svc.store.get_request(request.request_digest).attempts == 0
    finally:
        if thread.is_alive():
            svc._slots.release()
            thread.join(timeout=3)
        svc.close()


def test_legacy_sealed_pending_receipt_obeys_accounting_deadline_before_recovery(tmp_path):
    provider, clock = Provider(), Clock()
    svc = _service(tmp_path, provider, clock, accounting_window_s=900)
    try:
        request, _ = _seed(svc, {"provider_status": "DONE", "provider_job_id": "one-job", "result": "OK",
                                 "accounting": "pending", "accounting_pending_since": clock()})
        legacy = {"schema": "predator.qsearch.receipt.v2", "status": "OK",
                  "backend": {"kind": "ibm", "dry_run": False},
                  "hardware": {"provider_job_id": "one-job"},
                  "usage": {"usage_pending": True, "quantum_seconds": None}}
        svc.store.complete_request(request.request_digest, json.dumps(legacy))
        clock.now += 901
        raw = svc.store.get_request(request.request_digest).request_json.encode()
        for _ in range(4):
            status, _ = svc.submit_request("POST", REQUEST_PATH, raw,
                                          _headers("POST", REQUEST_PATH, raw, now=clock()), now=int(clock()))
            assert status == 503
        assert provider.recovers == provider.observes == provider.submits == 0
    finally:
        svc.close()
