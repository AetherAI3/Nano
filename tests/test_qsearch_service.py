import dataclasses
import errno
import http.client
import itertools
import json
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from nano_qsearch import service as service_mod
from nano_qsearch.auth import AUTH_HEADER, WINDOW_S, AuthError, sign_request, verify_request
from nano_qsearch.canonical import canonical_digest
from nano_qsearch.receipt import ok_receipt, verify_seal
from nano_qsearch.service import (
    MAX_ATTEMPTS, SEED_MAX, DataDirLocked, QSearchService, ServiceConfig, prepare, serve_in_thread, startup_banner,
)
from nano_qsearch.store import JobStore

RECEIPT_KEY = b"receipt-key-" + b"0" * 20
REQUEST_KEY = b"request-key-" + b"1" * 20
PATH = "/qsearch/v1/submit"


def _minimiser(job):
    """The job's true minimising assignment, by enumerating all 2^n bit strings."""
    return min(("".join(map(str, x)) for x in itertools.product((0, 1), repeat=job.n)),
               key=lambda bits: job.energy([int(c) for c in bits]))


class FakeRuntime:
    kind = "statevector"

    def __init__(self):
        self.calls = 0
        self.slow = threading.Event()
        self.slow.set()

    def run(self, job):
        self.calls += 1
        self.slow.wait()
        bits = _minimiser(job)
        energy = job.energy([int(c) for c in bits])
        return ok_receipt(job, {"kind": "statevector", "backend_id": "fake", "variant": "ordinary", "nuclei": [],
                                "compiled_plan_digest": "sha256:" + "a" * 64, "compile": {},
                                "metrics": {"logical_2q": 0, "native_2q": 0, "native_depth": 0},
                                "chi_max": None, "chi_reached": None, "truncated": None, "shots": 1, "seed": 7,
                                "wall_s": 0.0, "attempts": 1, "samples": [[bits, 1, energy]],
                                "best_sample_energy": energy, "mean_energy_sample": energy,
                                "mean_energy_ideal": None, "tvd_ideal": None, "exact_min_energy": None})


class ExplodingRuntime(FakeRuntime):
    def run(self, job):
        self.calls += 1
        raise RuntimeError("C:\\secret\\path should never reach a receipt")


def _config(tmp_path, **over):
    base = dict(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path, receipt_key=RECEIPT_KEY, request_key=REQUEST_KEY)
    return ServiceConfig(**{**base, **over})


@pytest.fixture
def body_bytes(fixtures_dir):
    return (fixtures_dir / "job_4var.json").read_bytes()


def _job_bytes(fixtures_dir, mutate):
    """The 4var job with ``mutate(body)`` applied and the digest re-signed."""
    body = json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))
    mutate(body)
    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    return json.dumps({**unsigned, "job_digest": canonical_digest(unsigned)}).encode("utf-8")


@pytest.fixture
def service(tmp_path):
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)
    yield svc, rt
    svc.close()


def _headers(body, ts, nonce="ab" * 16, key=REQUEST_KEY):
    return {AUTH_HEADER: sign_request(key, "POST", PATH, body, ts=ts, nonce=nonce)}


def _seed_pending(tmp_path, body_bytes, attempts):
    """Simulate a process that accepted the job, marked ``attempts`` runs, then died before complete."""
    digest = json.loads(body_bytes)["job_digest"]
    store = JobStore(tmp_path / "qsearch.sqlite3")
    store.begin(digest, body_bytes.decode("utf-8"))
    for _ in range(attempts):
        store.mark_attempt(digest)
    store.close()
    return digest


# --- plan Task 10 base tests -------------------------------------------------

def test_round_trip_sealed_ok(service, body_bytes):
    svc, rt = service
    status, receipt = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
    assert status == 200 and receipt["status"] == "OK"
    verify_seal(receipt, RECEIPT_KEY)
    assert receipt["job_digest"] == json.loads(body_bytes)["job_digest"] and rt.calls == 1
    assert svc.store.get(receipt["job_digest"]).attempts == 1


def test_unauthenticated_gets_401_and_no_receipt(service, body_bytes):
    svc, rt = service
    status, out = svc.submit("POST", PATH, body_bytes, {}, now=1000)
    assert status == 401 and "seal" not in out and "job_digest" not in out and rt.calls == 0
    status, out = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, key=b"wrong-key-" + b"9" * 22), now=1000)
    assert status == 401 and rt.calls == 0
    assert svc.store.get(json.loads(body_bytes)["job_digest"]) is None


def test_replay_rejected(service, body_bytes):
    svc, rt = service
    h = _headers(body_bytes, 1000)
    assert svc.submit("POST", PATH, body_bytes, h, now=1000)[0] == 200
    status, out = svc.submit("POST", PATH, body_bytes, h, now=1001)
    assert (status, out) == (401, {"error": "unauthorized"}) and rt.calls == 1
    # the reason is REPLAY: the nonce is recorded in the service's own store
    with pytest.raises(AuthError) as info:
        verify_request(REQUEST_KEY, h[AUTH_HEADER], "POST", PATH, body_bytes, now=1001, nonce_store=svc.store)
    assert info.value.code == "REPLAY"


def test_stale_timestamp_rejected(service, body_bytes):
    svc, rt = service
    nonce = "5a" * 16
    h = _headers(body_bytes, 1000, nonce=nonce)
    status, out = svc.submit("POST", PATH, body_bytes, h, now=1300)
    assert (status, out) == (401, {"error": "unauthorized"}) and rt.calls == 0
    with pytest.raises(AuthError) as info:
        verify_request(REQUEST_KEY, h[AUTH_HEADER], "POST", PATH, body_bytes, now=1300, nonce_store=svc.store)
    assert info.value.code == "STALE"
    assert svc.store.remember(nonce, 1000) is True      # a stale request never consumed its nonce
    # positive control: the same request inside the window would have been accepted (nonce now taken, so REPLAY)
    with pytest.raises(AuthError) as info:
        verify_request(REQUEST_KEY, h[AUTH_HEADER], "POST", PATH, body_bytes, now=1000, nonce_store=svc.store)
    assert info.value.code == "REPLAY"


