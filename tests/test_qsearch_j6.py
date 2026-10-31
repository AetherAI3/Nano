"""J6 (preregistration §3 item 2): configurable v2 PUB limit, one target per request, compile time recorded.

``NANO_QSEARCH_V2_MAX_PUBS`` (default 16, at most 4,096, invalid refuses startup) caps a v2
request before anything is compiled; a request's jobs compile against ONE resolved target;
the compile seconds are kept beside the receipt (``requests.record_json``), not in the sealed body.
The service tests use a stand-in ATLAS delegate (no ATLAS, no qiskit, no IBM); the compile tests
at the end need qiskit and ATLAS (engine marker).
"""
import json
import secrets
import sqlite3

import pytest

from nano_qsearch import service as service_mod
from nano_qsearch.auth import AUTH_HEADER, sign_request
from nano_qsearch.request import MAX_BLOCKS, RequestValidationError, max_pubs_from_env, parse_request
from nano_qsearch.runtime_ibm import IbmRuntime
from nano_qsearch.service import REQUEST_PATH, QSearchService, ServiceConfig
from nano_qsearch.store import JobStore
from qsearch_v2_support import atlas_request_receipt, request_body
from test_qsearch_v2_runtime import _plan, atlas  # noqa: F401  - the fake-ATLAS fixture

RECEIPT_KEY = b"receipt-key-" + b"0" * 20
REQUEST_KEY = b"request-key-" + b"1" * 20
BIG_BODY = 8 * 1024 * 1024


class Delegate:
    def __init__(self):
        self.compiled, self.max_pubs = [], []

    def compile(self, job):
        self.compiled.append(job.job_digest)
        return None

    def submit(self, request, compiled, **kw):
        self.max_pubs.append(kw.get("max_pubs"))
        return atlas_request_receipt(request)


def _service(tmp_path, delegate, **over):
    runtime = IbmRuntime(compile_fn=delegate.compile, request_submit_fn=delegate.submit, environ={})
    cfg = ServiceConfig(**{"bind": "127.0.0.1:0", "backend": "auto", "data_dir": tmp_path, "receipt_key": RECEIPT_KEY,
                           "request_key": REQUEST_KEY, "v2_enabled": True, **over})
    return QSearchService(cfg, runtime_factory=lambda kind: runtime)


def _post(svc, body):
    raw = json.dumps(body, sort_keys=True).encode("utf-8")
    headers = {AUTH_HEADER: sign_request(REQUEST_KEY, "POST", REQUEST_PATH, raw, ts=1000, nonce=secrets.token_hex(16))}
    return svc.submit_request("POST", REQUEST_PATH, raw, headers, now=1000)


def _env_keys(tmp_path, monkeypatch):
    (tmp_path / "k1").write_bytes(RECEIPT_KEY)
    (tmp_path / "k2").write_bytes(REQUEST_KEY)
    monkeypatch.setenv("NANO_QSEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NANO_QSEARCH_KEY_PATH", str(tmp_path / "k1"))
    monkeypatch.setenv("NANO_QSEARCH_REQUEST_KEY_PATH", str(tmp_path / "k2"))


# --- the limit --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [(None, 16), ("", 16), ("1", 1), ("1200", 1200), ("4096", 4096)])
def test_max_pubs_from_env(value, expected):
    assert max_pubs_from_env({} if value is None else {"NANO_QSEARCH_V2_MAX_PUBS": value}) == expected


@pytest.mark.parametrize("value, message", [
    ("0", "NANO_QSEARCH_V2_MAX_PUBS must be in 1..4096"), ("4097", "NANO_QSEARCH_V2_MAX_PUBS must be in 1..4096"),
    ("lots", "NANO_QSEARCH_V2_MAX_PUBS must be an integer"), ("12.5", "NANO_QSEARCH_V2_MAX_PUBS must be an integer"),
])
def test_an_invalid_limit_refuses_startup(tmp_path, monkeypatch, capsys, value, message):
    _env_keys(tmp_path, monkeypatch)
    monkeypatch.setenv("NANO_QSEARCH_V2_MAX_PUBS", value)
    with pytest.raises(ValueError, match=message):
        ServiceConfig.from_env()
    assert service_mod.main([]) == 2 and capsys.readouterr().err == f"nano-qsearch-serve: {message}\n"
    with pytest.raises(ValueError):
        ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path, receipt_key=RECEIPT_KEY,
                      request_key=REQUEST_KEY, v2_max_pubs=4097)


