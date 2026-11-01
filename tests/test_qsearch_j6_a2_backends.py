"""J6 amendment A2: ibm_kingston / ibm_marrakesh are live q-search targets with ibm_fez's semantics.

The runtime tests use the recording fake ATLAS (``test_qsearch_v2_runtime``'s ``atlas`` fixture): no
qiskit, no ATLAS, no IBM. The device-compile tests (marked ``engine``) transpile a basis-prep program
against FakeMarrakesh's target standing in for the resolved live backend; no account, no Aer run.
"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nano_qsearch import compile as compile_mod
from nano_qsearch import job as job_mod
from nano_qsearch import receipt, receipt_v2, runtime_ibm
from nano_qsearch import request as request_mod
from nano_qsearch.job import JobValidationError, parse_job
from nano_qsearch.receipt_v2 import request_receipt
from nano_qsearch.request import RequestValidationError, parse_request
from nano_qsearch.runtime_ibm import JOB_NEVER_SUBMITTED, RECOVERY_NOT_RUN, IbmRuntime, atlas_campaign_submit
from qsearch_v2_support import FIXTURES, atlas_request_receipt, job_body, request_body
from test_qsearch_v2_runtime import FakeTarget, _plan, atlas  # noqa: F401  - the fake-ATLAS fixture

A2_LIVE = ("ibm_kingston", "ibm_marrakesh")
LIVE = ("ibm_fez",) + A2_LIVE


def _live_context(state, backend):
    live = SimpleNamespace(name=backend, target=FakeTarget())
    state.context = SimpleNamespace(backend_name=backend, instance="inst", backend=live, service=object())
    return live


# --- target lists ------------------------------------------------------------------------------


def test_target_lists_add_the_two_live_backends_and_keep_fake_fez_the_only_dry_run_target():
    assert job_mod.TARGETS == ("heron_heavy_hex", "fake_fez") + LIVE
    assert request_mod.REQUEST_TARGETS == ("fake_fez",) + LIVE and request_mod.LIMITED_TARGETS == LIVE
    assert runtime_ibm.IBM_TARGETS == ("fake_fez",) + LIVE and runtime_ibm.LIVE_TARGETS == LIVE
    assert compile_mod.DEVICE_TARGETS == LIVE and compile_mod.LIVE_ONLY_TARGETS == A2_LIVE
    assert compile_mod.FEZ_TARGETS == ("fake_fez", "ibm_fez")                  # the FakeFez stand-in is Fez only
    for module in (runtime_ibm, receipt, receipt_v2):
        assert module.DRY_RUN_TARGETS == ("fake_fez",)
    for target in A2_LIVE:
        assert compile_mod.SNAPSHOT_ENTRIES[target] == (target,)               # never another device's calibration
    assert compile_mod.SNAPSHOT_ENTRIES["ibm_fez"] == ("ibm_fez",)
    assert compile_mod.SNAPSHOT_ENTRIES["fake_fez"] == ("fake_fez", "ibm_fez")


@pytest.mark.parametrize("target", ["ibm_torino", "fake_kingston", "fake_marrakesh", "IBM_KINGSTON", "ibm_kingston "])
def test_unknown_targets_are_refused(target):
    with pytest.raises(JobValidationError, match="COMPILE"):
        parse_job(job_body(target=target))
    body = request_body(2, target="ibm_kingston")
    body["descriptor"]["target"] = target
    with pytest.raises(RequestValidationError, match="DESCRIPTOR"):
        parse_request(body)


@pytest.mark.parametrize("backend", A2_LIVE)
def test_a_new_live_target_needs_qpu_seconds_like_ibm_fez(backend):
    with pytest.raises(RequestValidationError, match="LIMITS"):
        parse_request(request_body(2, target=backend, max_qpu_s=1, max_execution_time=10))
    assert parse_request(request_body(2, target="fake_fez", max_qpu_s=1, max_execution_time=10)).target == "fake_fez"
    parsed = parse_request(request_body(2, target=backend, max_qpu_s=5, max_execution_time=10))
    assert parsed.target == backend and {job.target for job in parsed.jobs} == {backend}


# --- runtime: gates, end to end, recovery --------------------------------------------------------


@pytest.mark.parametrize("backend", A2_LIVE)
def test_a_new_live_target_keeps_every_nano_gate(atlas, backend):
    request = parse_request(request_body(2, target=backend))
    calls = []

    def run(env):
        return IbmRuntime(environ=env, compile_fn=lambda job, backend=None: calls.append(job),
                          request_submit_fn=lambda *a, **k: calls.append(a)).run_request(request)
    assert run({})["reason"] == "refused: ibm: AETHER_AQRC_HW_GO=1 is required"
    assert run({"AETHER_AQRC_HW_GO": "1"})["reason"].startswith("refused: ibm: AETHER_AQRC_AUTH")
    for missing, reason in (("AETHER_AQRC_QSEARCH_LIVE", "AETHER_AQRC_QSEARCH_LIVE=1 is required"),
                            ("AETHER_AQRC_QSEARCH_PACKED", "AETHER_AQRC_QSEARCH_PACKED=1 is required for a packed request")):
        body = run({k: v for k, v in atlas.environ.items() if k != missing})
        assert body["status"] == "NOT_RUN" and body["reason"] == f"refused: ibm: {reason}"
    assert calls == [] and atlas.calls == []                                # refused before any context or compile
    atlas.ledger.v2 = False                                                 # V1 ledger: BLOCKED, no context
    blocked = IbmRuntime(environ=atlas.environ, compile_fn=lambda job: _plan()).run_request(request)
    assert blocked["reason"] == "refused: ibm: RuntimeError" and atlas.calls == []


@pytest.mark.parametrize("backend", A2_LIVE)
def test_a_new_live_target_request_flows_end_to_end_through_fake_atlas(atlas, backend):
    live = _live_context(atlas, backend)
    request = parse_request(request_body(2, target=backend, request_id=f"hw0j6-base-fp-{backend[4:]}"))
    compiled_with = []

    def compile_fn(job, *, backend=None):
        compiled_with.append(backend)
        return _plan(backend.target, backend_name=backend.name)
    atlas.live_receipt = atlas_request_receipt(request, usage=3.0)
    record = {}
    body = IbmRuntime(environ=atlas.environ, compile_fn=compile_fn, sampler_factory="S",
                      service_factory="svc").run_request(request, record=record)
    assert body["status"] == "OK" and body["usage"]["quantum_seconds"] == 3.0 and len(body["blocks"]) == 2
    assert body["backend"] == {"kind": "ibm", "id": backend, "dry_run": False}
    assert body["hardware"]["backend_name"] == backend and body["hardware"]["dry_run"] is False
    assert compiled_with == [live] * 2 and record["target_resolutions"] == 1
    assert [c[0] for c in atlas.calls] == ["context", "child", "child", "request", "submit"]   # never "dry"
    assert {c[1]["target"] for c in atlas.calls if c[0] == "child"} == {backend}
    assert atlas.calls[4][2]["context"] is atlas.context and atlas.calls[4][2]["sampler_factory"] == "S"
    # A plan bound to another device's target is refused before submission.
    atlas.calls.clear()
    other = IbmRuntime(environ=atlas.environ,
                       compile_fn=lambda job, backend=None: _plan(backend.target, backend_name="ibm_fez")).run_request(request)
    assert other["reason"] == "refused: ibm: ValueError" and "submit" not in [c[0] for c in atlas.calls]
    # Recovery re-attaches through ATLAS; it never re-runs and never reads the request as a dry run.
    runtime = IbmRuntime(environ=atlas.environ, service_factory="svc")
    assert runtime.recover_request(request)["reason"] == runtime_ibm.NEVER_SUBMITTED
    atlas.ledger.rows[request.request_id] = {"request_id": request.request_id, "receipt": "{}"}
    atlas.recovered = {"request_id": request.request_id,
                       "receipt": json.dumps(atlas_request_receipt(request, usage=1.5))}
    recovered = runtime.recover_request(request)
    assert recovered["status"] == "OK" and recovered["hardware"]["backend_name"] == backend


@pytest.mark.parametrize("backend", A2_LIVE)
def test_a_new_live_target_is_never_a_dry_run(atlas, backend):
    request = parse_request(request_body(2, target=backend))
    lying = atlas_request_receipt(request, usage=3.0)
    lying["dry_run"] = True
    with pytest.raises(Exception, match="dry_run flag contradicts"):
        request_receipt(request, lying, purpose=request.purpose)
    job = parse_job(job_body(target=backend, max_qpu_s=5))
    assert IbmRuntime(environ={})._spend_gate(backend, [5]) == "ibm: AETHER_AQRC_HW_GO=1 is required"
    assert IbmRuntime(environ={})._spend_gate("fake_fez", [0]) is None
    # v1 job on a v2 ledger: live context, the job's own limit, the device binding checked.
    live = _live_context(atlas, backend)
    assert atlas_campaign_submit(job, _plan(live.target, backend_name=backend), purpose="qcomms",
                                 environ=atlas.environ, sampler_factory="S") == {"live": True}
    assert [c[0] for c in atlas.calls] == ["context", "child", "submit_child"]
    assert atlas.calls[1][1]["target"] == backend and atlas.calls[1][1]["max_execution_time"] == 5
    # v1 recovery: a live job goes to ATLAS (no ledger row: never submitted), a dry run is never re-run.
    assert IbmRuntime(environ=atlas.environ).recover_job(job)["reason"] == JOB_NEVER_SUBMITTED
    assert IbmRuntime().recover_job(parse_job(job_body(target="fake_fez")))["reason"] == RECOVERY_NOT_RUN


# --- device compile (engine) -----------------------------------------------------------------------


def _diagonal(job, diag):
    n = len(diag)
    q = tuple(tuple(float(diag[i]) if i == j else 0.0 for j in range(n)) for i in range(n))
    return replace(job, q=q, variables=tuple(f"x{i}" for i in range(n)))


def _snapshot(good, n=156):
    readout = {str(q): 0.05 for q in range(n)}
    for rank, q in enumerate(good):
        readout[str(q)] = 0.001 + 1e-5 * rank
    return {"num_qubits": n, "last_update": "2026-10-31 16:50:00+00:00", "readout_error": readout,
            "one_qubit_error": {"x": {str(q): 3e-4 for q in range(n)}}, "t1_s": {str(q): 1.5e-4 for q in range(n)}}


@pytest.mark.engine
@pytest.mark.parametrize("backend", A2_LIVE)
def test_device_compile_binds_the_new_backend_and_nothing_stands_in_for_it(backend):
    pytest.importorskip("qiskit")
    fake_provider = pytest.importorskip("qiskit_ibm_runtime.fake_provider")
    from nano_qsearch.compile import (TARGET_METADATA_KEY, CompileOptions, check_isa, compile_jobs, compile_program,
                                      target_digest)
    four = parse_job(json.loads((FIXTURES / "job_4var.json").read_text(encoding="utf-8")))
    job = replace(_diagonal(four, [-1.0, 1.0, -0.5, 2.0]), target=backend)
    target = fake_provider.FakeMarrakesh().target
    live = SimpleNamespace(name=backend, target=target)                    # stands in for the resolved backend
    opts = CompileOptions(basis_prep=True)
    plan = compile_program(job, backend=live, options=opts)
    assert plan.compile_record["backend_name"] == backend == plan.isa.metadata[TARGET_METADATA_KEY]["backend_name"]
    assert plan.compile_record["target_digest"] == target_digest(target) and plan.coupling_map == f"target:{backend}"
    assert plan.compile_record["target"] == backend
    check_isa(plan.isa, target)
    runtime_ibm.check_device_binding(plan, SimpleNamespace(backend_name=backend, backend=live))
    assert [p.compiled_plan_digest for p in compile_jobs([job], backend=live, options=opts)] == [plan.compiled_plan_digest]
    # No backend: refused (neither FakeFez nor anything else stands in for a live-only target).
    for call in (lambda: compile_program(job, options=opts), lambda: compile_jobs([job], options=opts)):
        with pytest.raises(ValueError, match="only against its resolved live backend"):
            call()
    other = "ibm_marrakesh" if backend == "ibm_kingston" else "ibm_kingston"
    for name in ("ibm_fez", "fake_fez", other):
        with pytest.raises(ValueError, match="differs from the job's target"):
            compile_program(job, backend=SimpleNamespace(name=name, target=target), options=opts)
    with pytest.raises(ValueError, match="differs from the job's target"):
        compile_program(replace(job, target="ibm_fez"), backend=live, options=opts)
    # c2 layout: ranked from this backend's own snapshot entry (or its target), never another device's.
    good = [100 + 2 * i for i in range(4)]
    snap = compile_program(job, backend=live, options=CompileOptions(
        basis_prep=True, layout="calibration", calibration={backend: _snapshot(good)}, calibration_sha256="ef" * 32))
    layout = snap.compile_record["rewrites"]["layout"]
    assert layout["applied"] and layout["backend"] == backend and snap.inspection["initial_layout"] == good
    by_target = compile_program(job, backend=live, options=CompileOptions(basis_prep=True, layout="target"))
    assert by_target.compile_record["rewrites"]["layout"]["applied"] is True
    for entry in ("ibm_fez", other):
        with pytest.raises(ValueError, match="may not rank"):
            compile_program(job, backend=live, options=CompileOptions(
                layout="calibration", calibration={entry: _snapshot(good)}, layout_backend=entry))
