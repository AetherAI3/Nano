# nano_qsearch/store.py
"""Durable job and nonce store for the q-search service (SQLite, stdlib).

A job row is keyed by ``job_digest`` and moves ACCEPTED → DONE exactly once.
``pending()`` lists ACCEPTED rows so a restarted service can re-run what it
accepted before a crash; ``complete`` refuses to overwrite a DONE row so a
second divergent result can never replace the first. Nonces live in the same
file so replay protection survives a restart. One connection, one lock.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_digest TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('ACCEPTED', 'DONE')),
  job_json TEXT NOT NULL,
  receipt_json TEXT,
  accepted_at REAL NOT NULL,
  completed_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0
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


class JobStore:
    _COLS = "job_digest, state, job_json, receipt_json, accepted_at, completed_at, attempts"

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self._path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA busy_timeout=5000")      # first, so switching to WAL can wait out a peer's lock
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _row(row) -> JobRecord | None:
        return None if row is None else JobRecord(*row)

    def get(self, job_digest: str) -> JobRecord | None:
        with self._lock:
            return self._row(self._db.execute(f"SELECT {self._COLS} FROM jobs WHERE job_digest=?", (job_digest,)).fetchone())

    def begin(self, job_digest: str, job_json: str) -> JobRecord:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO jobs (job_digest, state, job_json, accepted_at) VALUES (?, 'ACCEPTED', ?, ?)",
                             (job_digest, job_json, time.time()))
            return self._row(self._db.execute(f"SELECT {self._COLS} FROM jobs WHERE job_digest=?", (job_digest,)).fetchone())

    def mark_attempt(self, job_digest: str) -> int:
        """Increment and return the attempt count in one statement; ``KeyError`` when the digest is unknown.

        ``RETURNING`` needs SQLite >= 3.35 (2021); the CPython builds this targets bundle newer.
        """
        with self._lock:
            # fetchall drains the cursor so the statement finishes and its autocommit lands now
            rows = self._db.execute("UPDATE jobs SET attempts = attempts + 1 WHERE job_digest=? RETURNING attempts",
                                    (job_digest,)).fetchall()
            if not rows:
                raise KeyError(job_digest)
            return int(rows[0][0])

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

    def remember(self, nonce: str, ts: int) -> bool:
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO nonces (nonce, ts) VALUES (?, ?)", (nonce, ts))
            return cur.rowcount == 1

    def prune_nonces(self, *, before_ts: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM nonces WHERE ts < ?", (before_ts,))


__all__ = ["JobRecord", "JobStore"]
