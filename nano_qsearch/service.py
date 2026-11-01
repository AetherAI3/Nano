"""The q-search HTTP service: ``POST /qsearch/v1/submit`` and, behind a flag, ``POST /qsearch/v2/submit_request``.

Order of operations for one request: refuse an oversized body (413, before
any MAC work, so it never consumes a nonce), authenticate (``auth``; a failure
is 401 with no receipt and nothing stored), parse the job (``job``; a failure
is 400 with no receipt), look the ``job_digest`` up in the durable store (a
DONE job returns its stored receipt without recomputing), run the selected
runtime at most once per digest at a time (single-flight across threads, and
at most ``max_concurrent`` runtime calls at once), seal the receipt
(``receipt``) and store it. A job over a service cap (variables, shots, bond
dimension, seed range) gets a sealed REFUSED receipt instead of a run; any
exception on the run path becomes a sealed NOT_RUN receipt.

The runtime kind is chosen once, when the job is accepted, and stored on the
row. A run records its attempt (``store.mark_attempt``) only once it holds a
concurrency slot, immediately before the runtime is called, so a job still
queued for a slot when the process dies has not used an attempt; a refusal
records none. On startup ``recover()``
finishes every job that was accepted but never completed: it re-runs it once
with the same body (same seed, same digest, same runtime kind) unless that
would be the third attempt or the job targets IBM hardware, which is never
resubmitted; those complete as NOT_RUN without running.

One service owns a data directory at a time: ``QSearchService`` holds an
exclusive OS lock on ``<data_dir>/qsearch.lock`` until ``close()``.

``/qsearch/v2/submit_request`` (lane HW0, S2) answers only when
``NANO_QSEARCH_V2=1`` (``ServiceConfig.v2_enabled``); otherwise it is 404
``NOT_ENABLED`` before authentication, so it burns no nonce. It uses the v1
request auth (the path is in the MAC), parses and cross-checks the request
(``request.parse_request``; a failure is 400 with no receipt), records the
request ACCEPTED before anything is submitted, marks its one attempt
immediately before the IBM runtime's ``run_request`` (one ATLAS request for
all N jobs), seals the v2 body (``receipt_v2.seal_receipt_v2``) and stores it.
A repeated request digest returns the stored receipt; a request whose attempt
was already used is never submitted again: it is re-attached through
``IbmRuntime.recover_request`` (ATLAS ``recover_qsearch_submission``) and
stays pending (503) until the provider job is done. ``recover()`` does the
same for every ACCEPTED request on startup.

J6: the PUB limit per request is ``NANO_QSEARCH_V2_MAX_PUBS`` (default 16,
at most 4,096; an invalid value refuses startup). A larger request also needs
a larger ``NANO_QSEARCH_MAX_BODY_BYTES``. The run's compile seconds are kept in
the request row's ``record_json`` and logged; they are not in the sealed body.

Accounting gate: a live v2 request whose provider usage is not final yet is
held ACCEPTED and answered 503 ``ACCOUNTING_PENDING`` (``ACCOUNTING_TIMEOUT``
after ``NANO_QSEARCH_ACCOUNTING_WINDOW_S``, default 900 s; still 503, still
never sealed, never NOT_RUN); every repeat drives the runtime's recovery, and
the OK / FAILED receipt is sealed once, with settled usage. A DONE receipt an
older version sealed while usage was pending is never served as final: it is
re-derived from the settled ledger row (identical results; the replaced seal is
kept in ``record_json["reseals"]``) or answered 503 until accounting settles.

Amendment R3 H1 / L1: a job or request that reached the provider is never
sealed NOT_RUN. When the IBM runtime returns None (submitted, outcome not
known yet) the row stays ACCEPTED with its attempt used and the caller gets
503; a repeat or a restart re-attaches it through ATLAS recovery
(``IbmRuntime.recover_job`` for v1, ``recover_request`` for v2), which yields
the receipt (v2: OK or FAILED) or keeps it pending. A runtime without
``recover_job`` keeps the v1 rule that hardware jobs are never run from recovery.
"""
from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import socket
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .auth import AUTH_HEADER, WINDOW_S, AuthError, verify_request
from .canonical import canonical_json
from .job import JobValidationError, QSearchJob, parse_job
from .lifecycle import job_state, lifecycle_error
from .receipt import MIN_KEY_BYTES, not_run_receipt, refused_receipt, seal_receipt
from .receipt_v2 import not_run_v2, seal_receipt_v2
from .request import (
    MAX_BLOCKS, MAX_BLOCKS_LIMIT, QSearchRequest, RequestValidationError, max_pubs_from_env, parse_request,
)
from .runtime_mps import SERVICE_KINDS, build_runtime, select_kind
from .store import JobRecord, JobStore, RequestIdConflict

