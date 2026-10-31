"""Lane HW0 through the real ATLAS ``campaign_runtime`` (A2-A4), with a fake provider. No QPU is reached.

Runs only when ``NANO_QSEARCH_ATLAS_PATH`` names an ATLAS checkout that has the HW0
functions (``resolve_qsearch_context`` ... ``recover_qsearch_submission``). Every
ibm_fez path injects a fake ``service_factory`` (a ``GenericBackendV2`` target named
``ibm_fez``) and a fake ``SamplerV2`` factory that records its options; Nano compiles
the jobs for real against that target. ``test_hardware_unreachable_when_any_flag_is_missing``
patches the real ``SamplerV2`` and ``QiskitRuntimeService`` to raise if touched. No test
sets ``AETHER_AQRC_HW_GO`` and ``AETHER_AQRC_QSEARCH_LIVE`` together.
"""
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.engine
pytest.importorskip("qiskit")
qiskit_ibm_runtime = pytest.importorskip("qiskit_ibm_runtime")
campaign_runtime = pytest.importorskip("aether_atlas.aqrc.campaign_runtime")
if not hasattr(campaign_runtime, "recover_qsearch_submission"):
    pytest.skip("ATLAS checkout predates the HW0 request functions", allow_module_level=True)

from qiskit.providers.fake_provider import GenericBackendV2  # noqa: E402
from qiskit.transpiler import CouplingMap  # noqa: E402
from qiskit_ibm_runtime import SamplerOptions  # noqa: E402

from aether_atlas.aqrc.campaign import (  # noqa: E402
    PROTOCOL_V2, CampaignLedger, authorize_execution, protocol_document,
)
from nano_qsearch.auth import AUTH_HEADER, sign_request  # noqa: E402
from nano_qsearch.compile import compile_program  # noqa: E402
from nano_qsearch.job import parse_job  # noqa: E402
from nano_qsearch.receipt_v2 import verify_seal_v2  # noqa: E402
from nano_qsearch.request import parse_request  # noqa: E402
from nano_qsearch.runtime_ibm import IbmRuntime, prepare_request_identity  # noqa: E402
from nano_qsearch.service import NOT_ENABLED, REQUEST_PATH, QSearchService, ServiceConfig  # noqa: E402
from qsearch_v2_support import MINIMUM_DISPLAY, job_body, request_body  # noqa: E402

RECEIPT_KEY = b"receipt-key-" + b"0" * 20
REQUEST_KEY = b"request-key-" + b"1" * 20
AUTHORITY_KEY = b"offline-test-host-key-never-used-for-hardware"
INSTANCE = "test-instance"
GO, LIVE, PACKED = "AETHER_AQRC_HW_GO", "AETHER_AQRC_QSEARCH_LIVE", "AETHER_AQRC_QSEARCH_PACKED"
_TARGET = GenericBackendV2(num_qubits=8, basis_gates=["cz", "rz", "sx", "x"],
                           coupling_map=CouplingMap.from_line(8).get_edges(), seed=11).target


class _Crash(BaseException):
    """A process death between two steps: not an Exception, so no handler records it."""


