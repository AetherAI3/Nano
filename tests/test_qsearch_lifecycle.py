"""Offline state-contract coverage: no provider, ledger, wall clock or credentials."""
import copy
import json
from dataclasses import replace

import pytest

from nano_qsearch.lifecycle import JOB_STATE_SCHEMA, NEXT_ACTIONS, job_state, lifecycle_error
from nano_qsearch.store import RequestRecord


def row(*, attempts=1, receipt=None):
    body = {"descriptor": {"target": "ibm_fez", "max_execution_time": 600},
            "jobs": [{"limits": {"max_qpu_s": 600}}]}
    return RequestRecord("sha256:" + "a" * 64, "request-1", "DONE" if receipt else "ACCEPTED",
                         json.dumps(body), json.dumps(receipt) if receipt else None, 100.0, None, attempts)


def project(record=None, receipt=None, *, now=200.0, request_row=None):
    return job_state(request_row or row(receipt=receipt), record or {}, receipt, now=now,
                     queue_deadline_s=3600, accounting_deadline_s=900)


def test_eleven_hour_queue_expiry_is_pure_and_preserves_unknown_held_usage():
    record = {"provider_job_id": "job-1", "provider_status": "QUEUED"}
    original = copy.deepcopy(record)
    before = project(record, now=3699)
    after = project(record, now=100 + 11 * 3600)
    assert before["state"] == "QUEUED"
    assert after["schema"] == JOB_STATE_SCHEMA
    assert after["state"] == "QUEUE_EXPIRED"
    assert after["next_action"] == "REQUEST_CANCEL"
    assert after["execution"] == {"status": "QUEUED", "outcome": "UNKNOWN", "terminal": False}
    assert after["usage"] == {"reservation_status": "HELD", "held_quantum_seconds": None,
                              "settled_quantum_seconds": None}
    assert record == original
    assert after == project(record, now=100 + 11 * 3600)


@pytest.mark.parametrize("provider_status,execution_status", [("RUNNING", "RUNNING"), ("DONE", "COMPLETED")])
def test_queue_race_actual_execution_evidence_wins(provider_status, execution_status):
    state = project({"provider_status": provider_status}, now=100 + 11 * 3600)
    assert state["execution"]["status"] == execution_status
    assert not state["deadlines"]["queue_expired"]
    assert state["next_action"] != "REQUEST_CANCEL"


def test_cancellation_request_and_acknowledgement_are_not_terminal_confirmation():
    state = project({"provider_status": "QUEUED", "cancellation_requested_at": 150,
                     "cancellation_confirmed_at": 160})
    assert state["state"] == "CANCELLATION_REQUESTED"
    assert state["cancellation"] == {"requested": True, "requested_at": 150.0,
                                     "confirmed": False, "confirmed_at": None}
    assert not state["execution"]["terminal"]
    assert state["next_action"] == "RECONCILE"


def test_confirmed_cancelled_unknown_zero_stays_held_after_accounting_deadline():
    record = {"provider_status": "CANCELLED", "result": "FAILED", "accounting": "pending",
              "quantum_seconds": 0, "accounting_pending_since": 150, "held_qpu_s": 40}
    before = project(record, now=1049)
    after = project(record, now=1050)
    assert before["state"] == "ACCOUNTING_PENDING"
    assert after["state"] == "ACCOUNTING_EXPIRED"
    assert after["execution"] == {"status": "CANCELLED", "outcome": "CANCELLED", "terminal": True}
    assert after["cancellation"]["confirmed"]
    assert after["usage"] == {"reservation_status": "HELD", "held_quantum_seconds": 40.0,
                              "settled_quantum_seconds": None}
    assert after["next_action"] == "MANUAL_RECONCILIATION"


@pytest.mark.parametrize("status,provider_status,execution", [("OK", "DONE", "SUCCEEDED"),
                                                            ("FAILED", "ERROR", "FAILED"),
                                                            ("FAILED", "CANCELLED", "CANCELLED")])
def test_settlement_is_independent_of_computational_outcome(status, provider_status, execution):
    receipt = {"status": status, "backend": {"dry_run": False},
               "hardware": {"provider_job_id": "job-1"},
               "usage": {"usage_pending": False, "quantum_seconds": 5}}
    state = project({"provider_status": provider_status}, receipt)
    assert state["state"] == "SETTLED"
    assert state["execution"]["status"] == execution
    assert state["accounting"]["status"] == "SETTLED"
    assert state["usage"]["settled_quantum_seconds"] == 5
    assert state["next_action"] == "NONE"
    assert "success" not in state


