"""The #59 projection authenticates before calling #58's optional zero predicate.

The predicate is mocked here to test the trust boundary without duplicating its
finality schema. Cross-repository accounting tests own the actual zero proof.
"""
import json
from dataclasses import replace

import pytest

from nano_qsearch import receipt_v2
from nano_qsearch.auth import AUTH_HEADER, sign_request
from nano_qsearch.lifecycle import job_state
from nano_qsearch.request import parse_request
from nano_qsearch.service import QSearchService, ServiceConfig
from nano_qsearch.store import RequestRecord
from qsearch_v2_support import request_body


KEY = b"lifecycle-zero-receipt-key-" + b"k" * 16


def case(provider_status="CANCELLED"):
    body = request_body(2, target="ibm_fez")
    request = parse_request(body)
    # Deliberately not a complete #58 proof: the mock predicate supplies that
    # independent trust decision. These tests cannot validate finality itself.
    unsigned = {"schema": receipt_v2.SCHEMA_V2, "receipt_version": 2, "status": "FAILED",
                "request": {"request_digest": request.request_digest, "block_count": 0},
                "backend": {"kind": "ibm", "id": "ibm_fez", "dry_run": False},
                "hardware": {"provider_job_id": "job-authenticated"},
                "usage": {"quantum_seconds": 0, "usage_pending": False,
                          "zero_usage_evidence": {"provider_status": provider_status}},
                "reason": "provider: " + provider_status}
    receipt = receipt_v2.seal_receipt_v2(unsigned, KEY)
    row = RequestRecord(request.request_digest, request.request_id, "DONE", json.dumps(body),
                        json.dumps(receipt), 1000, 1010, 1)
    return row, request, receipt


def project(row, receipt, record=None, *, key=KEY):
    return job_state(row, record or {"held_qpu_s": 10}, receipt, now=2000,
                     queue_deadline_s=3600, accounting_deadline_s=900, receipt_key=key)


def assert_held(state):
    assert state["accounting"]["status"] == "PENDING"
    assert state["usage"]["reservation_status"] == "HELD"
    assert state["usage"]["settled_quantum_seconds"] is None


def test_hardware_seal_is_verified_before_the_dedicated_predicate(monkeypatch):
    row, request, receipt = case()
    calls = []
    verify = receipt_v2.verify_seal_v2

    def authenticate(value, key):
        calls.append("authenticate")
        verify(value, key)

    def confirmed(parsed, value):
        calls.append("predicate")
        assert parsed.request_id == request.request_id
        assert parsed.request_digest == request.request_digest and parsed.target == request.target
        assert value == receipt
        return True

    monkeypatch.setattr(receipt_v2, "verify_seal_v2", authenticate)
    monkeypatch.setattr(receipt_v2, "confirmed_zero_receipt", confirmed, raising=False)
    state = project(row, receipt)
    assert calls == ["authenticate", "predicate"]
    assert state["state"] == "SETTLED" and state["next_action"] == "NONE"
    assert state["usage"] == {"reservation_status": "SETTLED", "held_quantum_seconds": 0.0,
                              "settled_quantum_seconds": 0.0}


@pytest.mark.parametrize("provider_status,outcome", [("CANCELLED", "CANCELLED"), ("ERROR", "FAILED")])
@pytest.mark.parametrize("record", [{}, {"provider_status": "RUNNING", "provider_job_id": "stale-job", "result": "OK"}])
def test_authenticated_zero_proof_preserves_terminal_outcome_without_fresh_run_record(
        monkeypatch, provider_status, outcome, record):
    row, _, receipt = case(provider_status)
    monkeypatch.setattr(receipt_v2, "confirmed_zero_receipt", lambda *args: True, raising=False)
    state = project(row, receipt, record)
    assert state["state"] == "SETTLED"
    assert state["provider"] == {"job_id": "job-authenticated", "status": provider_status}
    assert state["execution"] == {"status": outcome, "outcome": outcome, "terminal": True}
    assert state["cancellation"]["confirmed"] is (provider_status == "CANCELLED")


