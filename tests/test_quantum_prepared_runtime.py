"""Host-gate, approval and no-recompile checks using offline ATLAS stand-ins."""
from __future__ import annotations

import copy
import hashlib
import io
import sys
from types import SimpleNamespace

import pytest

from nano_qsearch import prepared_runtime as adapter
from nano_qsearch.canonical import canonical_digest
from nano_qsearch.prepare import PreparationError, load_prepared_plans, parse_prepared_artifact, parse_target_snapshot, prepare_request
from nano_qsearch.receipt_v3 import seal_receipt_v3, verify_receipt_v3
from nano_qsearch.runtime_ibm import IbmRuntime, _child_request_id, atlas_request_submit
from qsearch_v2_support import atlas_request_receipt
from test_quantum_prepare import fake_artifact, body, profile, snapshot


@pytest.fixture
def host(tmp_path, monkeypatch):
    artifact = parse_prepared_artifact(fake_artifact())
    plans = [SimpleNamespace(isa=object(), compiled_plan_digest=pub["compiled_plan_digest"])
             for pub in artifact.to_payload()["pubs"]]
    children = []
    frozen = []
    calls = []
    current = artifact.target_snapshot
    backend = object()
    context = SimpleNamespace(backend=backend)
    authority = tmp_path / "host-authority.json"
    authority.write_text("{}", encoding="utf-8")
    environ = {"AETHER_AQRC_HW_GO": "1", "AETHER_AQRC_QSEARCH_LIVE": "1",
               "AETHER_AQRC_QSEARCH_PACKED": "1", "AETHER_AQRC_AUTHORITY_PATH": str(authority)}

    def unexpected(*args, **kwargs):
        pytest.fail("approved execution must not recompile or construct an IBM service")

    def resolve(target, **kwargs):
        calls.append("resolve")
        return context

    def snapshot(value):
        assert value is backend
        calls.append("snapshot")
        return current

    def load(approved, *, current_snapshot, expected_digest):
        assert approved.artifact_digest == expected_digest == artifact.artifact_digest
        assert current_snapshot.digest == current.digest
        calls.append("load")
        return plans

    def identity(request, compiled, **kwargs):
        assert compiled is plans
        calls.append("freeze")
        pairs, prepared = make_identity(request, plans, artifact.compiled_plan_evidence)
        children[:] = prepared["envelope"]["children"]
        frozen[:] = [pairs, prepared]
        return pairs, prepared

    def submit(request, compiled, **kwargs):
        assert compiled is plans and kwargs["context"] is context
        assert kwargs["prepared_identity"][0] is frozen[0]
        assert kwargs["prepared_identity"][1] is frozen[1]
        calls.append("submit")
        atlas = atlas_request_receipt(request, [["1" * job.n] * job.shots for job in request.jobs], usage=2.0)
        atlas["request"]["envelope"]["children"] = list(children)
        for pub, child in zip(atlas["pubs"], children):
            pub["child_sha256"] = child
        return atlas

    runtime = IbmRuntime(compile_fn=unexpected, request_submit_fn=submit, context_fn=resolve,
                         service_factory=unexpected, submitted_probe=lambda *a, **k: False, environ=environ)
    monkeypatch.setattr(adapter, "snapshot_from_backend", snapshot)
    monkeypatch.setattr(adapter, "load_prepared_plans", load)
    monkeypatch.setattr("nano_qsearch.runtime_ibm.prepare_request_identity", identity)
    monkeypatch.setattr("nano_qsearch.compile.compile_jobs", unexpected)
    return SimpleNamespace(artifact=artifact, plans=plans, children=children, calls=calls, runtime=runtime,
                           current=current, record={}, submit=submit, identity=identity)