@pytest.mark.parametrize("seconds", [None, 0, -1, float("inf"), True])
def test_pending_or_invalid_usage_never_settles(seconds):
    state = project({"result": "FAILED", "accounting": "settled", "quantum_seconds": seconds})
    assert state["accounting"]["status"] == "PENDING"
    assert state["usage"]["settled_quantum_seconds"] is None


def test_receipt_with_pending_usage_stays_held_even_if_value_positive():
    receipt = {"status": "OK", "usage": {"usage_pending": True, "quantum_seconds": 3}}
    state = project({}, receipt)
    assert state["state"] == "ACCOUNTING_PENDING"
    assert state["usage"]["settled_quantum_seconds"] is None


def test_completed_receipt_preserves_actual_result_after_cancel_race():
    receipt = {"status": "OK", "usage": {"usage_pending": False, "quantum_seconds": 4}}
    state = project({"provider_status": "CANCELLED", "cancellation_requested_at": 120}, receipt)
    assert state["execution"]["outcome"] == "SUCCEEDED"
    assert not state["cancellation"]["confirmed"]
    assert state["usage"]["settled_quantum_seconds"] == 4


def test_unsubmitted_and_dry_run_have_no_claimed_reservation():
    for receipt in ({"status": "NOT_RUN"}, {"status": "OK", "backend": {"dry_run": True}}):
        state = project({}, receipt)
        assert state["accounting"]["status"] == "NOT_APPLICABLE"
        assert state["usage"]["reservation_status"] == "NOT_RESERVED"
        assert state["next_action"] == "NONE"


def test_untrusted_provider_text_and_metrics_are_not_returned():
    secret = "token=credential-secret"
    state = project({"provider_status": secret, "reason": secret, "provider_metrics": {"secret": secret},
                     "provider_job_id": secret})
    assert state["provider"] == {"job_id": None, "status": "UNKNOWN"}
    assert secret not in json.dumps(state)


def test_explicit_queue_start_and_deadline_boundary():
    assert project({"queue_started_at": 1000}, now=4599)["state"] == "SUBMITTED_UNKNOWN"
    assert project({"queue_started_at": 1000}, now=4600)["state"] == "QUEUE_EXPIRED"


@pytest.mark.parametrize("now,queue,accounting", [(float("nan"), 1, 1), (-1, 1, 1), (1, 0, 1),
                                                (1, True, 1), (1, 1, float("inf"))])
def test_invalid_clock_or_deadline_rejected(now, queue, accounting):
    with pytest.raises(ValueError):
        job_state(row(), {}, None, now=now, queue_deadline_s=queue, accounting_deadline_s=accounting)


def test_error_metadata_has_stable_next_action_and_optional_field():
    assert lifecycle_error("IDENTITY", field="request_digest", next_action="NONE") == {
        "error": "job lifecycle request failed", "code": "IDENTITY", "field": "request_digest",
        "retryable": False, "next_action": "NONE"}
    with pytest.raises(ValueError):
        lifecycle_error("ERROR", next_action="SUBMIT_REPLACEMENT")
    assert project()["next_action"] in NEXT_ACTIONS


@pytest.mark.parametrize("accepted_at", [float("nan"), float("inf"), -1, True])
def test_corrupt_persisted_acceptance_time_fails_before_http_serialization(accepted_at):
    with pytest.raises(ValueError):
        project(request_row=replace(row(), accepted_at=accepted_at))


def test_lineage_exposes_only_valid_linked_digests():
    parent, child = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    state = project({"replaces_request_digest": parent, "replacement_request_digest": child})
    assert state["lineage"] == {"replaces_request_digest": parent, "replacement_request_digest": child}
    state = project({"replaces_request_digest": "credential-secret", "replacement_request_digest": "sha256:bad"})
    assert state["lineage"] == {"replaces_request_digest": None, "replacement_request_digest": None}
    assert "credential-secret" not in json.dumps(state)


def test_finite_inputs_with_overflowed_deadline_fail_before_http_serialization():
    with pytest.raises(ValueError):
        job_state(replace(row(), accepted_at=1e308), {}, None, now=1,
                  queue_deadline_s=1e308, accounting_deadline_s=900)
