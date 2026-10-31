"""Amendment R3 (HW0 pre-flight review), Nano items, against a fake ATLAS module (no ATLAS, no qiskit, no IBM).

H1: once a request or job reached the provider it is never sealed NOT_RUN: the runtime returns
None (the service answers 503 and keeps the row), and a provider-terminal failure or a result
that does not audit becomes a v2 ``FAILED`` receipt. L1: v1 ibm_fez jobs re-attach on restart.
M3: GO, LIVE and (N > 1) PACKED are checked before any live context is resolved.
"""
import json
from types import SimpleNamespace

import pytest

from nano_qsearch.auth import AUTH_HEADER, sign_request
from nano_qsearch.job import parse_job
from nano_qsearch.receipt_v2 import HARDWARE_SEAL_PREFIX_V2, results_digest, seal_receipt_v2, verify_seal_v2
from nano_qsearch.request import parse_request
from nano_qsearch.runtime_ibm import JOB_NEVER_SUBMITTED, RECOVERY_NOT_RUN, IbmRuntime, NeverSubmitted
from nano_qsearch.service import REQUEST_PATH, SUBMIT_PATH, QSearchService, ServiceConfig
from qsearch_v2_support import MINIMUM_DISPLAY, atlas_request_receipt, job_body, request_body
from test_qsearch_v2_runtime import _plan, atlas  # noqa: F401  - the fake-ATLAS fixture

RECEIPT_KEY = b"receipt-key-" + b"0" * 20
REQUEST_KEY = b"request-key-" + b"1" * 20
FAILED_KEYS = {"schema", "receipt_version", "status", "request", "backend", "hardware", "blocks", "results_digest",
               "usage", "reason"}


class QSearchSubmittedPending(RuntimeError):
    """Stands in for ATLAS's exception of the same name (matched by class name)."""


def _raise(exc):
    def fn(*args, **kwargs):
        raise exc
    return fn


def _failed_atlas_receipt(request, *, usage=1.2, status="ERROR"):
    receipt = atlas_request_receipt(request, usage=usage)
    receipt.update(status="FAILED", pubs=[], provider_status=status,
                   reason=f"provider job ended {status}: free provider text never copied")
    return receipt


# --- M3: flags before any live context ------------------------------------------------------------


@pytest.mark.parametrize("drop, expected", [
    ("AETHER_AQRC_HW_GO", "AETHER_AQRC_HW_GO=1 is required"),
    ("AETHER_AQRC_QSEARCH_LIVE", "AETHER_AQRC_QSEARCH_LIVE=1 is required"),
    ("AETHER_AQRC_QSEARCH_PACKED", "AETHER_AQRC_QSEARCH_PACKED=1 is required for a packed request"),
])
def test_missing_flags_refuse_before_any_context_or_compile(atlas, drop, expected):
    environ = {k: v for k, v in atlas.environ.items() if k != drop}
    compiled = []
    body = IbmRuntime(environ=environ, compile_fn=lambda job, backend=None: compiled.append(job)).run_request(
        parse_request(request_body(3, target="ibm_fez")))
    assert body["status"] == "NOT_RUN" and body["reason"] == "refused: ibm: " + expected
    assert atlas.calls == [] and compiled == []


def test_a_singleton_request_needs_no_packed_flag(atlas):
    environ = {k: v for k, v in atlas.environ.items() if k != "AETHER_AQRC_QSEARCH_PACKED"}
    request = parse_request(request_body(1, target="ibm_fez", max_execution_time=5))
    atlas.live_receipt = atlas_request_receipt(request, usage=2.0)
    body = IbmRuntime(environ=environ, compile_fn=lambda job, backend=None: _plan(backend.target)).run_request(request)
    assert body["status"] == "OK" and atlas.calls[0][0] == "context"


def test_v1_live_context_needs_live_flag_before_resolving(atlas):
    environ = {k: v for k, v in atlas.environ.items() if k != "AETHER_AQRC_QSEARCH_LIVE"}
    body = IbmRuntime(environ=environ, compile_fn=lambda job, backend=None: _plan()).run(
        parse_job(job_body(target="ibm_fez")))
    assert body["status"] == "REFUSED" and body["reason"] == "ibm: RuntimeError" and atlas.calls == []


# --- H1: submitted means never NOT_RUN ------------------------------------------------------------


def test_submitted_pending_from_atlas_is_never_sealed(atlas):
    request = parse_request(request_body(3, target="ibm_fez"))
    runtime = IbmRuntime(environ=atlas.environ, compile_fn=lambda job: _plan(),
                         request_submit_fn=_raise(QSearchSubmittedPending("req-hw0", "job-1")))
    assert runtime.run_request(request) is None