class Provider:
    """The fake IBM side: one ``ibm_fez`` backend with a non-FakeFez target, a job store and a sampler."""

    def __init__(self, *, usage=2.0, crash_result=False, status="DONE", run_error=None):
        self.jobs, self.usage, self.crash_result = [], usage, crash_result
        self.status, self.run_error = status, run_error
        self.constructed, self.runs, self.services = [], [], 0

    # -- QiskitRuntimeService stand-in, built by service_factory(args) --
    def service_factory(self, args):
        self.services += 1
        service = SimpleNamespace()
        backend = SimpleNamespace(name="ibm_fez", _instance=args.instance, service=service, target=_TARGET,
                                  options=SimpleNamespace(use_fractional_gates=False),
                                  status=lambda: SimpleNamespace(operational=True, status_msg="active"))
        service.backend = lambda name, *, instance, use_fractional_gates: backend
        service.active_instance = lambda: INSTANCE
        service.usage = lambda: {"usage_remaining_seconds": 600.0}
        service.jobs = self._list
        service.job = lambda job_id: next(j for j in self.jobs if j.job_id() == job_id)
        return service

    def _list(self, **kw):
        if kw.get("pending"):
            return []
        tags = kw.get("job_tags") or []
        return [j for j in self.jobs if all(t in j.tags for t in tags)][:kw.get("limit", 10)]

    # -- SamplerV2 stand-in --
    def sampler_factory(self, *, mode, options=None):
        assert mode.name == "ibm_fez"
        self.constructed.append(options)
        sampler = SimpleNamespace(options=SamplerOptions(**(options or {})))
        sampler.run = lambda pubs, shots=None: self._run(sampler, pubs, shots)
        return sampler

    def _run(self, sampler, pubs, shots):
        pubs = [pub if isinstance(pub, tuple) else (pub, None, shots) for pub in pubs]
        self.runs.append(pubs)
        if self.run_error is not None:
            raise self.run_error
        job = FakeJob(f"fake-fez-job-{len(self.runs)}", pubs, list(sampler.options.environment.job_tags),
                      usage=self.usage, crash_result=self.crash_result, status=self.status)
        self.jobs.append(job)
        return job


class FakeJob:
    def __init__(self, job_id, pubs, tags, *, usage, crash_result, status="DONE"):
        self._id, self._pubs, self._usage, self.crash_result = job_id, list(pubs), usage, crash_result
        self._status = status
        self.tags, self.instance, self.inputs = list(tags), INSTANCE, {"pubs": [list(p) for p in pubs]}
        self.creation_date = datetime.now(timezone.utc)

    def job_id(self):
        return self._id

    def backend(self):
        return SimpleNamespace(name="ibm_fez")

    def status(self):
        return self._status

    def error_message(self):
        return "Job ran longer than the max_execution_time (free provider text)"

    def usage(self):
        return self._usage

    def metrics(self):
        return {"usage": {"quantum_seconds": self._usage}}

    def result(self):
        if self._status != "DONE":
            raise RuntimeError("provider job failed")
        if self.crash_result:
            self.crash_result = False
            raise _Crash()
        pubs = []
        for circuit, _, shots in self._pubs:
            register = circuit.cregs[0].name
            samples = [MINIMUM_DISPLAY] * shots
            data = SimpleNamespace(keys=lambda r=register: [r])
            setattr(data, register, SimpleNamespace(get_bitstrings=lambda s=samples: list(s)))
            pubs.append(SimpleNamespace(data=data, metadata={"shots": shots}))
        return type("Result", (list,), {"metadata": {"version": 2}})(pubs)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    for name in (GO, LIVE, PACKED, "NANO_QSEARCH_V2"):
        monkeypatch.delenv(name, raising=False)


def _ledger(tmp_path):
    protocol = protocol_document(campaign_id="hw0-nano", instance=INSTANCE, backend="ibm_fez",
                                 capability_mode="standard", cohort_sha256="a" * 64, scorer_sha256="b" * 64,
                                 schema=PROTOCOL_V2)
    return CampaignLedger.create(tmp_path / "ledger.sqlite", protocol, confirmed_start_seconds=600)


def _authorize(tmp_path, ledger, subject):
    token = authorize_execution(key=AUTHORITY_KEY, ledger=ledger, scope="plan", subject_sha256=subject,
                                max_seconds=60, expires_at=int(time.time()) + 600, operator="offline-test")
    (tmp_path / "authority.json").write_text(json.dumps(token), encoding="utf-8")
    (tmp_path / "authority.key").write_bytes(AUTHORITY_KEY)
    # LIVE in Nano's own environ (R3 M3); every runtime built from it injects the fake service and sampler.
    return {GO: "1", LIVE: "1", PACKED: "1", "AETHER_AQRC_AUTHORITY_PATH": str(tmp_path / "authority.json"),
            "AETHER_AQRC_AUTHORITY_KEY_PATH": str(tmp_path / "authority.key"),
            "AETHER_AQRC_LEDGER_PATH": str(tmp_path / "ledger.sqlite")}


