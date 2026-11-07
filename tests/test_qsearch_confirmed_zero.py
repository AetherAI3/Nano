"""Confirmed-zero projection contract; all delegates are offline stand-ins."""
import copy
import hashlib
import json

import pytest

from nano_qsearch.receipt_v2 import request_receipt, seal_receipt_v2, verify_seal_v2
from nano_qsearch.request import parse_request
from nano_qsearch.runtime_ibm import IbmRuntime, with_settled_usage
from nano_qsearch.service import QSearchService, ServiceConfig
from test_qsearch_accounting import (Provider, RECEIPT_KEY, REQUEST_KEY, _failed, _live,
                                     _post, _record, _service)


def confirmed_zero(request, status="CANCELLED"):
    receipt = _failed(0)(request)
    envelope = receipt["request"]["envelope"]
    envelope["client_request_digest"] = request.request_digest
    fingerprint = hashlib.sha256(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    receipt["request"]["request_sha256"] = fingerprint
    tags = [envelope["recovery_tag"], "atlas-ledger-" + "a" * 16]
    proof = {"schema": "AqrcQSearchZeroUsageEvidenceV1", "ledger_id": "a" * 32,
             "request_id": request.request_id, "request_sha256": fingerprint,
             "client_request_digest": request.request_digest, "child_sha": fingerprint, "plan_sha": fingerprint,
             "job_id": receipt["job_id"], "instance": "test-instance", "backend_name": request.target,
             "job_tags": tags, "provider_status": status, "usage_status": "complete",
             "evidence_sha256": "sha256:" + "b" * 64, "checked_at": "2026-11-01T12:00:00+00:00",
             "provider_api_version": "2026-04-15", "evidence_source": "ibm-runtime-job-metrics"}
    receipt.update(provider_status=status, job_tags=tags, usage_pending=False,
                   accounting_outcome="cancelled_no_usage", usage_final_status="complete", zero_usage_evidence=proof)
    return receipt


@pytest.mark.parametrize("status", ["CANCELLED", "ERROR"])
def test_zero_is_a_sealed_failed_outcome_and_replays_without_credentials(tmp_path, status):
    body = _live()
    provider = Provider(lambda r: confirmed_zero(r, status))
    svc = _service(tmp_path, provider)
    try:
        code, sealed = _post(svc, body)
        assert code == 200 and sealed["status"] == "FAILED" and sealed["blocks"] == []
        assert sealed["reason"] == "provider: " + status
        assert sealed["usage"]["quantum_seconds"] == 0 and sealed["usage"]["usage_pending"] is False
        assert sealed["usage"]["zero_usage_evidence"]["provider_status"] == status
        verify_seal_v2(sealed, RECEIPT_KEY)
        assert _record(svc, body)["accounting"] == "settled"
    finally:
        svc.close()
    def forbidden(*args, **kwargs):
        raise AssertionError("settled replay must not construct a runtime")
    svc = QSearchService(ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path,
                        receipt_key=RECEIPT_KEY, request_key=REQUEST_KEY, v2_enabled=True), runtime_factory=forbidden)
    try:
        assert svc.recover() == []
        assert _post(svc, body) == (200, sealed)
        assert provider.submits == 1 and provider.recovers == 0
    finally:
        svc.close()


@pytest.mark.parametrize("field,value", [
    ("schema", "future"), ("job_id", "wrong-job"), ("backend_name", "ibm_wrong"),
    ("instance", ""), ("request_id", "other"), ("request_sha256", "0" * 64),
    ("client_request_digest", "sha256:" + "0" * 64), ("child_sha", "0" * 64), ("plan_sha", "0" * 64),
    ("ledger_id", "0" * 32), ("usage_status", "pending"), ("usage_status", "typo"),
    ("provider_status", "DONE"), ("provider_api_version", "2025-05-01"),
    ("evidence_source", "aggregate-account-counter"), ("evidence_sha256", "missing"), ("checked_at", ""),
    ("job_tags", []),
    ("unreviewed_extra_field", True),
])
def test_wrong_or_unversioned_zero_evidence_stays_held_across_restart(tmp_path, field, value):
    body = _live()
    def invalid(request):
        receipt = confirmed_zero(request)
        receipt["zero_usage_evidence"][field] = value
        return receipt
    provider = Provider(invalid, recoveries=[invalid])
    svc = _service(tmp_path, provider)
    try:
        assert _post(svc, body)[0] == 503
    finally:
        svc.close()
    svc = _service(tmp_path, provider)
    try:
        assert svc.recover() == [] and _post(svc, body)[0] == 503
        row = svc.store.get_request(parse_request(body).request_digest)
        assert row.state == "ACCEPTED" and row.receipt_json is None and provider.submits == 1
    finally:
        svc.close()


def test_a_zero_proof_cannot_make_a_successful_computation_free():
    request = parse_request(_live())
    receipt = confirmed_zero(request)
    receipt["status"] = "DONE"
    # Keep a valid result from the existing delegate but attach the zero proof.
    from qsearch_v2_support import atlas_request_receipt
    success = {**receipt, "pubs": atlas_request_receipt(request)["pubs"]}
    body = request_receipt(request, success, purpose=request.purpose)
    assert body["status"] == "OK" and body["usage"]["usage_pending"] is True


def test_old_ledger_fallback_does_not_promote_zero():
    request = parse_request(_live())
    receipt = _failed(0)(request)
    assert with_settled_usage(receipt, {"job_id": receipt["job_id"], "usage": 0}) == receipt


def test_historical_cancelled_receipt_reseals_offline_and_keeps_previous_seal(tmp_path):
    body = _live()
    request = parse_request(body)
    final = confirmed_zero(request)
    old = copy.deepcopy(final)
    old.pop("zero_usage_evidence")
    old["usage_pending"] = True
    old_body = request_receipt(request, old, purpose=request.purpose)
    old_seal = seal_receipt_v2(old_body, RECEIPT_KEY)
    svc = _service(tmp_path, Provider(lambda r: final))
    svc.store.begin_request(request.request_digest, request.request_id, json.dumps(body))
    svc.store.mark_request_attempt(request.request_digest)
    svc.store.complete_request(request.request_digest, json.dumps(old_seal))
    svc.close()
    runtime = IbmRuntime(environ={}, request_settled_fn=lambda *a, **k: final)
    svc = QSearchService(ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path,
                        receipt_key=RECEIPT_KEY, request_key=REQUEST_KEY, v2_enabled=True),
                        runtime_factory=lambda kind: runtime)
    try:
        code, sealed = _post(svc, body)
        assert code == 200 and sealed["reason"] == "provider: CANCELLED"
        assert sealed["usage"]["usage_pending"] is False and sealed["usage"]["quantum_seconds"] == 0
        history = _record(svc, body)["reseals"]
        assert len(history) == 1 and history[0]["previous_seal"] == old_seal["seal"]
        assert _post(svc, body) == (200, sealed)
    finally:
        svc.close()
