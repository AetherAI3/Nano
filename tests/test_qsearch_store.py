# tests/test_qsearch_store.py
import json
import threading

import pytest

from nano_qsearch import store as store_mod
from nano_qsearch.store import JobRecord, JobStore

D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "qsearch.sqlite3")
    yield s
    s.close()


def test_begin_complete_get(store):
    assert store.get(D1) is None
    rec = store.begin(D1, '{"job":1}')
    assert isinstance(rec, JobRecord) and rec.state == "ACCEPTED" and rec.receipt_json is None
    assert store.begin(D1, '{"job":1}').state == "ACCEPTED"      # idempotent, same row
    store.complete(D1, '{"receipt":1}')
    got = store.get(D1)
    assert got.state == "DONE" and json.loads(got.receipt_json) == {"receipt": 1} and got.completed_at >= got.accepted_at


def test_complete_unknown_or_done_rejected(store):
    with pytest.raises(KeyError):
        store.complete(D1, "{}")
    store.begin(D1, "{}")
    store.complete(D1, "{}")
    with pytest.raises(ValueError):
        store.complete(D1, '{"second":true}')


def test_pending_lists_only_accepted(store):
    store.begin(D1, '{"a":1}')
    store.begin(D2, '{"b":2}')
    store.complete(D2, "{}")
    assert [r.job_digest for r in store.pending()] == [D1]


def test_survives_reopen(tmp_path):
    path = tmp_path / "db.sqlite3"
    s = JobStore(path); s.begin(D1, "{}"); s.close()
    s2 = JobStore(path)
    assert s2.get(D1).state == "ACCEPTED" and [r.job_digest for r in s2.pending()] == [D1]
    s2.close()


def test_nonces(store):
    assert store.remember("ab" * 16, 100) is True
    assert store.remember("ab" * 16, 101) is False
    store.prune_nonces(before_ts=150)
    assert store.remember("ab" * 16, 160) is True     # pruned, acceptable again (window has passed)


def test_thread_safe(store):
    def work(i):
        d = "sha256:" + format(i, "064x")
        store.begin(d, "{}"); store.complete(d, "{}")
    threads = [threading.Thread(target=work, args=(i,)) for i in range(16)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(store.pending()) == 0 and store.get("sha256:" + format(3, "064x")).state == "DONE"


@pytest.fixture(params=["returning", "pre_3_35"])
def any_store(request, tmp_path, monkeypatch):
    """A fresh store on each ``mark_attempt`` branch: ``UPDATE ... RETURNING`` and the pre-3.35 fallback."""
    monkeypatch.setattr(store_mod, "_HAS_RETURNING", request.param == "returning")
    s = JobStore(tmp_path / "qsearch.sqlite3")
    yield s
    s.close()


def test_mark_attempt_increments(any_store):
    any_store.begin(D1, "{}")
    assert any_store.mark_attempt(D1) == 1
    assert any_store.mark_attempt(D1) == 2
    assert any_store.get(D1).attempts == 2


def test_mark_attempt_unknown_digest_raises(any_store):
    with pytest.raises(KeyError):
        any_store.mark_attempt(D1)
    any_store.begin(D2, "{}")                       # the failed call left no transaction open
    assert any_store.mark_attempt(D2) == 1


def test_mark_attempt_fallback_is_thread_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "_HAS_RETURNING", False)
    s = JobStore(tmp_path / "qsearch.sqlite3")
    s.begin(D1, "{}")
    seen = []
    threads = [threading.Thread(target=lambda: seen.append(s.mark_attempt(D1))) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(seen) == list(range(1, 9)) and s.get(D1).attempts == 8
    s.close()


def test_returning_flag_matches_the_linked_sqlite():
    import sqlite3
    assert store_mod._HAS_RETURNING is (sqlite3.sqlite_version_info >= (3, 35, 0))


def test_rejected_second_complete_leaves_receipt_unchanged(store):
    store.begin(D1, "{}")
    store.complete(D1, '{"first":true}')
    before = store.get(D1)
    with pytest.raises(ValueError):
        store.complete(D1, '{"second":true}')
    after = store.get(D1)
    assert after.receipt_json == '{"first":true}' and after == before


def test_nonces_survive_reopen(tmp_path):
    path = tmp_path / "db.sqlite3"
    s = JobStore(path)
    assert s.remember("ab" * 16, 100) is True
    s.close()
    s2 = JobStore(path)
    assert s2.remember("ab" * 16, 101) is False       # still remembered: replay refused after restart
    assert s2.remember("cd" * 16, 101) is True
    s2.close()


def test_mark_attempt_is_durable_across_reopen(tmp_path):
    path = tmp_path / "db.sqlite3"
    s = JobStore(path)
    s.begin(D1, "{}")
    assert s.mark_attempt(D1) == 1
    s.close()
    s2 = JobStore(path)
    assert s2.get(D1).attempts == 1 and s2.mark_attempt(D1) == 2
    s2.close()


def test_begin_records_kind_and_first_call_wins(store):
    assert store.begin(D1, "{}", kind="statevector").kind == "statevector"
    assert store.begin(D1, "{}", kind="mps").kind == "statevector"      # INSERT OR IGNORE keeps the original
    assert store.begin(D2, "{}").kind is None
    assert store.pending()[0].kind == "statevector"


def test_old_schema_is_migrated_with_a_kind_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(str(path))
    db.executescript("""
        CREATE TABLE jobs (job_digest TEXT PRIMARY KEY, state TEXT NOT NULL CHECK (state IN ('ACCEPTED', 'DONE')),
                           job_json TEXT NOT NULL, receipt_json TEXT, accepted_at REAL NOT NULL, completed_at REAL,
                           attempts INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE nonces (nonce TEXT PRIMARY KEY, ts INTEGER NOT NULL);
        INSERT INTO jobs (job_digest, state, job_json, accepted_at, attempts) VALUES ('%s', 'ACCEPTED', '{}', 1.0, 1);
    """ % D1)
    db.commit()
    db.close()
    s = JobStore(path)
    try:
        old = s.get(D1)
        assert old.kind is None and old.attempts == 1 and old.state == "ACCEPTED"
        assert s.begin(D2, "{}", kind="mps").kind == "mps"
    finally:
        s.close()
    s = JobStore(path)      # reopening a migrated file is a no-op
    try:
        assert s.get(D2).kind == "mps"
    finally:
        s.close()
