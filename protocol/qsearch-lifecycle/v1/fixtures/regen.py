#!/usr/bin/env python3
"""Regenerate (or ``--check``) the ``nano.qsearch.lifecycle-wire.v1`` golden fixtures.

Every byte here comes from the actual Python implementation, never from hand
written expectations:

* ``signing.v1.json``: ``nano_qsearch.client.LifecycleClient.status`` run
  against a capturing fake transport; the exact URL and headers it sent are
  recorded and the MAC is re-derived from the CONTRACT.md framing.
* ``status-histories.v1.json``: ``QSearchService.status_request`` on a
  temporary durable store with an injected clock, reached through
  ``LifecycleClient`` and serialised exactly like ``_Handler._send``
  (``json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")``).
  Store rows, run records and sealed receipts are seeded the way the existing
  lifecycle tests seed them. ``confirmed_zero`` uses the real hardware seal and
  the real ``receipt_v2.confirmed_zero_receipt`` predicate (no monkeypatch).
* ``errors.v1.json``: real service error responses plus synthetic mapping
  cases; ``expected_error`` is what ``LifecycleClient`` raised for those bytes.
* ``negative.v1.json``: refusals the contract mandates. ``expected_error`` is
  the contract outcome; ``python_client`` is ``"agrees"`` when the Python
  client raises exactly that, and ``"accepts"`` when it accepts the bytes
  instead (returns a payload, or maps a body the contract refuses as a remote
  error). Any other Python outcome aborts generation.
* ``../SHA256SUMS``: ``sha256sum`` lines for every file in ``v1/`` but itself.

Stdlib plus ``nano_qsearch`` only: no network, Qiskit or ATLAS. Keys are
synthetic. Output is ASCII JSON (``indent=1, sort_keys=True``), LF, with a
trailing newline. ``--check`` exits 1 when a committed file differs.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent
V1 = FIXTURES.parent
REPO = V1.parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from nano_qsearch.auth import AUTH_HEADER, MemoryNonceStore, verify_request  # noqa: E402
from nano_qsearch.canonical import canonical_digest  # noqa: E402
from nano_qsearch.client import _MAX_RESPONSE_BYTES, LifecycleClient, LifecycleClientError  # noqa: E402
from nano_qsearch.receipt_v2 import not_run_v2, request_receipt, seal_receipt_v2  # noqa: E402
from nano_qsearch.request import parse_request  # noqa: E402
from nano_qsearch.service import QSearchService, ServiceConfig  # noqa: E402

CONTRACT = "nano.qsearch.lifecycle-wire.v1"
ORIGIN = "http://127.0.0.1:8798"
PREFIX = "/qsearch/v2/requests/"
RECEIPT_KEY = b"nano86-synthetic-receipt-key-01"
REQUEST_KEY = b"nano86-synthetic-request-key-01"
WRONG_REQUEST_KEY = b"nano86-synthetic-wrong-key-0001"
MANIFEST = "sha256:" + "1" * 64
BATCH = "sha256:" + "2" * 64
MINIMUM_DISPLAY = "1101"
INVALID = {"code": "INVALID_RESPONSE", "retryable": False, "next_action": "MANUAL_RECONCILIATION"}

# The tests/qsearch_fixtures/job_4var.json job, embedded so this script needs no test helpers.
JOB_4VAR: dict[str, Any] = {
    "schema": "predator.qsearch.job.v1",
    "compiled_search_digest": "sha256:a8f06cba3ed022dd2fd4efc1085510502517152067653b417fcf5bd50b1bc26d",
    "variables": ["c0", "c1", "c2", "c3"],
    "qubo": {"q": [[-1.0, 0.45, 0.35, 0.0], [0.45, -0.8, 0.25, 0.0], [0.35, 0.25, -0.6, -0.15],
                   [0.0, 0.0, -0.15, -0.4]], "offset": 0.0, "convention": "symmetric_half"},
    "compile": {"variant": "auto", "p": 1, "gammas": [0.4], "betas": [0.4], "opt_level": 3, "seed": 11,
                "target": "heron_heavy_hex", "training": "none"},
    "backend": {"kind": "auto", "chi_max": 64, "shots": 1024, "seed": 7},
    "limits": {"max_variables": 40, "max_wall_s": 120, "max_qpu_s": 0},
    "audit": {"classical_min_energy": -1.6},
}


# --- request bodies and fake ATLAS receipts (mirrors tests/qsearch_v2_support.py) -----------------

def _job(target: str, seed: int) -> dict[str, Any]:
    job = copy.deepcopy(JOB_4VAR)
    job["compile"]["target"] = target
    job["backend"].update(kind="ibm", shots=8, seed=seed)
    job["limits"]["max_qpu_s"] = 5
    return {**job, "job_digest": canonical_digest(job)}


def request_body(request_id: str, *, target: str = "ibm_fez") -> dict[str, Any]:
    jobs = [_job(target, 7)]
    blocks = [{"pub_index": i, "job_digest": job["job_digest"],
               "recipients": [{"agent_id": "agent-007", "chunk": i, "slot": 0}],
               "n": len(job["variables"]), "shots": job["backend"]["shots"], "seed": job["backend"]["seed"],
               "classical_min_energy": job["audit"]["classical_min_energy"]} for i, job in enumerate(jobs)]
    descriptor = {"schema": "predator.qsearch.request.v2", "request_id": request_id, "target": target,
                  "purpose": "qcomms", "max_execution_time": 5, "manifest_digest": MANIFEST, "batch_index": 0,
                  "batch_digest": BATCH, "blocks": blocks}
    return {"schema": "predator.qsearch.request.v2", "descriptor": descriptor, "jobs": jobs}


def atlas_receipt(request: Any, *, usage: Any = None, job_id: str) -> dict[str, Any]:
    dry = request.target == "fake_fez"
    samples = [[MINIMUM_DISPLAY] * (job.shots - 1) + ["0000"] for job in request.jobs]
    children = ["sha256:" + format(i + 10, "x").rjust(64, "0") for i in range(request.block_count)]
    envelope = {"schema": "AqrcQSearchRequestV1", "request_id": request.request_id, "purpose": request.purpose,
                "target": request.target, "children": children, "shots": [job.shots for job in request.jobs],
                "pub_count": request.block_count, "max_execution_time": request.max_execution_time,
                "recovery_tag": f"atlas-qsearch-req-{request.request_id}"}
    receipt = {"schema": "AqrcQSearchRequestReceiptV1", "request": {"envelope": envelope, "request_sha256": "x"},
               "purpose": request.purpose, "target": request.target, "backend_name": request.target,
               "dry_run": dry, "job_id": job_id, "status": "DONE", "pub_count": request.block_count,
               "max_execution_time": request.max_execution_time, "job_tags": [envelope["recovery_tag"]],
               "pubs": [{"pub_index": i, "register": "meas", "bit_order": "msb_first", "shots": len(raw),
                         "raw_samples": list(raw), "child_sha256": children[i]} for i, raw in enumerate(samples)]}
    if not dry:
        receipt.update(provider_final_usage_seconds=usage, usage_pending=not (usage and usage > 0),
                       provider_metrics={"usage": {"quantum_seconds": usage}})
    return receipt


def _sealed(request: Any, atlas: dict[str, Any]) -> dict[str, Any]:
    return seal_receipt_v2(request_receipt(request, atlas, purpose=request.purpose), RECEIPT_KEY)


def ok_receipt(*, usage: Any, job_id: str):
    return lambda request: _sealed(request, atlas_receipt(request, usage=usage, job_id=job_id))


def failed_pending_receipt(*, job_id: str):
    def build(request: Any) -> dict[str, Any]:
        atlas = atlas_receipt(request, usage=None, job_id=job_id)
        atlas.update(status="FAILED", pubs=[], provider_status="ERROR", reason="provider job ended ERROR")
        return _sealed(request, atlas)
    return build


def confirmed_zero_receipt(*, job_id: str, status: str = "CANCELLED"):
    """The ATLAS zero transition of tests/test_qsearch_confirmed_zero.py, sealed with the hardware key."""
    def build(request: Any) -> dict[str, Any]:
        atlas = atlas_receipt(request, usage=0, job_id=job_id)
        atlas.update(status="FAILED", pubs=[], provider_status="ERROR", reason="provider job ended ERROR")
        envelope = atlas["request"]["envelope"]
        envelope["client_request_digest"] = request.request_digest
        fingerprint = hashlib.sha256(json.dumps(envelope, sort_keys=True, separators=(",", ":"))
                                     .encode()).hexdigest()
        atlas["request"]["request_sha256"] = fingerprint
        tags = [envelope["recovery_tag"], "atlas-ledger-" + "a" * 16]
        proof = {"schema": "AqrcQSearchZeroUsageEvidenceV1", "ledger_id": "a" * 32,
                 "request_id": request.request_id, "request_sha256": fingerprint,
                 "client_request_digest": request.request_digest, "child_sha": fingerprint,
                 "plan_sha": fingerprint, "job_id": job_id, "instance": "nano86-synthetic-instance",
                 "backend_name": request.target, "job_tags": tags, "provider_status": status,
                 "usage_status": "complete", "evidence_sha256": "sha256:" + "b" * 64,
                 "checked_at": "2026-11-01T12:00:00+00:00", "provider_api_version": "2026-04-15",
                 "evidence_source": "ibm-runtime-job-metrics"}
        atlas.update(provider_status=status, job_tags=tags, usage_pending=False,
                     accounting_outcome="cancelled_no_usage", usage_final_status="complete",
                     zero_usage_evidence=proof)
        sealed = _sealed(request, atlas)
        if sealed["usage"].get("zero_usage_evidence") is None:
            raise RuntimeError("confirmed-zero evidence was not projected into the sealed receipt")
        return sealed
    return build


def not_run_receipt(request: Any) -> dict[str, Any]:
    return seal_receipt_v2(not_run_v2(request, "nano86 synthetic: never submitted"), RECEIPT_KEY)


# --- service plumbing --------------------------------------------------------------------------

class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Nonces:
    def __init__(self, start: int) -> None:
        self.next = start

    def __call__(self) -> str:
        self.next += 1
        return f"{self.next:032x}"


def _forbidden_runtime(kind: str) -> Any:
    raise AssertionError("a status read constructed a provider runtime")


def make_service(data_dir: Path, clock: Clock, *, v2_enabled: bool = True) -> QSearchService:
    config = ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=data_dir, receipt_key=RECEIPT_KEY,
                           request_key=REQUEST_KEY, v2_enabled=v2_enabled)
    return QSearchService(config, runtime_factory=_forbidden_runtime, clock=clock)


def send_bytes(payload: dict[str, Any]) -> bytes:
    """Exactly ``nano_qsearch.service._Handler._send``."""
    return json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")


class ServiceTransport:
    """``LifecycleClient`` transport that answers through ``QSearchService.status_request``."""

    def __init__(self, service: QSearchService) -> None:
        self.service = service
        self.last: tuple[int, bytes] | None = None

    def __call__(self, method: str, url: str, body: bytes, headers: dict[str, str], timeout_s: float):
        if not url.startswith(ORIGIN + PREFIX):
            raise AssertionError("client contacted an unexpected URL")
        status, payload = self.service.status_request(method, url[len(ORIGIN):], body,
                                                      {AUTH_HEADER: headers.get(AUTH_HEADER)})
        self.last = (status, send_bytes(payload))
        return self.last


def replay(status: int, raw: bytes):
    return lambda method, url, body, headers, timeout_s: (status, raw)


def run_client(transport, digest: str, *, key: bytes = REQUEST_KEY, clock: Any = None,
               nonces: Any = None) -> tuple[dict[str, Any] | None, LifecycleClientError | None]:
    client = LifecycleClient(ORIGIN, key, transport=transport, clock=clock or (lambda: 1000),
                             nonce_factory=nonces or Nonces(0x5000))
    try:
        return client.status(digest), None
    except LifecycleClientError as exc:
        return None, exc


def error_fields(exc: LifecycleClientError) -> dict[str, Any]:
    return {"code": exc.code, "field": exc.field, "retryable": exc.retryable,
            "next_action": exc.next_action, "http_status": exc.http_status}


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def seed(store: Any, clock: Clock, request_id: str, *, target: str = "ibm_fez", attempts: int = 1,
         record: dict[str, Any] | None = None, receipt: Any = None, accepted_at: float = 1000.0) -> Any:
    body = request_body(request_id, target=target)
    request = parse_request(body)
    clock.now = accepted_at
    store.begin_request(request.request_digest, request.request_id, json.dumps(body))
    for _ in range(attempts):
        store.mark_request_attempt(request.request_digest)
    if receipt is not None:
        store.complete_request(request.request_digest, json.dumps(receipt(request)) if callable(receipt) else receipt)
    if record is not None:
        store.set_request_record(request.request_digest, json.dumps(record))
    return request


# --- status histories --------------------------------------------------------------------------

# (name, request_id, seed keyword arguments, read time)
HISTORY_SPECS: list[tuple[str, str, dict[str, Any], float]] = [
    ("queued", "nano86-queued",
     {"record": {"provider_job_id": "nano86-job-queued", "provider_status": "QUEUED", "held_qpu_s": 10}}, 1010.0),
    ("running", "nano86-running",
     {"record": {"provider_job_id": "nano86-job-running", "provider_status": "RUNNING",
                 "queue_started_at": 1005.5}}, 1100.0),
    ("completed_unknown_outcome", "nano86-completed",
     {"record": {"provider_job_id": "nano86-job-done", "provider_status": "DONE", "held_qpu_s": 7.5}}, 1200.0),
    ("failed", "nano86-failed",
     {"receipt": failed_pending_receipt(job_id="nano86-job-failed"),
      "record": {"accounting_pending_since": 1100.0, "held_qpu_s": 5}}, 1300.0),
    ("cancelled_confirmed", "nano86-cancelled",
     {"record": {"provider_job_id": "nano86-job-cancelled", "provider_status": "CANCELLED",
                 "cancellation_requested_at": 1200.0, "cancellation_confirmed_at": 1260.5,
                 "accounting": "settled", "quantum_seconds": 1.5}}, 1400.0),
    ("accounting_pending", "nano86-acct-pending",
     {"receipt": ok_receipt(usage=None, job_id="nano86-job-acct-pending"),
      "record": {"accounting_pending_since": 1100.0, "held_qpu_s": 5}}, 1500.0),
    ("accounting_expired", "nano86-acct-expired",
     {"record": {"provider_job_id": "nano86-job-acct-expired", "provider_status": "DONE",
                 "accounting_pending_since": 1100.0}}, 2500.0),
    ("settled_positive", "nano86-settled",
     {"receipt": ok_receipt(usage=2.5, job_id="nano86-job-settled")}, 1600.0),
    ("confirmed_zero", "nano86-confirmed-zero",
     {"receipt": confirmed_zero_receipt(job_id="nano86-job-zero"),
      "record": {"provider_status": "QUEUED", "held_qpu_s": 10}}, 1700.0),
    ("unknown_zero_held", "nano86-unknown-zero",
     {"record": {"provider_job_id": "nano86-job-unknown-zero", "provider_status": "CANCELLED",
                 "accounting": "settled", "quantum_seconds": 0, "settled_qpu_s": 0, "held_qpu_s": 10}}, 1800.0),
    ("queue_expired", "nano86-queue-expired", {"attempts": 0, "accepted_at": 1000.5}, 5000.0),
    ("cancellation_requested_not_terminal", "nano86-cancel-requested",
     {"record": {"provider_job_id": "nano86-job-cancel-requested", "provider_status": "RUNNING",
                 "cancellation_requested_at": 1150.25}}, 1900.0),
    ("not_run", "nano86-not-run", {"receipt": not_run_receipt}, 2000.0),
    ("dry_run", "nano86-dry-run",
     {"target": "fake_fez", "receipt": ok_receipt(usage=None, job_id="nano86-job-dry-run")}, 2100.0),
]


def seed_histories(service: QSearchService, clock: Clock) -> list[tuple[str, Any, float]]:
    """Seed every history on ``service``; return ``(name, parsed request, read time)``."""
    store = service.store
    reads: list[tuple[str, Any, float]] = []
    for name, request_id, kwargs, read_at in HISTORY_SPECS:
        reads.append((name, seed(store, clock, request_id, **kwargs), read_at))
    # Replacement lineage: an original with authenticated confirmed zero, then its linked successor.
    parent = seed(store, clock, "nano86-replaced-original", receipt=confirmed_zero_receipt(job_id="nano86-job-parent"))
    child_body = request_body("nano86-replacement", target="ibm_kingston")
    child = parse_request(child_body)
    clock.now = 2200.0
    store.link_replacement(parent.request_digest, child.request_digest, child.request_id, json.dumps(child_body))
    reads.append(("replacement_lineage", child, 2300.0))
    return reads


def read_histories(service: QSearchService, clock: Clock,
                   reads: list[tuple[str, Any, float]], nonces: Nonces) -> list[dict[str, Any]]:
    histories = []
    for name, request, read_at in reads:
        clock.now = read_at
        transport = ServiceTransport(service)
        payload, error = run_client(transport, request.request_digest, clock=clock, nonces=nonces)
        status, raw = transport.last
        if error is not None or status != 200:
            raise RuntimeError(f"history {name} did not read as a 200 envelope")
        expected = json.loads(raw)
        if payload != expected:
            raise RuntimeError(f"history {name}: client result differs from the service body")
        histories.append({"name": name, "request_digest": request.request_digest, "http_status": status,
                          "body_b64": b64(raw), "body_sha256": hashlib.sha256(raw).hexdigest(),
                          "expected": expected})
    return histories


def history_key(expected: dict[str, Any]) -> tuple[Any, ...]:
    return (expected["state"], expected["execution"]["status"], expected["accounting"]["status"],
            expected["usage"]["reservation_status"], expected["usage"]["settled_quantum_seconds"])


def build_histories(service: QSearchService, clock: Clock) -> list[dict[str, Any]]:
    histories = read_histories(service, clock, seed_histories(service, clock), Nonces(0x86000))
    keys = [history_key(h["expected"]) for h in histories]
    if len(set(keys)) != len(keys):
        raise RuntimeError("histories are not pairwise distinct")
    return histories


# --- errors ------------------------------------------------------------------------------------

def _error_case(name: str, digest: str, status: int, raw: bytes, error: LifecycleClientError | None):
    if error is None:
        raise RuntimeError(f"error case {name} did not raise")
    expected = error_fields(error)
    if expected["http_status"] != status:
        raise RuntimeError(f"error case {name}: http_status mismatch")
    return {"name": name, "request_digest": digest, "http_status": status, "body_b64": b64(raw),
            "expected_error": expected}


def _through_service(service: QSearchService, digest: str, nonces: Nonces, *, key: bytes = REQUEST_KEY,
                     clock: Clock):
    transport = ServiceTransport(service)
    _, error = run_client(transport, digest, key=key, clock=clock, nonces=nonces)
    status, raw = transport.last
    return status, raw, error


SYNTHETIC_ERRORS: list[tuple[str, int, dict[str, Any]]] = [
    ("synthetic_unknown_code", 409, {"error": "job lifecycle request failed", "code": "FUTURE_CODE",
                                     "field": "request_digest", "retryable": True, "next_action": "RECONCILE"}),
    ("synthetic_unknown_field", 400, {"error": "job lifecycle request failed", "code": "INVALID_REQUEST",
                                      "field": "descriptor.secret", "retryable": False, "next_action": "NONE"}),
    ("synthetic_unknown_next_action", 503, {"error": "job lifecycle request failed",
                                            "code": "PROVIDER_UNAVAILABLE", "field": None, "retryable": True,
                                            "next_action": "RETRY_LATER"}),
    ("synthetic_retryable_string", 503, {"error": "job lifecycle request failed", "code": "STORE_UNAVAILABLE",
                                         "field": None, "retryable": "true", "next_action": "POLL_STATUS"}),
    ("synthetic_minimal_error", 500, {"error": "internal"}),
    ("synthetic_extra_members", 429, {"error": "busy", "code": "LIFECYCLE_BUSY", "field": "jobs",
                                      "retryable": True, "next_action": "POLL_STATUS",
                                      "detail": {"token": "must-be-discarded"}}),
]


def build_errors(tmp: Path, service: QSearchService, clock: Clock, queued: str) -> list[dict[str, Any]]:
    cases = []
    nonces = Nonces(0x87000)
    clock.now = 3000.0

    disabled = make_service(tmp / "disabled", clock, v2_enabled=False)
    try:
        status, raw, error = _through_service(disabled, queued, nonces, clock=clock)
    finally:
        disabled.close()
    cases.append(_error_case("not_enabled", queued, status, raw, error))

    status, raw, error = _through_service(service, queued, nonces, key=WRONG_REQUEST_KEY, clock=clock)
    cases.append(_error_case("unauthorized", queued, status, raw, error))

    missing = "sha256:" + hashlib.sha256(b"nano86-missing-request").hexdigest()
    status, raw, error = _through_service(service, missing, nonces, clock=clock)
    cases.append(_error_case("request_not_found", missing, status, raw, error))

    def unavailable(_digest: str) -> Any:
        raise sqlite3.OperationalError("synthetic store failure")
    service.store.get_request = unavailable
    try:
        status, raw, error = _through_service(service, queued, nonces, clock=clock)
    finally:
        del service.store.get_request
    cases.append(_error_case("store_unavailable", queued, status, raw, error))

    corrupt = seed(service.store, clock, "nano86-corrupt-snapshot", receipt="{corrupted-receipt")
    clock.now = 3000.0
    status, raw, error = _through_service(service, corrupt.request_digest, nonces, clock=clock)
    cases.append(_error_case("state_unavailable", corrupt.request_digest, status, raw, error))

    # The client never sends a body; this is the real service answer to one, mapped by the client.
    path = PREFIX + queued
    headers = {AUTH_HEADER: _sign(REQUEST_KEY, path, ts=3000, nonce=nonces())}
    status, payload = service.status_request("GET", path, b"{}", headers)
    raw = send_bytes(payload)
    cases.append(_error_case("empty_body_required", queued, status, raw, run_client(replay(status, raw), queued)[1]))

    for name, status, body in SYNTHETIC_ERRORS:
        raw = send_bytes(body)
        cases.append(_error_case(name, queued, status, raw, run_client(replay(status, raw), queued)[1]))
    return cases


# --- negatives ---------------------------------------------------------------------------------

SENTINEL = "__NANO86_LEXEME__"


def _get(value: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        value = value[key]
    return value


def _mutate(base: dict[str, Any], path: tuple[str, ...], value: Any = None, *, delete: bool = False) -> bytes:
    out = copy.deepcopy(base)
    target = _get(out, path[:-1])
    if delete:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return send_bytes(out)


def _lexeme(base: dict[str, Any], path: tuple[str, ...], lexeme: str) -> bytes:
    raw = _mutate(base, path, SENTINEL)
    marker = json.dumps(SENTINEL).encode()
    if raw.count(marker) != 1:
        raise RuntimeError("sentinel substitution is ambiguous")
    return raw.replace(marker, lexeme.encode())


def _duplicate(base: dict[str, Any], path: tuple[str, ...], first: Any) -> bytes:
    """Insert ``"<key>": first`` before the existing member so the LAST duplicate is the valid one."""
    raw = _mutate(base, path, SENTINEL)
    marker = (json.dumps(path[-1]) + ": " + json.dumps(SENTINEL)).encode()
    if raw.count(marker) != 1:
        raise RuntimeError("duplicate substitution is ambiguous")
    duplicate = json.dumps(path[-1]) + ": " + json.dumps(first)
    original = json.dumps(path[-1]) + ": " + json.dumps(_get(base, path))
    return raw.replace(marker, (duplicate + ", " + original).encode())


def _invalid(status: int | None = 200) -> dict[str, Any]:
    return {**INVALID, **({"http_status": status} if status is not None else {})}


def _redirect(status: int) -> dict[str, Any]:
    return {"code": "UNEXPECTED_REDIRECT", "retryable": False, "next_action": "NONE", "http_status": status}


def negative_specs(h: dict[str, dict[str, Any]]) -> list[tuple[str, str, int, bytes, int, dict[str, Any]]]:
    """(name, base history, http status, body, trailing pad, contract-expected error)."""
    q = h["queued"]
    held = ("usage", "held_quantum_seconds")
    deep: Any = 0
    for _ in range(80):
        deep = [deep]
    err = {"error": "job lifecycle request failed", "code": "STORE_UNAVAILABLE", "field": None,
           "retryable": True, "next_action": "NONE"}
    err_dup = send_bytes(err).replace(b'"code": "STORE_UNAVAILABLE"',
                                      b'"code": "UNAUTHORIZED", "code": "STORE_UNAVAILABLE"')
    cancel_req = h["cancellation_requested_not_terminal"]
    confirmed_early = {**cancel_req, "cancellation": {**cancel_req["cancellation"], "confirmed": True,
                                                      "confirmed_at": 1950.0}}
    oversized = send_bytes(q)
    return [
        ("wrong_request_digest", "queued", 200, _mutate(q, ("request", "request_digest"), "sha256:" + "f" * 64), 0,
         _invalid()),
        ("request_digest_not_string", "queued", 200, _mutate(q, ("request", "request_digest"), 1), 0, _invalid()),
        ("wrong_schema_version", "queued", 200, _mutate(q, ("schema",), "nano.qsearch.job-state.v2"), 0, _invalid()),
        ("schema_missing", "queued", 200, _mutate(q, ("schema",), delete=True), 0, _invalid()),
        ("next_action_unknown", "queued", 200, _mutate(q, ("next_action",), "RETRY"), 0, _invalid()),
        ("next_action_missing", "queued", 200, _mutate(q, ("next_action",), delete=True), 0, _invalid()),
        ("missing_member_cancellation", "queued", 200, _mutate(q, ("cancellation",), delete=True), 0, _invalid()),
        ("missing_member_usage_settled", "queued", 200,
         _mutate(q, ("usage", "settled_quantum_seconds"), delete=True), 0, _invalid()),
        ("extra_member_top_level", "queued", 200, _mutate(q, ("success",), True), 0, _invalid()),
        ("extra_member_nested", "queued", 200, _mutate(q, ("usage", "credit_quantum_seconds"), 0), 0, _invalid()),
        ("wrong_type_state_number", "queued", 200, _mutate(q, ("state",), 3), 0, _invalid()),
        ("wrong_type_terminal_string", "queued", 200, _mutate(q, ("execution", "terminal"), "false"), 0, _invalid()),
        ("bool_as_number", "queued", 200, _mutate(q, held, True), 0, _invalid()),
        ("number_as_bool", "queued", 200, _mutate(q, ("deadlines", "queue_expired"), 0), 0, _invalid()),
        ("null_not_allowed", "queued", 200, _mutate(q, ("deadlines", "queue_at"), None), 0, _invalid()),
        ("negative_amount", "queued", 200, _mutate(q, held, -1.0), 0, _invalid()),
        ("unknown_state_enum", "queued", 200, _mutate(q, ("state",), "PAUSED"), 0, _invalid()),
        ("unknown_provider_status_enum", "queued", 200, _mutate(q, ("provider", "status"), "SLEEPING"), 0,
         _invalid()),
        ("lexeme_nan", "queued", 200, _lexeme(q, held, "NaN"), 0, _invalid()),
        ("lexeme_infinity", "queued", 200, _lexeme(q, held, "Infinity"), 0, _invalid()),
        ("lexeme_negative_infinity", "queued", 200, _lexeme(q, held, "-Infinity"), 0, _invalid()),
        ("number_overflows_double", "queued", 200, _lexeme(q, held, "1e400"), 0, _invalid()),
        ("unsafe_integer", "queued", 200, _lexeme(q, held, "9007199254740993"), 0, _invalid()),
        ("duplicate_key_top_level", "queued", 200, _duplicate(q, ("state",), "RUNNING"), 0, _invalid()),
        ("duplicate_key_nested", "queued", 200, _duplicate(q, ("usage", "settled_quantum_seconds"), 0), 0,
         _invalid()),
        ("invariant_i1_settled_state", "settled_positive", 200,
         _mutate(h["settled_positive"], ("state",), "ACCOUNTING_PENDING"), 0, _invalid()),
        ("invariant_i2_not_applicable_reserved", "dry_run", 200,
         _mutate(h["dry_run"], ("usage", "reservation_status"), "HELD"), 0, _invalid()),
        ("invariant_i3_pending_with_settled", "queued", 200, _mutate(q, ("usage", "settled_quantum_seconds"), 1.0),
         0, _invalid()),
        ("invariant_i4_nonterminal_outcome", "queued", 200, _mutate(q, ("execution", "outcome"), "SUCCEEDED"), 0,
         _invalid()),
        ("invariant_i5_terminal_status", "queued", 200, _mutate(q, ("execution", "terminal"), True), 0, _invalid()),
        ("invariant_i6_cancelled_outcome", "cancelled_confirmed", 200,
         _mutate(h["cancelled_confirmed"], ("execution", "outcome"), "FAILED"), 0, _invalid()),
        ("invariant_i7_confirmed_without_cancelled", "cancellation_requested_not_terminal", 200,
         send_bytes(confirmed_early), 0, _invalid()),
        ("invariant_i8_requested_without_time", "queued", 200, _mutate(q, ("cancellation", "requested"), True), 0,
         _invalid()),
        ("malformed_json", "queued", 200, b'{"schema": ', 0, _invalid()),
        ("trailing_garbage", "queued", 200, send_bytes(q) + b" x", 0, _invalid()),
        ("empty_body", "queued", 200, b"", 0, _invalid()),
        ("invalid_utf8", "queued", 200, b'{"schema": "\xff"}', 0, _invalid()),
        ("encoding_utf8_bom", "queued", 200, b"\xef\xbb\xbf" + send_bytes(q), 0, _invalid()),
        ("encoding_utf16_le", "queued", 200, send_bytes(q).decode("ascii").encode("utf-16-le"), 0, _invalid()),
        ("encoding_utf32_be", "queued", 200, send_bytes(q).decode("ascii").encode("utf-32-be"), 0, _invalid()),
        ("top_level_array", "queued", 200, b"[]", 0, _invalid()),
        ("top_level_string", "queued", 200, b'"nano.qsearch.job-state.v1"', 0, _invalid()),
        ("top_level_null", "queued", 200, b"null", 0, _invalid()),
        ("oversized_body", "queued", 200, oversized, _MAX_RESPONSE_BYTES + 1 - len(oversized), _invalid()),
        ("redirect_302", "queued", 302, send_bytes(q), 0, _redirect(302)),
        ("redirect_308_empty_body", "queued", 308, b"", 0, _redirect(308)),
        ("status_99", "queued", 99, send_bytes(q), 0, _invalid(None)),
        ("status_600", "queued", 600, send_bytes(q), 0, _invalid(None)),
        ("error_body_top_level_array", "queued", 500, b"[]", 0, _invalid(500)),
        ("error_body_lexeme_nan", "queued", 503, _lexeme({**err, "retry_after": 0}, ("retry_after",), "NaN"), 0,
         _invalid(503)),
        ("error_body_duplicate_key", "queued", 503, err_dup, 0, _invalid(503)),
        ("error_body_unsafe_integer", "queued", 503,
         _lexeme({**err, "retry_after": 0}, ("retry_after",), "9007199254740993"), 0, _invalid(503)),
        ("error_body_depth_exceeds_64", "queued", 503, send_bytes({**err, "detail": deep}), 0, _invalid(503)),
    ]


def python_outcome(name: str, status: int, raw: bytes, digest: str, expected: dict[str, Any]) -> str:
    _, error = run_client(replay(status, raw), digest)
    if error is None:
        return "accepts"
    actual = {key: value for key, value in error_fields(error).items()
              if not (key in ("field", "http_status") and value is None)}
    if actual == expected:
        return "agrees"
    if expected["code"] == "INVALID_RESPONSE" and error.code != "INVALID_RESPONSE" and status >= 400:
        return "accepts"      # mapped as a remote error body although the contract refuses the bytes
    raise RuntimeError(f"negative {name}: unexpected Python outcome {actual}")


def build_negatives(histories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {h["name"]: h["expected"] for h in histories}
    digests = {h["name"]: h["request_digest"] for h in histories}
    cases = []
    for name, base, status, raw, pad, expected in negative_specs(by_name):
        digest = digests[base]
        cases.append({"name": name, "request_digest": digest, "http_status": status, "body_b64": b64(raw),
                      "pad_trailing_spaces": pad, "expected_error": expected,
                      "python_client": python_outcome(name, status, raw + b" " * pad, digest, expected)})
    return cases


# --- signing -----------------------------------------------------------------------------------

def _message(path: str, ts: int, nonce: str) -> bytes:
    return "\n".join(("GET", path, hashlib.sha256(b"").hexdigest(), str(ts), nonce)).encode("utf-8")


def _sign(key: bytes, path: str, *, ts: int, nonce: str) -> str:
    return f"v1 ts={ts} nonce={nonce} mac={hmac.new(key, _message(path, ts, nonce), hashlib.sha256).hexdigest()}"


SIGNING_SPECS: list[tuple[str, str, bytes, Any, str]] = [
    ("key16_fractional_clock", "http://127.0.0.1:8798", b"nano86-synth-k16", 1700000000.9, "0123456789abcdef" * 2),
    ("key32_trailing_slash_nonce64", "https://qsearch.example.test/", b"nano86-synthetic-request-key-032",
     1000, "fedcba9876543210" * 4),
    ("key31_localhost_slash_nonce16", "http://localhost:8798/", REQUEST_KEY, 1730000000, "a1b2c3d4e5f60718"),
    ("key64_ipv6_origin_sub_second_clock", "https://[::1]:8443", bytes(range(64)), 0.5, "00" * 16),
    ("key16_far_clock_nonce40", "https://qsearch.example.test", b"\xff" * 16, 4102444800.25, "9" * 40),
]


def build_signing() -> list[dict[str, Any]]:
    vectors = []
    for name, base_url, key, clock, nonce in SIGNING_SPECS:
        digest = "sha256:" + hashlib.sha256(name.encode()).hexdigest()
        sent: dict[str, Any] = {}

        def transport(method, url, body, headers, timeout_s, sent=sent):
            sent.update(method=method, url=url, body=body, headers=dict(headers))
            return 503, b'{"error": "capture only"}'

        client = LifecycleClient(base_url, key, transport=transport, clock=lambda clock=clock: clock,
                                 nonce_factory=lambda nonce=nonce: nonce)
        try:
            client.status(digest)
        except LifecycleClientError:
            pass
        path = PREFIX + digest
        ts = int(clock)
        message = _message(path, ts, nonce)
        mac = hmac.new(key, message, hashlib.sha256).hexdigest()
        header = f"v1 ts={ts} nonce={nonce} mac={mac}"
        if (sent.get("method") != "GET" or sent.get("body") != b"" or sent["headers"].get(AUTH_HEADER) != header
                or sent["url"] != base_url.rstrip("/") + path):
            raise RuntimeError(f"signing vector {name} does not match the client")
        verify_request(key, header, "GET", path, b"", now=ts, nonce_store=MemoryNonceStore())
        vectors.append({"name": name, "base_url": base_url, "request_digest": digest, "clock": clock,
                        "key_hex": key.hex(), "method": "GET", "path": path, "url": sent["url"],
                        "body_sha256": hashlib.sha256(b"").hexdigest(), "ts": ts, "nonce": nonce,
                        "message_b64": b64(message), "mac_hex": mac, "header": header, "headers": sent["headers"]})
    return vectors


# --- output ------------------------------------------------------------------------------------

def render(value: Any) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


def build_fixtures() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="nano86-regen-", ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        clock = Clock()
        service = make_service(tmp_path / "service", clock)
        try:
            histories = build_histories(service, clock)
            queued = next(h["request_digest"] for h in histories if h["name"] == "queued")
            errors = build_errors(tmp_path, service, clock, queued)
        finally:
            service.close()
    return {
        "fixtures/signing.v1.json": render({"contract": CONTRACT, "vectors": build_signing()}),
        "fixtures/status-histories.v1.json": render({"contract": CONTRACT, "histories": histories}),
        "fixtures/errors.v1.json": render({"contract": CONTRACT, "cases": errors}),
        "fixtures/negative.v1.json": render({"contract": CONTRACT, "cases": build_negatives(histories)}),
    }


def hashed_files(generated: dict[str, bytes]) -> dict[str, bytes]:
    """Every file under v1/ except SHA256SUMS, bytecode and dot-paths (tool caches), with ``generated`` overriding disk content."""
    files = {}
    for path in sorted(V1.rglob("*")):
        rel = path.relative_to(V1).as_posix()
        parts = path.relative_to(V1).parts
        if (path.is_file() and rel != "SHA256SUMS" and path.suffix != ".pyc"
                and not any(part == "__pycache__" or part.startswith(".") for part in parts)):
            files[rel] = path.read_bytes()
    files.update(generated)
    return files


def sha256sums(files: dict[str, bytes]) -> bytes:
    return "".join(f"{hashlib.sha256(files[rel]).hexdigest()}  {rel}\n" for rel in sorted(files)).encode("ascii")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate the q-search lifecycle wire v1 fixtures.")
    parser.add_argument("--check", action="store_true", help="exit 1 if a committed file differs")
    args = parser.parse_args(argv)
    outputs = build_fixtures()
    outputs["SHA256SUMS"] = sha256sums(hashed_files(outputs))
    stale = [rel for rel, data in sorted(outputs.items())
             if not (V1 / rel).is_file() or (V1 / rel).read_bytes() != data]
    if args.check:
        for rel in stale:
            print(f"stale: {rel}", file=sys.stderr)
        return 1 if stale else 0
    for rel in stale:
        (V1 / rel).write_bytes(outputs[rel])
        print(f"wrote {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