def test_the_default_16_is_unchanged_and_refuses_before_compile(tmp_path):
    assert MAX_BLOCKS == 16 and parse_request(request_body(16)).block_count == 16
    with pytest.raises(RequestValidationError) as err:
        parse_request(request_body(17))
    assert err.value.code == "DESCRIPTOR"
    delegate = Delegate()
    svc = _service(tmp_path, delegate)
    try:
        assert _post(svc, request_body(17)) == (400, {"error": "invalid request", "code": "DESCRIPTOR"})
        status, receipt = _post(svc, request_body(16))
        assert status == 200 and len(receipt["blocks"]) == 16 and delegate.max_pubs == [16]
        assert len(delegate.compiled) == 16                                       # only the admitted request
    finally:
        svc.close()


def test_a_1200_job_request_is_accepted_with_the_limit_raised(tmp_path, monkeypatch):
    _env_keys(tmp_path, monkeypatch)
    for name, value in {"NANO_QSEARCH_V2": "1", "NANO_QSEARCH_V2_MAX_PUBS": "1200",
                        "NANO_QSEARCH_MAX_BODY_BYTES": str(BIG_BODY)}.items():
        monkeypatch.setenv(name, value)
    cfg = ServiceConfig.from_env()
    assert cfg.v2_max_pubs == 1200 and cfg.v2_enabled
    delegate = Delegate()
    runtime = IbmRuntime(compile_fn=delegate.compile, request_submit_fn=delegate.submit, environ={})
    svc = QSearchService(cfg, runtime_factory=lambda kind: runtime)
    try:
        body = request_body(1201, max_execution_time=30)
        assert _post(svc, body)[1]["code"] == "DESCRIPTOR" and delegate.compiled == []   # over the limit: no compile
        body["jobs"].pop()
        body["descriptor"]["blocks"].pop()
        status, receipt = _post(svc, body)
        assert status == 200 and receipt["status"] == "OK" and receipt["request"]["block_count"] == 1200
        assert [b["pub_index"] for b in receipt["blocks"]] == list(range(1200)) and delegate.max_pubs == [1200]
        record = json.loads(svc.store.request_record(receipt["request"]["request_digest"]))
        assert record["compiled_jobs"] == 1200 and record["compile_s"] >= 0 and record["target_resolutions"] == 0
        assert "compile_s" not in json.dumps(receipt)                             # beside the receipt, not in it
    finally:
        svc.close()


def test_a_live_request_resolves_its_target_once(atlas):
    request = parse_request(request_body(12, target="ibm_fez", max_qpu_s=5, max_execution_time=30),
                            max_blocks=1200)
    backends = []

    def compile_fn(job, *, backend=None):
        backends.append(backend)
        return _plan(backend.target)
    atlas.live_receipt = atlas_request_receipt(request, usage=2.0)
    record = {}
    body = IbmRuntime(environ=atlas.environ, compile_fn=compile_fn).run_request(request, max_pubs=1200, record=record)
    assert body["status"] == "OK" and len(body["blocks"]) == 12
    assert [c[0] for c in atlas.calls].count("context") == 1 and len(backends) == 12
    assert len({id(b) for b in backends}) == 1
    assert record["target_resolutions"] == 1 and record["compiled_jobs"] == 12
    assert [c for c in atlas.calls if c[0] == "request"][0][2]["max_pubs"] == 1200