def test_401_body_is_generic_for_every_auth_failure(service, body_bytes):
    svc, _ = service
    bodies = {
        "malformed": svc.submit("POST", PATH, body_bytes, {AUTH_HEADER: "v1 nope"}, now=1000)[1],
        "empty": svc.submit("POST", PATH, body_bytes, {AUTH_HEADER: ""}, now=1000)[1],
        "stale": svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce="71" * 16), now=1500)[1],
        "mac": svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce="72" * 16, key=b"k" * 32), now=1000)[1],
    }
    assert all(b == {"error": "unauthorized"} for b in bodies.values()), bodies


def test_identical_resubmission_returns_stored_receipt(service, body_bytes):
    svc, rt = service
    _, first = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce="aa" * 16), now=1000)
    _, second = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1001, nonce="bb" * 16), now=1001)
    assert first == second and rt.calls == 1


def test_invalid_job_is_400_without_receipt(service, body_bytes):
    svc, rt = service
    bad = body_bytes.replace(b'"predator.qsearch.job.v1"', b'"nope"')
    status, out = svc.submit("POST", PATH, bad, _headers(bad, 1000), now=1000)
    assert status == 400 and "seal" not in out and rt.calls == 0
    status, out = svc.submit("POST", PATH, b"{nope", _headers(b"{nope", 1000, nonce="cc" * 16), now=1000)
    assert status == 400 and out["code"] == "JSON"


def test_over_service_cap_is_refused_receipt(tmp_path, body_bytes):
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path, max_variables=3), runtime_factory=lambda kind: rt)
    try:
        status, receipt = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
        assert status == 200 and receipt["status"] == "REFUSED" and rt.calls == 0
        assert receipt["reason"] == "limits: 4 variables exceed the service cap of 3"
        verify_seal(receipt, RECEIPT_KEY)
    finally:
        svc.close()


def test_single_flight_concurrent_identical_jobs(service, body_bytes):
    svc, rt = service
    rt.slow.clear()
    results = []

    def go(nonce):
        results.append(svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce=nonce), now=1000))

    threads = [threading.Thread(target=go, args=(f"{i:02d}" * 16,)) for i in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    rt.slow.set()
    for t in threads:
        t.join()
    assert rt.calls == 1 and len({json.dumps(r[1], sort_keys=True) for r in results}) == 1
    assert {r[0] for r in results} == {200}


def test_runtime_selection_uses_service_default(tmp_path, body_bytes):
    kinds = []

    class RT(FakeRuntime):
        def __init__(self, kind):
            super().__init__()
            self.kind = kind

    svc = QSearchService(_config(tmp_path, backend="mps"), runtime_factory=lambda kind: (kinds.append(kind), RT(kind))[1])
    try:
        svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
        assert kinds == ["mps"]
    finally:
        svc.close()


def test_same_key_for_both_purposes_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        _config(tmp_path, request_key=RECEIPT_KEY)


def test_http_round_trip_over_localhost(tmp_path, body_bytes):
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)
    server, thread = serve_in_thread(svc)
    try:
        url = f"http://127.0.0.1:{server.server_port}{PATH}"
        now = int(time.time())
        req = urllib.request.Request(url, data=body_bytes, method="POST",
                                     headers={"Content-Type": "application/json", **_headers(body_bytes, now)})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.headers["Content-Type"] == "application/json"
            receipt = json.loads(resp.read())
        verify_seal(receipt, RECEIPT_KEY)
        assert receipt["status"] == "OK"
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(urllib.request.Request(url, data=body_bytes, method="POST"), timeout=10)
        assert info.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(urllib.request.Request(url.replace("submit", "x"), data=b"{}", method="POST"), timeout=10)
        assert info.value.code == 404
    finally:
        server.shutdown()
        thread.join()
        svc.close()


def test_http_oversized_content_length_is_413_without_reading_the_body(tmp_path):
    import http.client
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path, max_body_bytes=16), runtime_factory=lambda kind: rt)
    server, thread = serve_in_thread(svc)
    try:
        # Headers only: the server must answer from Content-Length alone, never waiting for the body.
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        conn.putrequest("POST", PATH)
        conn.putheader("Content-Length", str(10**9))
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 413 and json.loads(resp.read()) == {"error": "body too large"} and rt.calls == 0
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        conn.putrequest("POST", PATH)
        conn.putheader("Content-Length", "-5")
        conn.endheaders()
        assert conn.getresponse().status == 400
        conn.close()
    finally:
        server.shutdown()
        thread.join()
        svc.close()


# --- 1. size check before auth -----------------------------------------------

def test_oversized_body_is_413_before_auth_and_burns_no_nonce(tmp_path, body_bytes):
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path, max_body_bytes=len(body_bytes) - 1), runtime_factory=lambda kind: rt)
    try:
        nonce = "de" * 16
        status, out = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce=nonce), now=1000)
        assert (status, out) == (413, {"error": "body too large"}) and rt.calls == 0
        assert svc.store.remember(nonce, 1000) is True          # the nonce was never consumed
        # an unauthenticated oversized body is also 413, not 401: no MAC is computed for it
        assert svc.submit("POST", PATH, body_bytes, {}, now=1000)[0] == 413
    finally:
        svc.close()


# --- 2. service caps → sealed, stored REFUSED ---------------------------------

