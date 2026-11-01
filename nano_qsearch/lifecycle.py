"""Read-only, clock-injected projection of durable q-search request evidence.

This module owns no provider, store, ledger or budget. In particular a deadline
is a decision to stop waiting, never evidence of cancellation or final usage.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from .store import RequestRecord

JOB_STATE_SCHEMA = "nano.qsearch.job-state.v1"
NEXT_ACTIONS = frozenset({"NONE", "POLL_STATUS", "REQUEST_CANCEL", "RECONCILE", "MANUAL_RECONCILIATION"})
_PROVIDER_STATUSES = frozenset({"INITIALIZING", "VALIDATING", "QUEUED", "PENDING", "RUNNING", "DONE",
                                "COMPLETED", "ERROR", "FAILED", "CANCELLED", "CANCELED"})
_QUEUED = frozenset({"INITIALIZING", "VALIDATING", "QUEUED", "PENDING"})
_FAILED = frozenset({"ERROR", "FAILED"})
_CANCELLED = frozenset({"CANCELLED", "CANCELED"})
_DONE = frozenset({"DONE", "COMPLETED"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def lifecycle_error(code: str, *, field: str | None = None, retryable: bool = False,
                    next_action: str = "NONE") -> dict[str, Any]:
    """Stable error metadata; callers pass constant codes and field paths, never exceptions."""
    if next_action not in NEXT_ACTIONS:
        raise ValueError("unknown next action")
    return {"error": "job lifecycle request failed", "code": code, "field": field,
            "retryable": retryable, "next_action": next_action}


def job_state(row: RequestRecord, record: Mapping[str, Any], receipt: Mapping[str, Any] | None, *,
              now: float, queue_deadline_s: float, accounting_deadline_s: float) -> dict[str, Any]:
    """Project a snapshot without I/O or mutation; all times are host-injected Unix seconds.

    A missing held amount stays null. The execution cap is not the reservation.
    Only a positive, explicitly settled usage value is recognized here; issue
    #58 owns any future confirmed-terminal-zero accounting transition.
    """
    if _number(now) is None or any(_number(x) is None or x <= 0
                                  for x in (queue_deadline_s, accounting_deadline_s)):
        raise ValueError("now must be finite and nonnegative; deadlines must be finite and positive")
    receipt = _mapping(receipt)
    try:
        request = _mapping(json.loads(row.request_json))
    except (ValueError, RecursionError):
        request = {}
    descriptor = _mapping(request.get("descriptor"))
    backend = _mapping(receipt.get("backend"))
    hardware = _mapping(receipt.get("hardware"))
    usage = _mapping(receipt.get("usage"))
    provider_status = record.get("provider_status")
    provider_status = provider_status.upper() if isinstance(provider_status, str) else "UNKNOWN"
    if provider_status not in _PROVIDER_STATUSES:
        provider_status = "UNKNOWN"
    provider_job_id = _identifier(record.get("provider_job_id")) or _identifier(hardware.get("provider_job_id"))
    result = receipt.get("status") or record.get("result")

    if receipt.get("status") == "OK":
        execution_status, outcome, terminal = "SUCCEEDED", "SUCCEEDED", True
    elif provider_status in _CANCELLED:
        execution_status, outcome, terminal = "CANCELLED", "CANCELLED", True
    elif result == "FAILED" or provider_status in _FAILED:
        execution_status, outcome, terminal = "FAILED", "FAILED", True
    elif result == "OK":
        execution_status, outcome, terminal = "SUCCEEDED", "SUCCEEDED", True
    elif result == "NOT_RUN" and not provider_job_id:
        execution_status, outcome, terminal = "NOT_RUN", "NOT_RUN", True
    elif provider_status in _DONE:
        execution_status, outcome, terminal = "COMPLETED", "UNKNOWN", True
    elif provider_status == "RUNNING":
        execution_status, outcome, terminal = "RUNNING", "UNKNOWN", False
    elif provider_status in _QUEUED:
        execution_status, outcome, terminal = "QUEUED", "UNKNOWN", False
    else:
        execution_status, outcome, terminal = ("SUBMITTED_UNKNOWN" if row.attempts else "ACCEPTED"), "UNKNOWN", False

    # Do not infer a zero settlement from an amount, a terminal status, or an
    # elapsed reconciliation window. These do not establish provider finality.
    settled = _number(usage.get("quantum_seconds")) if usage.get("usage_pending") is False else None
    if settled is None and record.get("accounting") == "settled":
        settled = _number(record.get("quantum_seconds"))
        if settled is None:
            settled = _number(record.get("settled_qpu_s"))
    settled = settled if settled is not None and settled > 0 else None
    dry_run = backend.get("dry_run") is True
    unsubmitted = execution_status == "NOT_RUN"
    if dry_run or unsubmitted:
        accounting_status, reservation_status = "NOT_APPLICABLE", "NOT_RESERVED"
        held, settled = None, None
    elif settled is not None:
        accounting_status, reservation_status, held = "SETTLED", "SETTLED", 0.0
    else:
        accounting_status, reservation_status = "PENDING", "HELD" if row.attempts or provider_job_id else "UNKNOWN"
        held = _number(record.get("held_qpu_s"))
        if held is None:
            held = _number(record.get("held_quantum_seconds"))

    queue_start = _number(record.get("queue_started_at"))
    queue_at = (row.accepted_at if queue_start is None else queue_start) + queue_deadline_s
    queue_expired = not terminal and execution_status != "RUNNING" and now >= queue_at
    accounting_start = _number(record.get("accounting_pending_since"))
    accounting_at = None if accounting_start is None else accounting_start + accounting_deadline_s
    accounting_expired = accounting_status == "PENDING" and accounting_at is not None and now >= accounting_at
    requested_at = _number(record.get("cancellation_requested_at"))
    confirmed_at = _number(record.get("cancellation_confirmed_at"))
    # A request/acknowledgement timestamp alone is never terminal confirmation.
    confirmed = execution_status == "CANCELLED"
    requested = requested_at is not None

    if accounting_status == "SETTLED":
        state, next_action = "SETTLED", "NONE"
    elif accounting_status == "NOT_APPLICABLE":
        state, next_action = execution_status, "NONE"
    elif terminal:
        state = "ACCOUNTING_EXPIRED" if accounting_expired else "ACCOUNTING_PENDING"
        next_action = "MANUAL_RECONCILIATION" if accounting_expired else "RECONCILE"
    elif requested:
        state, next_action = "CANCELLATION_REQUESTED", "RECONCILE"
    elif queue_expired:
        state, next_action = "QUEUE_EXPIRED", "REQUEST_CANCEL"
    else:
        state, next_action = execution_status, "POLL_STATUS"

    return {
        "schema": JOB_STATE_SCHEMA,
        "request": {"request_id": row.request_id, "request_digest": row.request_digest,
                    "target": _identifier(descriptor.get("target"))},
        "state": state,
        "provider": {"job_id": provider_job_id, "status": provider_status},
        "execution": {"status": execution_status, "outcome": outcome, "terminal": terminal},
        "accounting": {"status": accounting_status},
        "usage": {"reservation_status": reservation_status, "held_quantum_seconds": held,
                  "settled_quantum_seconds": settled},
        "cancellation": {"requested": requested, "requested_at": requested_at,
                         "confirmed": confirmed, "confirmed_at": confirmed_at if confirmed else None},
        "deadlines": {"queue_at": queue_at, "queue_expired": queue_expired,
                      "accounting_at": accounting_at, "accounting_expired": accounting_expired},
        "next_action": next_action,
    }


__all__ = ["JOB_STATE_SCHEMA", "NEXT_ACTIONS", "job_state", "lifecycle_error"]
