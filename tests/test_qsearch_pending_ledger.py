"""Unreadable submission evidence stays pending; all delegates are local fakes."""
import json

import pytest

from nano_qsearch.auth import AUTH_HEADER, sign_request
from nano_qsearch.job import parse_job
from nano_qsearch.request import parse_request
from nano_qsearch.runtime_ibm import IbmRuntime
from nano_qsearch.service import REQUEST_PATH, SUBMIT_PATH, QSearchService, ServiceConfig
from qsearch_v2_support import atlas_request_receipt, job_body, request_body
from test_qsearch_lifecycle_service import Clock
from test_qsearch_r3 import RECEIPT_KEY, REQUEST_KEY, _atlas_job_receipt, _raise
from test_qsearch_v2_runtime import _plan


def _environ():
    return {"AETHER_AQRC_HW_GO": "1", "AETHER_AQRC_QSEARCH_LIVE": "1",
            "AETHER_AQRC_QSEARCH_PACKED": "1", "AETHER_AQRC_AUTHORITY_PATH": __file__}


@pytest.mark.parametrize("target", ["ibm_fez", "ibm_kingston", "ibm_marrakesh"])
@pytest.mark.parametrize("packed", [False, True], ids=["v1", "v2"])
@pytest.mark.parametrize("failure", [TimeoutError, ValueError])
def test_unreadable_ledger_after_submission_failure_stays_pending(target, packed, failure):
    runtime = IbmRuntime(
        environ=_environ(), compile_fn=lambda job: _plan(), context_fn=None,
        submit_fn=_raise(failure("submission outcome unknown")),
        request_submit_fn=_raise(failure("submission outcome unknown")),
        submitted_probe=_raise(OSError("ledger temporarily unreadable")))
    if packed:
        assert runtime.run_request(parse_request(request_body(target=target))) is None
    else:
        assert runtime.run(parse_job(job_body(target=target))) is None


def test_unreadable_probe_with_a_returned_held_v1_row_stays_pending():
    runtime = IbmRuntime(
        environ=_environ(), compile_fn=lambda job: _plan(), context_fn=None,
        submit_fn=lambda *args, **kwargs: {"state": "submitted", "job_id": "provider-job"},
        submitted_probe=_raise(OSError("ledger temporarily unreadable")))
    assert runtime.run(parse_job(job_body(target="ibm_fez"))) is None


@pytest.mark.parametrize("packed", [False, True], ids=["v1", "v2"])
def test_definitely_absent_submission_still_returns_not_run(packed):
    runtime = IbmRuntime(
        environ=_environ(), compile_fn=lambda job: _plan(), context_fn=None,
        submit_fn=_raise(TimeoutError()), request_submit_fn=_raise(TimeoutError()),
        submitted_probe=lambda *args, **kwargs: False)
    body = (runtime.run_request(parse_request(request_body(target="ibm_fez"))) if packed
            else runtime.run(parse_job(job_body(target="ibm_fez"))))
    assert body["status"] == "NOT_RUN"


@pytest.mark.parametrize("packed", [False, True], ids=["v1", "v2"])
@pytest.mark.parametrize("failure", [TimeoutError, ValueError])
def test_compile_failure_before_submission_does_not_need_ledger_evidence(packed, failure):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("submit-or-probe")
        raise OSError("must not reach a submission or ledger probe")

    runtime = IbmRuntime(
        environ=_environ(), compile_fn=_raise(failure()), context_fn=None,
        submit_fn=forbidden, request_submit_fn=forbidden, submitted_probe=forbidden)
    body = (runtime.run_request(parse_request(request_body(target="ibm_fez"))) if packed
            else runtime.run(parse_job(job_body(target="ibm_fez"))))
    assert body["status"] == ("REFUSED" if not packed and failure is ValueError else "NOT_RUN")
    assert calls == []


@pytest.mark.parametrize("packed", [False, True], ids=["v1", "v2"])
def test_service_retains_unknown_submission_and_recovers_once_after_restart(tmp_path, packed):
    body = request_body(target="ibm_fez") if packed else job_body(target="ibm_fez")
    parsed = parse_request(body) if packed else parse_job(body)
    digest = parsed.request_digest if packed else parsed.job_digest
    path = REQUEST_PATH if packed else SUBMIT_PATH
    raw = json.dumps(body, sort_keys=True).encode("utf-8")
    submissions = []
    plan = _plan()
    ready = {"value": False}
    recoveries = []
    clock = Clock()

    def submit(*args, **kwargs):
        submissions.append("accepted-by-provider")
        raise TimeoutError("response lost after provider acceptance")

    def recover_request(request, **kwargs):
        recoveries.append("request")
        return atlas_request_receipt(request, usage=2.0) if ready["value"] else None

    def recover_job(job, **kwargs):
        recoveries.append("job")
        result = _atlas_job_receipt(job, plan.compiled_plan_digest) if ready["value"] else None
        return result, None

    runtime = IbmRuntime(
        environ=_environ(), compile_fn=lambda job, **kwargs: plan, context_fn=None,
        submit_fn=submit, request_submit_fn=submit,
        submitted_probe=_raise(OSError("ledger temporarily unreadable")),
        request_recover_fn=recover_request, job_recover_fn=recover_job)
    config = ServiceConfig(bind="127.0.0.1:0", backend="ibm", data_dir=tmp_path,
                           receipt_key=RECEIPT_KEY, request_key=REQUEST_KEY, v2_enabled=True)
    service = QSearchService(config, runtime_factory=lambda kind: runtime, clock=clock)

    def post(svc, nonce):
        headers = {AUTH_HEADER: sign_request(REQUEST_KEY, "POST", path, raw, ts=1000, nonce=nonce)}
        return (svc.submit_request if packed else svc.submit)("POST", path, raw, headers, now=1000)

    def row(svc):
        return (svc.store.get_request if packed else svc.store.get)(digest)

    try:
        assert post(service, "d1" * 16) == (503, {"error": "job not completed"})
        assert (row(service).state, row(service).attempts, row(service).receipt_json) == ("ACCEPTED", 1, None)
        assert post(service, "d2" * 16)[0] == 503
        assert submissions == ["accepted-by-provider"]
    finally:
        service.close()

    restarted = QSearchService(config, runtime_factory=lambda kind: runtime, clock=clock)
    try:
        assert restarted.recover() == []
        assert row(restarted).state == "ACCEPTED"
        ready["value"] = True
        if packed:
            reads = len(recoveries)
            assert restarted.recover() == []                              # durable throttle survives restart
            assert len(recoveries) == reads and submissions == ["accepted-by-provider"]
            clock.now += config.poll_interval_s
        assert restarted.recover() == [digest]
        status, receipt = post(restarted, "d3" * 16)
        assert status == 200 and receipt["status"] == "OK"
        assert row(restarted).state == "DONE" and row(restarted).attempts == 1
        assert submissions == ["accepted-by-provider"]
    finally:
        restarted.close()