@pytest.mark.parametrize("mutate, over, reason", [
    (lambda b: b["backend"].__setitem__("shots", 100_001), {}, "limits: backend.shots exceeds the service cap of 100000"),
    (lambda b: b["backend"].__setitem__("shots", 5000), {"max_shots": 4096},
     "limits: backend.shots exceeds the service cap of 4096"),
    (lambda b: b["backend"].__setitem__("chi_max", 257), {}, "limits: backend.chi_max exceeds the service cap of 256"),
    (lambda b: b["backend"].__setitem__("seed", -1), {}, "limits: backend.seed outside 0..4294967295"),
    (lambda b: b["backend"].__setitem__("seed", 2**32), {}, "limits: backend.seed outside 0..4294967295"),
    (lambda b: b["compile"].__setitem__("seed", -7), {}, "limits: compile.seed outside 0..4294967295"),
    (lambda b: b["compile"].__setitem__("seed", 2**40), {}, "limits: compile.seed outside 0..4294967295"),
])
def test_service_caps_give_sealed_stored_refused_receipt(tmp_path, fixtures_dir, mutate, over, reason):
    body = _job_bytes(fixtures_dir, mutate)
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path, **over), runtime_factory=lambda kind: rt)
    try:
        status, receipt = svc.submit("POST", PATH, body, _headers(body, 1000), now=1000)
        assert status == 200 and receipt["status"] == "REFUSED" and receipt["reason"] == reason and rt.calls == 0
        verify_seal(receipt, RECEIPT_KEY)
        rec = svc.store.get(receipt["job_digest"])
        assert rec.state == "DONE" and json.loads(rec.receipt_json) == receipt
    finally:
        svc.close()


def test_seed_bounds_are_inclusive(tmp_path, fixtures_dir):
    def edge(b):
        b["backend"]["seed"] = SEED_MAX
        b["compile"]["seed"] = 0
    body = _job_bytes(fixtures_dir, edge)
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)
    try:
        status, receipt = svc.submit("POST", PATH, body, _headers(body, 1000), now=1000)
        assert status == 200 and receipt["status"] == "OK" and rt.calls == 1
    finally:
        svc.close()


def test_caps_are_read_from_env(tmp_path, monkeypatch):
    (tmp_path / "r.key").write_bytes(RECEIPT_KEY)
    (tmp_path / "q.key").write_bytes(REQUEST_KEY)
    for var, value in {"NANO_QSEARCH_DATA_DIR": str(tmp_path), "NANO_QSEARCH_KEY_PATH": str(tmp_path / "r.key"),
                       "NANO_QSEARCH_REQUEST_KEY_PATH": str(tmp_path / "q.key"), "NANO_QSEARCH_MAX_SHOTS": "2048",
                       "NANO_QSEARCH_MAX_CHI": "32", "NANO_QSEARCH_MAX_VARIABLES": "12",
                       "NANO_QSEARCH_MAX_BODY_BYTES": "4096"}.items():
        monkeypatch.setenv(var, value)
    for var in ("NANO_QSEARCH_BIND", "NANO_QSEARCH_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    cfg = ServiceConfig.from_env()
    assert (cfg.max_shots, cfg.max_chi, cfg.max_variables, cfg.max_body_bytes) == (2048, 32, 12, 4096)
    assert cfg.bind == "127.0.0.1:8798" and cfg.backend == "auto"
    monkeypatch.delenv("NANO_QSEARCH_MAX_SHOTS")
    monkeypatch.delenv("NANO_QSEARCH_MAX_CHI")
    for var in ("NANO_QSEARCH_MAX_CONCURRENT", "NANO_QSEARCH_REQUEST_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)
    cfg = ServiceConfig.from_env()
    assert (cfg.max_shots, cfg.max_chi) == (100_000, 256)
    assert (cfg.max_concurrent, cfg.request_timeout_s) == (2, 30)
    monkeypatch.setenv("NANO_QSEARCH_MAX_CONCURRENT", "5")
    monkeypatch.setenv("NANO_QSEARCH_REQUEST_TIMEOUT_S", "2.5")
    cfg = ServiceConfig.from_env()
    assert (cfg.max_concurrent, cfg.request_timeout_s) == (5, 2.5)


@pytest.mark.parametrize("field", ["max_shots", "max_chi", "max_variables", "max_body_bytes", "max_concurrent"])
def test_non_positive_caps_are_rejected(tmp_path, field):
    with pytest.raises(ValueError):
        _config(tmp_path, **{field: 0})


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "30"])
def test_bad_request_timeout_is_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        _config(tmp_path, request_timeout_s=value)
    assert _config(tmp_path, request_timeout_s=0.5).request_timeout_s == 0.5      # positive control


# --- 3. _execute never raises -------------------------------------------------

def test_runtime_exception_becomes_sealed_not_run(tmp_path, body_bytes):
    rt = ExplodingRuntime()
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)
    try:
        status, receipt = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
        assert status == 200 and receipt["status"] == "NOT_RUN" and receipt["reason"] == "service: RuntimeError"
        assert "secret" not in json.dumps(receipt)
        verify_seal(receipt, RECEIPT_KEY)
        assert svc.store.get(receipt["job_digest"]).state == "DONE"
    finally:
        svc.close()


def test_runtime_factory_exception_becomes_sealed_not_run(tmp_path, body_bytes):
    def factory(kind):
        raise ImportError("qiskit missing")
    svc = QSearchService(_config(tmp_path), runtime_factory=factory)
    try:
        status, receipt = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
        assert status == 200 and receipt["status"] == "NOT_RUN" and receipt["reason"] == "service: ImportError"
        verify_seal(receipt, RECEIPT_KEY)
    finally:
        svc.close()


# --- 4. attempts and "re-run once" recovery -----------------------------------

def test_crash_recovery_reruns_once_with_same_digest(tmp_path, body_bytes):
    digest = _seed_pending(tmp_path, body_bytes, attempts=1)
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)
    try:
        assert svc.store.get(digest).attempts == 1
        assert svc.recover() == [digest] and rt.calls == 1
        rec = svc.store.get(digest)
        assert rec.state == "DONE" and rec.attempts == 2
        stored = json.loads(rec.receipt_json)
        verify_seal(stored, RECEIPT_KEY)
        assert stored["status"] == "OK" and stored["job_digest"] == digest
        # a later identical submission returns the recovered receipt, no second run
        _, again = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
        assert again == stored and rt.calls == 1
        assert svc.recover() == [] and rt.calls == 1
    finally:
        svc.close()