def _request_identity(body, ledger, provider):
    """What the operator computes before issuing authority: compile against the live target, freeze, digest."""
    request = parse_request(body)
    context = campaign_runtime.resolve_qsearch_context(ledger, service_factory=provider.service_factory, environ={})
    compiled = [compile_program(job, backend=context.backend) for job in request.jobs]
    _, prepared = prepare_request_identity(request, compiled, purpose=request.purpose)
    return prepared["request_sha256"]


def _service(tmp_path, environ, provider, *, v2=True):
    def factory(kind):
        return IbmRuntime(environ=environ, service_factory=provider.service_factory,
                          sampler_factory=provider.sampler_factory)
    cfg = ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path / "nano", receipt_key=RECEIPT_KEY,
                        request_key=REQUEST_KEY, v2_enabled=v2)
    return QSearchService(cfg, runtime_factory=factory)


def _post(svc, body, nonce):
    raw = json.dumps(body, sort_keys=True).encode("utf-8")
    headers = {AUTH_HEADER: sign_request(REQUEST_KEY, "POST", REQUEST_PATH, raw, ts=1000, nonce=nonce)}
    return svc.submit_request("POST", REQUEST_PATH, raw, headers, now=1000)


def _packed_setup(tmp_path, monkeypatch, provider, n=3, limit=10, request_id="req-p"):
    body = request_body(n, target="ibm_fez", shots=8, max_qpu_s=5, max_execution_time=limit, request_id=request_id)
    ledger = _ledger(tmp_path)
    environ = _authorize(tmp_path, ledger, _request_identity(body, ledger, provider))
    monkeypatch.setenv(GO, "1")                              # ATLAS reads GO / PACKED from os.environ
    if n > 1:
        monkeypatch.setenv(PACKED, "1")
    return body, ledger, environ


# --- HW0-d: provider options and reservation; one request, N PUBs ---------------------------------


def test_packed_request_runs_once_with_its_limit_and_tag_and_reserves_exactly_that(tmp_path, monkeypatch):
    provider = Provider()
    body, ledger, environ = _packed_setup(tmp_path, monkeypatch, provider)
    svc = _service(tmp_path, environ, provider)
    try:
        status, receipt = _post(svc, body, "a1" * 16)
        assert status == 200 and receipt["status"] == "OK", receipt
        verify_seal_v2(receipt, RECEIPT_KEY)
        tag = "atlas-qsearch-req-req-p"
        [options] = provider.constructed
        tags = options["environment"]["job_tags"]
        assert options["max_execution_time"] == 10 and tags[0] == tag
        assert len(provider.runs) == 1 and len(provider.runs[0]) == 3          # one sampler.run, three PUBs
        row = ledger.get("req-p")
        assert (row["cap"], row["pubs"], row["state"], row["usage"]) == (10, 3, "done", 2.0)
        assert receipt["hardware"]["job_tags"] == tags and receipt["hardware"]["max_execution_time"] == 10
        assert receipt["hardware"]["provider_job_id"] == "fake-fez-job-1" and receipt["backend"]["dry_run"] is False
        assert receipt["usage"]["quantum_seconds"] == 2.0 and receipt["usage"]["usage_pending"] is False
        assert [b["job_digest"] for b in receipt["blocks"]] == [j["job_digest"] for j in body["jobs"]]
        assert all(b["counts"] == {MINIMUM_DISPLAY: 8} and b["audit"]["agree"] for b in receipt["blocks"])
        assert _post(svc, body, "a2" * 16) == (200, receipt) and len(provider.runs) == 1
    finally:
        svc.close()