@pytest.mark.parametrize("row, pending", [
    ({"job_id": "job-7", "state": "submitted"}, True),
    ({"job_id": None, "state": "ambiguous_submission"}, True),
    ({"job_id": None, "state": "reserved"}, False),
    (None, False),
])
def test_a_failure_after_hand_over_stays_pending_else_not_run(atlas, row, pending):
    request = parse_request(request_body(3, target="ibm_fez"))
    if row is not None:
        atlas.ledger.rows["req-hw0"] = {"request_id": "req-hw0", **row}
    runtime = IbmRuntime(environ=atlas.environ, compile_fn=lambda job: _plan(),
                         request_submit_fn=_raise(ConnectionResetError()))
    body = runtime.run_request(request)
    if pending:
        assert body is None
    else:
        assert body["status"] == "NOT_RUN" and body["reason"] == "ibm: ConnectionResetError"


def test_a_submitted_result_that_does_not_audit_is_failed_not_not_run(atlas):
    request = parse_request(request_body(2, target="ibm_fez"))
    bad = atlas_request_receipt(request, usage=3.0)
    bad["pubs"][1]["raw_samples"] = bad["pubs"][1]["raw_samples"][:-1]          # one shot short
    body = IbmRuntime(environ=atlas.environ, compile_fn=lambda job: _plan(),
                      request_submit_fn=lambda *a, **k: bad).run_request(request)
    assert body["status"] == "FAILED" and set(body) == FAILED_KEYS
    assert body["blocks"] == [] and body["request"] == {"request_digest": request.request_digest, "block_count": 0}
    assert body["reason"] == "refused: ibm: ReceiptAuditError" and body["results_digest"] == results_digest([])
    assert body["hardware"]["provider_job_id"] == "fake-job-1" and body["usage"]["quantum_seconds"] == 3.0
    # A FakeFez dry run that does not audit was never submitted: NOT_RUN as before.
    dry = parse_request(request_body(2))
    bad_dry = atlas_request_receipt(dry)
    bad_dry["pubs"].pop()
    body = IbmRuntime(compile_fn=lambda job: _plan(), request_submit_fn=lambda *a, **k: bad_dry).run_request(dry)
    assert body["status"] == "NOT_RUN"


def test_recovery_of_a_provider_failure_is_failed_and_sealed_with_the_hardware_seal(atlas):
    request = parse_request(request_body(3, target="ibm_fez"))
    atlas.ledger.rows["req-hw0"] = {"request_id": "req-hw0", "job_id": "fake-job-1", "receipt": "{}"}
    atlas.recovered = {"request_id": "req-hw0", "receipt": json.dumps(_failed_atlas_receipt(request))}
    body = IbmRuntime(environ=atlas.environ).recover_request(request)
    assert body["status"] == "FAILED" and set(body) == FAILED_KEYS and body["reason"] == "provider: ERROR"
    assert "free provider text" not in json.dumps(body)
    assert body["hardware"]["register"] == "meas" and body["hardware"]["job_tags"] == ["atlas-qsearch-req-req-hw0"]
    assert body["usage"] == {"quantum_seconds": 1.2, "usage_pending": False, "provider_sub_jobs": None,
                             "provider_metrics": {"usage": {"quantum_seconds": 1.2}}}
    sealed = seal_receipt_v2(body, RECEIPT_KEY)
    assert sealed["seal"].startswith(HARDWARE_SEAL_PREFIX_V2)
    verify_seal_v2(sealed, RECEIPT_KEY)


def test_service_keeps_a_handed_over_request_accepted_then_seals_failed_from_recovery(tmp_path):
    body = request_body(3, target="ibm_fez")
    request = parse_request(body)
    recovered = []
    runtime = IbmRuntime(environ={"AETHER_AQRC_HW_GO": "1", "AETHER_AQRC_QSEARCH_LIVE": "1",
                                  "AETHER_AQRC_QSEARCH_PACKED": "1", "AETHER_AQRC_AUTHORITY_PATH": __file__},
                         compile_fn=lambda job: None, context_fn=None,
                         request_submit_fn=_raise(QSearchSubmittedPending("req-hw0", "job-1")),
                         request_recover_fn=lambda req, **k: recovered.pop() if recovered else None)
    cfg = ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path, receipt_key=RECEIPT_KEY,
                        request_key=REQUEST_KEY, v2_enabled=True)
    svc = QSearchService(cfg, runtime_factory=lambda kind: runtime)
    raw = json.dumps(body, sort_keys=True).encode("utf-8")

    def post(nonce):
        headers = {AUTH_HEADER: sign_request(REQUEST_KEY, "POST", REQUEST_PATH, raw, ts=1000, nonce=nonce)}
        return svc.submit_request("POST", REQUEST_PATH, raw, headers, now=1000)
    try:
        assert post("a1" * 16) == (503, {"error": "job not completed"})
        row = svc.store.get_request(request.request_digest)
        assert row.state == "ACCEPTED" and row.attempts == 1
        assert post("a2" * 16)[0] == 503                                    # still running at the provider
        recovered.append(_failed_atlas_receipt(request))
        status, receipt = post("a3" * 16)
        assert status == 200 and receipt["status"] == "FAILED" and receipt["blocks"] == []
        verify_seal_v2(receipt, RECEIPT_KEY)
    finally:
        svc.close()