def test_recovery_at_attempt_limit_is_not_run_without_running(tmp_path, body_bytes):
    digest = _seed_pending(tmp_path, body_bytes, attempts=MAX_ATTEMPTS)
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)
    try:
        assert svc.recover() == [digest] and rt.calls == 0
        rec = svc.store.get(digest)
        receipt = json.loads(rec.receipt_json)
        assert receipt["status"] == "NOT_RUN" and receipt["reason"] == "recovery: attempt limit reached"
        assert rec.attempts == MAX_ATTEMPTS
        verify_seal(receipt, RECEIPT_KEY)
    finally:
        svc.close()


@pytest.mark.parametrize("attempts", [0, 1])
def test_ibm_jobs_are_never_resubmitted_on_recovery(tmp_path, body_bytes, attempts):
    digest = _seed_pending(tmp_path, body_bytes, attempts=attempts)
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path, backend="ibm"), runtime_factory=lambda kind: rt)
    try:
        assert svc.recover() == [digest] and rt.calls == 0
        receipt = json.loads(svc.store.get(digest).receipt_json)
        assert receipt["status"] == "NOT_RUN" and receipt["reason"] == "recovery: hardware jobs are never resubmitted"
        verify_seal(receipt, RECEIPT_KEY)
    finally:
        svc.close()


def test_recovery_is_idempotent(tmp_path, body_bytes):
    digest = _seed_pending(tmp_path, body_bytes, attempts=1)
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)
    try:
        assert svc.recover() == [digest]
        first = svc.store.get(digest)
        assert svc.recover() == [] and rt.calls == 1 and svc.store.get(digest) == first
    finally:
        svc.close()
    svc2 = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)      # and across a restart
    try:
        assert svc2.recover() == [] and rt.calls == 1
    finally:
        svc2.close()


# --- 5. single-flight owner failure -------------------------------------------

def test_owner_store_failure_gives_waiters_503_not_a_crash(service, body_bytes):
    svc, rt = service
    real_complete = svc.store.complete
    failures = []

    def flaky_complete(digest, receipt_json):
        if not failures:
            failures.append(digest)
            raise RuntimeError("disk full")
        return real_complete(digest, receipt_json)

    svc.store.complete = flaky_complete
    rt.slow.clear()
    results = []

    def go(nonce):
        results.append(svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce=nonce), now=1000))

    threads = [threading.Thread(target=go, args=(f"{i:02d}" * 16,)) for i in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    rt.slow.set()
    for t in threads:
        t.join()
    assert len(results) == 4 and all(r == (503, {"error": "job not completed"}) for r in results)
    assert rt.calls == 1 and len(failures) == 1
    digest = json.loads(body_bytes)["job_digest"]
    assert svc.store.get(digest).state == "ACCEPTED"
    # the row is still ACCEPTED with one attempt; the next submission re-runs it once and completes
    status, receipt = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1001, nonce="ef" * 16), now=1001)
    assert status == 200 and receipt["status"] == "OK" and rt.calls == 2
    assert svc.store.get(digest).attempts == 2


# --- 7. startup banner --------------------------------------------------------

def test_startup_banner_names_backend_bind_and_recovered_but_no_keys(tmp_path):
    cfg = _config(tmp_path, backend="mps")
    line = startup_banner(cfg, ("127.0.0.1", 8798), 3)
    assert line == "nano-qsearch-serve: backend=mps bind=127.0.0.1:8798 recovered=3"
    assert "key" not in line.lower() and RECEIPT_KEY.decode() not in line and REQUEST_KEY.decode() not in line


# --- the Predator-side signing opener documented in docs/qsearch-service.md ----

def test_documented_predator_opener_is_accepted_by_the_service(tmp_path, body_bytes):
    import re
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[1] / "docs" / "qsearch-service.md").read_text(encoding="utf-8")
    section = doc.split("## Predator side", 1)[1]
    code = re.search(r"```python\n(.*?)```", section, re.DOTALL).group(1)
    built = {}

    class NanoRuntimeQSearchBackend:      # stand-in: records what the doc passes to Predator's constructor
        def __init__(self, endpoint, *, key, opener):
            built.update(endpoint=endpoint, key=key, opener=opener)

    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)
    server, thread = serve_in_thread(svc)
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        exec(code, {"NanoRuntimeQSearchBackend": NanoRuntimeQSearchBackend, "endpoint": endpoint,
                    "receipt_key": RECEIPT_KEY, "request_key": REQUEST_KEY})
        assert built["key"] == RECEIPT_KEY
        raw = built["opener"](endpoint + PATH, body_bytes, 10.0)
        receipt = json.loads(raw)
        verify_seal(receipt, RECEIPT_KEY)
        assert receipt["status"] == "OK" and rt.calls == 1
    finally:
        server.shutdown()
        thread.join()
        svc.close()


# --- review fixes: socket timeout, data-dir lock, bind before recover ---------

def _http(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_partial_headers_are_dropped_after_the_request_timeout(tmp_path):
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path, request_timeout_s=1), runtime_factory=lambda kind: rt)
    server, thread = serve_in_thread(svc)
    try:
        assert server.RequestHandlerClass.timeout == 1
        sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=10)
        try:
            sock.sendall(b"POST " + PATH.encode("ascii") + b" HTTP/1.1\r\nHost: x\r\n")     # headers never finish
            start = time.monotonic()
            try:
                data = sock.recv(1024)
            except ConnectionResetError:
                data = b""
            elapsed = time.monotonic() - start
        finally:
            sock.close()
        assert data == b"" and 0.5 <= elapsed < 5, (data, elapsed)
        assert rt.calls == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        svc.close()


