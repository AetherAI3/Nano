"""``nano.quantum.native-request.v1``: one prepared native artifact plus its signed descriptor.

The descriptor is the identity: ``request_digest = canonical_digest(descriptor)``.
A host can refuse declared resources and unadmitted profile/target digests from
the descriptor alone, before the artifact is parsed. Stdlib only.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from nano.quantum import QuantumValidationError
from nano.quantum.canonical import canonical_digest, canonical_json
from nano.quantum.native import REQUEST_SCHEMA, check_schema, exact, parse_request_descriptor

from .native_artifact import NativeArtifact, parse_native_artifact

_BODY_KEYS = {"schema", "descriptor", "artifact"}
_IDENTITY_KEYS = ("artifact_digest", "plan_digest", "pub_map_digest", "profile_digest", "target_digest")
Admission = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class NativeRequest:
    request_id: str
    request_digest: str
    _descriptor: str
    artifact: NativeArtifact

    @property
    def descriptor(self) -> dict[str, Any]:
        return json.loads(self._descriptor)


def _descriptor(artifact: NativeArtifact, request_id: str) -> dict[str, Any]:
    payload = artifact.to_payload()
    return {"schema": REQUEST_SCHEMA, "request_id": request_id, "target": payload["target"]["name"],
            **{key: payload[key] for key in _IDENTITY_KEYS}, "resources": payload["resources"]}


def build_native_request(artifact: Any, *, request_id: str) -> dict[str, Any]:
    artifact = parse_native_artifact(artifact)
    descriptor = parse_request_descriptor(_descriptor(artifact, request_id))
    return {"schema": REQUEST_SCHEMA, "descriptor": descriptor, "artifact": artifact.to_payload()}


def request_digest(body: Mapping[str, Any]) -> str:
    """The identity of a native request body (its descriptor's canonical digest)."""
    return canonical_digest(parse_request_descriptor(exact(body, _BODY_KEYS, "native request")["descriptor"]))


def is_native_request(raw: Any) -> bool:
    return isinstance(raw, Mapping) and raw.get("schema") == REQUEST_SCHEMA


def parse_native_request(body: Any, *, admit: Admission | None = None) -> NativeRequest:
    """Header, then host admission, then the artifact against the descriptor's digest."""
    if isinstance(body, Mapping):
        check_schema(body.get("schema"), REQUEST_SCHEMA, "native request")
    body = exact(body, _BODY_KEYS, "native request")
    descriptor = parse_request_descriptor(body["descriptor"])
    if admit is not None:
        admit(descriptor)
    artifact = parse_native_artifact(body["artifact"], expected_digest=descriptor["artifact_digest"])
    expected = _descriptor(artifact, descriptor["request_id"])
    if canonical_json(expected) != canonical_json(descriptor):
        raise QuantumValidationError("IDENTITY", "native request descriptor differs from its artifact")
    return NativeRequest(descriptor["request_id"], canonical_digest(descriptor), canonical_json(descriptor), artifact)


__all__ = ["NativeRequest", "build_native_request", "is_native_request", "parse_native_request", "request_digest"]