def make_identity(request, plans, evidence):
    """ATLAS PR182/ad06bb20 ABI: ASCII canonical fingerprints and bare hashes."""
    pairs = []
    for plan, job, pub in zip(plans, request.jobs, evidence):
        child_id = _child_request_id(job)
        envelope = {"schema": "AqrcQSearchChildV1", "request_id": child_id, "purpose": request.purpose,
            "target": request.target, "shots": job.shots, "job_digest": job.job_digest,
            "compiled_plan_digest": plan.compiled_plan_digest, "qpy_sha256": pub["isa_digest"][7:],
            "register": "meas", "register_size": job.n, "bit_order": "msb_first",
            "max_execution_time": request.max_execution_time, "recovery_tag": "atlas-qsearch-" + child_id}
        pairs.append((plan.isa, {"envelope": envelope, "child_sha256": adapter._atlas_fingerprint(envelope)}))
    envelope = {"schema": "AqrcQSearchRequestV1", "request_id": request.request_id, "purpose": request.purpose,
        "target": request.target, "children": [child["child_sha256"] for _, child in pairs],
        "shots": [job.shots for job in request.jobs], "pub_count": request.block_count,
        "max_execution_time": request.max_execution_time, "recovery_tag": "atlas-qsearch-req-" + request.request_id,
        "client_request_digest": request.request_digest}
    return pairs, {"envelope": envelope, "request_sha256": adapter._atlas_fingerprint(envelope)}


def test_approved_artifact_uses_saved_isa_host_gates_and_v3_manifest(host):
    result = host.runtime.run_prepared_request(host.artifact,
        expected_artifact_digest=host.artifact.artifact_digest, record=host.record)
    assert host.calls == ["resolve", "snapshot", "load", "freeze", "submit"]
    assert host.record["compiled_jobs"] == 0
    assert host.record["prepared_artifact_digest"] == host.artifact.artifact_digest
    manifest = host.record["compile_manifest"]
    assert [p["atlas_child_digest"] for p in manifest["pubs"]] == ["sha256:" + child for child in host.children]
    assert [p["n"] for p in manifest["pubs"]] == [4, 3]
    assert [p["compiled_plan_digest"] for p in manifest["pubs"]] == [p.compiled_plan_digest for p in host.plans]
    sealed = seal_receipt_v3(result, b"x" * 32)
    verify_receipt_v3(sealed, b"x" * 32, expected_manifest=manifest, expected_request=host.artifact.request)


def test_expected_artifact_digest_is_checked_before_context_or_submission(host):
    with pytest.raises(PreparationError, match="expected digest"):
        host.runtime.run_prepared_request(host.artifact, expected_artifact_digest="sha256:" + "0" * 64)
    assert host.calls == []


@pytest.mark.parametrize("flag", ["AETHER_AQRC_HW_GO", "AETHER_AQRC_QSEARCH_LIVE", "AETHER_AQRC_QSEARCH_PACKED"])
def test_existing_host_admission_flags_precede_context_and_saved_isa_load(host, flag):
    host.runtime._environ.pop(flag)
    with pytest.raises(PreparationError) as exc:
        host.runtime.run_prepared_request(host.artifact, expected_artifact_digest=host.artifact.artifact_digest)
    assert exc.value.code == "admission_refused" and host.calls == []


def test_calibration_drift_refuses_before_loading_or_submitting_old_approval(host, monkeypatch):
    changed = host.current.to_payload()
    changed["instructions"]["x"][0]["error"] = 0.03
    monkeypatch.setattr(adapter, "snapshot_from_backend", lambda backend: parse_target_snapshot(changed))
    monkeypatch.setattr(adapter, "load_prepared_plans", load_prepared_plans)
    with pytest.raises(PreparationError) as exc:
        host.runtime.run_prepared_request(host.artifact, expected_artifact_digest=host.artifact.artifact_digest)
    assert exc.value.code == "target_drift" and host.calls == ["resolve"]


def test_caller_snapshot_cannot_override_the_permitted_live_target(host):
    changed = host.current.to_payload()
    changed["qubit_properties"][0]["t1"] *= 2
    with pytest.raises(PreparationError) as exc:
        host.runtime.run_prepared_request(host.artifact, expected_artifact_digest=host.artifact.artifact_digest,
                                         current_snapshot=changed)
    assert exc.value.code == "target_drift" and "submit" not in host.calls