def test_second_service_on_the_same_data_dir_is_refused_until_the_first_closes(tmp_path):
    first = QSearchService(_config(tmp_path), runtime_factory=lambda kind: FakeRuntime())
    try:
        assert (tmp_path / "qsearch.lock").exists()
        with pytest.raises(DataDirLocked) as info:
            QSearchService(_config(tmp_path), runtime_factory=lambda kind: FakeRuntime())
        assert str(info.value) == "data directory is already in use by another nano-qsearch-serve"
        other = QSearchService(_config(tmp_path / "other"), runtime_factory=lambda kind: FakeRuntime())
        other.close()                                    # a different data dir is independent
    finally:
        first.close()
    first.close()                                        # closing twice is harmless
    second = QSearchService(_config(tmp_path), runtime_factory=lambda kind: FakeRuntime())
    second.close()


def test_main_exits_2_with_one_line_when_the_data_dir_is_locked(tmp_path, capsys, monkeypatch):
    (tmp_path / "r.key").write_bytes(RECEIPT_KEY)
    (tmp_path / "q.key").write_bytes(REQUEST_KEY)
    monkeypatch.delenv("NANO_QSEARCH_BACKEND", raising=False)
    data = tmp_path / "data"
    holder = QSearchService(_config(data), runtime_factory=lambda kind: FakeRuntime())
    try:
        rc = service_mod.main(["--bind", "127.0.0.1:0", "--data-dir", str(data), "--key-path", str(tmp_path / "r.key"),
                               "--request-key-path", str(tmp_path / "q.key")])
    finally:
        holder.close()
    err = capsys.readouterr().err
    assert rc == 2 and err == "nano-qsearch-serve: data directory is already in use by another nano-qsearch-serve\n"


def test_prepare_binds_the_port_before_recovering(tmp_path, body_bytes, monkeypatch):
    digest = _seed_pending(tmp_path, body_bytes, attempts=0)
    servers, seen = [], []
    real_make_server = service_mod.make_server
    real_recover = QSearchService.recover

    def recording_make_server(svc):
        servers.append(real_make_server(svc))
        return servers[-1]

    def checking_recover(self):
        assert servers, "recover ran before the port was bound"
        with socket.create_connection(("127.0.0.1", servers[0].server_port), timeout=5):
            seen.append("listening")                      # accepted into the backlog while nothing serves yet
        return real_recover(self)

    monkeypatch.setattr(service_mod, "make_server", recording_make_server)
    monkeypatch.setattr(QSearchService, "recover", checking_recover)
    rt = FakeRuntime()
    svc, server, recovered = prepare(_config(tmp_path), runtime_factory=lambda kind: rt)
    try:
        assert seen == ["listening"] and server is servers[0]
        assert recovered == [digest] and rt.calls == 1
    finally:
        server.server_close()
        svc.close()


# --- review fixes: store errors -----------------------------------------------

def test_complete_value_error_returns_the_stored_done_receipt(service, body_bytes):
    svc, rt = service
    real_complete = svc.store.complete
    calls = []

    def racing_complete(digest, receipt_json):
        calls.append(digest)
        if len(calls) == 1:
            real_complete(digest, json.dumps({"stored": "first"}))     # the row becomes DONE with another receipt ...
            raise ValueError("job already completed; a second result is never stored")
        return real_complete(digest, receipt_json)

    svc.store.complete = racing_complete
    status, receipt = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
    assert (status, receipt) == (200, {"stored": "first"}) and rt.calls == 1     # ... and the stored one is returned


def test_complete_value_error_without_a_done_row_is_503(service, body_bytes):
    svc, rt = service

    def broken_complete(digest, receipt_json):
        raise ValueError("unexpected")

    svc.store.complete = broken_complete
    status, out = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
    assert (status, out) == (503, {"error": "job not completed"}) and rt.calls == 1


def test_nonce_store_failures_are_503_and_run_nothing(service, body_bytes):
    svc, rt = service
    digest = json.loads(body_bytes)["job_digest"]

    def broken_remember(nonce, ts):
        raise sqlite3.OperationalError("database is locked")

    def broken_prune(*, before_ts):
        raise OSError("disk I/O error")

    svc.store.remember = broken_remember
    status, out = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce="a1" * 16), now=1000)
    assert (status, out) == (503, {"error": "job not completed"}) and rt.calls == 0
    del svc.store.remember
    svc.store.prune_nonces = broken_prune
    status, out = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce="a2" * 16), now=1000)
    assert (status, out) == (503, {"error": "job not completed"}) and rt.calls == 0
    assert svc.store.get(digest) is None
    del svc.store.prune_nonces                            # positive control: a healthy store serves the job
    assert svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce="a3" * 16), now=1000)[0] == 200


def test_nonces_are_pruned_after_twice_the_window(service, body_bytes):
    svc, _ = service
    real_prune = svc.store.prune_nonces
    seen = []

    def spy(*, before_ts):
        seen.append(before_ts)
        return real_prune(before_ts=before_ts)

    svc.store.prune_nonces = spy
    assert svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)[0] == 200
    assert seen == [1000 - 2 * WINDOW_S]


# --- review fixes: concurrency cap --------------------------------------------

OVERLAP_WAIT_S = 3.0      # generous: SQLite fsyncs before the second run can take a while on Windows


class CountingRuntime(FakeRuntime):
    """Tracks how many runs overlap; each run waits briefly for a second one to join."""

    def __init__(self):
        super().__init__()
        self._mu = threading.Lock()
        self.in_flight = self.max_in_flight = self.runs = 0
        self.overlap = threading.Event()

    def run(self, job):
        with self._mu:
            self.runs += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            if self.in_flight >= 2:
                self.overlap.set()
        try:
            self.overlap.wait(OVERLAP_WAIT_S)
            return super().run(job)
        finally:
            with self._mu:
                self.in_flight -= 1