# --- H1 / L1 for v1 singletons ---------------------------------------------------------------------


def _atlas_job_receipt(job, plan_digest, *, status="DONE"):
    receipt = {"schema": "AqrcQSearchJobReceiptV1", "target": job.target, "backend_name": job.target,
               "job_id": "fake-v1-job", "dry_run": False, "status": status, "purpose": "qsearch",
               "child": {"envelope": {"compiled_plan_digest": plan_digest}},
               "pubs": [{"pub_index": 0, "register": "meas", "bit_order": "msb_first", "shots": job.shots,
                         "raw_samples": [MINIMUM_DISPLAY] * job.shots}]}
    if status == "FAILED":
        receipt["pubs"] = []
    return receipt


def test_v1_job_handed_over_stays_pending_and_recovers(atlas):
    job = parse_job(job_body(target="ibm_fez", shots=8))
    plan = _plan(atlas.context.backend.target)
    runtime = IbmRuntime(environ=atlas.environ, compile_fn=lambda job, backend=None: plan,
                         submit_fn=_raise(QSearchSubmittedPending("x", "y")), context_fn=None)
    assert runtime.run(job) is None
    outcomes = {"never": NeverSubmitted("no row"), "pending": (None, atlas.context),
                "ok": (_atlas_job_receipt(job, plan.compiled_plan_digest), atlas.context),
                "drift": (_atlas_job_receipt(job, "sha256:" + "d" * 64), atlas.context),
                "failed": (_atlas_job_receipt(job, plan.compiled_plan_digest, status="FAILED"), atlas.context)}

    def recover(case):
        def fn(job, **kw):
            value = outcomes[case]
            if isinstance(value, Exception):
                raise value
            return value
        return IbmRuntime(environ=atlas.environ, compile_fn=lambda job, backend=None: plan,
                          job_recover_fn=fn).recover_job(job)
    never = recover("never")
    assert never["status"] == "NOT_RUN" and never["reason"] == JOB_NEVER_SUBMITTED
    assert recover("pending") is None
    ok = recover("ok")
    assert ok["status"] == "OK" and ok["hardware"]["job_id"] == "fake-v1-job" and ok["audit"]["agree"] is True
    drift = recover("drift")
    assert drift["status"] == "REFUSED" and drift["reason"] == "recovery: ReceiptAuditError"
    assert recover("failed")["reason"] == "ibm: provider job failed"
    assert IbmRuntime().recover_job(parse_job(job_body(target="fake_fez")))["reason"] == RECOVERY_NOT_RUN


def test_v1_service_answers_503_then_reattaches_on_restart(tmp_path):
    body = job_body(target="ibm_fez", shots=8)
    job = parse_job(body)
    raw = json.dumps(body, sort_keys=True).encode("utf-8")
    plan = SimpleNamespace(compiled_plan_digest="sha256:" + "c" * 64, variant="ordinary", nuclei=[],
                           compile_record={"target": "ibm_fez"}, metrics={})
    stage = {"done": False}

    def job_recover(job, **kw):
        return (_atlas_job_receipt(job, plan.compiled_plan_digest), None) if stage["done"] else (None, None)
    runtime = IbmRuntime(environ={"AETHER_AQRC_HW_GO": "1", "AETHER_AQRC_AUTHORITY_PATH": __file__},
                         compile_fn=lambda job, backend=None: plan, context_fn=None,
                         submit_fn=_raise(QSearchSubmittedPending("x", "y")), job_recover_fn=job_recover)
    cfg = ServiceConfig(bind="127.0.0.1:0", backend="ibm", data_dir=tmp_path, receipt_key=RECEIPT_KEY,
                        request_key=REQUEST_KEY)
    svc = QSearchService(cfg, runtime_factory=lambda kind: runtime)

    def post(nonce):
        headers = {AUTH_HEADER: sign_request(REQUEST_KEY, "POST", SUBMIT_PATH, raw, ts=1000, nonce=nonce)}
        return svc.submit("POST", SUBMIT_PATH, raw, headers, now=1000)
    try:
        assert post("b1" * 16) == (503, {"error": "job not completed"})
        record = svc.store.get(job.job_digest)
        assert record.state == "ACCEPTED" and record.attempts == 1
        assert post("b2" * 16)[0] == 503 and svc.recover() == []
        stage["done"] = True
        assert svc.recover() == [job.job_digest]
        status, receipt = post("b3" * 16)
        assert status == 200 and receipt["status"] == "OK" and receipt["hardware"]["job_id"] == "fake-v1-job"
    finally:
        svc.close()
