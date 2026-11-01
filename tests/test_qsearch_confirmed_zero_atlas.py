"""Nano/ATLAS/Predator zero settlement contract with an injected provider only."""
import json

import pytest

from test_qsearch_hw0_atlas import (Provider, RECEIPT_KEY, _packed_setup, _post, _service,
                                     campaign_runtime)
from nano_qsearch.receipt_v2 import verify_seal_v2
from nano_qsearch.request import parse_request

pytestmark = pytest.mark.engine
if not hasattr(campaign_runtime, "QSEARCH_ZERO_USAGE_EVIDENCE_SCHEMA"):
    # Presence of the legacy #185 release function is insufficient: it predates
    # the explicitly versioned billing contract.
    if not hasattr(campaign_runtime, "QSEARCH_BILLING_API_VERSION"):
        pytest.skip("ATLAS predates the reviewed confirmed-zero contract", allow_module_level=True)


def _billing(monkeypatch, *, status="complete", charge=0):
    monkeypatch.setattr(campaign_runtime, "_read_qsearch_billing_metrics", lambda job, context:
                        {"usage": {"status": status, "qpu_charge_time_seconds": charge}})


@pytest.mark.parametrize("terminal", ["CANCELLED", "ERROR"])
def test_real_ledger_zero_transition_seals_failed_and_replays_offline(tmp_path, monkeypatch, terminal):
    provider = Provider(usage=0, status=terminal)
    body, ledger, environ = _packed_setup(tmp_path, monkeypatch, provider)
    _billing(monkeypatch)
    svc = _service(tmp_path, environ, provider)
    try:
        code, sealed = _post(svc, body, "e1" * 16)
        if code == 503:
            code, sealed = _post(svc, body, "e2" * 16)
        assert code == 200, sealed
        assert sealed["status"] == "FAILED" and sealed["reason"] == "provider: " + terminal
        assert sealed["usage"]["quantum_seconds"] == 0 and sealed["usage"]["usage_pending"] is False
        verify_seal_v2(sealed, RECEIPT_KEY)
        row = ledger.get(parse_request(body).request_id)
        assert row["state"] == "cancelled_no_usage" and row["usage"] == 0 and row["job_id"]
        assert ledger.cancelled_attempt(row["request_id"])["prior_receipt"]
        assert len(provider.runs) == 1
    finally:
        svc.close()
    def forbidden(*args, **kwargs):
        raise AssertionError("settled replay must not read provider")
    provider.service_factory = forbidden
    svc = _service(tmp_path, {"AETHER_AQRC_LEDGER_PATH": environ["AETHER_AQRC_LEDGER_PATH"]}, provider)
    try:
        assert _post(svc, body, "e3" * 16) == (200, sealed)
    finally:
        svc.close()
    # Fresh Nano storage reconstructs from the real ATLAS ledger with no flags.
    other = tmp_path / "recovered"
    other.mkdir()
    svc = _service(other, {"AETHER_AQRC_LEDGER_PATH": environ["AETHER_AQRC_LEDGER_PATH"]}, provider)
    try:
        req = parse_request(body)
        svc.store.begin_request(req.request_digest, req.request_id, json.dumps(body))
        svc.store.mark_request_attempt(req.request_digest)
        assert svc.recover() == [req.request_digest]
        assert _post(svc, body, "e4" * 16) == (200, sealed)
    finally:
        svc.close()
    receipt_v2 = pytest.importorskip("predator.drive.qsearch_receipt_v2")
    expected = receipt_v2.ExpectedRequestV2.from_descriptor(body["descriptor"])
    verified = receipt_v2.verify_qsearch_receipt(sealed, key=RECEIPT_KEY, expected=expected)
    assert verified.status == "FAILED" and verified.usage["usage_pending"] is False


@pytest.mark.parametrize("billing_status,charge", [("pending", 0), ("typo", 0), (None, 0), ("complete", 0.8)])
def test_incomplete_or_positive_billing_never_closes_at_zero(tmp_path, monkeypatch, billing_status, charge):
    provider = Provider(usage=0, status="CANCELLED")
    body, ledger, environ = _packed_setup(tmp_path, monkeypatch, provider)
    _billing(monkeypatch, status=billing_status, charge=charge)
    for attempt in range(2):
        svc = _service(tmp_path, environ, provider)
        try:
            assert _post(svc, body, f"{attempt + 1:02x}" * 16)[0] == 503
            assert ledger.get(parse_request(body).request_id)["usage"] is None
            assert len(provider.runs) == 1
        finally:
            svc.close()
