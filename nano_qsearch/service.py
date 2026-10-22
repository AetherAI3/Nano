"""The q-search HTTP service: ``POST /qsearch/v1/submit``.

Order of operations for one request: refuse an oversized body (413, before
any MAC work, so it never consumes a nonce), authenticate (``auth``; a failure
is 401 with no receipt and nothing stored), parse the job (``job``; a failure
is 400 with no receipt), look the ``job_digest`` up in the durable store (a
DONE job returns its stored receipt without recomputing), run the selected
runtime at most once per digest at a time (single-flight across threads), seal
the receipt (``receipt``) and store it. A job over a service cap (variables,
shots, bond dimension, seed range) gets a sealed REFUSED receipt instead of a
run; any exception on the run path becomes a sealed NOT_RUN receipt.

Every run is preceded by ``store.mark_attempt``. On startup ``recover()``
finishes every job that was accepted but never completed: it re-runs it once
with the same body (same seed, same digest) unless that would be the third
attempt or the job targets IBM hardware, which is never resubmitted; those
complete as NOT_RUN without running.
"""
from __future__ import annotations

import argparse
import json
import os
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
SEED_MAX = 2**32 - 1          # Aer and the transpiler take uint32 seeds; Predator allows any integer
MAX_ATTEMPTS = 2              # the original attempt plus one recovery re-run
STORE_FILE = "qsearch.sqlite3"
NOT_COMPLETED = {"error": "job not completed"}


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON constant {name}")


def _read_key(path: str, name: str) -> bytes:
    data = Path(path).read_bytes()
    if len(data) < MIN_KEY_BYTES:
        raise ValueError(f"{name} must hold at least {MIN_KEY_BYTES} bytes")
    return data


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
    verbose: bool = False

    def __post_init__(self) -> None:
        if self.backend not in SERVICE_KINDS:
            raise ValueError(f"backend must be one of {SERVICE_KINDS}")
        for name, key in (("receipt_key", self.receipt_key), ("request_key", self.request_key)):
            if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
                raise ValueError(f"{name} must be at least {MIN_KEY_BYTES} bytes")
        if bytes(self.receipt_key) == bytes(self.request_key):
            raise ValueError("receipt key and request key must differ")
        for name in ("max_variables", "max_body_bytes", "max_shots", "max_chi"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
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
            return int(env.get(var, default))

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
                   verbose=bool(getattr(args, "verbose", False)))


RuntimeFactory = Callable[[str], Any]


def _log(message: str) -> None:
    print(f"nano-qsearch: {message}", file=sys.stderr, flush=True)


class QSearchService:
    def __init__(self, config: ServiceConfig, *, runtime_factory: RuntimeFactory = build_runtime) -> None:
        self.config = config
        self.store = JobStore(config.data_dir / STORE_FILE)
        self._runtime_factory = runtime_factory
        self._runtimes: dict[str, Any] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self.store.close()

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

    def _execute(self, job: QSearchJob) -> dict[str, Any]:
        """Run (or refuse) one job and return the SEALED receipt; never raises."""
        try:
            reason = self._cap_violation(job)
            if reason is not None:
                unsigned = refused_receipt(job, reason)
            else:
                unsigned = self._runtime(select_kind(self.config.backend, job)).run(job)
            return seal_receipt(unsigned, self.config.receipt_key)
        except Exception as exc:  # noqa: BLE001 - any failure is reported as NOT_RUN by class name only
            _log(f"job {job.job_digest} not run: {type(exc).__name__}")
            return seal_receipt(not_run_receipt(job, f"service: {type(exc).__name__}"), self.config.receipt_key)

    def _attempt(self, job: QSearchJob, record: JobRecord, *, recovering: bool) -> dict[str, Any]:
        """Decide, from the stored attempt count, whether this job may run; return the sealed receipt."""
        resuming = recovering or record.attempts > 0
        if record.attempts >= MAX_ATTEMPTS:
            unsigned = not_run_receipt(job, "recovery: attempt limit reached")
        elif resuming and select_kind(self.config.backend, job) == "ibm":
            unsigned = not_run_receipt(job, "recovery: hardware jobs are never resubmitted")
        else:
            self.store.mark_attempt(job.job_digest)
            return self._execute(job)
        return seal_receipt(unsigned, self.config.receipt_key)

    def _stored_receipt(self, job_digest: str) -> tuple[int, dict[str, Any]] | None:
        record = self.store.get(job_digest)
        if record is None or record.state != "DONE":
            return None
        return 200, json.loads(record.receipt_json)

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
            record = self.store.begin(job.job_digest, canonical_json(job.payload))
            sealed = self._attempt(job, record, recovering=recovering)
            self.store.complete(job.job_digest, json.dumps(sealed, sort_keys=True, allow_nan=False))
            return 200, sealed
        except Exception as exc:  # noqa: BLE001 - the store failed; the row stays ACCEPTED for a later attempt
            _log(f"job {job.job_digest} not completed: {type(exc).__name__}")
            return 503, dict(NOT_COMPLETED)
        finally:
            with self._lock:
                self._inflight.pop(job.job_digest, None)
            waiter.set()

    def submit(self, method: str, path: str, body: bytes, headers: Mapping[str, str | None], *,
               now: int | None = None) -> tuple[int, dict[str, Any]]:
        if len(body) > self.config.max_body_bytes:
            return 413, {"error": "body too large"}
        now = int(time.time()) if now is None else now
        try:
            verify_request(self.config.request_key, headers.get(AUTH_HEADER), method, path, body, now=now, nonce_store=self.store)
        except AuthError as exc:
            return 401, {"error": "unauthorized", "code": exc.code}
        self.store.prune_nonces(before_ts=now - 2 * WINDOW_S)
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

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = self._content_length()
        if self.path != SUBMIT_PATH:
            if 0 < length <= self.service.config.max_body_bytes:
                self.rfile.read(length)      # drain, so closing the socket does not reset the client mid-response
            self._send(404, {"error": "not found"})
            return
        if length < 0:
            self._send(400, {"error": "bad content length"})
            return
        if length > self.service.config.max_body_bytes:
            # Never read an oversized body. A client still sending it may see the connection reset.
            self._send(413, {"error": "body too large"})
            return
        body = self.rfile.read(length)
        status, payload = self.service.submit("POST", self.path, body, {AUTH_HEADER: self.headers.get(AUTH_HEADER)})
        self._send(status, payload)

    def do_GET(self) -> None:  # noqa: N802
        self._send(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.service.config.verbose:
            super().log_message(fmt, *args)


def make_server(service: QSearchService) -> ThreadingHTTPServer:
    handler = type("QSearchHandler", (_Handler,), {"service": service})
    server = ThreadingHTTPServer(service.config.host_port, handler)
    server.daemon_threads = True
    return server


def serve_in_thread(service: QSearchService) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = make_server(service)
    thread = threading.Thread(target=server.serve_forever, name="nano-qsearch-http", daemon=True)
    thread.start()
    return server, thread


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
    service = QSearchService(config)
    recovered = service.recover()
    server = make_server(service)
    print(startup_banner(config, server.server_address[:2], len(recovered)), file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
    return 0


__all__ = ["DEFAULT_BIND", "MAX_ATTEMPTS", "QSearchService", "SEED_MAX", "SUBMIT_PATH", "ServiceConfig", "main",
           "make_server", "serve_in_thread", "startup_banner"]
