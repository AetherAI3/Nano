"""Lane HW0 N1/N3/N4 runtime wiring against a fake ATLAS module (no ATLAS, no qiskit, no IBM).

A stand-in ``aether_atlas.aqrc.campaign_runtime`` / ``campaign`` pair is placed in
``sys.modules`` and records every call, so these tests pin what Nano passes to ATLAS
(context, provider limit, sampler factory, one request over N children) without
depending on the ATLAS implementation.
"""
import json
import sys
import types
from types import SimpleNamespace

import pytest

from nano_qsearch.compile import target_digest
from nano_qsearch.job import parse_job
from nano_qsearch.request import parse_request
from nano_qsearch.runtime_ibm import (
    NEVER_SUBMITTED, IbmRuntime, NeverSubmitted, atlas_campaign_submit, atlas_request_recover, atlas_request_submit,
    atlas_resolve_context, check_device_binding,
)
from qsearch_v2_support import atlas_request_receipt, child_sha, job_body, request_body


class FakeTarget:
    """Just enough of a qiskit ``Target`` for ``target_digest``."""

    def __init__(self, edges=((0, 1), (1, 2))):
        self.num_qubits = 3
        self._ops = {"cz": set(edges), "measure": {(q,) for q in range(3)}, "rz": None}
        self.operation_names = list(self._ops)

    def qargs_for_operation_name(self, name):
        return self._ops[name]


class FakeLedger:
    def __init__(self, *, v2=True, rows=None):
        self.v2, self.rows = v2, dict(rows or {})

    def configuration(self):
        return {"protocol": {"instance": "inst", "stage_purposes": {"Q": "qsearch"} if self.v2 else {}}}

    def get(self, request_id):
        return self.rows.get(request_id)


@pytest.fixture
def atlas(monkeypatch, tmp_path):
    """Install a recording fake ATLAS and return its call log plus knobs."""
    state = SimpleNamespace(calls=[], ledger=FakeLedger(), dry_receipt=None, live_receipt=None, recovered=None)
    backend = SimpleNamespace(name="ibm_fez", target=FakeTarget())
    state.context = SimpleNamespace(backend_name="ibm_fez", instance="inst", backend=backend, service=object())

    cr = types.ModuleType("aether_atlas.aqrc.campaign_runtime")

    def prepare_qsearch_child(circuit, **kw):
        state.calls.append(("child", kw))
        return {"envelope": kw, "child_sha256": child_sha(len([c for c in state.calls if c[0] == "child"]) - 1)}

    def prepare_qsearch_request(pairs, **kw):
        state.calls.append(("request", len(pairs), kw))
        return {"envelope": kw, "request_sha256": "sha256:" + "e" * 64}

    def run_qsearch_request_dry(pairs, request, **kw):
        state.calls.append(("dry", len(pairs), kw))
        return state.dry_receipt

    def submit_qsearch_request(pairs, request, **kw):
        state.calls.append(("submit", len(pairs), kw))
        return state.live_receipt

    def resolve_qsearch_context(ledger, **kw):
        state.calls.append(("context", kw))
        return state.context

    def recover_qsearch_submission(request_id, **kw):
        state.calls.append(("recover", request_id, kw))
        return state.recovered

    def run_qsearch_dry(circuit, child):
        state.calls.append(("dry_child", child))
        return {"dry": True}

    def submit_qsearch_child(circuit, child, **kw):
        state.calls.append(("submit_child", child, kw))
        return {"live": True}

    for fn in (prepare_qsearch_child, prepare_qsearch_request, run_qsearch_request_dry, submit_qsearch_request,
               resolve_qsearch_context, recover_qsearch_submission, run_qsearch_dry, submit_qsearch_child):
        setattr(cr, fn.__name__, fn)
    campaign = types.ModuleType("aether_atlas.aqrc.campaign")
    campaign.CampaignLedger = lambda path: state.ledger
    campaign.qsearch_stage = lambda protocol: "Q" if protocol["stage_purposes"] else None
    aqrc = types.ModuleType("aether_atlas.aqrc")
    aqrc.campaign_runtime, aqrc.campaign = cr, campaign
    root = types.ModuleType("aether_atlas")
    root.aqrc = aqrc
    for name, module in (("aether_atlas", root), ("aether_atlas.aqrc", aqrc),
                         ("aether_atlas.aqrc.campaign_runtime", cr), ("aether_atlas.aqrc.campaign", campaign)):
        monkeypatch.setitem(sys.modules, name, module)
    authority = tmp_path / "authority.json"
    authority.write_text(json.dumps({"body": {"scope": "plan"}}), encoding="utf-8")
    (tmp_path / "authority.key").write_bytes(b"k" * 32)
    (tmp_path / "ledger.sqlite").write_bytes(b"")
    state.environ = {"AETHER_AQRC_HW_GO": "1", "AETHER_AQRC_QSEARCH_LIVE": "1", "AETHER_AQRC_QSEARCH_PACKED": "1",
                     "AETHER_AQRC_AUTHORITY_PATH": str(authority),
                     "AETHER_AQRC_AUTHORITY_KEY_PATH": str(tmp_path / "authority.key"),
                     "AETHER_AQRC_LEDGER_PATH": str(tmp_path / "ledger.sqlite")}
    return state