@pytest.mark.parametrize("cap", [1, 2])
def test_concurrency_cap_bounds_overlapping_runs(tmp_path, fixtures_dir, body_bytes, cap):
    other = _job_bytes(fixtures_dir, lambda b: b["backend"].__setitem__("seed", 8))
    rt = CountingRuntime()
    svc = QSearchService(_config(tmp_path, max_concurrent=cap), runtime_factory=lambda kind: rt)
    results = []

    def go(body, nonce):
        results.append(svc.submit("POST", PATH, body, _headers(body, 1000, nonce=nonce), now=1000))

    try:
        threads = [threading.Thread(target=go, args=args) for args in ((body_bytes, "c1" * 16), (other, "c2" * 16))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(r[0] for r in results) == [200, 200] and rt.runs == 2
        assert rt.max_in_flight == cap
    finally:
        svc.close()


# --- review fixes: recovery backend, recovery rules ---------------------------

class _Crash(BaseException):
    """Stands in for the process dying mid-run: escapes every ``except Exception``."""


def test_recovery_uses_the_backend_recorded_at_accept_time(tmp_path, body_bytes):
    class CrashingRuntime(FakeRuntime):
        def run(self, job):
            raise _Crash()

    svc = QSearchService(_config(tmp_path, backend="statevector"), runtime_factory=lambda kind: CrashingRuntime())
    try:
        with pytest.raises(_Crash):
            svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
    finally:
        svc.close()
    digest = json.loads(body_bytes)["job_digest"]
    kinds, rt = [], FakeRuntime()
    svc = QSearchService(_config(tmp_path, backend="mps"), runtime_factory=lambda kind: (kinds.append(kind), rt)[1])
    try:
        rec = svc.store.get(digest)
        assert (rec.state, rec.kind, rec.attempts) == ("ACCEPTED", "statevector", 1)
        assert svc.recover() == [digest] and kinds == ["statevector"] and rt.calls == 1
    finally:
        svc.close()


def test_recovery_of_a_row_without_a_kind_falls_back_to_the_service_backend(tmp_path, body_bytes):
    digest = _seed_pending(tmp_path, body_bytes, attempts=1)          # written without a kind, like an old file
    kinds, rt = [], FakeRuntime()
    svc = QSearchService(_config(tmp_path, backend="mps"), runtime_factory=lambda kind: (kinds.append(kind), rt)[1])
    try:
        assert svc.store.get(digest).kind is None
        assert svc.recover() == [digest] and kinds == ["mps"]
    finally:
        svc.close()


def test_recovery_of_a_row_that_never_marked_an_attempt_runs_once(tmp_path, body_bytes):
    digest = _seed_pending(tmp_path, body_bytes, attempts=0)          # crashed between begin and mark_attempt
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)
    try:
        assert svc.recover() == [digest] and rt.calls == 1
        rec = svc.store.get(digest)
        assert rec.state == "DONE" and rec.attempts == 1 and json.loads(rec.receipt_json)["status"] == "OK"
    finally:
        svc.close()


def test_ibm_live_resubmission_after_owner_store_failure_is_never_resubmitted(tmp_path, body_bytes):
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path, backend="ibm"), runtime_factory=lambda kind: rt)
    try:
        real_complete = svc.store.complete
        failed = []

        def flaky_complete(digest, receipt_json):
            if not failed:
                failed.append(digest)
                raise RuntimeError("disk full")
            return real_complete(digest, receipt_json)

        svc.store.complete = flaky_complete
        first = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce="d1" * 16), now=1000)
        assert first == (503, {"error": "job not completed"}) and rt.calls == 1
        status, receipt = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1001, nonce="d2" * 16), now=1001)
        assert status == 200 and receipt["status"] == "NOT_RUN"
        assert receipt["reason"] == "recovery: hardware jobs are never resubmitted" and rt.calls == 1
        verify_seal(receipt, RECEIPT_KEY)
    finally:
        svc.close()


# --- review fixes: body parsing and HTTP surface ------------------------------

@pytest.mark.parametrize("bad", [
    lambda b: b.replace(b'"seed": 7', b'"seed": NaN', 1),
    lambda b: b.replace(b'"seed": 7', b'"seed": Infinity', 1),
    lambda b: b.replace(b'"c0"', b'"c\xff0"', 1),                    # invalid UTF-8
], ids=["nan", "infinity", "invalid-utf8"])
def test_non_finite_constants_and_invalid_utf8_are_400(service, body_bytes, bad):
    svc, rt = service
    raw = bad(body_bytes)
    assert raw != body_bytes
    status, out = svc.submit("POST", PATH, raw, _headers(raw, 1000), now=1000)
    assert (status, out) == (400, {"error": "invalid job", "code": "JSON"}) and rt.calls == 0
    assert svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce="e1" * 16), now=1000)[0] == 200


def test_http_other_paths_methods_and_empty_auth(tmp_path, body_bytes):
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)
    server, thread = serve_in_thread(svc)
    try:
        port = server.server_port
        for i, path in enumerate((PATH + "/", PATH + "?x=1")):
            signed = {AUTH_HEADER: sign_request(REQUEST_KEY, "POST", path, body_bytes, ts=int(time.time()), nonce=f"f{i}" * 16)}
            status, data = _http(port, "POST", path, body_bytes, signed)
            assert (status, json.loads(data)) == (404, {"error": "not found"}), path
        for method in ("GET", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"):
            status, data = _http(port, method, PATH, b"{}" if method in ("PUT", "PATCH") else None)
            assert (status, json.loads(data)) == (404, {"error": "not found"}), method
        assert _http(port, "HEAD", PATH) == (404, b"")
        status, data = _http(port, "POST", PATH, body_bytes, {AUTH_HEADER: ""})
        assert (status, json.loads(data)) == (401, {"error": "unauthorized"}) and rt.calls == 0
        status, data = _http(port, "POST", PATH, body_bytes, _headers(body_bytes, int(time.time()), nonce="f9" * 16))
        assert status == 200 and json.loads(data)["status"] == "OK" and rt.calls == 1      # positive control
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        svc.close()


# --- review fixes: attempts count only once a run starts ----------------------

class _SpySlots:
    """Wraps the service's semaphore and signals once a second caller is waiting at it."""

    def __init__(self, inner):
        self._inner = inner
        self._mu = threading.Lock()
        self.entered = 0
        self.second_waiting = threading.Event()

    def __enter__(self):
        with self._mu:
            self.entered += 1
            if self.entered >= 2:
                self.second_waiting.set()
        return self._inner.__enter__()

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.01)