def test_missing_live_context_does_not_use_artifacts_embedded_target_as_authority(host):
    host.runtime._context_fn = None
    with pytest.raises(PreparationError) as exc:
        host.runtime.run_prepared_request(host.artifact, expected_artifact_digest=host.artifact.artifact_digest)
    assert exc.value.code == "missing_target" and host.calls == []


@pytest.mark.parametrize("mutation", ["child", "partial", "order"])
def test_post_submission_mismatched_evidence_stays_held_under_original_manifest(host, mutation):
    def substitute(request, compiled, **kwargs):
        atlas = host.submit(request, compiled, **kwargs)
        if mutation == "child":
            other = canonical_digest({"different": "child"})
            atlas["request"]["envelope"]["children"][0] = other
            atlas["pubs"][0]["child_sha256"] = other
        elif mutation == "partial":
            atlas["pubs"].pop()
        else:
            atlas["pubs"].reverse()
        return atlas
    host.runtime._request_submit = substitute
    result = host.runtime.run_prepared_request(host.artifact,
        expected_artifact_digest=host.artifact.artifact_digest, record=host.record)
    assert result is None and host.record["result"] == "unverified"
    assert host.record["provider_job_id"] == "fake-job-1"
    assert host.record["compile_manifest"]["pubs"][0]["atlas_child_digest"] == "sha256:" + host.children[0]
    assert host.calls.count("submit") == 1


def test_pending_accounting_retains_original_v3_body_for_later_usage_only_reseal(host):
    def pending(request, compiled, **kwargs):
        atlas = host.submit(request, compiled, **kwargs)
        atlas.update(provider_final_usage_seconds=None, usage_pending=True)
        return atlas
    host.runtime._request_submit = pending
    result = host.runtime.run_prepared_request(host.artifact,
        expected_artifact_digest=host.artifact.artifact_digest, record=host.record)
    assert result is None and host.record["accounting"] == "pending"
    assert host.record["pending_receipt_body"]["compile_manifest"] == host.record["compile_manifest"]


def test_declared_pub_cap_is_enforced_before_host_resolution(host):
    with pytest.raises(PreparationError):
        host.runtime.run_prepared_request(host.artifact, expected_artifact_digest=host.artifact.artifact_digest, max_pubs=1)
    assert host.calls == []


@pytest.mark.parametrize("field", ["hash", "qpy_sha256", "compiled_plan_digest", "job_digest", "target",
                                   "shots", "register_size", "register", "purpose", "request_hash", "children",
                                   "client_request_digest", "max_execution_time"])
def test_malformed_frozen_atlas_identity_never_reaches_submission(host, monkeypatch, field):
    def malformed(request, plans, **kwargs):
        pairs, prepared = host.identity(request, plans, **kwargs)
        if field == "hash":
            pairs[0][1].pop("child_sha256")
        elif field == "request_hash":
            prepared["request_sha256"] = "0" * 64
        elif field in ("children", "client_request_digest", "max_execution_time"):
            if field == "children":
                prepared["envelope"][field].reverse()
            else:
                prepared["envelope"][field] = 1 if field == "max_execution_time" else "sha256:" + "0" * 64
            prepared["request_sha256"] = adapter._atlas_fingerprint(prepared["envelope"])
        else:
            pairs[0][1]["envelope"][field] = True if field in ("shots", "register_size") else "different"
            pairs[0][1]["child_sha256"] = adapter._atlas_fingerprint(pairs[0][1]["envelope"])
        return pairs, prepared
    monkeypatch.setattr("nano_qsearch.runtime_ibm.prepare_request_identity", malformed)
    with pytest.raises(PreparationError) as exc:
        host.runtime.run_prepared_request(host.artifact, expected_artifact_digest=host.artifact.artifact_digest)
    assert exc.value.code == "invalid_plan" and "submit" not in host.calls