def _plan(target_obj=None, backend_name="ibm_fez"):
    record = {"target": "ibm_fez"}
    if target_obj is not None:
        record.update(backend_name=backend_name, target_digest=target_digest(target_obj))
    return SimpleNamespace(isa=object(), compiled_plan_digest="sha256:" + "c" * 64, compile_record=record,
                           variant="ordinary", nuclei=[], metrics={})


# --- run_request through injected delegates --------------------------------------------------------


def test_run_request_compiles_each_job_once_and_makes_one_request_call():
    request = parse_request(request_body(3))
    seen = []

    def submit(req, compiled, **kw):
        seen.append((req.request_id, len(compiled), sorted(kw)))
        return atlas_request_receipt(req)
    body = IbmRuntime(compile_fn=lambda job: _plan(), request_submit_fn=submit).run_request(request)
    assert body["status"] == "OK" and len(body["blocks"]) == 3
    assert seen == [("req-hw0", 3, ["backend_factory", "context", "environ", "max_pubs", "purpose", "sampler_factory"])]


def test_ibm_fez_request_keeps_nanos_spend_gates():
    request = parse_request(request_body(2, target="ibm_fez"))
    calls = []

    def runtime(env):
        return IbmRuntime(environ=env, compile_fn=lambda j: calls.append(j),
                          request_submit_fn=lambda *a, **k: calls.append(a))
    body = runtime({}).run_request(request)
    assert body["status"] == "NOT_RUN" and body["reason"] == "refused: ibm: AETHER_AQRC_HW_GO=1 is required"
    assert runtime({"AETHER_AQRC_HW_GO": "1"}).run_request(request)["reason"].startswith("refused: ibm: AETHER_AQRC_AUTH")
    assert body["blocks"] == [] and body["backend"] == {"kind": "none"} and calls == []


@pytest.mark.parametrize("exc, reason", [
    (MemoryError(), "ibm: MemoryError"), (ConnectionResetError(), "ibm: ConnectionResetError"),
    (ValueError("x"), "refused: ibm: ValueError"), (RuntimeError("BLOCKED"), "refused: ibm: RuntimeError"),
])
def test_failures_are_not_run_with_no_blocks(exc, reason):
    request = parse_request(request_body(3))

    def submit(*a, **k):
        raise exc
    body = IbmRuntime(compile_fn=lambda job: _plan(), request_submit_fn=submit).run_request(request)
    assert body["status"] == "NOT_RUN" and body["blocks"] == [] and body["reason"] == reason


def test_a_receipt_with_fewer_blocks_is_never_returned():
    request = parse_request(request_body(3))

    def submit(req, compiled, **kw):
        receipt = atlas_request_receipt(req)
        receipt["pubs"].pop()
        return receipt
    body = IbmRuntime(compile_fn=lambda job: _plan(), request_submit_fn=submit).run_request(request)
    assert body["status"] == "NOT_RUN" and body["blocks"] == [] and body["reason"] == "refused: ibm: ReceiptAuditError"