def test_a_job_queued_for_a_slot_has_not_used_an_attempt_and_recovery_runs_it_in_full(tmp_path, fixtures_dir, body_bytes):
    # B is a non-IBM job: what matters is that a crash while B waits for a slot leaves it at attempts == 0,
    # so recovery spends B's original attempt rather than its single re-run (B ends at 1, not 2). An IBM
    # row only ever reaches "hardware jobs are never resubmitted" now with attempts >= 1 when its run really
    # started; a still-queued IBM row (attempts == 0) keeps the existing conservative rule on recovery.
    body_b = _job_bytes(fixtures_dir, lambda b: b["backend"].__setitem__("seed", 8))
    digest_a, digest_b = json.loads(body_bytes)["job_digest"], json.loads(body_b)["job_digest"]
    live = tmp_path / "live"
    rt = FakeRuntime()
    rt.slow.clear()                                       # A blocks inside the runtime, holding the only slot
    svc = QSearchService(_config(live, max_concurrent=1), runtime_factory=lambda kind: rt)
    spy = svc._slots = _SpySlots(svc._slots)
    results = {}

    def go(name, body, nonce):
        results[name] = svc.submit("POST", PATH, body, _headers(body, 1000, nonce=nonce), now=1000)

    threads = {"a": threading.Thread(target=go, args=("a", body_bytes, "b1" * 16)),
               "b": threading.Thread(target=go, args=("b", body_b, "b2" * 16))}
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    try:
        threads["a"].start()
        _wait_for(lambda: rt.calls == 1)
        threads["b"].start()
        assert spy.second_waiting.wait(10)                # B is at the slot A holds
        assert svc.store.get(digest_a).attempts == 1
        assert svc.store.get(digest_b).state == "ACCEPTED" and svc.store.get(digest_b).attempts == 0
        # "crash" now: snapshot the store exactly as a dying process would leave it on disk
        src = sqlite3.connect(str(live / "qsearch.sqlite3"))
        dst = sqlite3.connect(str(crashed / "qsearch.sqlite3"))
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
    finally:
        rt.slow.set()
        for t in threads.values():
            t.join()
        svc.close()
    assert results["a"][0] == results["b"][0] == 200       # positive control: the live process finished both
    rt2 = FakeRuntime()
    svc2 = QSearchService(_config(crashed), runtime_factory=lambda kind: rt2)
    try:
        assert sorted(svc2.recover()) == sorted([digest_a, digest_b]) and rt2.calls == 2
        rec_a, rec_b = svc2.store.get(digest_a), svc2.store.get(digest_b)
        assert (rec_a.state, rec_a.attempts) == ("DONE", 2)      # A had started: this was its one re-run
        assert (rec_b.state, rec_b.attempts) == ("DONE", 1)      # B never started: this was its first attempt
        assert json.loads(rec_b.receipt_json)["status"] == "OK"
    finally:
        svc2.close()


def test_a_refusal_records_no_attempt(tmp_path, body_bytes):
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path, max_variables=3), runtime_factory=lambda kind: rt)
    try:
        status, receipt = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
        assert status == 200 and receipt["status"] == "REFUSED" and rt.calls == 0
        assert svc.store.get(receipt["job_digest"]).attempts == 0
    finally:
        svc.close()


def test_a_mark_attempt_failure_is_503_and_runs_nothing(service, body_bytes):
    svc, rt = service

    def broken_mark(digest):
        raise sqlite3.OperationalError("disk I/O error")

    svc.store.mark_attempt = broken_mark
    status, out = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce="b3" * 16), now=1000)
    assert (status, out) == (503, {"error": "job not completed"}) and rt.calls == 0
    digest = json.loads(body_bytes)["job_digest"]
    assert svc.store.get(digest).state == "ACCEPTED"
    del svc.store.mark_attempt                            # positive control: the slot was released, the job now runs
    status, receipt = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1001, nonce="b4" * 16), now=1001)
    assert status == 200 and receipt["status"] == "OK" and rt.calls == 1


# --- review fixes: handler attribute lookup -----------------------------------

def test_handler_resolves_only_the_current_requests_method():
    handler = object.__new__(service_mod._Handler)
    handler.command = "TRACE"
    assert hasattr(handler, "do_TRACE") and handler.do_TRACE == handler._not_found
    assert not hasattr(handler, "do_POTS") and not hasattr(handler, "do_OPTIONS")
    assert not hasattr(handler, "something_else")
    bare = object.__new__(service_mod._Handler)          # before parse_request sets command
    assert not hasattr(bare, "do_TRACE") and not hasattr(bare, "do_")


# --- review fixes: data-dir lock errors ---------------------------------------

def test_only_lock_contention_maps_to_data_dir_locked(tmp_path, monkeypatch):
    def raising(err):
        def lock(fd):
            raise OSError(err, os.strerror(err))
        return staticmethod(lock)

    monkeypatch.setattr(service_mod._DataDirLock, "_lock", raising(errno.EINVAL))
    with pytest.raises(OSError) as info:
        QSearchService(_config(tmp_path), runtime_factory=lambda kind: FakeRuntime())
    assert not isinstance(info.value, DataDirLocked) and info.value.errno == errno.EINVAL
    monkeypatch.setattr(service_mod._DataDirLock, "_lock", raising(errno.EACCES))       # positive control
    with pytest.raises(DataDirLocked):
        QSearchService(_config(tmp_path), runtime_factory=lambda kind: FakeRuntime())
    monkeypatch.undo()
    QSearchService(_config(tmp_path), runtime_factory=lambda kind: FakeRuntime()).close()    # nothing leaked