@pytest.mark.parametrize("ledger_cap", [1, 4])
def test_default_delegate_uses_the_exact_frozen_identity_and_current_ledger_cap(host, monkeypatch, ledger_cap):
    pairs, prepared = make_identity(host.artifact.request, host.plans, host.artifact.compiled_plan_evidence)
    atlas = atlas_request_receipt(host.artifact.request,
        [["1" * job.n] * job.shots for job in host.artifact.request.jobs], usage=2.0)
    for pub, child in zip(atlas["pubs"], prepared["envelope"]["children"]):
        pub["child_sha256"] = child
    calls = []
    def submit(got_pairs, got_request, **kwargs):
        assert got_pairs is pairs and got_request is prepared
        calls.append("submit")
        return atlas
    campaign = SimpleNamespace(qsearch_max_request_pubs=lambda ledger: ledger_cap, submit_qsearch_request=submit)
    monkeypatch.setitem(sys.modules, "aether_atlas", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "aether_atlas.aqrc", SimpleNamespace(campaign_runtime=campaign))
    monkeypatch.setattr("nano_qsearch.runtime_ibm._authority_from_env", lambda env: ({}, b"x" * 32, object()))
    monkeypatch.setattr("nano_qsearch.runtime_ibm._has_qsearch_stage", lambda ledger: True)
    monkeypatch.setattr("nano_qsearch.runtime_ibm.prepare_request_identity", lambda *a, **k: pytest.fail("identity refrozen"))
    monkeypatch.setattr("nano_qsearch.runtime_ibm.check_device_binding", lambda *a, **k: None)
    monkeypatch.setattr("nano_qsearch.compile.target_digest", lambda target: "test-digest")
    context = SimpleNamespace(backend=SimpleNamespace(target=object()))
    def run():
        return atlas_request_submit(host.artifact.request, host.plans, purpose=host.artifact.request.purpose,
            context=context, environ={}, prepared_identity=(pairs, prepared))
    if ledger_cap == 1:
        with pytest.raises(ValueError, match="PUB cap"):
            run()
        assert calls == []
    else:
        assert run() is atlas and calls == ["submit"]


def test_actual_pinned_compile_load_raw_qpy_freeze_and_expected_v3_result(tmp_path, monkeypatch):
    qiskit = pytest.importorskip("qiskit")
    if qiskit.__version__ != "2.3.1":
        pytest.skip("actual preparation requires the repository's pinned Qiskit 2.3.1")
    artifact = prepare_request(body(), profile(), snapshot())
    backend = artifact.target_snapshot.to_backend()
    context = SimpleNamespace(backend=backend)
    authority = tmp_path / "authority.json"
    authority.write_text("{}", encoding="utf-8")
    environ = {"AETHER_AQRC_HW_GO": "1", "AETHER_AQRC_QSEARCH_LIVE": "1",
               "AETHER_AQRC_QSEARCH_PACKED": "1", "AETHER_AQRC_AUTHORITY_PATH": str(authority)}
    def freeze(request, plans, **kwargs):
        for plan, pub in zip(plans, artifact.compiled_plan_evidence):
            raw = io.BytesIO()
            qiskit.qpy.dump(plan.isa, raw)
            assert hashlib.sha256(raw.getvalue()).hexdigest() == pub["isa_digest"][7:]
        return make_identity(request, plans, artifact.compiled_plan_evidence)
    def submit(request, plans, **kwargs):
        pairs, prepared = kwargs["prepared_identity"]
        assert all(circuit is plan.isa for (circuit, child), plan in zip(pairs, plans))
        atlas = atlas_request_receipt(request, [["1" * job.n] * job.shots for job in request.jobs], usage=2.0)
        atlas["request"] = prepared
        for pub, child in zip(atlas["pubs"], prepared["envelope"]["children"]):
            pub["child_sha256"] = child
        return atlas
    monkeypatch.setattr("nano_qsearch.runtime_ibm.prepare_request_identity", freeze)
    runtime = IbmRuntime(compile_fn=lambda *a, **k: pytest.fail("recompile"), environ=environ,
        context_fn=lambda *a, **k: context, request_submit_fn=submit)
    record = {}
    result = runtime.run_prepared_request(artifact, expected_artifact_digest=artifact.artifact_digest, record=record)
    verify_receipt_v3(seal_receipt_v3(result, b"x" * 32), b"x" * 32,
                      expected_manifest=record["compile_manifest"], expected_request=artifact.request)
