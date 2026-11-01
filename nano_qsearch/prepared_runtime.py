"""Explicit host adapter for approved artifacts; never selected by a service route.

The host still owns durable admission, authority and retry/recovery. In particular,
``None`` after hand-over is a held result, never permission to submit again.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from .compile_manifest import build_compile_manifest
from .prepare import (PreparationError, PreparedArtifact, load_prepared_plans,
                      parse_prepared_artifact, parse_target_snapshot, snapshot_from_backend)
from .receipt_v3 import request_receipt_v3


def _atlas_fingerprint(envelope: Mapping[str, Any]) -> str:
    # ATLAS uses ASCII escaping and bare hex, separately from Nano's UTF-8,
    # sha256:-prefixed canonical digest contract.
    return hashlib.sha256(json.dumps(envelope, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")).hexdigest()


def _validate_identity(approved: PreparedArtifact, plans: list[Any], pairs: Any, prepared: Any) -> list[str]:
    """Compare the exact ATLAS ABI with independently approved compiler evidence."""
    from .runtime_ibm import _child_request_id
    request = approved.request
    if not isinstance(pairs, list) or len(pairs) != request.block_count or len(plans) != request.block_count:
        raise PreparationError("invalid_plan", "ATLAS did not provide every approved child")
    children = []
    for (circuit, child), job, plan, evidence in zip(pairs, request.jobs, plans, approved.compiled_plan_evidence):
        if circuit is not plan.isa or not isinstance(child, Mapping) or set(child) != {"envelope", "child_sha256"}:
            raise PreparationError("invalid_plan", "ATLAS child does not name the approved ISA")
        child_id = _child_request_id(job)
        expected = {"schema": "AqrcQSearchChildV1", "request_id": child_id, "purpose": request.purpose,
            "target": request.target, "shots": job.shots, "job_digest": job.job_digest,
            "compiled_plan_digest": evidence["compiled_plan_digest"], "qpy_sha256": evidence["isa_digest"][7:],
            "register": "meas", "register_size": job.n, "bit_order": "msb_first",
            "max_execution_time": request.max_execution_time, "recovery_tag": "atlas-qsearch-" + child_id}
        if (json.dumps(child["envelope"], sort_keys=True, separators=(",", ":"), allow_nan=False)
                != json.dumps(expected, sort_keys=True, separators=(",", ":"), allow_nan=False)
                or child["child_sha256"] != _atlas_fingerprint(expected)
                or plan.compiled_plan_digest != evidence["compiled_plan_digest"]):
            raise PreparationError("invalid_plan", "ATLAS child differs from the approved job, plan or ISA")
        children.append(child["child_sha256"])
    envelope = {"schema": "AqrcQSearchRequestV1", "request_id": request.request_id, "purpose": request.purpose,
        "target": request.target, "children": children, "shots": [job.shots for job in request.jobs],
        "pub_count": request.block_count, "max_execution_time": request.max_execution_time,
        "recovery_tag": "atlas-qsearch-req-" + request.request_id, "client_request_digest": request.request_digest}
    if (not isinstance(prepared, Mapping) or set(prepared) != {"envelope", "request_sha256"}
            or json.dumps(prepared["envelope"], sort_keys=True, separators=(",", ":"), allow_nan=False)
                != json.dumps(envelope, sort_keys=True, separators=(",", ":"), allow_nan=False)
            or prepared["request_sha256"] != _atlas_fingerprint(envelope)):
        raise PreparationError("invalid_plan", "ATLAS request differs from the approved ordered children")
    return children


def run_prepared_request(runtime: Any, artifact: PreparedArtifact | Mapping[str, Any], *,
                         expected_artifact_digest: str, current_snapshot: Any = None,
                         max_pubs: int | None = None, record: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Revalidate and submit saved ISA circuits once, without recompiling them.

    The independently retained digest is required. Live targets are compared with
    the backend resolved through the existing host gates, including calibration.
    A host-supplied snapshot cannot override that backend. No environment variable
    chooses a compile profile. The caller seals the returned v3 body explicitly.
    """
    from .runtime_ibm import LIVE_TARGETS, atlas_request_submit, prepare_request_identity

    raw = artifact.to_payload() if isinstance(artifact, PreparedArtifact) else artifact
    approved = parse_prepared_artifact(raw, expected_digest=expected_artifact_digest)
    request = approved.request
    if max_pubs is not None and (type(max_pubs) is not int or not 1 <= max_pubs <= 4096
                                 or request.block_count > max_pubs):
        raise PreparationError("invalid_plan", "request exceeds the declared PUB limit")
    reason = runtime._request_gate(request)
    if reason is not None:
        raise PreparationError("admission_refused", reason)
    context = runtime._context(request.target, pub_count=request.block_count,
                               default_delegate=runtime._request_submit is atlas_request_submit)
    if request.target in LIVE_TARGETS:
        if context is None or getattr(context, "backend", None) is None:
            raise PreparationError("missing_target", "the permitted live target was not resolved")
        current = snapshot_from_backend(context.backend)
        if current_snapshot is not None:
            supplied = (current_snapshot if hasattr(current_snapshot, "digest")
                        else parse_target_snapshot(current_snapshot))
            if supplied.digest != current.digest:
                raise PreparationError("target_drift", "caller snapshot differs from the permitted target")
    else:
        if current_snapshot is None:
            raise PreparationError("missing_target", "the host must supply its permitted target snapshot")
        current = current_snapshot
    plans = load_prepared_plans(approved, current_snapshot=current, expected_digest=expected_artifact_digest)
    pairs, prepared = prepare_request_identity(request, plans, purpose=request.purpose, max_pubs=max_pubs)
    try:
        children = _validate_identity(approved, plans, pairs, prepared)
    except PreparationError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise PreparationError("invalid_plan", "ATLAS returned a malformed frozen identity") from None
    manifest = build_compile_manifest(request, atlas_children=children,
        compiled_plans=approved.compiled_plan_evidence, target_snapshot_digest=approved.snapshot_digest,
        calibration_digest=approved.calibration_digest, profile=approved.profile_reference,
        compiler=approved.compiler)
    record = {} if record is None else record
    record.update(prepared_artifact_digest=approved.artifact_digest, compile_manifest=manifest,
                  target_resolutions=1 if context is not None else 0, compiled_jobs=0)
    try:
        atlas_receipt = runtime._request_submit(request, plans, purpose=request.purpose, environ=runtime._environ,
            context=context, sampler_factory=runtime._sampler_factory,
            backend_factory=runtime._backend_factory, max_pubs=max_pubs, prepared_identity=(pairs, prepared))
    except Exception as exc:
        if runtime._handed_over(request.target, request.request_id, exc):
            return runtime._held_unknown(exc, record)
        raise
    # Once a provider job may exist, malformed evidence remains held. Recreating
    # a manifest from the returned children would silently approve another job.
    try:
        body = request_receipt_v3(request, atlas_receipt, expected_manifest=manifest, purpose=request.purpose)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        record.update(result="unverified", reason="prepared result differs from the approved manifest")
        if isinstance(atlas_receipt, Mapping) and isinstance(atlas_receipt.get("job_id"), str):
            record["provider_job_id"] = atlas_receipt["job_id"]
        return None
    if body["usage"]["usage_pending"]:
        record["pending_receipt_body"] = body
    return runtime._final(request, body, record, atlas_receipt)


__all__ = ["run_prepared_request"]