def test_old_store_files_gain_the_record_column(tmp_path):
    path = tmp_path / "qsearch.sqlite3"
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE requests (request_digest TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
        state TEXT NOT NULL, request_json TEXT NOT NULL, receipt_json TEXT, accepted_at REAL NOT NULL,
        completed_at REAL, attempts INTEGER NOT NULL DEFAULT 0);""")
    con.close()
    store = JobStore(path)
    try:
        store.begin_request("sha256:" + "a" * 64, "req-old", "{}")
        store.set_request_record("sha256:" + "a" * 64, '{"compile_s": 1.0}')
        assert json.loads(store.request_record("sha256:" + "a" * 64)) == {"compile_s": 1.0}
    finally:
        store.close()


# --- one target for the whole compile (engine) ----------------------------------------------------


@pytest.fixture
def engine():
    pytest.importorskip("qiskit")
    pytest.importorskip("aether_atlas")
    from nano_qsearch import compile as compile_mod
    return compile_mod


@pytest.mark.engine
def test_compile_jobs_reads_the_live_target_once_and_matches_per_job_compiles(engine):
    from qiskit.providers.fake_provider import GenericBackendV2
    from qiskit.transpiler import CouplingMap
    target = GenericBackendV2(num_qubits=8, basis_gates=["cz", "rz", "sx", "x"],
                              coupling_map=CouplingMap.from_line(8).get_edges(), seed=11).target

    class CountingBackend:
        name = "ibm_fez"
        reads = 0

        @property
        def target(self):
            CountingBackend.reads += 1
            return target
    request = parse_request(request_body(5, target="ibm_fez"))
    plans = engine.compile_jobs(request.jobs, backend=CountingBackend(), options=engine.CompileOptions())
    assert CountingBackend.reads == 1
    frozen = engine.FrozenTarget(name="ibm_fez", target=target, target_digest=engine.target_digest(target))
    alone = [engine.compile_program(job, backend=frozen, options=engine.CompileOptions()) for job in request.jobs]
    assert [p.compiled_plan_digest for p in plans] == [p.compiled_plan_digest for p in alone]


@pytest.mark.engine
def test_compile_jobs_builds_one_fakefez_for_a_dry_run(engine, monkeypatch):
    pytest.importorskip("qiskit_ibm_runtime")
    built = []
    real = engine._fez_backend

    def counting(job):
        built.append(job.job_digest)
        return real(job)
    request = parse_request(request_body(4))
    alone = [engine.compile_program(job, options=engine.CompileOptions()) for job in request.jobs]
    monkeypatch.setattr(engine, "_fez_backend", counting)
    plans = engine.compile_jobs(request.jobs, options=engine.CompileOptions())
    assert len(built) == 1 and [p.compiled_plan_digest for p in plans] == [p.compiled_plan_digest for p in alone]


def test_atlas_gets_the_ledger_cap_live_and_the_service_cap_for_a_dry_run(atlas):
    import sys
    sys.modules["aether_atlas.aqrc.campaign_runtime"].qsearch_max_request_pubs = lambda ledger: 1320
    live = parse_request(request_body(3, target="ibm_fez", max_qpu_s=5, max_execution_time=10))
    atlas.live_receipt = atlas_request_receipt(live, usage=2.0)
    body = IbmRuntime(environ=atlas.environ, compile_fn=lambda job, backend=None: _plan(backend.target)).run_request(
        live, max_pubs=1200)
    assert body["status"] == "OK"
    assert [c for c in atlas.calls if c[0] == "request"][-1][2]["max_pubs"] == 1320       # the ledger's Q cap
    atlas.calls.clear()                                  # the fake numbers its children per call log
    dry = parse_request(request_body(3))
    atlas.dry_receipt = atlas_request_receipt(dry)
    assert IbmRuntime(compile_fn=lambda job: _plan()).run_request(dry, max_pubs=1200)["status"] == "OK"
    assert [c for c in atlas.calls if c[0] == "request"][-1][2]["max_pubs"] == 1200       # the service's limit
