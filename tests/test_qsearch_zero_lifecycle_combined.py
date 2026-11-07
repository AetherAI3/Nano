"""#58/#59 bridge acceptance: authenticated zero, real ledger, restart and replacement.

All provider and replacement execution is injected; no live credentials or QPU.
"""
import copy
import json
import secrets

import pytest

from nano_qsearch.auth import AUTH_HEADER, sign_request
from nano_qsearch.receipt_v2 import not_run_v2, request_receipt, seal_receipt_v2, verify_seal_v2
from nano_qsearch.request import parse_request
from nano_qsearch.service import QSearchService, ServiceConfig
from qsearch_v2_support import request_body
from test_qsearch_confirmed_zero import confirmed_zero
from test_qsearch_accounting import RECEIPT_KEY, REQUEST_KEY

PREFIX = "/qsearch/v2/requests/"


def _cfg(path):
    return ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=path, receipt_key=RECEIPT_KEY,
                         request_key=REQUEST_KEY, v2_enabled=True)


def _forbidden(*args, **kwargs):
    raise AssertionError("settled lifecycle replay must not construct a provider runtime")


def _headers(method, path, body=b""):
    return {AUTH_HEADER: sign_request(REQUEST_KEY, method, path, body, ts=1000, nonce=secrets.token_hex(16))}


def _get(svc, request):
    path = PREFIX + request.request_digest
    return svc.status_request("GET", path, b"", _headers("GET", path), now=1000)


def _control(svc, request, action, body=b""):
    path = PREFIX + request.request_digest + "/" + action
    return svc.control_request("POST", path, body, _headers("POST", path, body), now=1000)


def _seed(path, *, status="CANCELLED", mutate=None, record=None, runtime_factory=_forbidden):
    body = request_body(3, target="ibm_fez")
    request = parse_request(body)
    sealed = seal_receipt_v2(request_receipt(request, confirmed_zero(request, status), purpose=request.purpose),
                             RECEIPT_KEY)
    if mutate:
        sealed = mutate(copy.deepcopy(sealed))
    svc = QSearchService(_cfg(path), runtime_factory=runtime_factory)
    svc.store.begin_request(request.request_digest, request.request_id, json.dumps(body))
    svc.store.mark_request_attempt(request.request_digest)
    svc.store.complete_request(request.request_digest, json.dumps(sealed))
    svc.store.set_request_record(request.request_digest, json.dumps(record or {}))
    return svc, request, sealed


@pytest.mark.parametrize("status", ["CANCELLED", "ERROR"])
@pytest.mark.parametrize("record", [{}, {"provider_status": "QUEUED", "held_qpu_s": 10}])
def test_verified_zero_status_and_refresh_survive_restart_without_provider(tmp_path, status, record):
    svc, request, sealed = _seed(tmp_path, status=status, record=record)
    verify_seal_v2(sealed, RECEIPT_KEY)
    try:
        before = svc.store.get_request(request.request_digest)
        for _ in range(3):
            code, state = _get(svc, request)
            assert code == 200 and state["state"] == "SETTLED"
            assert state["accounting"]["status"] == "SETTLED"
            assert state["usage"] == {"reservation_status": "SETTLED", "held_quantum_seconds": 0.0,
                                      "settled_quantum_seconds": 0.0}
            assert state["execution"]["status"] == ("CANCELLED" if status == "CANCELLED" else "FAILED")
            assert state["execution"]["terminal"] is True and state["next_action"] == "NONE"
        assert _control(svc, request, "refresh")[1]["state"] == "SETTLED"
        assert svc.store.get_request(request.request_digest) == before
        assert not svc._runtimes
    finally:
        svc.close()
    svc = QSearchService(_cfg(tmp_path), runtime_factory=_forbidden)
    try:
        assert svc.recover() == []
        assert _get(svc, request)[1]["state"] == "SETTLED"
        assert svc.store.get_request(request.request_digest).receipt_json == before.receipt_json
        assert not svc._runtimes
    finally:
        svc.close()


def _resign_mutation(path, value):
    def change(receipt):
        target = receipt
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        return seal_receipt_v2(receipt, RECEIPT_KEY)
    return change


@pytest.mark.parametrize("mutate", [
    _resign_mutation(("usage", "zero_usage_evidence"), None),
    _resign_mutation(("usage", "usage_pending"), True),
    _resign_mutation(("usage", "zero_usage_evidence", "request_id"), "wrong"),
    _resign_mutation(("usage", "zero_usage_evidence", "job_id"), "wrong"),
    _resign_mutation(("usage", "zero_usage_evidence", "backend_name"), "ibm_other"),
    _resign_mutation(("usage", "zero_usage_evidence", "provider_api_version"), "2025-05-01"),
    lambda receipt: {**receipt, "seal": receipt["seal"][:-1] + ("0" if receipt["seal"][-1] != "0" else "1")},
])
def test_unknown_bad_mac_or_wrong_identity_zero_stays_held_and_cannot_replace(tmp_path, mutate):
    svc, request, _ = _seed(tmp_path, mutate=mutate, record={"provider_status": "CANCELLED",
        "provider_job_id": "fake-job-1", "accounting": "settled", "quantum_seconds": 0, "held_qpu_s": 10})
    replacement = request_body(3, target="ibm_kingston", request_id="replacement")
    try:
        for _ in range(2):
            code, state = _get(svc, request)
            assert code == 200 and state["accounting"]["status"] == "PENDING"
            assert state["usage"]["reservation_status"] == "HELD"
            assert state["usage"]["settled_quantum_seconds"] is None
        code, error = _control(svc, request, "replace", json.dumps(replacement).encode())
        assert code == 409 and error["code"] == "ORIGINAL_ATTEMPT_UNRECONCILED"
        assert svc.store.get_request(parse_request(replacement).request_digest) is None
        assert not svc._runtimes
    finally:
        svc.close()


