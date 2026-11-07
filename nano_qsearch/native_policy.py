"""Host admission policy and local result records for the native service adapter.

A policy names the exact profile and target digests the host admits plus
finite caps. Nothing here enables hardware: native targets are local only and
a native result can never carry a hardware seal or provider-final accounting.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from nano.quantum.canonical import canonical_json, validate_digest
from nano.quantum.native import MAX_LOCAL_QUBITS, RESULT_SCHEMA, check_schema

from .native_request import NativeRequest

POLICY_SCHEMA = "nano.qsearch.native-host-policy.v1"
SEAL_PREFIX = "hmac-sha256+native-local-v1:"
SEAL_DOMAIN = b"nano.quantum.native-local-result.v1\n"
MAX_POLICY_PUBS = 4096
MAX_POLICY_DEADLINE_S = 600
MAX_POLICY_SHOTS = 10_000_000
_POLICY_KEYS = {"schema", "admitted_profiles", "admitted_targets", "max_pubs", "max_total_shots", "max_qubits",
                "max_deadline_s", "evidence_banks"}
_IDENTITY_KEYS = ("artifact_digest", "plan_digest", "pub_map_digest", "profile_digest", "target_digest")


class NativeRefusal(Exception):
    """A stable, public refusal before a request row exists."""

    def __init__(self, status: int, code: str, field_path: str | None = None) -> None:
        self.status, self.code, self.field = status, code, field_path
        super().__init__(code)


def _digests(values: Any, name: str) -> frozenset[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError(f"{name} must be a collection of sha256 digests")
    return frozenset(validate_digest(v, field=name) for v in values)


def _bounded(value: Any, name: str, high: int) -> None:
    if type(value) is not int or not 1 <= value <= high:
        raise ValueError(f"{name} must be an int in 1..{high}")


@dataclass(frozen=True)
class NativeHostPolicy:
    admitted_profiles: frozenset[str]
    admitted_targets: frozenset[str]
    max_pubs: int = 16
    max_total_shots: int = 100_000
    max_qubits: int = 16
    max_deadline_s: int = 60
    evidence_banks: Mapping[str, str] | None = field(default=None)

    def __post_init__(self) -> None:
        object.__setattr__(self, "admitted_profiles", _digests(self.admitted_profiles, "admitted_profiles"))
        object.__setattr__(self, "admitted_targets", _digests(self.admitted_targets, "admitted_targets"))
        _bounded(self.max_pubs, "max_pubs", MAX_POLICY_PUBS)
        _bounded(self.max_total_shots, "max_total_shots", MAX_POLICY_SHOTS)
        _bounded(self.max_qubits, "max_qubits", MAX_LOCAL_QUBITS)
        _bounded(self.max_deadline_s, "max_deadline_s", MAX_POLICY_DEADLINE_S)
        if self.evidence_banks is not None:
            if not isinstance(self.evidence_banks, Mapping) or any(
                    not isinstance(k, str) or not k for k in self.evidence_banks):
                raise ValueError("evidence_banks must map references to digests")
            banks = {k: validate_digest(v, field="evidence_banks") for k, v in self.evidence_banks.items()}
            object.__setattr__(self, "evidence_banks", MappingProxyType(banks))


def parse_native_policy(body: Any) -> NativeHostPolicy:
    if isinstance(body, Mapping):
        check_schema(body.get("schema"), POLICY_SCHEMA, "native host policy")
    if not isinstance(body, Mapping) or set(body) != _POLICY_KEYS:
        raise ValueError("native host policy key set differs from the schema")
    return NativeHostPolicy(admitted_profiles=body["admitted_profiles"], admitted_targets=body["admitted_targets"],
                            max_pubs=body["max_pubs"], max_total_shots=body["max_total_shots"],
                            max_qubits=body["max_qubits"], max_deadline_s=body["max_deadline_s"],
                            evidence_banks=body["evidence_banks"])


def load_native_policy(path: str) -> NativeHostPolicy:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"native host policy is unreadable ({type(exc).__name__})") from None
    return parse_native_policy(json.loads(text))


def check_admission(descriptor: Mapping[str, Any], policy: NativeHostPolicy) -> None:
    """Refuse caps, then unadmitted identities, from the descriptor alone (no artifact parsing)."""
    resources = descriptor["resources"]
    caps = (("pub_count", policy.max_pubs), ("total_shots", policy.max_total_shots),
            ("max_width", policy.max_qubits), ("deadline_s", policy.max_deadline_s))
    for name, cap in caps:
        if resources[name] is not None and resources[name] > cap:
            raise NativeRefusal(400, "RESOURCE_LIMIT", f"descriptor.resources.{name}")
    if descriptor["profile_digest"] not in policy.admitted_profiles:
        raise NativeRefusal(403, "NOT_ADMITTED", "descriptor.profile_digest")
    if descriptor["target_digest"] not in policy.admitted_targets:
        raise NativeRefusal(403, "NOT_ADMITTED", "descriptor.target_digest")


def check_evidence(request: NativeRequest, policy: NativeHostPolicy) -> None:
    if policy.evidence_banks is None:
        return
    for packet in (p for o in request.artifact.plan.operations for p in getattr(o, "packets", ())):
        if policy.evidence_banks.get(packet.evidence_reference) != packet.evidence_digest:
            raise NativeRefusal(409, "STALE_EVIDENCE", "artifact.plan.packets.evidence_bank")


def result_record(request: NativeRequest, status: str, *, runtime_kind: str, qiskit_version: str | None,
                  observations: Mapping[str, Any] | None = None, reason: str | None = None) -> dict[str, Any]:
    """An unsealed local result; source, execution, decoding and accounting are separate fields."""
    descriptor = request.descriptor
    return {"schema": RESULT_SCHEMA,
            "request": {"request_id": request.request_id, "request_digest": request.request_digest},
            "identity": {key: descriptor[key] for key in _IDENTITY_KEYS}, "status": status, "reason": reason,
            "backend": {"kind": runtime_kind, "dry_run": True, "qiskit_version": qiskit_version},
            "effect": "none" if status == "NOT_RUN" else "local_simulation", "source_verification": "admitted",
            "observations": None if observations is None else json.loads(canonical_json(observations)),
            "decoding": {"delivery_verdict": "unverified", "optimization_verdict": "not_applicable"},
            "accounting": {"status": "not_applicable", "provider_usage": None}, "hardware": None}


def _mac(body: Mapping[str, Any], key: bytes) -> str:
    return hmac.new(bytes(key), SEAL_DOMAIN + canonical_json(body).encode("utf-8"), hashlib.sha256).hexdigest()


def seal_native_result(unsigned: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    body = {k: v for k, v in unsigned.items() if k != "seal"}
    if body.get("hardware") is not None or body.get("backend", {}).get("dry_run") is not True:
        raise ValueError("a native local result never claims hardware")
    return {**body, "seal": SEAL_PREFIX + _mac(body, key)}


def verify_native_seal(record: Mapping[str, Any], key: bytes) -> None:
    seal = record.get("seal") if isinstance(record, Mapping) else None
    if not isinstance(seal, str) or not seal.startswith(SEAL_PREFIX):
        raise ValueError("native result must carry the local native seal")
    body = {k: v for k, v in record.items() if k != "seal"}
    if not hmac.compare_digest(seal[len(SEAL_PREFIX):], _mac(body, key)):
        raise ValueError("native result seal does not verify")


__all__ = ["MAX_POLICY_DEADLINE_S", "MAX_POLICY_PUBS", "POLICY_SCHEMA", "SEAL_PREFIX", "NativeHostPolicy",
           "NativeRefusal", "check_admission", "check_evidence", "load_native_policy", "parse_native_policy",
           "result_record", "seal_native_result", "verify_native_seal"]
