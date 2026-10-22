"""The q-search HTTP service: ``POST /qsearch/v1/submit``.

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
"""
from __future__ import annotations

import argparse
import errno
import json
import math
import os
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
from .receipt import MIN_KEY_BYTES, not_run_receipt, refused_receipt, seal_receipt
from .runtime_mps import SERVICE_KINDS, build_runtime, select_kind
from .store import JobRecord, JobStore

SUBMIT_PATH = "/qsearch/v1/submit"
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
    data = Path(path).read_bytes()
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
                   verbose=bool(getattr(args, "verbose", False)))


RuntimeFactory = Callable[[str], Any]


def _log(message: str) -> None:
    print(f"nano-qsearch: {message}", file=sys.stderr, flush=True)


class QSearchService:
    def __init__(self, config: ServiceConfig, *, runtime_factory: RuntimeFactory = build_runtime) -> None:
        self.config = config
        self._dir_lock = _DataDirLock(config.data_dir / LOCK_FILE)      # raises DataDirLocked
        try:
            self.store = JobStore(config.data_dir / STORE_FILE)
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

    def _sealed(self, job: QSearchJob, build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Seal the receipt ``build()`` returns; any failure becomes a sealed NOT_RUN naming the class only."""
        try:
            return seal_receipt(build(), self.config.receipt_key)
        except Exception as exc:  # noqa: BLE001 - any failure is reported as NOT_RUN by class name only
            _log(f"job {job.job_digest} not run: {type(exc).__name__}")
            return seal_receipt(not_run_receipt(job, f"service: {type(exc).__name__}"), self.config.receipt_key)

    def _execute(self, job: QSearchJob, kind: str) -> dict[str, Any]:
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

    def _attempt(self, job: QSearchJob, record: JobRecord, *, recovering: bool) -> dict[str, Any]:
        """Decide, from the stored attempt count, whether this job may run; return the sealed receipt.

        The runtime kind is the one recorded when the job was accepted; rows from before
        that column existed fall back to the current service backend.
        """
        kind = record.kind or select_kind(self.config.backend, job)
        resuming = recovering or record.attempts > 0
        if record.attempts >= MAX_ATTEMPTS:
            unsigned = not_run_receipt(job, "recovery: attempt limit reached")
        elif resuming and kind == "ibm":
            unsigned = not_run_receipt(job, "recovery: hardware jobs are never resubmitted")
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

    def recover(self) -> list[str]:
        """Finish every ACCEPTED job left by a previous process; return the digests completed."""
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
        if self.path != SUBMIT_PATH:
            self._not_found()
            return
        length = self._content_length()
        if length < 0:
            self._send(400, {"error": "bad content length"})
            return
        if length > self.service.config.max_body_bytes:
            # Never read an oversized body. A client still sending it may see the connection reset.
            self._send(413, {"error": "body too large"})
            return
        body = self.rfile.read(length)
        status, payload = self.service.submit("POST", SUBMIT_PATH, body, {AUTH_HEADER: self.headers.get(AUTH_HEADER)})
        self._send(status, payload)

    def do_GET(self) -> None:  # noqa: N802
        self._not_found()

    do_PUT = do_DELETE = do_PATCH = do_GET

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


__all__ = ["DEFAULT_BIND", "DataDirLocked", "MAX_ATTEMPTS", "QSearchService", "SEED_MAX", "SUBMIT_PATH", "ServiceConfig",
           "main", "make_server", "prepare", "serve_in_thread", "startup_banner"]