def test_zero_usage_is_pending_and_the_reservation_stays_held(tmp_path, monkeypatch):
    provider = Provider(usage=0)
    body, ledger, environ = _packed_setup(tmp_path, monkeypatch, provider)
    svc = _service(tmp_path, environ, provider)
    try:
        status, receipt = _post(svc, body, "b1" * 16)
        assert receipt["usage"] == {"quantum_seconds": 0.0, "usage_pending": True, "provider_sub_jobs": None,
                                    "provider_metrics": {"usage": {"quantum_seconds": 0}}}
        row = ledger.get("req-p")
        assert row["usage"] is None and row["state"] == "awaiting_accounting" and row["cap"] == 10
    finally:
        svc.close()


def test_singletons_carry_their_5s_limit_through_v2_and_v1(tmp_path, monkeypatch):
    provider = Provider()
    body, ledger, environ = _packed_setup(tmp_path, monkeypatch, provider, n=1, limit=5, request_id="req-s")
    svc = _service(tmp_path, environ, provider)
    try:
        status, receipt = _post(svc, body, "c1" * 16)
        assert status == 200 and receipt["status"] == "OK" and len(receipt["blocks"]) == 1
        assert provider.constructed[-1]["max_execution_time"] == 5 and ledger.get("req-s")["cap"] == 5
    finally:
        svc.close()
    # N3 on the v1 path: the child's limit is the job's limits.max_qpu_s and the reservation equals it.
    v1 = tmp_path / "v1"
    v1.mkdir()
    job = parse_job(job_body(target="ibm_fez", shots=8, seed=31, max_qpu_s=5))
    ledger1 = _ledger(v1)
    context = campaign_runtime.resolve_qsearch_context(ledger1, service_factory=provider.service_factory, environ={})
    plan = compile_program(job, backend=context.backend)
    child = campaign_runtime.prepare_qsearch_child(
        plan.isa, purpose="qsearch", target="ibm_fez", shots=job.shots, job_digest=job.job_digest,
        compiled_plan_digest=plan.compiled_plan_digest, request_id=job.job_digest[7:47], max_execution_time=5)
    env1 = _authorize(v1, ledger1, child["child_sha256"])
    receipt = IbmRuntime(environ=env1, service_factory=provider.service_factory,
                         sampler_factory=provider.sampler_factory, purpose="qsearch").run(job)
    assert receipt["status"] == "OK", receipt
    tag = child["envelope"]["recovery_tag"]
    assert provider.constructed[-1]["max_execution_time"] == 5
    assert provider.constructed[-1]["environment"]["job_tags"][0] == tag
    assert ledger1.get(job.job_digest[7:47])["cap"] == 5 and receipt["compile"]["coupling_map"] == "target:ibm_fez"


# --- HW0-c: restart recovery ------------------------------------------------------------------


def test_restart_after_submission_reattaches_and_the_sampler_ran_once(tmp_path, monkeypatch):
    provider = Provider(crash_result=True)
    body, ledger, environ = _packed_setup(tmp_path, monkeypatch, provider)
    svc = _service(tmp_path, environ, provider)
    with pytest.raises(_Crash):                                 # Nano dies after ATLAS attached the job id
        _post(svc, body, "d1" * 16)
    svc.close()
    digest = parse_request(body).request_digest
    assert ledger.get("req-p")["job_id"] == "fake-fez-job-1" and len(provider.runs) == 1
    svc = _service(tmp_path, environ, provider)
    try:
        assert svc.recover() == [digest]
        status, receipt = _post(svc, body, "d2" * 16)
        assert status == 200 and receipt["status"] == "OK"
        assert receipt["hardware"]["provider_job_id"] == "fake-fez-job-1"
        verify_seal_v2(receipt, RECEIPT_KEY)
    finally:
        svc.close()
    svc = _service(tmp_path, environ, provider)                 # restart after the receipt was stored
    try:
        assert svc.recover() == [] and _post(svc, body, "d3" * 16) == (200, receipt)
    finally:
        svc.close()
    assert len(provider.runs) == 1 and ledger.get("req-p")["state"] == "done"


