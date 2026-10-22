import itertools
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from nano_qsearch.auth import AUTH_HEADER, sign_request
from nano_qsearch.canonical import canonical_digest
from nano_qsearch.receipt import ok_receipt, verify_seal
from nano_qsearch.service import (
    MAX_ATTEMPTS, SEED_MAX, QSearchService, ServiceConfig, serve_in_thread, startup_banner,
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
    assert status == 401 and out["code"] == "REPLAY" and rt.calls == 1


def test_stale_timestamp_rejected(service, body_bytes):
    svc, rt = service
    assert svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1300)[0] == 401 and rt.calls == 0


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
    cfg = ServiceConfig.from_env()
    assert (cfg.max_shots, cfg.max_chi) == (100_000, 256)


@pytest.mark.parametrize("field", ["max_shots", "max_chi", "max_variables", "max_body_bytes"])
def test_non_positive_caps_are_rejected(tmp_path, field):
    with pytest.raises(ValueError):
        _config(tmp_path, **{field: 0})


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