def test_live_context_is_resolved_once_and_compile_gets_its_backend(atlas):
    request = parse_request(request_body(2, target="ibm_fez"))
    backends = []

    def compile_fn(job, *, backend=None):
        backends.append(backend)
        return _plan(backend.target)
    atlas.live_receipt = atlas_request_receipt(request, usage=3.0)
    sampler = object()
    body = IbmRuntime(environ=atlas.environ, compile_fn=compile_fn, sampler_factory=sampler,
                      service_factory="svc").run_request(request)
    assert body["status"] == "OK" and body["usage"]["quantum_seconds"] == 3.0
    assert backends == [atlas.context.backend] * 2
    assert [c[0] for c in atlas.calls] == ["context", "child", "child", "request", "submit"]
    assert atlas.calls[0][1] == {"service_factory": "svc", "environ": atlas.environ}
    assert [c[1]["max_execution_time"] for c in atlas.calls if c[0] == "child"] == [10, 10]
    assert atlas.calls[3] == ("request", 2, {"request_id": "req-hw0", "max_execution_time": 10,
                                             "client_request_digest": request.request_digest})
    submit_kw = atlas.calls[4][2]
    assert submit_kw["context"] is atlas.context and submit_kw["sampler_factory"] is sampler
    assert submit_kw["authority"] == {"body": {"scope": "plan"}} and submit_kw["authority_key"] == b"k" * 32


def test_live_request_refuses_a_plan_not_bound_to_the_resolved_target(atlas):
    request = parse_request(request_body(2, target="ibm_fez"))
    atlas.live_receipt = atlas_request_receipt(request, usage=3.0)
    other = FakeTarget(edges=((0, 2),))
    body = IbmRuntime(environ=atlas.environ, compile_fn=lambda job, backend=None: _plan(other)).run_request(request)
    assert body["status"] == "NOT_RUN" and body["reason"] == "refused: ibm: ValueError"
    assert "submit" not in [c[0] for c in atlas.calls]


def test_live_request_on_a_v1_ledger_is_blocked_without_a_context(atlas):
    atlas.ledger.v2 = False
    request = parse_request(request_body(2, target="ibm_fez"))
    body = IbmRuntime(environ=atlas.environ, compile_fn=lambda job: _plan()).run_request(request)
    assert body["reason"] == "refused: ibm: RuntimeError" and atlas.calls == []      # blocked before any child


def test_dry_request_goes_through_run_qsearch_request_dry_with_injected_factories(atlas):
    request = parse_request(request_body(3))
    atlas.dry_receipt = atlas_request_receipt(request)
    body = IbmRuntime(compile_fn=lambda job: _plan(), sampler_factory="S", backend_factory="B").run_request(request)
    assert body["status"] == "OK" and body["hardware"]["dry_run"] is True
    assert [c[0] for c in atlas.calls] == ["child", "child", "child", "request", "dry"]
    assert atlas.calls[-1] == ("dry", 3, {"sampler_factory": "S", "backend_factory": "B"})


def test_request_submit_checks_pub_children_and_reads_a_durable_row(atlas):
    request = parse_request(request_body(2))
    receipt = atlas_request_receipt(request)
    receipt["pubs"][0]["child_sha256"], receipt["pubs"][1]["child_sha256"] = child_sha(1), child_sha(0)
    atlas.dry_receipt = receipt
    with pytest.raises(Exception, match="prepared children"):
        atlas_request_submit(request, [_plan(), _plan()], purpose="qcomms")
    atlas.calls.clear()
    atlas.dry_receipt = {"request_id": "req-hw0", "receipt": json.dumps(atlas_request_receipt(request))}
    assert atlas_request_submit(request, [_plan(), _plan()], purpose="qcomms")["schema"] == "AqrcQSearchRequestReceiptV1"
    atlas.calls.clear()
    atlas.dry_receipt = {"request_id": "req-hw0", "receipt": json.dumps({"schema": "AqrcQSearchSubmissionV1"})}
    with pytest.raises(RuntimeError, match="held request row"):
        atlas_request_submit(request, [_plan(), _plan()], purpose="qcomms")


# --- N1 / N3: the v1 path ---------------------------------------------------------------------