SUBMIT_PATH = "/qsearch/v1/submit"
REQUEST_PATH = "/qsearch/v2/submit_request"
REQUEST_STATUS_PREFIX = "/qsearch/v2/requests/"
_REQUEST_STATUS = re.compile(re.escape(REQUEST_STATUS_PREFIX) + r"(sha256:[0-9a-f]{64})\Z")
_REQUEST_CONTROL = re.compile(re.escape(REQUEST_STATUS_PREFIX) + r"(sha256:[0-9a-f]{64})/(cancel|refresh|replace)\Z")
V2_ENV = "NANO_QSEARCH_V2"
REQUEST_RUNTIME_KIND = "ibm"   # every v2 job is an ibm job (request.parse_request enforces it)
DEFAULT_BIND = "127.0.0.1:8798"
DEFAULT_MAX_VARIABLES = 40
DEFAULT_MAX_BODY_BYTES = 1_048_576
DEFAULT_MAX_SHOTS = 100_000
DEFAULT_MAX_CHI = 256
DEFAULT_REQUEST_TIMEOUT_S = 30
DEFAULT_MAX_CONCURRENT = 2
SEED_MAX = 2**32 - 1          # Aer and the transpiler take uint32 seeds; Predator allows any integer
MAX_ATTEMPTS = 2              # the original attempt plus one recovery re-run
STORE_FILE = "qsearch.sqlite3"
LOCK_FILE = "qsearch.lock"
NOT_COMPLETED = {"error": "job not completed"}
UNAUTHORIZED = {"error": "unauthorized"}
NOT_FOUND = {"error": "not found"}
NOT_ENABLED = {"error": "not found", "code": "NOT_ENABLED"}
ACCOUNTING_PENDING = {"error": "job not completed", "code": "ACCOUNTING_PENDING"}
ACCOUNTING_TIMEOUT = {"error": "job not completed", "code": "ACCOUNTING_TIMEOUT"}
ACCOUNTING_PENDING_TERMINAL = {"error": "job not completed", "code": "ACCOUNTING_PENDING_TERMINAL"}
ACCOUNTING_NEEDS_LIVE_CONTEXT = {"error": "job not completed", "code": "ACCOUNTING_NEEDS_LIVE_CONTEXT"}
SUBMITTED_PENDING = {"error": "job not completed", "code": "SUBMITTED_PENDING"}
RESEAL_MISMATCH = {"error": "job not completed", "code": "RESEAL_MISMATCH"}
ACCOUNTING_WINDOW_ENV = "NANO_QSEARCH_ACCOUNTING_WINDOW_S"
DEFAULT_ACCOUNTING_WINDOW_S = 900.0
LISTEN_BACKLOG = 64           # connections queued in the kernel while recovery runs or every handler is busy
_WIN_EDEADLOCK = 36           # Windows CRT EDEADLOCK; msvcrt.locking reports contention as this or EACCES
# Only these errnos mean "another process holds the lock"; any other OSError is a real failure and re-raises.
_LOCK_CONTENDED = (frozenset({errno.EACCES, _WIN_EDEADLOCK}) if os.name == "nt"
                   else frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}))


class DataDirLocked(RuntimeError):
    """Another service already holds this data directory."""

    def __init__(self) -> None:
        super().__init__("data directory is already in use by another nano-qsearch-serve")


