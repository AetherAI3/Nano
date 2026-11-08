"""Stdlib validator for ``protocol/qsearch-lifecycle/v1/job-state.v1.schema.json``.

The runtime stays free of jsonschema, so this module restates that closed schema by
hand: exact member sets, member types, enums, patterns, nullability, amounts and the
cross-field invariants I1-I8 of its ``allOf``. ``tests/test_qsearch_lifecycle_wire_contract.py``
proves accept/reject agreement with jsonschema over every single-member mutation of the
golden histories and over I1-I8 cross-products.

Patterns use ``re.fullmatch``: the schema's ECMA-262 ``^...$`` never matches before a
trailing newline, whereas Python's ``$`` would. Booleans are never numbers and numbers
never booleans. An amount is a finite int or float in ``0..2^53-1``.
"""
from __future__ import annotations

import math
import re
from typing import Any

JOB_STATE_SCHEMA_ID = "nano.qsearch.job-state.v1"
_MAX_AMOUNT = 2**53 - 1

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,200}")

_STATES = frozenset({"ACCEPTED", "SUBMITTED_UNKNOWN", "QUEUED", "RUNNING", "COMPLETED", "SUCCEEDED", "FAILED",
                     "CANCELLED", "NOT_RUN", "QUEUE_EXPIRED", "CANCELLATION_REQUESTED", "ACCOUNTING_PENDING",
                     "ACCOUNTING_EXPIRED", "SETTLED"})
_PROVIDER_STATUSES = frozenset({"CANCELED", "CANCELLED", "COMPLETED", "DONE", "ERROR", "FAILED", "INITIALIZING",
                                "PENDING", "QUEUED", "RUNNING", "UNKNOWN", "VALIDATING"})
_NON_TERMINAL_STATUSES = frozenset({"ACCEPTED", "SUBMITTED_UNKNOWN", "QUEUED", "RUNNING"})
_TERMINAL_STATUSES = frozenset({"COMPLETED", "SUCCEEDED", "FAILED", "CANCELLED", "NOT_RUN"})
_EXECUTION_STATUSES = _NON_TERMINAL_STATUSES | _TERMINAL_STATUSES
_OUTCOMES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "NOT_RUN", "UNKNOWN"})
_ACCOUNTING_STATUSES = frozenset({"PENDING", "SETTLED", "NOT_APPLICABLE"})
_RESERVATION_STATUSES = frozenset({"HELD", "UNKNOWN", "SETTLED", "NOT_RESERVED"})
_NEXT_ACTIONS = frozenset({"NONE", "POLL_STATUS", "REQUEST_CANCEL", "RECONCILE", "MANUAL_RECONCILIATION"})
_PENDING_STATES = frozenset({"ACCEPTED", "SUBMITTED_UNKNOWN", "QUEUED", "RUNNING", "QUEUE_EXPIRED",
                             "CANCELLATION_REQUESTED", "ACCOUNTING_PENDING", "ACCOUNTING_EXPIRED"})
# I6: a decided execution status fixes the outcome; COMPLETED (provider DONE) stays UNKNOWN.
_OUTCOME_FOR_STATUS = {"SUCCEEDED": "SUCCEEDED", "FAILED": "FAILED", "CANCELLED": "CANCELLED",
                       "NOT_RUN": "NOT_RUN", "COMPLETED": "UNKNOWN"}

_MEMBERS = {
    "": frozenset({"schema", "request", "lineage", "state", "provider", "execution", "accounting", "usage",
                   "cancellation", "deadlines", "next_action"}),
    "request": frozenset({"request_id", "request_digest", "target"}),
    "lineage": frozenset({"replaces_request_digest", "replacement_request_digest"}),
    "provider": frozenset({"job_id", "status"}),
    "execution": frozenset({"status", "outcome", "terminal"}),
    "accounting": frozenset({"status"}),
    "usage": frozenset({"reservation_status", "held_quantum_seconds", "settled_quantum_seconds"}),
    "cancellation": frozenset({"requested", "requested_at", "confirmed", "confirmed_at"}),
    "deadlines": frozenset({"queue_at", "queue_expired", "accounting_at", "accounting_expired"}),
}


def _closed(value: Any, members: frozenset[str]) -> bool:
    return isinstance(value, dict) and value.keys() == members


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _nullable(pattern: re.Pattern[str], value: Any) -> bool:
    return value is None or _matches(pattern, value)


def _one_of(allowed: frozenset[str], value: Any) -> bool:
    return isinstance(value, str) and value in allowed