def test_restart_after_a_crash_before_attach_job_recovers_by_tag(tmp_path, monkeypatch):
    provider = Provider()
    body, ledger, environ = _packed_setup(tmp_path, monkeypatch, provider)
    original = CampaignLedger.attach_job
    calls = []

    def crash_once(self, request_id, job_id):
        if not calls:
            calls.append(job_id)
            raise _Crash()
        return original(self, request_id, job_id)
    monkeypatch.setattr(CampaignLedger, "attach_job", crash_once)
    svc = _service(tmp_path, environ, provider)
    with pytest.raises(_Crash):
        _post(svc, body, "e1" * 16)
    svc.close()
    assert ledger.get("req-p")["job_id"] is None and len(provider.runs) == 1
    svc = _service(tmp_path, environ, provider)
    try:
        assert svc.recover() == [parse_request(body).request_digest]
        status, receipt = _post(svc, body, "e2" * 16)
        assert receipt["status"] == "OK" and ledger.get("req-p")["job_id"] == "fake-fez-job-1"
    finally:
        svc.close()
    assert len(provider.runs) == 1


# --- HW0-e: hardware unreachable --------------------------------------------------------------


def test_hardware_unreachable_when_any_flag_is_missing(tmp_path, monkeypatch):
    touched = []

    def tripwire(name):
        def raise_if_touched(*args, **kwargs):
            touched.append(name)
            raise AssertionError(f"real {name} touched in an offline test")
        return raise_if_touched
    monkeypatch.setattr(qiskit_ibm_runtime, "SamplerV2", tripwire("SamplerV2"))
    monkeypatch.setattr(qiskit_ibm_runtime, "QiskitRuntimeService", tripwire("QiskitRuntimeService"))
    provider = Provider()
    body, ledger, environ = _packed_setup(tmp_path, monkeypatch, provider)
    services_before = provider.services               # the operator's identity step resolved one fake context
    monkeypatch.delenv(GO)
    monkeypatch.delenv(PACKED)

    def production(env, name):
        """The runtime as deployed: no injected service or sampler factory."""
        cfg = ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path / name,
                            receipt_key=RECEIPT_KEY, request_key=REQUEST_KEY, v2_enabled=True)
        return QSearchService(cfg, runtime_factory=lambda kind: IbmRuntime(environ=env))

    no_go = {k: v for k, v in environ.items() if k != GO}
    no_live = {k: v for k, v in environ.items() if k != LIVE}
    no_packed = {k: v for k, v in environ.items() if k != PACKED}
    # NANO_QSEARCH_V2 missing: 404 before anything is parsed.
    svc = _service(tmp_path, environ, provider, v2=False)
    assert _post(svc, body, "f1" * 16) == (404, NOT_ENABLED)
    svc.close()
    # GO missing: Nano's own gate.
    svc = production(no_go, "no-go")
    status, receipt = _post(svc, body, "f2" * 16)
    assert receipt["status"] == "NOT_RUN" and receipt["reason"] == f"refused: ibm: {GO}=1 is required"
    svc.close()
    # LIVE missing (GO set): Nano refuses before resolving any live context (R3 M3).
    monkeypatch.setenv(GO, "1")
    svc = production(no_live, "no-live")
    status, receipt = _post(svc, body, "f3" * 16)
    assert receipt["status"] == "NOT_RUN" and receipt["reason"] == f"refused: ibm: {LIVE}=1 is required"
    svc.close()
    # PACKED missing (GO and LIVE set, so the fakes are injected): Nano refuses before any context too.
    svc = _service(tmp_path, no_packed, provider)
    status, receipt = _post(svc, body, "f4" * 16)
    assert receipt["status"] == "NOT_RUN" and receipt["reason"].startswith(f"refused: ibm: {PACKED}=1 is required")
    svc.close()
    # And ATLAS's own PACKED gate still holds when only Nano's flag is set (os.environ lacks it).
    svc = _service(tmp_path / "atlas-packed", environ, provider)
    status, receipt = _post(svc, body, "f5" * 16)
    assert receipt["status"] == "NOT_RUN" and receipt["reason"] == "refused: ibm: RuntimeError"
    svc.close()
    assert touched == [] and provider.constructed == [] and provider.runs == [] and ledger.totals()["jobs"] == 0
    assert provider.services == services_before + 1    # only the ATLAS-gate case resolved a (fake) context