@pytest.mark.parametrize("bad_key", [None, b"wrong-receipt-key" * 2])
def test_missing_key_or_bad_hardware_mac_keeps_hold_and_never_calls_predicate(monkeypatch, bad_key):
    row, _, receipt = case()

    def forbidden(*args):
        pytest.fail("zero predicate ran without successful hardware authentication")

    monkeypatch.setattr(receipt_v2, "confirmed_zero_receipt", forbidden, raising=False)
    assert_held(project(row, receipt, key=bad_key))


def test_modified_receipt_fails_authentication_before_zero_predicate(monkeypatch):
    row, _, receipt = case()
    receipt["hardware"]["provider_job_id"] = "substituted-job"
    calls = []
    monkeypatch.setattr(receipt_v2, "confirmed_zero_receipt", lambda *args: calls.append(1) or True, raising=False)
    assert_held(project(row, receipt))
    assert calls == []


def test_old_receipt_module_without_predicate_fails_closed(monkeypatch):
    row, _, receipt = case()
    monkeypatch.delattr(receipt_v2, "confirmed_zero_receipt", raising=False)
    assert_held(project(row, receipt))


@pytest.mark.parametrize("result", [False, None, 1])
def test_invalid_or_unrecognized_proof_keeps_zero_held(monkeypatch, result):
    row, _, receipt = case()
    monkeypatch.setattr(receipt_v2, "confirmed_zero_receipt", lambda *args: result, raising=False)
    assert_held(project(row, receipt))


def test_zero_record_markers_without_authenticated_receipt_cannot_settle(monkeypatch):
    row, _, _ = case()
    calls = []
    monkeypatch.setattr(receipt_v2, "confirmed_zero_receipt", lambda *args: calls.append(1) or True, raising=False)
    state = project(row, None, {"accounting": "settled", "quantum_seconds": 0, "settled_qpu_s": 0,
                                "provider_status": "CANCELLED", "held_qpu_s": 10})
    assert_held(state)
    assert state["usage"]["held_quantum_seconds"] == 10
    assert calls == []


def test_stored_request_identity_must_match_parsed_body_before_predicate(monkeypatch):
    row, _, receipt = case()
    calls = []
    monkeypatch.setattr(receipt_v2, "confirmed_zero_receipt", lambda *args: calls.append(1) or True, raising=False)
    assert_held(project(replace(row, request_digest="sha256:" + "a" * 64), receipt))
    assert calls == []


@pytest.mark.parametrize("change", [{"quantum_seconds": True}, {"usage_pending": True},
                                   {"quantum_seconds": None}])
def test_pending_or_invalid_zero_amount_cannot_reach_predicate(monkeypatch, change):
    row, _, receipt = case()
    receipt["usage"].update(change)
    receipt = receipt_v2.seal_receipt_v2(receipt, KEY)
    calls = []
    monkeypatch.setattr(receipt_v2, "confirmed_zero_receipt", lambda *args: calls.append(1) or True, raising=False)
    assert_held(project(row, receipt))
    assert calls == []


def test_service_supplies_receipt_key_to_internal_and_authenticated_read_projection(tmp_path, monkeypatch):
    row, request, receipt = case()
    monkeypatch.setattr(receipt_v2, "confirmed_zero_receipt", lambda *args: True, raising=False)
    request_key = b"lifecycle-zero-request-key-" + b"r" * 16

    def forbidden_runtime(kind):
        pytest.fail("zero status projection accessed a provider runtime")

    config = ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path, receipt_key=KEY,
                           request_key=request_key, v2_enabled=True)
    service = QSearchService(config, runtime_factory=forbidden_runtime, clock=lambda: 2000)
    try:
        service.store.begin_request(request.request_digest, request.request_id, row.request_json)
        service.store.mark_request_attempt(request.request_digest)
        service.store.complete_request(request.request_digest, json.dumps(receipt))
        assert service._state(request.request_digest)["usage"]["settled_quantum_seconds"] == 0
        path = "/qsearch/v2/requests/" + request.request_digest
        headers = {AUTH_HEADER: sign_request(request_key, "GET", path, b"", ts=2000, nonce="af" * 16)}
        status, state = service.status_request("GET", path, b"", headers, now=2000)
        assert status == 200 and state["state"] == "SETTLED"
        assert state["execution"]["outcome"] == "CANCELLED"
        assert state["usage"]["settled_quantum_seconds"] == 0
    finally:
        service.close()