class _DataDirLock:
    """An exclusive, non-blocking OS lock on one byte of ``path``, held until ``release``."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._lock(self._fd)
        except OSError as exc:
            os.close(self._fd)
            self._fd = None
            if exc.errno in _LOCK_CONTENDED:
                raise DataDirLocked() from None
            raise

    @staticmethod
    def _lock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON constant {name}")


def _read_key(path: str, name: str) -> bytes:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError(f"{name} is unreadable ({type(exc).__name__})") from None
    if len(data) < MIN_KEY_BYTES:
        raise ValueError(f"{name} must hold at least {MIN_KEY_BYTES} bytes")
    return data


def _env_number(var: str, default: int | float, parse: Callable[[str], int | float]) -> int | float:
    """``parse`` the environment variable ``var``, or ``default`` when unset; a bad value names ``var``, not its text."""
    raw = os.environ.get(var)
    if raw is None:
        return default
    try:
        return parse(raw)
    except ValueError:
        kind = "an integer" if parse is int else "a number"
        raise ValueError(f"{var} must be {kind}") from None


@dataclass(frozen=True)
class ServiceConfig:
    bind: str
    backend: str
    data_dir: Path
    receipt_key: bytes
    request_key: bytes
    max_variables: int = DEFAULT_MAX_VARIABLES
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_shots: int = DEFAULT_MAX_SHOTS
    max_chi: int = DEFAULT_MAX_CHI
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    verbose: bool = False
    v2_enabled: bool = False          # NANO_QSEARCH_V2=1: /qsearch/v2/submit_request answers
    v2_max_pubs: int = MAX_BLOCKS     # NANO_QSEARCH_V2_MAX_PUBS: PUBs per v2 request, 1..MAX_BLOCKS_LIMIT
    accounting_window_s: float = DEFAULT_ACCOUNTING_WINDOW_S   # after this, 503 says ACCOUNTING_TIMEOUT
    queue_deadline_s: float = 3600
    poll_interval_s: float = 30

    def __post_init__(self) -> None:
        if self.backend not in SERVICE_KINDS:
            raise ValueError(f"backend must be one of {SERVICE_KINDS}")
        for name, key in (("receipt_key", self.receipt_key), ("request_key", self.request_key)):
            if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
                raise ValueError(f"{name} must be at least {MIN_KEY_BYTES} bytes")
        if bytes(self.receipt_key) == bytes(self.request_key):
            raise ValueError("receipt key and request key must differ")
        for name in ("max_variables", "max_body_bytes", "max_shots", "max_chi", "max_concurrent"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("accounting_window_s", "queue_deadline_s", "poll_interval_s"):
            window = getattr(self, name)
            if isinstance(window, bool) or not isinstance(window, (int, float)) or not (math.isfinite(window) and window > 0):
                raise ValueError(f"{name} must be a positive number of seconds")
        if (isinstance(self.v2_max_pubs, bool) or not isinstance(self.v2_max_pubs, int)
                or not 1 <= self.v2_max_pubs <= MAX_BLOCKS_LIMIT):
            raise ValueError(f"v2_max_pubs must be an int in 1..{MAX_BLOCKS_LIMIT}")
        timeout = self.request_timeout_s
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (math.isfinite(timeout) and timeout > 0):
            raise ValueError("request_timeout_s must be a positive number of seconds")
        host, _, port = self.bind.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError("bind must be host:port")

    @property
    def host_port(self) -> tuple[str, int]:
        host, _, port = self.bind.rpartition(":")
        return host, int(port)

    @classmethod
    def from_env(cls, args: argparse.Namespace | None = None) -> "ServiceConfig":
        env = os.environ

        def pick(flag: str, var: str, default: str | None = None) -> str | None:
            value = getattr(args, flag, None) if args is not None else None
            return value if value else env.get(var, default)

        def cap(var: str, default: int) -> int:
            return _env_number(var, default, int)

        data_dir = pick("data_dir", "NANO_QSEARCH_DATA_DIR")
        key_path = pick("key_path", "NANO_QSEARCH_KEY_PATH")
        request_key_path = pick("request_key_path", "NANO_QSEARCH_REQUEST_KEY_PATH")
        if not data_dir or not key_path or not request_key_path:
            raise ValueError("NANO_QSEARCH_DATA_DIR, NANO_QSEARCH_KEY_PATH and NANO_QSEARCH_REQUEST_KEY_PATH are required")
        return cls(bind=pick("bind", "NANO_QSEARCH_BIND", DEFAULT_BIND), backend=pick("backend", "NANO_QSEARCH_BACKEND", "auto"),
                   data_dir=Path(data_dir), receipt_key=_read_key(key_path, "NANO_QSEARCH_KEY_PATH"),
                   request_key=_read_key(request_key_path, "NANO_QSEARCH_REQUEST_KEY_PATH"),
                   max_variables=cap("NANO_QSEARCH_MAX_VARIABLES", DEFAULT_MAX_VARIABLES),
                   max_body_bytes=cap("NANO_QSEARCH_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES),
                   max_shots=cap("NANO_QSEARCH_MAX_SHOTS", DEFAULT_MAX_SHOTS),
                   max_chi=cap("NANO_QSEARCH_MAX_CHI", DEFAULT_MAX_CHI),
                   max_concurrent=cap("NANO_QSEARCH_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT),
                   request_timeout_s=_env_number("NANO_QSEARCH_REQUEST_TIMEOUT_S", DEFAULT_REQUEST_TIMEOUT_S, float),
                   verbose=bool(getattr(args, "verbose", False)), v2_enabled=env.get(V2_ENV) == "1",
                   v2_max_pubs=max_pubs_from_env(env),
                   accounting_window_s=_env_number(ACCOUNTING_WINDOW_ENV, DEFAULT_ACCOUNTING_WINDOW_S, float),
                   queue_deadline_s=_env_number("NANO_QSEARCH_QUEUE_DEADLINE_S", 3600, float),
                   poll_interval_s=_env_number("NANO_QSEARCH_POLL_INTERVAL_S", 30, float))


RuntimeFactory = Callable[[str], Any]


def _log(message: str) -> None:
    print(f"nano-qsearch: {message}", file=sys.stderr, flush=True)


class QSearchService:
    def __init__(self, config: ServiceConfig, *, runtime_factory: RuntimeFactory = build_runtime,
                 clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self._clock = clock
        self._dir_lock = _DataDirLock(config.data_dir / LOCK_FILE)      # raises DataDirLocked
        try:
            self.store = JobStore(config.data_dir / STORE_FILE, clock=clock)
        except BaseException:
            self._dir_lock.release()
            raise
        self._runtime_factory = runtime_factory
        self._runtimes: dict[str, Any] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(config.max_concurrent)

    def close(self) -> None:
        try:
            self.store.close()
        finally:
            self._dir_lock.release()

    def _runtime(self, kind: str):
        with self._lock:
            if kind not in self._runtimes:
                self._runtimes[kind] = self._runtime_factory(kind)
            return self._runtimes[kind]

    def _cap_violation(self, job: QSearchJob) -> str | None:
        """A fixed sentence naming the first service cap ``job`` exceeds, or None."""
        cfg = self.config
        if job.n > cfg.max_variables:
            return f"limits: {job.n} variables exceed the service cap of {cfg.max_variables}"
        if job.shots > cfg.max_shots:
            return f"limits: backend.shots exceeds the service cap of {cfg.max_shots}"
        if job.chi_max > cfg.max_chi:
            return f"limits: backend.chi_max exceeds the service cap of {cfg.max_chi}"
        if not 0 <= job.backend_seed <= SEED_MAX:
            return f"limits: backend.seed outside 0..{SEED_MAX}"
        if not 0 <= job.compile_seed <= SEED_MAX:
            return f"limits: compile.seed outside 0..{SEED_MAX}"
        return None

    def _sealed(self, job: QSearchJob, build: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
        """Seal the receipt ``build()`` returns; any failure becomes a sealed NOT_RUN naming the class only.

        None from ``build()`` means the job reached the provider and is still pending: nothing is sealed.
        """
        try:
            unsigned = build()
            if unsigned is None:
                return None
            return seal_receipt(unsigned, self.config.receipt_key)
        except Exception as exc:  # noqa: BLE001 - any failure is reported as NOT_RUN by class name only
            _log(f"job {job.job_digest} not run: {type(exc).__name__}")
            return seal_receipt(not_run_receipt(job, f"service: {type(exc).__name__}"), self.config.receipt_key)

    def _execute(self, job: QSearchJob, kind: str) -> dict[str, Any] | None:
        """Refuse or run one job on runtime ``kind`` and return the SEALED receipt.

        A refusal needs no slot and records no attempt. A run waits for a concurrency slot
        and only then records its attempt, so a job still queued when the process dies has
        not used one. Only a failure to record the attempt escapes (the caller answers 503
        and the row stays ACCEPTED); every other failure becomes a sealed NOT_RUN.
        """
        reason = self._cap_violation(job)
        if reason is not None:
            return self._sealed(job, lambda: refused_receipt(job, reason))
        with self._slots:
            self.store.mark_attempt(job.job_digest)
            return self._sealed(job, lambda: self._runtime(kind).run(job))

    def _attempt(self, job: QSearchJob, record: JobRecord, *, recovering: bool) -> dict[str, Any] | None:
        """Decide, from the stored attempt count, whether this job may run; return the sealed receipt,
        or None while a job that reached the provider is still pending.

        The runtime kind is the one recorded when the job was accepted; rows from before
        that column existed fall back to the current service backend. A resumed IBM job is
        never run again: a runtime with ``recover_job`` re-attaches it (L1), any other
        completes it as NOT_RUN.
        """
        kind = record.kind or select_kind(self.config.backend, job)
        resuming = recovering or record.attempts > 0
        if record.attempts >= MAX_ATTEMPTS:
            unsigned = not_run_receipt(job, "recovery: attempt limit reached")
        elif resuming and kind == "ibm":
            recover = getattr(self._runtime(kind), "recover_job", None)
            if recover is not None:
                return self._sealed(job, lambda: recover(job))
            unsigned = not_run_receipt(job, "recovery: hardware jobs are never run from recovery")
        else:
            return self._execute(job, kind)
        return seal_receipt(unsigned, self.config.receipt_key)

    def _stored_receipt(self, job_digest: str) -> tuple[int, dict[str, Any]] | None:
        record = self.store.get(job_digest)
        if record is None or record.state != "DONE":
            return None
        return 200, json.loads(record.receipt_json)

    def _complete(self, job: QSearchJob, sealed: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Store ``sealed``; if the row is already DONE (``ValueError``), the stored receipt wins."""
        try:
            self.store.complete(job.job_digest, json.dumps(sealed, sort_keys=True, allow_nan=False))
        except ValueError:
            stored = self._stored_receipt(job.job_digest)
            if stored is None:
                raise
            _log(f"job {job.job_digest} was already completed; returning the stored receipt")
            return stored
        return 200, sealed

    def _run_once(self, job: QSearchJob, *, recovering: bool = False) -> tuple[int, dict[str, Any]]:
        """Single-flight by digest: compute once, store, and share the stored receipt."""
        with self._lock:
            stored = self._stored_receipt(job.job_digest)
            if stored is not None:
                return stored
            waiter = self._inflight.get(job.job_digest)
            owner = waiter is None
            if owner:
                waiter = self._inflight[job.job_digest] = threading.Event()
        if not owner:
            waiter.wait()
            stored = self._stored_receipt(job.job_digest)
            return stored if stored is not None else (503, dict(NOT_COMPLETED))
        try:
            record = self.store.begin(job.job_digest, canonical_json(job.payload),
                                      kind=select_kind(self.config.backend, job))
            sealed = self._attempt(job, record, recovering=recovering)
            if sealed is None:                 # reached the provider, outcome not known yet: stays ACCEPTED
                return 503, dict(NOT_COMPLETED)
            return self._complete(job, sealed)
        except Exception as exc:  # noqa: BLE001 - the store failed; the row stays ACCEPTED for a later attempt
            _log(f"job {job.job_digest} not completed: {type(exc).__name__}")
            return 503, dict(NOT_COMPLETED)
        finally:
            with self._lock:
                self._inflight.pop(job.job_digest, None)
            waiter.set()

    def _authenticate(self, method: str, path: str, body: bytes, headers: Mapping[str, str | None],
                      now: int) -> tuple[int, dict[str, Any]] | None:
        """None when the request is authentic; otherwise the (status, body) to answer with."""
        try:
            verify_request(self.config.request_key, headers.get(AUTH_HEADER), method, path, body, now=now, nonce_store=self.store)
            self.store.prune_nonces(before_ts=now - 2 * WINDOW_S)
        except AuthError as exc:
            if self.config.verbose:
                _log(f"unauthorized request: {exc.code}")
            return 401, dict(UNAUTHORIZED)
        except (sqlite3.Error, OSError) as exc:
            _log(f"nonce store failed: {type(exc).__name__}")
            return 503, dict(NOT_COMPLETED)
        return None

    def submit(self, method: str, path: str, body: bytes, headers: Mapping[str, str | None], *,
               now: int | None = None) -> tuple[int, dict[str, Any]]:
        if len(body) > self.config.max_body_bytes:
            return 413, {"error": "body too large"}
        now = int(time.time()) if now is None else now
        refusal = self._authenticate(method, path, body, headers, now)
        if refusal is not None:
            return refusal
        try:
            job = parse_job(json.loads(body.decode("utf-8"), parse_constant=_reject_constant))
        except JobValidationError as exc:
            return 400, {"error": "invalid job", "code": exc.code}
        except (ValueError, UnicodeDecodeError, RecursionError):
            return 400, {"error": "invalid job", "code": "JSON"}
        return self._run_once(job)

    # --- v2 requests ---------------------------------------------------------------------------

    def _state(self, digest: str) -> dict[str, Any]:
        row = self.store.get_request(digest)
        if row is None:
            raise KeyError(digest)
        return job_state(row, self._stored_record(digest),
                         json.loads(row.receipt_json) if row.receipt_json else None, now=self._clock(),
                         queue_deadline_s=self.config.queue_deadline_s,
                         accounting_deadline_s=self.config.accounting_window_s)

    def control_request(self, method: str, path: str, body: bytes, headers: Mapping[str, str | None], *,
                        now: int | None = None) -> tuple[int, dict[str, Any]]:
        """Explicit host operation on an existing attempt, serialized with submit/recovery.

        Cancellation is fenced and journaled before external I/O. Lost acknowledgements
        require observation; they never authorize another submission or cancel RPC.
        """
        if not self.config.v2_enabled:
            return 404, dict(NOT_ENABLED)
        match = _REQUEST_CONTROL.fullmatch(path)
        if method != "POST" or match is None:
            return 404, lifecycle_error("NOT_FOUND")
        if len(body) > self.config.max_body_bytes:
            return 413, lifecycle_error("BODY_TOO_LARGE", field="body")
        digest, action = match.groups()
        if action != "replace" and body:
            return 400, lifecycle_error("EMPTY_BODY_REQUIRED", field="body")
        refusal = self._authenticate(method, path, body, headers, int(self._clock()) if now is None else now)
        if refusal is not None:
            return refusal[0], lifecycle_error("UNAUTHORIZED" if refusal[0] == 401 else "STORE_UNAVAILABLE",
                                              retryable=refusal[0] == 503)
        flight = "request:" + digest
        owner = False
        try:
            row = self.store.get_request(digest)
            if row is None:
                return 404, lifecycle_error("REQUEST_NOT_FOUND", field="request_digest")
            if action == "cancel":
                # Must also fence an in-flight submission before waiting for its acknowledgement.
                self.store.request_event(digest, "cancellation_requested")
            with self._lock:
                if flight in self._inflight:
                    return 409, lifecycle_error("REQUEST_BUSY", retryable=True, next_action="POLL_STATUS")
                waiter = self._inflight[flight] = threading.Event()
                owner = True
            state = self._state(digest)
            if action == "replace":
                return self._replace_request(row, state, body)
            if state["accounting"]["status"] in ("SETTLED", "NOT_APPLICABLE"):
                return 200, state
            request = parse_request(json.loads(row.request_json), max_blocks=MAX_BLOCKS_LIMIT)
            if action == "cancel" and row.attempts == 0:
                self._complete_request(request, seal_receipt_v2(
                    not_run_v2(request, "cancelled before submission"), self.config.receipt_key))
                return 200, self._state(digest)
            record = self._stored_record(digest)
            last_poll = record.get("last_provider_poll_at")
            if (isinstance(last_poll, (int, float)) and self._clock() - last_poll < self.config.poll_interval_s
                    and (action != "cancel" or "cancellation_dispatched_at" in record)):
                return 200, state
            # Persist the throttle before I/O; restart and failed reads retain the bound.
            record = self.store.merge_request_record(digest, {"last_provider_poll_at": self._clock()})
            runtime = self._runtime(REQUEST_RUNTIME_KIND)
            try:
                if action == "cancel" and "cancellation_dispatched_at" not in record:
                    def before_cancel() -> None:
                        # The adapter calls this after authority, identity and
                        # status checks, immediately before the external RPC.
                        # Failed preflight must not consume the dispatch claim.
                        if "cancellation_dispatched_at" in record:
                            raise RuntimeError("cancellation dispatch already claimed")
                        record.update(self.store.request_event(digest, "cancellation_dispatched"))

                    runtime.cancel_request(request, record=record, before_cancel=before_cancel)
                else:
                    runtime.observe_request(request, record=record)
                if record.get("provider_status") in ("CANCELLED", "CANCELED"):
                    self.store.request_event(digest, "cancellation_confirmed")
                terminal = record.get("provider_status") in ("DONE", "COMPLETED", "ERROR", "FAILED", "CANCELLED", "CANCELED")
                if terminal:
                    record.setdefault("accounting_pending_since", self._clock())
                    # Explicit reconciliation reattaches only this provider job, never replacement.
                    if row.receipt_json:
                        self._settle_sealed(request, json.loads(row.receipt_json), record)
                    else:
                        unsigned = runtime.recover_request(request, record=record)
                        if unsigned is not None:
                            self._complete_request(request, seal_receipt_v2(unsigned, self.config.receipt_key))
            finally:
                # This evidence write must succeed; controls cannot silently lose their fence.
                self.store.merge_request_record(digest, record)
            return 200, self._state(digest)
        except RequestIdConflict:
            return 409, lifecycle_error("REPLACEMENT_CONFLICT", field="request_id")
        except RequestValidationError:
            return 400, lifecycle_error("INVALID_REQUEST", field="body")
        except (sqlite3.Error, OSError):
            return 503, lifecycle_error("PROVIDER_OR_STORE_UNAVAILABLE", retryable=True, next_action="RECONCILE")
        except (ValueError, TypeError):
            return 400, lifecycle_error("INVALID_REQUEST", field="body")
        except Exception as exc:
            from .runtime_ibm import LifecycleAdapterError
            if isinstance(exc, LifecycleAdapterError):
                return 503, lifecycle_error(exc.code, field=exc.field, retryable=exc.retryable,
                                           next_action=exc.next_action)
            return 503, lifecycle_error("LIFECYCLE_UNAVAILABLE", retryable=True, next_action="RECONCILE")
        finally:
            if owner:
                with self._lock:
                    self._inflight.pop(flight, None)
                waiter.set()

    def _replace_request(self, row, state: dict[str, Any], body: bytes) -> tuple[int, dict[str, Any]]:
        if not state["execution"]["terminal"] or state["accounting"]["status"] not in ("SETTLED", "NOT_APPLICABLE"):
            return 409, lifecycle_error("ORIGINAL_ATTEMPT_UNRECONCILED", next_action="RECONCILE")
        parsed = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
        request = parse_request(parsed, max_blocks=self.config.v2_max_pubs)
        if request.request_id == row.request_id or request.request_digest == row.request_digest:
            return 409, lifecycle_error("NEW_IDENTITY_REQUIRED", field="descriptor.request_id")
        if request.target == state["request"]["target"]:
            return 409, lifecycle_error("NEW_TARGET_REQUIRED", field="descriptor.target")
        self.store.link_replacement(row.request_digest, request.request_digest, request.request_id, canonical_json(parsed))
        self.store.request_event(row.request_digest, "replacement_linked")
        # ATLAS's ordinary target-bound plan compilation/authorization runs for the NEW identity.
        status, payload = self._run_request_once(request, canonical_json(parsed))
        if status not in (200, 503):
            return status, payload
        return 200, self._state(request.request_digest)

    def status_request(self, method: str, path: str, body: bytes, headers: Mapping[str, str | None], *,
                       now: int | None = None) -> tuple[int, dict[str, Any]]:
        """Authenticated durable snapshot only; never constructs a runtime or reconciles a ledger."""
        if not self.config.v2_enabled:
            return 404, dict(NOT_ENABLED)
        match = _REQUEST_STATUS.fullmatch(path)
        if method != "GET" or match is None:
            return 404, lifecycle_error("NOT_FOUND")
        if body:
            return 400, lifecycle_error("EMPTY_BODY_REQUIRED", field="body")
        refusal = self._authenticate(method, path, body, headers, int(self._clock()) if now is None else now)
        if refusal is not None:
            return refusal[0], lifecycle_error("UNAUTHORIZED" if refusal[0] == 401 else "STORE_UNAVAILABLE",
                                              retryable=refusal[0] == 503)
        try:
            row = self.store.get_request(match[1])
            if row is None:
                return 404, lifecycle_error("REQUEST_NOT_FOUND", field="request_digest")
            raw_record = self.store.request_record(row.request_digest)
            record = json.loads(raw_record) if raw_record else {}
            receipt = json.loads(row.receipt_json) if row.receipt_json else None
            if not isinstance(record, dict) or (receipt is not None and not isinstance(receipt, dict)):
                raise ValueError("invalid persisted snapshot")
            return 200, job_state(row, record, receipt, now=self._clock(),
                                  queue_deadline_s=self.config.queue_deadline_s,
                                  accounting_deadline_s=self.config.accounting_window_s)
        except (sqlite3.Error, OSError):
            return 503, lifecycle_error("STORE_UNAVAILABLE", retryable=True)
        except (ValueError, TypeError):
            return 503, lifecycle_error("STATE_UNAVAILABLE", retryable=False, next_action="MANUAL_RECONCILIATION")

    def _sealed_v2(self, request: QSearchRequest,
                   build: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
        """Seal the v2 body ``build()`` returns; any failure becomes a sealed NOT_RUN naming the class only.

        None from ``build()`` means the request reached the provider and is still pending: nothing is sealed.
        """
        try:
            unsigned = build()
            if unsigned is None:
                return None
            return seal_receipt_v2(unsigned, self.config.receipt_key)
        except Exception as exc:  # noqa: BLE001 - any failure is reported as NOT_RUN by class name only
            _log(f"request {request.request_digest} not run: {type(exc).__name__}")
            return seal_receipt_v2(not_run_v2(request, f"service: {type(exc).__name__}"), self.config.receipt_key)

    def _stored_request(self, request_digest: str) -> tuple[int, dict[str, Any]] | None:
        record = self.store.get_request(request_digest)
        if record is None or record.state != "DONE":
            return None
        return 200, json.loads(record.receipt_json)

    def _request_cap_violation(self, request: QSearchRequest) -> str | None:
        for job in request.jobs:
            reason = self._cap_violation(job)
            if reason is not None:
                return reason
        return None

    def _attempt_request(self, request: QSearchRequest, *, resuming: bool,
                         record: dict[str, Any]) -> dict[str, Any] | None:
        """The SEALED v2 receipt for ``request``, or None while a submitted request is still pending.

        A request whose one attempt is used (or any request seen by startup recovery)
        is never submitted again: the runtime re-attaches it. Otherwise a cap refusal
        needs no attempt; a run marks its attempt, then calls ``run_request`` once.
        """
        runtime = self._runtime(REQUEST_RUNTIME_KIND)
        if resuming:
            return self._sealed_v2(request, lambda: runtime.recover_request(request, record=record))
        reason = self._request_cap_violation(request)
        if reason is not None:
            return self._sealed_v2(request, lambda: not_run_v2(request, "refused: " + reason))
        queue_at = self._state(request.request_digest)["deadlines"]["queue_at"]
        if not self._slots.acquire(timeout=min(self.config.poll_interval_s, max(0, queue_at - self._clock()))):
            return None
        try:
            prior = self._stored_record(request.request_digest)
            if "cancellation_requested_at" in prior or self._state(request.request_digest)["deadlines"]["queue_expired"]:
                return None
            if not self.store.claim_request_attempt(request.request_digest, queue_at=queue_at):
                return None
            return self._sealed_v2(request, lambda: runtime.run_request(
                request, max_pubs=self.config.v2_max_pubs, record=record))
        finally:
            self._slots.release()

    def _stored_record(self, request_digest: str) -> dict[str, Any]:
        try:
            raw = self.store.request_record(request_digest)
            stored = json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001 - an unreadable record is treated as empty, never as a receipt
            return {}
        return stored if isinstance(stored, dict) else {}

    def _keep_record(self, request: QSearchRequest, record: dict[str, Any]) -> dict[str, Any]:
        """Merge, store and log the run record (compile seconds, accounting); never touches the receipt.

        Returns the merged record. ``accounting_pending_since`` is set once, the first time a
        result is held for accounting.
        """
        if not record:
            return self._stored_record(request.request_digest)
        merged = {**self._stored_record(request.request_digest), **record}
        if merged.get("accounting") == "pending" and "accounting_pending_since" not in merged:
            merged["accounting_pending_since"] = self._clock()
        try:
            merged = self.store.merge_request_record(request.request_digest, merged)
        except Exception as exc:  # noqa: BLE001 - the record is evidence beside the receipt, not the receipt
            _log(f"request {request.request_digest} record not stored: {type(exc).__name__}")
        if record.get("accounting") == "pending":
            _log(f"request {request.request_digest} results held: provider usage not final yet")
        if "compile_s" in record:
            _log(f"request {request.request_digest} compiled {record.get('compiled_jobs')} jobs "
                 f"in {record['compile_s']} s ({record.get('target_resolutions')} target resolution)")
        return merged

    def _pending(self, record: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """503 with what the client can record while nothing is sealed.

        Outcome not known: ``SUBMITTED_PENDING`` with the provider job id when ATLAS named it (else
        the plain body). Results held for accounting: ``ACCOUNTING_NEEDS_LIVE_CONTEXT`` (the accounting
        read needs the live flags), else ``ACCOUNTING_TIMEOUT`` after the window, else
        ``ACCOUNTING_PENDING_TERMINAL`` (the provider job ended FAILED: cancelled, errored, max time) or
        ``ACCOUNTING_PENDING``; each with the job id, the result, the provider status, the reason and
        ``terminal``. Usage is never reported final, and a zero is never a free run.
        """
        job_id = record.get("provider_job_id")
        if record.get("accounting") != "pending":
            return 503, ({**SUBMITTED_PENDING, "job_id": job_id} if isinstance(job_id, str) and job_id
                         else dict(NOT_COMPLETED))
        since = record.get("accounting_pending_since")
        late = isinstance(since, (int, float)) and self._clock() - since >= self.config.accounting_window_s
        terminal = record.get("result") == "FAILED"
        if record.get("accounting_blocker") == "needs_live_context":
            base = ACCOUNTING_NEEDS_LIVE_CONTEXT
        elif late:
            base = ACCOUNTING_TIMEOUT
        else:
            base = ACCOUNTING_PENDING_TERMINAL if terminal else ACCOUNTING_PENDING
        detail = {"job_id": job_id, "result": record.get("result"), "provider_status": record.get("provider_status"),
                  "terminal": terminal, "usage_pending": True}
        if terminal:
            detail["reason"] = record.get("reason")
        return 503, {**base, **detail}

    @staticmethod
    def _sealed_pending(receipt: Mapping[str, Any]) -> bool:
        """A live OK / FAILED v2 receipt sealed while usage was pending: never served as final."""
        backend, usage = receipt.get("backend"), receipt.get("usage")
        return (receipt.get("schema") == "predator.qsearch.receipt.v2" and receipt.get("status") in ("OK", "FAILED")
                and isinstance(backend, Mapping) and backend.get("kind") == "ibm" and backend.get("dry_run") is False
                and isinstance(usage, Mapping) and usage.get("usage_pending") is not False)

    def _settle_sealed(self, request: QSearchRequest, stored: dict[str, Any],
                       record: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Re-derive a receipt sealed while usage was pending from the settled ledger row; else 503.

        The re-sealed body must equal the stored one except ``usage`` (and ``seal``); the replaced
        seal and usage are kept in ``record_json["reseals"]``.
        """
        runtime = self._runtime(REQUEST_RUNTIME_KIND)
        unsigned = runtime.recover_request(request, record=record)
        if unsigned is None:
            record["accounting"] = "pending"
            return self._pending(self._keep_record(request, record))
        same = {k: v for k, v in stored.items() if k not in ("usage", "seal")}
        if {k: v for k, v in unsigned.items() if k != "usage"} != same or unsigned["usage"]["usage_pending"]:
            _log(f"request {request.request_digest} settled receipt differs from the stored results; not re-sealed")
            return 503, dict(RESEAL_MISMATCH)
        sealed = seal_receipt_v2(unsigned, self.config.receipt_key)
        old_json = self.store.get_request(request.request_digest).receipt_json
        self.store.reseal_request(request.request_digest, old_json, json.dumps(sealed, sort_keys=True, allow_nan=False))
        history = list(self._stored_record(request.request_digest).get("reseals", []))
        history.append({"at": self._clock(), "reason": "accounting settled", "previous_seal": stored.get("seal"),
                        "previous_usage": stored.get("usage")})
        self._keep_record(request, {**record, "reseals": history})
        return 200, sealed

    def _complete_request(self, request: QSearchRequest, sealed: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Store ``sealed``; if the row is already DONE (``ValueError``), the stored receipt wins."""
        try:
            self.store.complete_request(request.request_digest, json.dumps(sealed, sort_keys=True, allow_nan=False))
        except ValueError:
            stored = self._stored_request(request.request_digest)
            if stored is None:
                raise
            return stored
        return 200, sealed

    def _run_request_once(self, request: QSearchRequest, body_json: str, *,
                          recovering: bool = False) -> tuple[int, dict[str, Any]]:
        """Single-flight by request digest: record before submitting, submit at most once, store once."""
        flight = "request:" + request.request_digest
        with self._lock:
            stored = self._stored_request(request.request_digest)
            if stored is not None and not self._sealed_pending(stored[1]):
                return stored
            waiter = self._inflight.get(flight)
            owner = waiter is None
            if owner:
                waiter = self._inflight[flight] = threading.Event()
        if not owner:
            waiter.wait(timeout=self.config.poll_interval_s)
            stored = self._stored_request(request.request_digest)
            if stored is not None and not self._sealed_pending(stored[1]):
                return stored
            return self._pending(self._stored_record(request.request_digest))
        run_record: dict[str, Any] = {}
        try:
            existing = self.store.get_request(request.request_digest)
            if existing is not None and (recovering or existing.attempts > 0):
                prior = self._stored_record(request.request_digest)
                state = self._state(request.request_digest)
                if ("cancellation_requested_at" in prior or state["deadlines"]["queue_expired"]
                        or state["deadlines"]["accounting_expired"]):
                    return self._pending(prior)
                last_poll = prior.get("last_provider_poll_at")
                if isinstance(last_poll, (int, float)) and self._clock() - last_poll < self.config.poll_interval_s:
                    return self._pending(prior)
                self.store.merge_request_record(request.request_digest, {"last_provider_poll_at": self._clock()})
            if stored is not None:                 # sealed while usage was pending: settle, never serve as final
                return self._settle_sealed(request, stored[1], run_record)
            row = self.store.begin_request(request.request_digest, request.request_id, body_json)
            try:
                sealed = self._attempt_request(request, resuming=recovering or row.attempts > 0, record=run_record)
            finally:
                merged = self._keep_record(request, run_record)
            if sealed is None:
                return self._pending(merged)
            return self._complete_request(request, sealed)
        except RequestIdConflict:
            return 409, {"error": "request_id conflict"}
        except Exception as exc:  # noqa: BLE001 - the store failed; the row stays ACCEPTED for recovery
            _log(f"request {request.request_digest} not completed: {type(exc).__name__}")
            return 503, dict(NOT_COMPLETED)
        finally:
            with self._lock:
                self._inflight.pop(flight, None)
            waiter.set()

    def submit_request(self, method: str, path: str, body: bytes, headers: Mapping[str, str | None], *,
                       now: int | None = None) -> tuple[int, dict[str, Any]]:
        """``POST /qsearch/v2/submit_request``: 404 NOT_ENABLED unless ``v2_enabled``; v1 auth; one request."""
        if not self.config.v2_enabled:
            return 404, dict(NOT_ENABLED)
        if len(body) > self.config.max_body_bytes:
            return 413, {"error": "body too large"}
        now = int(self._clock()) if now is None else now
        refusal = self._authenticate(method, path, body, headers, now)
        if refusal is not None:
            return refusal
        try:
            parsed = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
            request = parse_request(parsed, max_blocks=self.config.v2_max_pubs)
        except RequestValidationError as exc:
            return 400, {"error": "invalid request", "code": exc.code}
        except (ValueError, UnicodeDecodeError, RecursionError):
            return 400, {"error": "invalid request", "code": "JSON"}
        return self._run_request_once(request, canonical_json(parsed))

    def recover(self) -> list[str]:
        """Finish every ACCEPTED job and request left by a previous process; return the digests completed."""
        recovered = []
        for record in self.store.pending():
            try:
                job = parse_job(json.loads(record.job_json))
            except (JobValidationError, ValueError, RecursionError) as exc:
                _log(f"pending job {record.job_digest} is unreadable and stays pending: {type(exc).__name__}")
                continue
            status, _ = self._run_once(job, recovering=True)
            if status == 200:
                recovered.append(job.job_digest)
        for row in self.store.pending_requests():
            try:
                request = parse_request(json.loads(row.request_json), max_blocks=MAX_BLOCKS_LIMIT)  # admitted
            except (RequestValidationError, ValueError, RecursionError) as exc:
                _log(f"pending request {row.request_digest} is unreadable and stays pending: {type(exc).__name__}")
                continue
            status, _ = self._run_request_once(request, row.request_json, recovering=True)
            if status == 200:
                recovered.append(request.request_digest)
        return recovered


class _Handler(BaseHTTPRequestHandler):
    service: QSearchService  # set per server by make_server
    timeout: float = DEFAULT_REQUEST_TIMEOUT_S  # socket timeout; make_server sets it from the config

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _content_length(self) -> int:
        try:
            return int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return -1

    def _not_found(self) -> None:
        length = self._content_length()
        if 0 < length <= self.service.config.max_body_bytes:
            self.rfile.read(length)      # drain, so closing the socket does not reset the client mid-response
        self._send(404, dict(NOT_FOUND))

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if (self.path == REQUEST_PATH or _REQUEST_CONTROL.fullmatch(self.path)) and not self.service.config.v2_enabled:
            length = self._content_length()
            if 0 < length <= self.service.config.max_body_bytes:
                self.rfile.read(length)  # drain, so closing the socket does not reset the client mid-response
            self._send(404, dict(NOT_ENABLED))
            return
        if self.path not in (SUBMIT_PATH, REQUEST_PATH) and not _REQUEST_CONTROL.fullmatch(self.path):
            self._not_found()
            return
        length = self._content_length()
        if length < 0:
            payload = (lifecycle_error("INVALID_CONTENT_LENGTH", field="headers.Content-Length")
                       if _REQUEST_CONTROL.fullmatch(self.path) else {"error": "bad content length"})
            self._send(400, payload)
            return
        if length > self.service.config.max_body_bytes:
            # Never read an oversized body. A client still sending it may see the connection reset.
            payload = (lifecycle_error("BODY_TOO_LARGE", field="body")
                       if _REQUEST_CONTROL.fullmatch(self.path) else {"error": "body too large"})
            self._send(413, payload)
            return
        body = self.rfile.read(length)
        headers = {AUTH_HEADER: self.headers.get(AUTH_HEADER)}
        if _REQUEST_CONTROL.fullmatch(self.path):
            status, payload = self.service.control_request("POST", self.path, body, headers)
        elif self.path == REQUEST_PATH:
            status, payload = self.service.submit_request("POST", REQUEST_PATH, body, headers)
        else:
            status, payload = self.service.submit("POST", SUBMIT_PATH, body, headers)
        self._send(status, payload)

    def do_GET(self) -> None:  # noqa: N802
        if _REQUEST_STATUS.fullmatch(self.path):
            length = self._content_length()
            if length != 0:
                self.close_connection = True
                self._send(400, lifecycle_error("EMPTY_BODY_REQUIRED", field="body"))
                return
            status, payload = self.service.status_request("GET", self.path, b"",
                                                         {AUTH_HEADER: self.headers.get(AUTH_HEADER)})
            self._send(status, payload)
        else:
            self._not_found()

    do_PUT = do_DELETE = do_PATCH = _not_found

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def __getattr__(self, name: str) -> Any:
        # Any other method (OPTIONS, TRACE, ...) is a 404 too, rather than http.server's 501 HTML page.
        # Only the current request's own ``do_<METHOD>`` resolves: http.server sets ``command`` in
        # ``parse_request`` before its ``hasattr`` check, and every other missing attribute stays missing.
        command = self.__dict__.get("command") or ""
        if command and name == "do_" + command:
            return self._not_found
        raise AttributeError(name)

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.service.config.verbose:
            super().log_message(fmt, *args)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = LISTEN_BACKLOG     # the listen() backlog
    # On Windows SO_REUSEADDR lets a second process bind a port that is already listening, so
    # two services could silently share one; claim the port exclusively there instead.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(service: QSearchService) -> ThreadingHTTPServer:
    """Bind (and listen on) the configured address; connections queue until ``serve_forever``."""
    handler = type("QSearchHandler", (_Handler,), {"service": service, "timeout": service.config.request_timeout_s})
    return _Server(service.config.host_port, handler)


def serve_in_thread(service: QSearchService) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = make_server(service)
    thread = threading.Thread(target=server.serve_forever, name="nano-qsearch-http", daemon=True)
    thread.start()
    return server, thread


def prepare(config: ServiceConfig, *, runtime_factory: RuntimeFactory = build_runtime
            ) -> tuple[QSearchService, ThreadingHTTPServer, list[str]]:
    """Lock the data dir, bind the port, then recover pending jobs (requests wait in the backlog meanwhile).

    Raises ``DataDirLocked`` before binding when another service owns the data dir.
    """
    service = QSearchService(config, runtime_factory=runtime_factory)
    server = None
    try:
        server = make_server(service)
        recovered = service.recover()
    except BaseException:
        if server is not None:
            server.server_close()
        service.close()
        raise
    return service, server, recovered


def startup_banner(config: ServiceConfig, address: tuple[str, int], recovered: int) -> str:
    """The one stderr line printed at startup: backend, bind and recovery count, never key material."""
    return f"nano-qsearch-serve: backend={config.backend} bind={address[0]}:{address[1]} recovered={recovered}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nano-qsearch-serve", description="Serve POST /qsearch/v1/submit for Predator Drive.")
    parser.add_argument("--bind")
    parser.add_argument("--backend", choices=SERVICE_KINDS)
    parser.add_argument("--data-dir", dest="data_dir")
    parser.add_argument("--key-path", dest="key_path")
    parser.add_argument("--request-key-path", dest="request_key_path")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = ServiceConfig.from_env(args)
    except (OSError, ValueError) as exc:
        print(f"nano-qsearch-serve: {exc}", file=sys.stderr)
        return 2
    try:
        service, server, recovered = prepare(config)
    except DataDirLocked as exc:
        print(f"nano-qsearch-serve: {exc}", file=sys.stderr)      # a fixed sentence, never OS text
        return 2
    except (OSError, sqlite3.Error) as exc:
        # The class only: OS and SQLite messages can carry paths. A port already in use lands here.
        print(f"nano-qsearch-serve: startup failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(startup_banner(config, server.server_address[:2], len(recovered)), file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
    return 0


__all__ = ["DEFAULT_BIND", "DataDirLocked", "MAX_ATTEMPTS", "NOT_ENABLED", "QSearchService", "REQUEST_PATH", "SEED_MAX",
           "SUBMIT_PATH", "ServiceConfig", "V2_ENV", "main", "make_server", "prepare", "serve_in_thread",
           "startup_banner"]


if __name__ == "__main__":
    sys.exit(main())
