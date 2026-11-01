"""Actual ATLAS ledger/identity/authority contract with an entirely fake provider."""
import json

import pytest

pytestmark = pytest.mark.engine
pytest.importorskip("qiskit")
pytest.importorskip("qiskit_ibm_runtime")
pytest.importorskip("aether_atlas.aqrc.campaign_runtime")

from nano_qsearch.request import parse_request  # noqa: E402
from nano_qsearch.runtime_ibm import IbmRuntime, LifecycleAdapterError  # noqa: E402
from test_qsearch_hw0_atlas import Provider, _packed_setup  # noqa: E402


def setup_runtime(tmp_path, monkeypatch):
    provider = Provider(usage=0, status="QUEUED")
    body, ledger, env = _packed_setup(tmp_path, monkeypatch, provider)
    runtime = IbmRuntime(environ=env, service_factory=provider.service_factory,
                         sampler_factory=provider.sampler_factory)
    request = parse_request(body)
    record = {}
    assert runtime.run_request(request, record=record) is None
    assert record["provider_status"] == "QUEUED"
    assert record["provider_job_id"] == provider.jobs[0].job_id()
    assert len(provider.runs) == 1
    return request, runtime, provider, ledger, env


def test_real_atlas_observe_and_cancel_reuse_reservation_without_settlement(tmp_path, monkeypatch):
    request, runtime, provider, ledger, _ = setup_runtime(tmp_path, monkeypatch)
    before = ledger.get(request.request_id)
    job = provider.jobs[0]
    calls = []

    def cancel():
        calls.append("cancel")
        job._status = "CANCELLED"

    job.cancel = cancel
    record = {"cancellation_requested_at": 100.0}
    runtime.observe_request(request, record=record)
    assert record["provider_status"] == "QUEUED"
    runtime.cancel_request(request, record=record)
    assert record["provider_status"] == "CANCELLED"
    assert record["cancellation_confirmed"] is True
    assert record["held_qpu_s"] == before["cap"]
    assert record["settled_qpu_s"] is None
    assert ledger.get(request.request_id) == before
    assert calls == ["cancel"]
    assert len(provider.runs) == 1


def test_real_atlas_wrong_provider_instance_cannot_cancel(tmp_path, monkeypatch):
    request, runtime, provider, ledger, _ = setup_runtime(tmp_path, monkeypatch)
    before = ledger.get(request.request_id)
    provider.jobs[0].instance = "other-instance"
    provider.jobs[0].cancel = lambda: pytest.fail("wrong provider instance must never be cancelled")
    with pytest.raises(LifecycleAdapterError, match="PROVIDER_IDENTITY_MISMATCH"):
        runtime.cancel_request(request, record={"cancellation_requested_at": 100.0})
    assert ledger.get(request.request_id) == before
    assert len(provider.runs) == 1


def test_real_atlas_tampered_authority_cannot_cancel(tmp_path, monkeypatch):
    request, runtime, provider, ledger, env = setup_runtime(tmp_path, monkeypatch)
    before = ledger.get(request.request_id)
    authority_path = tmp_path / "authority.json"
    token = json.loads(authority_path.read_text(encoding="utf-8"))
    token["body"]["subject_sha256"] = "sha256:" + "f" * 64
    authority_path.write_text(json.dumps(token), encoding="utf-8")
    provider.jobs[0].cancel = lambda: pytest.fail("invalid signed authority must never cancel")
    with pytest.raises(LifecycleAdapterError, match="CANCELLATION_NOT_AUTHORIZED"):
        runtime.cancel_request(request, record={"cancellation_requested_at": 100.0})
    assert ledger.get(request.request_id) == before
    assert len(provider.runs) == 1