# --- review fixes: startup errors and env parsing in main() -------------------

def _env_config(tmp_path, monkeypatch):
    (tmp_path / "r.key").write_bytes(RECEIPT_KEY)
    (tmp_path / "q.key").write_bytes(REQUEST_KEY)
    for var, value in {"NANO_QSEARCH_DATA_DIR": str(tmp_path / "data"), "NANO_QSEARCH_KEY_PATH": str(tmp_path / "r.key"),
                       "NANO_QSEARCH_REQUEST_KEY_PATH": str(tmp_path / "q.key")}.items():
        monkeypatch.setenv(var, value)
    for var in ("NANO_QSEARCH_BIND", "NANO_QSEARCH_BACKEND", "NANO_QSEARCH_MAX_SHOTS", "NANO_QSEARCH_MAX_CHI",
                "NANO_QSEARCH_MAX_VARIABLES", "NANO_QSEARCH_MAX_BODY_BYTES", "NANO_QSEARCH_MAX_CONCURRENT",
                "NANO_QSEARCH_REQUEST_TIMEOUT_S"):
        monkeypatch.delenv(var, raising=False)


def test_main_exits_2_with_one_line_when_the_port_is_in_use(tmp_path, capsys, monkeypatch):
    _env_config(tmp_path, monkeypatch)
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        rc = service_mod.main(["--bind", f"127.0.0.1:{port}"])
    finally:
        blocker.close()
    err = capsys.readouterr().err
    assert rc == 2 and re.fullmatch(r"nano-qsearch-serve: startup failed: [A-Za-z]*Error\n", err), err
    # the data dir lock was released on the way out
    QSearchService(_config(tmp_path / "data"), runtime_factory=lambda kind: FakeRuntime()).close()


def test_a_second_server_cannot_bind_a_port_a_service_is_listening_on(tmp_path):
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: FakeRuntime())
    other = QSearchService(_config(tmp_path / "other"), runtime_factory=lambda kind: FakeRuntime())
    first = service_mod.make_server(svc)
    try:
        assert first.request_queue_size == 64
        port = first.server_address[1]
        with pytest.raises(OSError):
            service_mod.make_server(_Rebound(other, port))
    finally:
        first.server_close()
        svc.close()
        other.close()


class _Rebound:
    """A service stand-in whose config binds a fixed port (``make_server`` reads only ``config``)."""

    def __init__(self, svc, port):
        self.config = dataclasses.replace(svc.config, bind=f"127.0.0.1:{port}")


@pytest.mark.parametrize("var, value, expected", [
    ("NANO_QSEARCH_MAX_SHOTS", "lots", "NANO_QSEARCH_MAX_SHOTS must be an integer"),
    ("NANO_QSEARCH_REQUEST_TIMEOUT_S", "soon", "NANO_QSEARCH_REQUEST_TIMEOUT_S must be a number"),
])
def test_unparsable_env_numbers_name_the_variable(tmp_path, capsys, monkeypatch, var, value, expected):
    _env_config(tmp_path, monkeypatch)
    assert ServiceConfig.from_env().max_shots == 100_000                     # positive control: all valid
    monkeypatch.setenv(var, value)
    with pytest.raises(ValueError) as info:
        ServiceConfig.from_env()
    assert str(info.value) == expected and value not in str(info.value)
    assert service_mod.main([]) == 2
    assert capsys.readouterr().err == f"nano-qsearch-serve: {expected}\n"


# --- review fixes: cleanup on startup failure, HEAD has no body ---------------

def test_a_store_failure_in_init_releases_the_data_dir_lock(tmp_path, monkeypatch):
    class BrokenStore:
        def __init__(self, path):
            raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(service_mod, "JobStore", BrokenStore)
    with pytest.raises(sqlite3.OperationalError):
        QSearchService(_config(tmp_path), runtime_factory=lambda kind: FakeRuntime())
    monkeypatch.undo()
    QSearchService(_config(tmp_path), runtime_factory=lambda kind: FakeRuntime()).close()


def test_a_recovery_failure_in_prepare_frees_the_lock_and_closes_the_port(tmp_path, monkeypatch):
    servers = []
    real_make_server = service_mod.make_server

    def recording_make_server(svc):
        servers.append(real_make_server(svc))
        return servers[-1]

    def broken_recover(self):
        with socket.create_connection(("127.0.0.1", servers[0].server_port), timeout=5):
            pass                                          # positive control: the port was listening
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(service_mod, "make_server", recording_make_server)
    monkeypatch.setattr(QSearchService, "recover", broken_recover)
    with pytest.raises(sqlite3.OperationalError):
        prepare(_config(tmp_path), runtime_factory=lambda kind: FakeRuntime())
    monkeypatch.undo()
    port = servers[0].server_port
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
    QSearchService(_config(tmp_path), runtime_factory=lambda kind: FakeRuntime()).close()


def test_head_response_ends_at_the_blank_line(tmp_path):
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: FakeRuntime())
    server, thread = serve_in_thread(svc)
    try:
        sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=10)
        try:
            sock.sendall(b"HEAD " + PATH.encode("ascii") + b" HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = b""
            while chunk := sock.recv(4096):
                data += chunk
        finally:
            sock.close()
        assert data.startswith(b"HTTP/1.0 404 ") and data.endswith(b"\r\n\r\n"), data
        assert data.count(b"\r\n\r\n") == 1 and b"Content-Length: 0\r\n" in data
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        svc.close()