def test_confirmed_zero_opens_only_linked_replacement_gate_and_retains_original(tmp_path):
    class Replacement:
        calls = 0
        def run_request(self, request, **kwargs):
            self.calls += 1
            assert request.target == "ibm_kingston" and request.request_id == "replacement"
            return not_run_v2(request, "offline replacement gate probe; no provider submission")
    replacement_runtime = Replacement()
    svc, request, original = _seed(tmp_path, runtime_factory=lambda kind: replacement_runtime)
    body = request_body(3, target="ibm_kingston", request_id="replacement")
    new = parse_request(body)
    try:
        before = svc.store.get_request(request.request_digest)
        for _ in range(2):
            code, state = _control(svc, request, "replace", json.dumps(body).encode())
            assert code == 200 and state["execution"]["status"] == "NOT_RUN"
        assert replacement_runtime.calls == 1
        assert svc.store.get_request(request.request_digest).receipt_json == before.receipt_json
        assert json.loads(before.receipt_json) == original
        assert svc.store.get_request(request.request_digest).attempts == 1
        assert svc.store.get_request(new.request_digest).attempts == 1
        assert json.loads(svc.store.get_request(new.request_digest).receipt_json)["status"] == "NOT_RUN"
        assert _get(svc, request)[1]["lineage"]["replacement_request_digest"] == new.request_digest
    finally:
        svc.close()


@pytest.mark.engine
def test_real_combined_atlas_no_wait_zero_then_complete_reconciles_once(tmp_path, monkeypatch):
    from nano_qsearch.runtime_ibm import IbmRuntime
    from test_qsearch_lifecycle_service import Clock
    from test_qsearch_hw0_atlas import Provider, _packed_setup, _post, campaign_runtime
    provider = Provider(usage=0, status="QUEUED")
    body, ledger, environ = _packed_setup(tmp_path, monkeypatch, provider)
    request = parse_request(body)
    billing = {"status": "pending", "qpu_charge_time_seconds": 0}
    monkeypatch.setattr(campaign_runtime, "_read_qsearch_billing_metrics", lambda job, context: {"usage": dict(billing)})
    clock = Clock()
    def runtime_factory(kind):
        return IbmRuntime(environ=environ, service_factory=provider.service_factory,
                          sampler_factory=provider.sampler_factory)
    svc = QSearchService(_cfg(tmp_path / "nano"), runtime_factory=runtime_factory, clock=clock)
    try:
        assert _post(svc, body, "e1" * 16)[0] == 503
        assert len(provider.runs) == 1 and ledger.get(request.request_id)["usage"] is None
        provider.jobs[0]._status = "CANCELLED"
        clock.now += svc.config.poll_interval_s + 1
        code, state = _control(svc, request, "refresh")
        assert code == 200 and state["execution"]["status"] == "CANCELLED"
        assert state["usage"]["reservation_status"] == "HELD"
        billing["status"] = "complete"
        clock.now += svc.config.poll_interval_s + 1
        code, state = _control(svc, request, "refresh")
        assert code == 200 and state["state"] == "SETTLED"
        assert state["execution"]["status"] == "CANCELLED"
        assert state["usage"]["settled_quantum_seconds"] == 0
        assert ledger.get(request.request_id)["state"] == "cancelled_no_usage"
        assert ledger.cancelled_attempt(request.request_id)["prior_receipt"]
        assert len(provider.runs) == 1
        stored = svc.store.get_request(request.request_digest).receipt_json
        verify_seal_v2(json.loads(stored), RECEIPT_KEY)
        assert _get(svc, request)[1]["state"] == "SETTLED"
    finally:
        svc.close()
    def no_services(*args, **kwargs):
        raise AssertionError("settled restart must not resolve provider credentials")
    provider.service_factory = no_services
    svc = QSearchService(_cfg(tmp_path / "nano"), runtime_factory=_forbidden, clock=clock)
    try:
        assert svc.recover() == []
        assert _get(svc, request)[1]["state"] == "SETTLED"
        assert _control(svc, request, "refresh")[1]["state"] == "SETTLED"
        assert svc.store.get_request(request.request_digest).receipt_json == stored
        assert len(provider.runs) == 1
    finally:
        svc.close()