def _amount(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):   # ints are exact: isfinite would overflow
        return False
    return 0 <= value <= _MAX_AMOUNT


def _nullable_amount(value: Any) -> bool:
    return value is None or _amount(value)


def _structure_ok(p: Any) -> bool:
    """Closed objects, required members, types, enums, patterns and nullability."""
    if not _closed(p, _MEMBERS[""]) or not all(_closed(p[name], members)
                                               for name, members in _MEMBERS.items() if name):
        return False
    request, lineage, provider = p["request"], p["lineage"], p["provider"]
    execution, usage, cancellation, deadlines = p["execution"], p["usage"], p["cancellation"], p["deadlines"]
    return (p["schema"] == JOB_STATE_SCHEMA_ID
            and _matches(_REQUEST_ID, request["request_id"])
            and _matches(_DIGEST, request["request_digest"])
            and _nullable(_IDENTIFIER, request["target"])
            and _nullable(_DIGEST, lineage["replaces_request_digest"])
            and _nullable(_DIGEST, lineage["replacement_request_digest"])
            and _one_of(_STATES, p["state"])
            and _nullable(_IDENTIFIER, provider["job_id"])
            and _one_of(_PROVIDER_STATUSES, provider["status"])
            and _one_of(_EXECUTION_STATUSES, execution["status"])
            and _one_of(_OUTCOMES, execution["outcome"])
            and isinstance(execution["terminal"], bool)
            and _one_of(_ACCOUNTING_STATUSES, p["accounting"]["status"])
            and _one_of(_RESERVATION_STATUSES, usage["reservation_status"])
            and _nullable_amount(usage["held_quantum_seconds"])
            and _nullable_amount(usage["settled_quantum_seconds"])
            and isinstance(cancellation["requested"], bool)
            and _nullable_amount(cancellation["requested_at"])
            and isinstance(cancellation["confirmed"], bool)
            and _nullable_amount(cancellation["confirmed_at"])
            and _amount(deadlines["queue_at"])
            and isinstance(deadlines["queue_expired"], bool)
            and _nullable_amount(deadlines["accounting_at"])
            and isinstance(deadlines["accounting_expired"], bool)
            and _one_of(_NEXT_ACTIONS, p["next_action"]))


def _accounting_ok(p: dict[str, Any]) -> bool:
    """I1-I3 on a structurally valid envelope."""
    status, usage = p["accounting"]["status"], p["usage"]
    if status == "SETTLED":                                                       # I1
        return (p["state"] == "SETTLED" and p["next_action"] == "NONE"
                and usage["reservation_status"] == "SETTLED"
                and usage["held_quantum_seconds"] == 0 and usage["settled_quantum_seconds"] is not None)
    if status == "NOT_APPLICABLE":                                                # I2
        return (p["next_action"] == "NONE" and usage["reservation_status"] == "NOT_RESERVED"
                and usage["held_quantum_seconds"] is None and usage["settled_quantum_seconds"] is None)
    return (p["state"] in _PENDING_STATES                                         # I3 (PENDING)
            and usage["reservation_status"] in ("HELD", "UNKNOWN") and usage["settled_quantum_seconds"] is None)


def _execution_ok(p: dict[str, Any]) -> bool:
    """I4-I8 on a structurally valid envelope."""
    execution, cancellation = p["execution"], p["cancellation"]
    status, outcome, terminal = execution["status"], execution["outcome"], execution["terminal"]
    if not terminal and (status not in _NON_TERMINAL_STATUSES or outcome != "UNKNOWN"):     # I4
        return False
    if terminal and status not in _TERMINAL_STATUSES:                                        # I5
        return False
    if status in _OUTCOME_FOR_STATUS and (outcome != _OUTCOME_FOR_STATUS[status] or not terminal):  # I6
        return False
    if status == "CANCELLED":                                                                # I7
        if not cancellation["confirmed"]:
            return False
    elif cancellation["confirmed"] or cancellation["confirmed_at"] is not None:
        return False
    if cancellation["requested"]:                                                            # I8
        return cancellation["requested_at"] is not None
    return cancellation["requested_at"] is None


def is_valid_job_state(payload: Any) -> bool:
    """True exactly when ``payload`` satisfies job-state.v1.schema.json, invariants included."""
    return _structure_ok(payload) and _accounting_ok(payload) and _execution_ok(payload)


__all__ = ["JOB_STATE_SCHEMA_ID", "is_valid_job_state"]
