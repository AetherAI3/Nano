"""Isolated #59/#63 integration controls: frozen identity and nonblocking handover."""
from types import SimpleNamespace
import sys

import pytest

from nano_qsearch import prepared_runtime as adapter
from nano_qsearch import runtime_ibm as ibm
from nano_qsearch.receipt_v3 import seal_receipt_v3, verify_receipt_v3
from qsearch_v2_support import atlas_request_receipt
from test_quantum_prepared_runtime import host  # noqa: F401 - shared offline fixture


def install_delegate(host, monkeypatch, submit, cap=4):
    campaign = SimpleNamespace(qsearch_max_request_pubs=lambda ledger: cap, submit_qsearch_request=submit)
    monkeypatch.setitem(sys.modules, "aether_atlas", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "aether_atlas.aqrc", SimpleNamespace(campaign_runtime=campaign))
    monkeypatch.setattr(ibm, "_authority_from_env", lambda env: ({}, b"x" * 32, object()))
    monkeypatch.setattr(ibm, "_has_qsearch_stage", lambda ledger: True)
    monkeypatch.setattr(ibm, "check_device_binding", lambda *args, **kwargs: None)
    monkeypatch.setattr("nano_qsearch.compile.target_digest", lambda target: "test-target")
    monkeypatch.setattr(adapter, "snapshot_from_backend", lambda backend: host.current)
    context = SimpleNamespace(backend=SimpleNamespace(target=object()))
    host.runtime._context_fn = lambda *args, **kwargs: context
    host.runtime._request_submit = ibm.atlas_request_submit


def test_exact_frozen_prepared_identity_and_wait_false_reach_the_same_submission(host, monkeypatch):
    observed = []
    def submit(pairs, prepared, *, wait_for_result, **kwargs):
        assert wait_for_result is False
        assert len(pairs) == len(host.plans)
        assert all(circuit is plan.isa for (circuit, child), plan in zip(pairs, host.plans))
        assert prepared["envelope"]["children"] == host.children
        observed.append(prepared["request_sha256"])
        request = host.artifact.request
        atlas = atlas_request_receipt(request, [["1" * job.n] * job.shots for job in request.jobs], usage=2.0)
        atlas["request"] = prepared
        for pub, child in zip(atlas["pubs"], prepared["envelope"]["children"]):
            pub["child_sha256"] = child
        return atlas
    install_delegate(host, monkeypatch, submit)
    body = host.runtime.run_prepared_request(host.artifact,
        expected_artifact_digest=host.artifact.artifact_digest, record=host.record)
    assert len(observed) == 1 and host.calls.count("freeze") == 1
    assert host.record["compiled_jobs"] == 0
    verify_receipt_v3(seal_receipt_v3(body, b"x" * 32), b"x" * 32,
        expected_manifest=host.record["compile_manifest"], expected_request=host.artifact.request)


def test_nonblocking_held_handover_keeps_approved_manifest_and_unknown_usage(host, monkeypatch):
    calls = []
    def submit(pairs, prepared, *, wait_for_result, **kwargs):
        assert wait_for_result is False
        calls.append(prepared["request_sha256"])
        return {"state": "HELD", "job_id": "provider-pending"}
    install_delegate(host, monkeypatch, submit)
    host.runtime._submitted_probe = lambda *args, **kwargs: True
    body = host.runtime.run_prepared_request(host.artifact,
        expected_artifact_digest=host.artifact.artifact_digest, record=host.record)
    assert body is None and len(calls) == 1 and host.calls.count("freeze") == 1
    assert host.record["prepared_artifact_digest"] == host.artifact.artifact_digest
    assert host.record["compile_manifest"]["pubs"][0]["atlas_child_digest"] == "sha256:" + host.children[0]
    assert "quantum_seconds" not in host.record and "settled_qpu_s" not in host.record


def test_missing_nonblocking_capability_refuses_without_fallback_or_handover(host, monkeypatch):
    def unsupported(pairs, prepared):
        pytest.fail("unsupported ATLAS adapter must not receive the submission")
    install_delegate(host, monkeypatch, unsupported)
    with pytest.raises(ibm.LifecycleAdapterError, match="LIFECYCLE_ADAPTER_UNAVAILABLE"):
        host.runtime.run_prepared_request(host.artifact,
            expected_artifact_digest=host.artifact.artifact_digest, record=host.record)