def test_v1_ibm_fez_on_a_v2_ledger_uses_the_live_context_and_the_job_limit(atlas):
    job = parse_job(job_body(target="ibm_fez", max_qpu_s=5))
    plan = _plan(atlas.context.backend.target)
    assert atlas_campaign_submit(job, plan, purpose="qcomms", environ=atlas.environ, sampler_factory="S") == {"live": True}
    assert [c[0] for c in atlas.calls] == ["context", "child", "submit_child"]
    assert atlas.calls[1][1]["max_execution_time"] == 5 and atlas.calls[1][1]["request_id"] == job.job_digest[7:47]
    submit_kw = atlas.calls[2][2]
    assert submit_kw["context"] is atlas.context and submit_kw["sampler_factory"] == "S"


def test_v1_ibm_fez_on_a_v1_ledger_keeps_the_declared_context_and_default_child(atlas):
    atlas.ledger.v2 = False
    job = parse_job(job_body(target="ibm_fez", max_qpu_s=5))
    atlas_campaign_submit(job, _plan(), purpose="qsearch", environ=atlas.environ)
    assert [c[0] for c in atlas.calls] == ["child", "submit_child"]
    assert "max_execution_time" not in atlas.calls[0][1]
    context = atlas.calls[1][2]["context"]
    assert (context.backend_name, context.instance) == ("ibm_fez", "inst") and not hasattr(context, "backend")
    assert "sampler_factory" not in atlas.calls[1][2]


def test_v1_runtime_resolves_the_context_before_compiling(atlas):
    job = parse_job(job_body(target="ibm_fez", max_qpu_s=5))
    order = []

    def compile_fn(job, *, backend=None):
        order.append(("compile", backend))
        return _plan(backend.target)

    def submit_fn(job, compiled, *, purpose, environ, context=None, sampler_factory=None):
        order.append(("submit", context))
        return {"schema": "nope"}
    # An injected submit_fn owns its context: no lookup unless context_fn is given.
    IbmRuntime(environ=atlas.environ, compile_fn=lambda j: _plan(), submit_fn=submit_fn).run(job)
    assert order == [("submit", None)] and atlas.calls == []
    order.clear()
    body = IbmRuntime(environ=atlas.environ, compile_fn=compile_fn, submit_fn=submit_fn,
                      context_fn=atlas_resolve_context).run(job)
    assert order == [("compile", atlas.context.backend), ("submit", atlas.context)]
    assert body["status"] == "REFUSED"


def test_check_device_binding():
    target = FakeTarget()
    context = SimpleNamespace(backend_name="ibm_fez", backend=SimpleNamespace(name="ibm_fez", target=target))
    check_device_binding(_plan(target), context)
    for bad in (_plan(None), _plan(FakeTarget(edges=((0, 2),))), _plan(target, backend_name="ibm_torino")):
        with pytest.raises(ValueError):
            check_device_binding(bad, context)


# --- recovery ----------------------------------------------------------------------------------


def test_recover_request_never_submits(atlas):
    request = parse_request(request_body(2, target="ibm_fez"))
    runtime = IbmRuntime(environ=atlas.environ, service_factory="svc")
    with pytest.raises(NeverSubmitted):
        atlas_request_recover(request, environ=atlas.environ)
    assert runtime.recover_request(request)["reason"] == NEVER_SUBMITTED
    atlas.ledger.rows["req-hw0"] = {"request_id": "req-hw0", "receipt": "{}"}
    atlas.recovered = {"request_id": "req-hw0", "receipt": json.dumps({"schema": "AqrcQSearchSubmissionV1"})}
    assert runtime.recover_request(request) is None                                     # still running: pending
    atlas.recovered = {"request_id": "req-hw0", "receipt": json.dumps(atlas_request_receipt(request, usage=1.5))}
    body = runtime.recover_request(request)
    assert body["status"] == "OK" and body["usage"]["quantum_seconds"] == 1.5
    assert {c[0] for c in atlas.calls} == {"context", "recover"}
    assert [c for c in atlas.calls if c[0] == "context"][0][1]["service_factory"] == "svc"
    fake = parse_request(request_body(2))
    assert IbmRuntime().recover_request(fake)["reason"] == "recovery: hardware requests are never run from recovery"
