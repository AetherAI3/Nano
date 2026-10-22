# tests/test_qsearch_store.py
import json
import threading

import pytest

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


def test_mark_attempt_increments(store):
    store.begin(D1, "{}")
    assert store.mark_attempt(D1) == 1
    assert store.mark_attempt(D1) == 2


def test_mark_attempt_unknown_digest_raises(store):
    with pytest.raises(KeyError):
        store.mark_attempt(D1)


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
