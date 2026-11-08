"""Caller-side verification of native results against independently retained identity.

The caller computes its expected identity from its own plan before submission.
A returned record is checked against that retained copy, never against the
service's stored manifest, then decoded with the caller's plan and evidence.
Execution, decoding and accounting remain separate; hardware qualification is
always ``none`` for a local record. Stdlib only.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from nano.quantum import QuantumPlan, QuantumValidationError
from nano.quantum.native import RESULT_SCHEMA, check_schema, exact, native_plan, pub_map, pub_map_digest

from .native_policy import SEAL_PREFIX, verify_native_seal
from .native_request import parse_native_request
from .native_runtime import LOCAL_RUNTIME_KINDS
from .quantum_adapter import AuthenticationVerifier, VerifiedResult, decode_verified

_RESULT_KEYS = {"schema", "request", "identity", "status", "reason", "backend", "effect", "source_verification",
                "observations", "decoding", "accounting", "hardware", "seal"}
_IDENTITY_KEYS = ("artifact_digest", "plan_digest", "pub_map_digest", "profile_digest", "target_digest")


def _fail(code: str, message: str) -> None:
    raise QuantumValidationError(code, message)


@dataclass(frozen=True)
class NativeExpectedIdentity:
    request_id: str
    request_digest: str
    artifact_digest: str
    plan_digest: str
    pub_map_digest: str
    profile_digest: str
    target_digest: str
    plan: QuantumPlan


@dataclass(frozen=True)
class NativeVerifiedResult:
    request_digest: str
    source_verification: str
    execution_status: str
    decoded: VerifiedResult | None
    delivery_verdict: str
    accounting_status: str
    hardware_qualification: str = "none"


def retain_identity(plan: QuantumPlan, request_body: Mapping[str, Any]) -> NativeExpectedIdentity:
    """Bind the caller's own plan to the request it is about to submit."""
    plan = native_plan(plan)
    request = parse_native_request(copy.deepcopy(dict(request_body)))
    descriptor = request.descriptor
    if descriptor["plan_digest"] != plan.plan_digest or descriptor["pub_map_digest"] != pub_map_digest(plan):
        _fail("IDENTITY", "the request does not carry the caller's own plan and PUB map")
    return NativeExpectedIdentity(request.request_id, request.request_digest, descriptor["artifact_digest"],
                                  plan.plan_digest, descriptor["pub_map_digest"], descriptor["profile_digest"],
                                  descriptor["target_digest"], plan)


def _check_claims(record: Mapping[str, Any]) -> None:
    seal, backend = record["seal"], record["backend"]
    if not isinstance(seal, str) or not seal.startswith(SEAL_PREFIX):
        _fail("SEAL", "a native result must carry only the local native seal")
    if (record["hardware"] is not None or not isinstance(backend, Mapping) or backend.get("dry_run") is not True
            or backend.get("kind") not in LOCAL_RUNTIME_KINDS):
        _fail("HARDWARE_CLAIM", "a native local result cannot claim hardware execution")
    if record["accounting"] != {"status": "not_applicable", "provider_usage": None}:
        _fail("ACCOUNTING", "a native local result cannot claim provider accounting")
    if record["decoding"] != {"delivery_verdict": "unverified", "optimization_verdict": "not_applicable"}:
        _fail("DECODING", "the service never decides decoded verdicts")
    status, reason = record["status"], record["reason"]
    if record["effect"] != ("none" if status == "NOT_RUN" else "local_simulation"):
        _fail("EFFECT", "native result effect differs from its local execution status")
    if (reason is not None and not isinstance(reason, str)) or (status == "OK" and reason is not None):
        _fail("TYPE", "native result reason must be text, and absent on success")


def _check_identity(expected: NativeExpectedIdentity, record: Mapping[str, Any]) -> None:
    if record["request"] != {"request_id": expected.request_id, "request_digest": expected.request_digest}:
        _fail("IDENTITY", "result names a different request than the caller retained")
    identity = record["identity"]
    if not isinstance(identity, Mapping) or set(identity) != set(_IDENTITY_KEYS) or any(
            identity[key] != getattr(expected, key) for key in _IDENTITY_KEYS):
        _fail("IDENTITY", "result artifact, plan, PUB map, profile or target differs from the caller-retained identity")
    if record["source_verification"] != "admitted":
        _fail("IDENTITY", "result does not record source admission")


def _check_mapping(expected: NativeExpectedIdentity, decoded: VerifiedResult) -> None:
    wanted = [(row["pub_index"], p["recipient_id"], p["chunk"], p["slot"])
              for row in pub_map(expected.plan) for p in row["packets"]]
    actual = [(r.pub_index, r.recipient_id, r.chunk, r.slot) for r in decoded.records]
    if actual != wanted:
        _fail("PUB", "decoded recipient/chunk/slot/PUB mapping differs from the caller's plan")


def verify_native_result(expected: NativeExpectedIdentity, record: Any, evidence_catalog: Mapping[str, Any], *,
                         receipt_key: bytes,
                         authenticate: AuthenticationVerifier | None = None) -> NativeVerifiedResult:
    if not isinstance(expected, NativeExpectedIdentity):
        _fail("TYPE", "expected identity must be retained with retain_identity before submission")
    if isinstance(record, Mapping):
        check_schema(record.get("schema"), RESULT_SCHEMA, "native result")
    record = exact(record, _RESULT_KEYS, "native result")
    if not isinstance(receipt_key, (bytes, bytearray)) or not receipt_key:
        _fail("SEAL", "an explicit receipt key is required; unsealed records are never verified")
    try:
        verify_native_seal(record, receipt_key)
    except (ValueError, TypeError, QuantumValidationError):
        _fail("SEAL", "native result seal does not verify")
    _check_claims(record)
    _check_identity(expected, record)
    status = record["status"]
    if status not in ("OK", "FAILED", "NOT_RUN"):
        _fail("TYPE", "native result status is unknown")
    if status != "OK":
        if record["observations"] is not None:
            _fail("PARTIAL", "a FAILED or NOT_RUN record cannot carry observations")
        return NativeVerifiedResult(expected.request_digest, "pass", status, None, "not_applicable", "not_applicable")
    decoded = decode_verified(expected.plan, record["observations"], evidence_catalog, authenticate=authenticate)
    _check_mapping(expected, decoded)
    return NativeVerifiedResult(expected.request_digest, "pass", "OK", decoded, decoded.delivery_verdict,
                                "not_applicable")


__all__ = ["NativeExpectedIdentity", "NativeVerifiedResult", "retain_identity", "verify_native_result"]
