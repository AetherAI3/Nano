"""Opt-in native prepare/sample routes on the existing q-search service.

``POST /qsearch/native/v1/submit``, ``GET /qsearch/native/v1/requests/<digest>``
and ``POST .../<digest>/cancel`` answer only when the service config carries a
``NativeHostPolicy``; otherwise they are 404 ``NOT_ENABLED`` before
authentication. They reuse the service's request MAC and nonce store, durable
``requests`` rows, attempt claim, cancellation fence and the read-only
``nano.qsearch.job-state.v1`` projection. Only local runtimes are accepted;
nothing here can submit to a provider or spend QPU time.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
from collections.abc import Callable, Mapping
from typing import Any

from nano.quantum import QuantumValidationError, parse_result

from .canonical import canonical_json
from .lifecycle import job_state, lifecycle_error
from .native_policy import (NativeHostPolicy, NativeRefusal, check_admission, check_evidence,
                            load_native_policy, result_record, seal_native_result)
from .native_request import NativeRequest, is_native_request, parse_native_request
from .native_runtime import LOCAL_RUNTIME_KINDS, LocalNativeRuntime
from .store import RequestIdConflict, RequestRecord

NATIVE_SUBMIT_PATH = "/qsearch/native/v1/submit"
NATIVE_REQUEST_PREFIX = "/qsearch/native/v1/requests/"
_STATUS = re.compile(re.escape(NATIVE_REQUEST_PREFIX) + r"(sha256:[0-9a-f]{64})\Z")
_CANCEL = re.compile(re.escape(NATIVE_REQUEST_PREFIX) + r"(sha256:[0-9a-f]{64})/cancel\Z")
NATIVE_ENV = "NANO_QSEARCH_NATIVE"
NATIVE_POLICY_ENV = "NANO_QSEARCH_NATIVE_POLICY"
NOT_ENABLED = {"error": "not found", "code": "NOT_ENABLED"}
_STATUS_FOR_CODE = {"UNAVAILABLE_DEPENDENCY": 501}
RuntimeFactory = Callable[[], Any]


def _log(message: str) -> None:
    print(f"nano-qsearch: {message}", file=sys.stderr, flush=True)


def is_native_path(path: str) -> bool:
    return path == NATIVE_SUBMIT_PATH or bool(_STATUS.fullmatch(path) or _CANCEL.fullmatch(path))


def policy_from_env(environ: Mapping[str, str]) -> NativeHostPolicy | None:
    """``NANO_QSEARCH_NATIVE=1`` requires an explicit policy file; anything else leaves native disabled."""
    if environ.get(NATIVE_ENV) != "1":
        return None
    path = environ.get(NATIVE_POLICY_ENV)
    if not path:
        raise ValueError(f"{NATIVE_ENV}=1 requires {NATIVE_POLICY_ENV}")
    return load_native_policy(path)


def is_native_row(row: RequestRecord) -> bool:
    if '"nano.quantum.native-request.v1"' not in row.request_json:
        return False                     # cheap exclusion before parsing a possibly large body
    try:
        return is_native_request(json.loads(row.request_json))
    except (ValueError, RecursionError):
        return False


def _loads(body: bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate JSON key")
            out[key] = value
        return out

    def constant(name: str) -> None:
        raise ValueError(f"non-finite JSON constant {name}")

    return json.loads(body.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)


def _pending(code: str) -> tuple[int, dict[str, Any]]:
    return 503, {**lifecycle_error(code, retryable=True, next_action="POLL_STATUS"), "error": "job not completed"}


class NativeService:
    """Native routes bound to one ``QSearchService`` (its store, auth, clock, slots and flights)."""

    def __init__(self, host: Any, policy: NativeHostPolicy | None, runtime_factory: RuntimeFactory | None = None):
        if policy is not None and not isinstance(policy, NativeHostPolicy):
            raise ValueError("native_policy must be a NativeHostPolicy")
        self._host = host
        self.policy = policy
        self._factory = runtime_factory or LocalNativeRuntime
        self._runtime_instance: Any = None

    @property
    def enabled(self) -> bool:
        return self.policy is not None

    def _runtime(self) -> Any:
        if self._runtime_instance is None:
            runtime = self._factory()
            if getattr(runtime, "kind", None) not in LOCAL_RUNTIME_KINDS:
                raise NativeRefusal(501, "UNSUPPORTED_RUNTIME", "runtime")
            self._runtime_instance = runtime
        return self._runtime_instance

    def _auth(self, method: str, path: str, body: bytes, headers: Mapping[str, str | None],
              now: int | None) -> tuple[int, dict[str, Any]] | None:
        stamp = int(self._host._clock()) if now is None else now
        refusal = self._host._authenticate(method, path, body, headers, stamp)
        if refusal is None:
            return None
        return refusal[0], lifecycle_error("UNAUTHORIZED" if refusal[0] == 401 else "STORE_UNAVAILABLE",
                                           retryable=refusal[0] == 503)

    def _stored(self, digest: str) -> tuple[int, dict[str, Any]] | None:
        row = self._host.store.get_request(digest)
        if row is None or row.state != "DONE" or not is_native_row(row):
            return None
        return 200, json.loads(row.receipt_json)

    # --- submit ----------------------------------------------------------------------------------

    def submit(self, method: str, path: str, body: bytes, headers: Mapping[str, str | None], *,
               now: int | None = None) -> tuple[int, dict[str, Any]]:
        if not self.enabled:
            return 404, dict(NOT_ENABLED)
        if method != "POST" or path != NATIVE_SUBMIT_PATH:
            return 404, lifecycle_error("NOT_FOUND")
        if len(body) > self._host.config.max_body_bytes:
            return 413, lifecycle_error("BODY_TOO_LARGE", field="body")
        refusal = self._auth(method, path, body, headers, now)
        if refusal is not None:
            return refusal
        try:
            parsed = _loads(body)
            request = parse_native_request(parsed, admit=lambda d: check_admission(d, self.policy))
            check_evidence(request, self.policy)
            if request.artifact.resources["pub_count"] == 0:
                raise NativeRefusal(400, "PREPARATION_ONLY", "artifact.pubs")
            stored = self._stored(request.request_digest)
            if stored is not None:
                return stored
            row = self._host.store.get_request(request.request_digest)
            if row is None or row.attempts == 0:
                self._preflight(request)
        except NativeRefusal as exc:
            return exc.status, lifecycle_error(exc.code, field=exc.field)
        except QuantumValidationError as exc:
            return _STATUS_FOR_CODE.get(exc.code, 400), lifecycle_error(exc.code, field="body")
        except (ValueError, UnicodeDecodeError, RecursionError):
            return 400, lifecycle_error("JSON", field="body")
        except (sqlite3.Error, OSError):
            return 503, lifecycle_error("STORE_UNAVAILABLE", retryable=True, next_action="POLL_STATUS")
        return self._run_once(request, canonical_json(parsed))

    def _preflight(self, request: NativeRequest) -> None:
        """Load and re-verify the QPY while holding a concurrency slot, so parallel submits stay bounded."""
        runtime = self._runtime()
        if not self._host._slots.acquire(timeout=self._host.config.poll_interval_s):
            raise NativeRefusal(503, "SERVICE_BUSY", None)
        try:
            runtime.preflight(request.artifact, expected_digest=request.descriptor["artifact_digest"])
        finally:
            self._host._slots.release()

    def _run_once(self, request: NativeRequest, body_json: str, *,
                  recovering: bool = False) -> tuple[int, dict[str, Any]]:
        """Single-flight by request digest (shared with controls); execute at most once, store once."""
        host, digest = self._host, request.request_digest
        flight = "request:" + digest
        with host._lock:
            stored = self._stored(digest)
            if stored is not None:
                return stored
            if flight in host._inflight:
                return 409, lifecycle_error("REQUEST_BUSY", retryable=True, next_action="POLL_STATUS")
            waiter = host._inflight[flight] = threading.Event()
        try:
            row = host.store.begin_request(digest, request.request_id, body_json)
            if row.attempts > 0:
                return self._reattach(request)
            if recovering:
                return _pending("NOT_STARTED")      # startup recovery never starts execution
            return self._execute(request)
        except RequestIdConflict:
            return 409, lifecycle_error("REQUEST_ID_CONFLICT", field="descriptor.request_id")
        except NativeRefusal as exc:
            return exc.status, lifecycle_error(exc.code, field=exc.field)
        except (sqlite3.Error, OSError):
            return 503, lifecycle_error("STORE_UNAVAILABLE", retryable=True, next_action="POLL_STATUS")
        except (KeyError, ValueError, TypeError, QuantumValidationError) as exc:
            # A claimed attempt stays held (attempts > 0); it can never execute again.
            _log(f"native request {digest} state unavailable: {type(exc).__name__}")
            return 503, lifecycle_error("STATE_UNAVAILABLE", next_action="MANUAL_RECONCILIATION")
        finally:
            with host._lock:
                host._inflight.pop(flight, None)
            waiter.set()

    def _execute(self, request: NativeRequest) -> tuple[int, dict[str, Any]]:
        host, digest = self._host, request.request_digest
        runtime = self._runtime()
        queue_at = host._state(digest)["deadlines"]["queue_at"]
        if not host._slots.acquire(timeout=min(host.config.poll_interval_s, max(0.0, queue_at - host._clock()))):
            return _pending("NOT_STARTED")
        try:
            if not host.store.claim_request_attempt(digest, queue_at=queue_at):
                return _pending("NOT_STARTED")      # cancelled, queue-expired or already claimed: never run
            record = host.store.merge_request_record(digest, {"execution_started_at": host._clock(),
                                                              "runtime_kind": runtime.kind})
            try:
                observations = runtime.run(request.artifact, expected_digest=request.descriptor["artifact_digest"],
                                           record=record)
            except Exception as exc:  # noqa: BLE001 - a local failure is sealed FAILED by class/code only
                reason = f"local runtime: {getattr(exc, 'code', type(exc).__name__)}"
                return self._finish(request, runtime, "FAILED", reason=reason)
            return self._accept(request, runtime, observations)
        finally:
            host._slots.release()

    def _reattach(self, request: NativeRequest) -> tuple[int, dict[str, Any]]:
        """A used attempt is never executed again; only the runtime's own recovery may finish it."""
        runtime = self._runtime()
        try:
            observations = runtime.recover(request.artifact, record=self._host._stored_record(request.request_digest))
        except Exception as exc:  # noqa: BLE001 - recovery failure leaves the outcome unknown (held)
            _log(f"native request {request.request_digest} recovery failed: {type(exc).__name__}")
            observations = None
        return self._accept(request, runtime, observations)

    def _accept(self, request: NativeRequest, runtime: Any, observations: Any) -> tuple[int, dict[str, Any]]:
        if observations is None:
            self._host.store.merge_request_record(request.request_digest, {"outcome": "unknown"})
            return _pending("OUTCOME_UNKNOWN")
        try:
            parse_result(request.artifact.plan, observations)
            if (observations["optimization_verdict"] != "not_applicable"
                    or observations["delivery_verdict"] != "unverified"):
                raise ValueError("a local runtime does not decide verdicts")
        except (QuantumValidationError, ValueError, TypeError, KeyError):
            return self._finish(request, runtime, "FAILED", reason="result differs from the admitted PUB inventory")
        return self._finish(request, runtime, "OK", observations=observations)

    def _finish(self, request: NativeRequest, runtime: Any, status: str, *, observations: Any = None,
                reason: str | None = None) -> tuple[int, dict[str, Any]]:
        unsigned = result_record(request, status, runtime_kind=runtime.kind,
                                 qiskit_version=getattr(runtime, "qiskit_version", None),
                                 observations=observations, reason=reason)
        sealed = seal_native_result(unsigned, self._host.config.receipt_key)
        try:
            self._host.store.complete_request(request.request_digest,
                                              json.dumps(sealed, sort_keys=True, allow_nan=False))
        except ValueError:
            stored = self._stored(request.request_digest)
            if stored is None:
                raise
            return stored
        return 200, sealed

    # --- status and cancel -----------------------------------------------------------------------

    def _row(self, digest: str) -> RequestRecord | None:
        row = self._host.store.get_request(digest)
        return row if row is not None and is_native_row(row) else None

    def status(self, method: str, path: str, body: bytes, headers: Mapping[str, str | None], *,
               now: int | None = None) -> tuple[int, dict[str, Any]]:
        """Authenticated durable snapshot only; never constructs a runtime or changes a row."""
        if not self.enabled:
            return 404, dict(NOT_ENABLED)
        match = _STATUS.fullmatch(path)
        if method != "GET" or match is None:
            return 404, lifecycle_error("NOT_FOUND")
        if body:
            return 400, lifecycle_error("EMPTY_BODY_REQUIRED", field="body")
        refusal = self._auth(method, path, body, headers, now)
        if refusal is not None:
            return refusal
        host = self._host
        try:
            row = self._row(match[1])
            if row is None:
                return 404, lifecycle_error("REQUEST_NOT_FOUND", field="request_digest")
            raw = host.store.request_record(row.request_digest)
            record = json.loads(raw) if raw else {}
            receipt = json.loads(row.receipt_json) if row.receipt_json else None
            if not isinstance(record, dict) or (receipt is not None and not isinstance(receipt, dict)):
                raise ValueError("invalid persisted snapshot")
            return 200, job_state(row, record, receipt, now=host._clock(), queue_deadline_s=host.config.queue_deadline_s,
                                  accounting_deadline_s=host.config.accounting_window_s,
                                  receipt_key=host.config.receipt_key)
        except (sqlite3.Error, OSError):
            return 503, lifecycle_error("STORE_UNAVAILABLE", retryable=True)
        except (ValueError, TypeError):
            return 503, lifecycle_error("STATE_UNAVAILABLE", next_action="MANUAL_RECONCILIATION")

    def cancel(self, method: str, path: str, body: bytes, headers: Mapping[str, str | None], *,
               now: int | None = None) -> tuple[int, dict[str, Any]]:
        """Persist the fence first; seal NOT_RUN only before a claim; a running/finished result wins."""
        if not self.enabled:
            return 404, dict(NOT_ENABLED)
        match = _CANCEL.fullmatch(path)
        if method != "POST" or match is None:
            return 404, lifecycle_error("NOT_FOUND")
        if body:
            return 400, lifecycle_error("EMPTY_BODY_REQUIRED", field="body")
        refusal = self._auth(method, path, body, headers, now)
        if refusal is not None:
            return refusal
        host, digest = self._host, match[1]
        flight, owner = "request:" + digest, False
        try:
            if self._row(digest) is None:
                return 404, lifecycle_error("REQUEST_NOT_FOUND", field="request_digest")
            host.store.request_event(digest, "cancellation_requested")
            with host._lock:
                if flight in host._inflight:
                    return 409, lifecycle_error("REQUEST_BUSY", retryable=True, next_action="POLL_STATUS")
                waiter = host._inflight[flight] = threading.Event()
                owner = True
            row = host.store.get_request(digest)
            if row.state != "DONE" and row.attempts == 0:
                request = parse_native_request(json.loads(row.request_json))
                self._finish(request, self._runtime(), "NOT_RUN", reason="cancelled before execution")
            return 200, host._state(digest)
        except NativeRefusal as exc:
            return exc.status, lifecycle_error(exc.code, field=exc.field)
        except (sqlite3.Error, OSError):
            return 503, lifecycle_error("STORE_UNAVAILABLE", retryable=True, next_action="RECONCILE")
        except (QuantumValidationError, ValueError, TypeError):
            return 503, lifecycle_error("STATE_UNAVAILABLE", next_action="MANUAL_RECONCILIATION")
        finally:
            if owner:
                with host._lock:
                    host._inflight.pop(flight, None)
                waiter.set()

    def recover(self) -> list[str]:
        """Re-attach claimed native rows after a restart; unclaimed rows wait for resubmission."""
        if not self.enabled:
            return []
        completed = []
        for row in self._host.store.pending_requests():
            if not is_native_row(row) or row.attempts == 0:
                continue
            try:
                request = parse_native_request(json.loads(row.request_json))
            except (QuantumValidationError, ValueError, RecursionError) as exc:
                _log(f"pending native request {row.request_digest} is unreadable and stays held: {type(exc).__name__}")
                continue
            status, _ = self._run_once(request, row.request_json, recovering=True)
            if status == 200:
                completed.append(request.request_digest)
        return completed


__all__ = ["NATIVE_ENV", "NATIVE_POLICY_ENV", "NATIVE_REQUEST_PREFIX", "NATIVE_SUBMIT_PATH", "NOT_ENABLED",
           "NativeService", "is_native_path", "is_native_row", "policy_from_env"]