# --- R3 H1: a submitted request is never NOT_RUN ------------------------------------------------


def test_provider_error_is_pending_then_failed_never_not_run(tmp_path, monkeypatch):
    provider = Provider(usage=0.7, status="ERROR")
    body, ledger, environ = _packed_setup(tmp_path, monkeypatch, provider)
    svc = _service(tmp_path, environ, provider)
    try:
        assert _post(svc, body, "0a" * 16) == (503, {"error": "job not completed"})
        digest = parse_request(body).request_digest
        assert svc.store.get_request(digest).state == "ACCEPTED" and ledger.get("req-p")["job_id"] == "fake-fez-job-1"
        assert svc.recover() == [digest]
        status, receipt = _post(svc, body, "0b" * 16)
        assert status == 200 and receipt["status"] == "FAILED" and receipt["blocks"] == []
        assert receipt["reason"] == "provider: ERROR" and "free provider text" not in json.dumps(receipt)
        assert receipt["hardware"]["provider_job_id"] == "fake-fez-job-1" and receipt["request"]["block_count"] == 0
        assert receipt["usage"]["quantum_seconds"] == 0.7 and receipt["usage"]["usage_pending"] is False
        verify_seal_v2(receipt, RECEIPT_KEY)
        assert ledger.get("req-p")["usage"] == 0.7 and len(provider.runs) == 1
    finally:
        svc.close()


def test_an_ambiguous_submission_stays_pending_until_released(tmp_path, monkeypatch):
    provider = Provider(run_error=ConnectionError("socket closed mid-request"))
    body, ledger, environ = _packed_setup(tmp_path, monkeypatch, provider)
    svc = _service(tmp_path, environ, provider)
    try:
        assert _post(svc, body, "1a" * 16)[0] == 503
        assert ledger.get("req-p")["state"] == "ambiguous_submission"
        assert svc.recover() == [] and _post(svc, body, "1b" * 16)[0] == 503      # no job found by tag yet
        ledger.release_unsubmitted("req-p", reason="operator: zero-match tag lookup after grace")
        assert svc.recover() == [parse_request(body).request_digest]
        status, receipt = _post(svc, body, "1c" * 16)
        assert receipt["status"] == "NOT_RUN" and receipt["reason"] == "recovery: request was never submitted"
    finally:
        svc.close()
    assert len(provider.runs) == 1


def test_a_definite_provider_refusal_is_not_run_and_releases_the_reservation(tmp_path, monkeypatch):
    from qiskit_ibm_runtime.exceptions import IBMInputValueError
    provider = Provider(run_error=IBMInputValueError("circuit is not ISA for this target"))
    body, ledger, environ = _packed_setup(tmp_path, monkeypatch, provider)
    svc = _service(tmp_path, environ, provider)
    try:
        status, receipt = _post(svc, body, "2a" * 16)
        assert receipt["status"] == "NOT_RUN" and receipt["reason"] == "refused: ibm: QSearchNotSubmitted"
        assert ledger.get("req-p") is None and ledger.totals()["jobs"] == 0
    finally:
        svc.close()
