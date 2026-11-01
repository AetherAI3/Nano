# nano_qsearch/store.py
"""Durable job and nonce store for the q-search service (SQLite, stdlib).

A job row is keyed by ``job_digest`` and moves ACCEPTED → DONE exactly once.
``pending()`` lists ACCEPTED rows so a restarted service can re-run what it
accepted before a crash; ``complete`` refuses to overwrite a DONE row so a
second divergent result can never replace the first. Nonces live in the same
file so replay protection survives a restart. One connection, one lock.
Each row also records the runtime ``kind`` chosen at accept time, so recovery
re-runs on the same backend; files from before that column are migrated in
place (``kind`` is NULL on their old rows).

v2 requests (lane HW0) live in their own ``requests`` table, keyed by
``request_digest`` with a unique ``request_id``: a row is recorded ACCEPTED
before anything is submitted, its attempt is marked immediately before the
single submission, and it moves to DONE exactly once with the sealed v2
receipt. ``pending_requests()`` lists ACCEPTED rows for restart recovery.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Detected once: ``UPDATE ... RETURNING`` arrived in SQLite 3.35.0.
_HAS_RETURNING = sqlite3.sqlite_version_info >= (3, 35, 0)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_digest TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('ACCEPTED', 'DONE')),
  job_json TEXT NOT NULL,
  receipt_json TEXT,
  accepted_at REAL NOT NULL,
  completed_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  kind TEXT
);
CREATE TABLE IF NOT EXISTS requests (
  request_digest TEXT PRIMARY KEY,
  request_id TEXT UNIQUE NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('ACCEPTED', 'DONE')),
  request_json TEXT NOT NULL,
  receipt_json TEXT,
  accepted_at REAL NOT NULL,
  completed_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  record_json TEXT
);
CREATE TABLE IF NOT EXISTS nonces (
  nonce TEXT PRIMARY KEY,
  ts INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class JobRecord:
    job_digest: str
    state: str
    job_json: str
    receipt_json: str | None
    accepted_at: float
    completed_at: float | None
    attempts: int
    kind: str | None = None     # runtime kind chosen at accept time; None on rows written before the column existed


@dataclass(frozen=True)
class RequestRecord:
    request_digest: str
    request_id: str
    state: str
    request_json: str
    receipt_json: str | None
    accepted_at: float
    completed_at: float | None
    attempts: int


class RequestIdConflict(ValueError):
    """A different request digest already owns this ``request_id``."""


class JobStore:
    _COLS = "job_digest, state, job_json, receipt_json, accepted_at, completed_at, attempts, kind"

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self._path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA busy_timeout=5000")      # first, so switching to WAL can wait out a peer's lock
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring a file written by an older version up to the current schema (``jobs.kind``, ``requests.record_json``)."""
        request_columns = {row[1] for row in self._db.execute("PRAGMA table_info(requests)").fetchall()}
        if "record_json" not in request_columns:
            self._db.execute("ALTER TABLE requests ADD COLUMN record_json TEXT")
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(jobs)").fetchall()}
        if "kind" not in columns:
            self._db.execute("ALTER TABLE jobs ADD COLUMN kind TEXT")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _row(row) -> JobRecord | None:
        return None if row is None else JobRecord(*row)

    def get(self, job_digest: str) -> JobRecord | None:
        with self._lock:
            return self._row(self._db.execute(f"SELECT {self._COLS} FROM jobs WHERE job_digest=?", (job_digest,)).fetchone())

    def begin(self, job_digest: str, job_json: str, kind: str | None = None) -> JobRecord:
        """Insert an ACCEPTED row (first call wins: an existing row keeps its body and ``kind``) and return it."""
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO jobs (job_digest, state, job_json, accepted_at, kind) "
                             "VALUES (?, 'ACCEPTED', ?, ?, ?)", (job_digest, job_json, time.time(), kind))
            return self._row(self._db.execute(f"SELECT {self._COLS} FROM jobs WHERE job_digest=?", (job_digest,)).fetchone())

    def mark_attempt(self, job_digest: str) -> int:
        """Increment and return the attempt count atomically; ``KeyError`` when the digest is unknown.

        ``UPDATE ... RETURNING`` needs SQLite >= 3.35 (2021); older libraries
        (Ubuntu 20.04 ships 3.31) do the UPDATE and a SELECT inside one
        ``BEGIN IMMEDIATE`` transaction instead.
        """
        with self._lock:
            if _HAS_RETURNING:
                # fetchall drains the cursor so the statement finishes and its autocommit lands now
                rows = self._db.execute("UPDATE jobs SET attempts = attempts + 1 WHERE job_digest=? RETURNING attempts",
                                        (job_digest,)).fetchall()
            else:
                rows = self._update_then_select_attempts(job_digest)
            if not rows:
                raise KeyError(job_digest)
            return int(rows[0][0])

    def _update_then_select_attempts(self, job_digest: str) -> list[tuple]:
        """The pre-3.35 path: one explicit write transaction (the connection is autocommit). Caller holds the lock."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = self._db.execute("UPDATE jobs SET attempts = attempts + 1 WHERE job_digest=?", (job_digest,))
            rows = (self._db.execute("SELECT attempts FROM jobs WHERE job_digest=?", (job_digest,)).fetchall()
                    if cur.rowcount == 1 else [])
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:     # a failed COMMIT may already have ended it; never mask the cause
                self._db.execute("ROLLBACK")
            raise
        return rows

    def complete(self, job_digest: str, receipt_json: str) -> None:
        """ACCEPTED → DONE in one statement; ``KeyError`` if unknown, ``ValueError`` if already DONE."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE jobs SET state='DONE', receipt_json=?, completed_at=? WHERE job_digest=? AND state='ACCEPTED'",
                (receipt_json, time.time(), job_digest))
            if cur.rowcount == 1:
                return
            row = self._db.execute("SELECT state FROM jobs WHERE job_digest=?", (job_digest,)).fetchone()
            if row is None:
                raise KeyError(job_digest)
            raise ValueError("job already completed; a second result is never stored")

    def pending(self) -> list[JobRecord]:
        with self._lock:
            rows = self._db.execute(f"SELECT {self._COLS} FROM jobs WHERE state='ACCEPTED' ORDER BY accepted_at").fetchall()
        return [self._row(r) for r in rows]

    # --- v2 requests -------------------------------------------------------------------------

    _REQUEST_COLS = "request_digest, request_id, state, request_json, receipt_json, accepted_at, completed_at, attempts"

    def _request_row(self, request_digest: str) -> RequestRecord | None:
        row = self._db.execute(f"SELECT {self._REQUEST_COLS} FROM requests WHERE request_digest=?",
                               (request_digest,)).fetchone()
        return None if row is None else RequestRecord(*row)

    def get_request(self, request_digest: str) -> RequestRecord | None:
        with self._lock:
            return self._request_row(request_digest)

    def begin_request(self, request_digest: str, request_id: str, request_json: str) -> RequestRecord:
        """Insert an ACCEPTED request row (first call wins) and return it; ``RequestIdConflict`` when
        another digest already owns ``request_id``."""
        with self._lock:
            try:
                self._db.execute("INSERT OR IGNORE INTO requests (request_digest, request_id, state, request_json, "
                                 "accepted_at) VALUES (?, ?, 'ACCEPTED', ?, ?)",
                                 (request_digest, request_id, request_json, time.time()))
            except sqlite3.IntegrityError:
                raise RequestIdConflict("request_id already belongs to another request") from None
            row = self._request_row(request_digest)
            if row is None:
                raise RequestIdConflict("request_id already belongs to another request")
            return row

    def mark_request_attempt(self, request_digest: str) -> int:
        """Increment and return the request's attempt count; ``KeyError`` when the digest is unknown."""
        with self._lock:
            cur = self._db.execute("UPDATE requests SET attempts = attempts + 1 WHERE request_digest=?",
                                   (request_digest,))
            if cur.rowcount != 1:
                raise KeyError(request_digest)
            return int(self._db.execute("SELECT attempts FROM requests WHERE request_digest=?",
                                        (request_digest,)).fetchone()[0])

    def set_request_record(self, request_digest: str, record_json: str) -> None:
        """Keep the run record (compile seconds, ...) beside the request; never part of the sealed receipt."""
        with self._lock:
            cur = self._db.execute("UPDATE requests SET record_json=? WHERE request_digest=?",
                                   (record_json, request_digest))
            if cur.rowcount != 1:
                raise KeyError(request_digest)

    def reseal_request(self, request_digest: str, old_receipt_json: str, new_receipt_json: str) -> None:
        """Replace a DONE receipt sealed while usage was pending by its settled re-seal (compare-and-swap).

        ``ValueError`` unless the row is DONE and still holds exactly ``old_receipt_json``; the caller
        keeps the replaced seal in the request record, so the history stays visible.
        """
        with self._lock:
            cur = self._db.execute("UPDATE requests SET receipt_json=?, completed_at=? WHERE request_digest=? "
                                   "AND state='DONE' AND receipt_json=?",
                                   (new_receipt_json, time.time(), request_digest, old_receipt_json))
            if cur.rowcount != 1:
                raise ValueError("stored receipt changed or is not DONE; nothing re-sealed")

    def request_record(self, request_digest: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT record_json FROM requests WHERE request_digest=?",
                                   (request_digest,)).fetchone()
        return None if row is None else row[0]

    def complete_request(self, request_digest: str, receipt_json: str) -> None:
        """ACCEPTED → DONE in one statement; ``KeyError`` if unknown, ``ValueError`` if already DONE."""
        with self._lock:
            cur = self._db.execute("UPDATE requests SET state='DONE', receipt_json=?, completed_at=? "
                                   "WHERE request_digest=? AND state='ACCEPTED'",
                                   (receipt_json, time.time(), request_digest))
            if cur.rowcount == 1:
                return
            if self._request_row(request_digest) is None:
                raise KeyError(request_digest)
            raise ValueError("request already completed; a second result is never stored")

    def pending_requests(self) -> list[RequestRecord]:
        with self._lock:
            rows = self._db.execute(f"SELECT {self._REQUEST_COLS} FROM requests WHERE state='ACCEPTED' "
                                    "ORDER BY accepted_at").fetchall()
        return [RequestRecord(*row) for row in rows]

    def remember(self, nonce: str, ts: int) -> bool:
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO nonces (nonce, ts) VALUES (?, ?)", (nonce, ts))
            return cur.rowcount == 1

    def prune_nonces(self, *, before_ts: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM nonces WHERE ts < ?", (before_ts,))


__all__ = ["JobRecord", "JobStore", "RequestIdConflict", "RequestRecord"]
