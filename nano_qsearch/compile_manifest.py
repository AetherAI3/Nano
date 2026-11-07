"""Versioned, public compile identity for packed quantum requests.

This contract is opt-in. It contains only identities and ordered routing metadata,
never QPY bytes, credentials, samples or mutable usage/accounting information.
An application must retain its expected manifest before execution; a manifest
received along with a receipt is not an independent expectation.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import canonical_digest, canonical_json, validate_digest
from .request import QSearchRequest

MANIFEST_SCHEMA = "nano.qsearch.compile-manifest.v1"
MAX_JSON_INTEGER = 2**53 - 1
MAX_PUBS = 4096
MAX_WIDTH = 4096
MAX_SHOTS = 2**31 - 1
MAX_RECIPIENTS = 4096
_KEYS = {"schema", "request_digest", "target", "profile", "compiler", "pubs"}
_PUB_KEYS = {"pub_index", "job_digest", "recipients", "n", "shots", "seed",
             "atlas_child_digest", "compiled_plan_digest", "isa_digest", "layout"}
_COMPILER_KEYS = {"name", "version", "qiskit_version", "qiskit_ibm_runtime_version"}


class ManifestValidationError(ValueError):
    """A compile manifest is malformed or differs from an independent expectation."""


def _object(value: Any, keys: set[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ManifestValidationError(f"{field}: key set differs from the schema")
    return value


def _string(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ManifestValidationError(f"{field}: expected a nonempty string of at most 256 characters")


def _integer(value: Any, field: str, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= MAX_JSON_INTEGER:
        raise ManifestValidationError(f"{field}: expected an integer in {minimum}..{MAX_JSON_INTEGER}")


def _digest(value: Any, field: str) -> None:
    try:
        validate_digest(value, field=field)
    except ValueError as exc:
        raise ManifestValidationError(str(exc)) from None


def normalize_atlas_child_digest(value: Any) -> str:
    """Translate ATLAS's bare lower-hex wire identity into the manifest digest form.

    Older local contract fixtures already use the prefixed spelling. Accept
    precisely those two spellings at the ATLAS boundary; manifests themselves
    always contain the canonical ``sha256:`` spelling. No raw evidence document
    is rewritten, so its receipt evidence digest remains over its original bytes.
    """
    if isinstance(value, str) and len(value) == 64:
        value = "sha256:" + value
    _digest(value, "ATLAS child digest")
    return value


def validate_compile_manifest(value: Any, *, request: QSearchRequest | None = None) -> dict[str, Any]:
    """Strictly validate and detach a JSON manifest, optionally checking the frozen request."""
    manifest = _object(value, _KEYS, "manifest")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise ManifestValidationError("manifest: unsupported schema version")
    _digest(manifest["request_digest"], "request_digest")
    target = _object(manifest["target"], {"name", "snapshot_digest", "calibration_digest"}, "target")
    _string(target["name"], "target.name")
    _digest(target["snapshot_digest"], "target.snapshot_digest")
    if target["calibration_digest"] is not None:
        _digest(target["calibration_digest"], "target.calibration_digest")
    profile = _object(manifest["profile"], {"schema", "id", "digest"}, "profile")
    for field in ("schema", "id"):
        _string(profile[field], "profile." + field)
    _digest(profile["digest"], "profile.digest")
    compiler = _object(manifest["compiler"], _COMPILER_KEYS, "compiler")
    for field, item in compiler.items():
        if item is not None or field != "qiskit_ibm_runtime_version":
            _string(item, "compiler." + field)
    pubs = manifest["pubs"]
    if not isinstance(pubs, list) or not 1 <= len(pubs) <= MAX_PUBS:
        raise ManifestValidationError("pubs: expected 1..4096 ordered PUBs")
    jobs: set[str] = set()
    deliveries: set[tuple[str, int]] = set()
    for index, raw in enumerate(pubs):
        pub = _object(raw, _PUB_KEYS, f"pubs[{index}]")
        for field, minimum in (("pub_index", 0), ("n", 1), ("shots", 1), ("seed", 0)):
            _integer(pub[field], field, minimum)
        if pub["pub_index"] != index:
            raise ManifestValidationError("pubs: index differs from ordered position")
        if pub["n"] > MAX_WIDTH or pub["shots"] > MAX_SHOTS:
            raise ManifestValidationError("pubs: width or shots exceeds the v1 manifest limit")
        for field in ("job_digest", "atlas_child_digest", "compiled_plan_digest", "isa_digest"):
            _digest(pub[field], field)
        if pub["job_digest"] in jobs:
            raise ManifestValidationError("pubs: duplicate requested job")
        jobs.add(pub["job_digest"])
        layout = pub["layout"]
        if not isinstance(layout, list) or len(layout) != pub["n"]:
            raise ManifestValidationError("layout: expected one physical index per logical bit")
        for physical in layout:
            _integer(physical, "layout")
        if len(set(layout)) != len(layout):
            raise ManifestValidationError("layout: duplicate physical index")
        recipients = pub["recipients"]
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= MAX_RECIPIENTS:
            raise ManifestValidationError("recipients: expected a nonempty ordered list")
        previous_slot = -1
        for recipient in recipients:
            recipient = _object(recipient, {"agent_id", "chunk", "slot"}, "recipient")
            _string(recipient["agent_id"], "recipient.agent_id")
            for field in ("chunk", "slot"):
                _integer(recipient[field], "recipient." + field)
            delivery = recipient["agent_id"], recipient["chunk"]
            if delivery in deliveries or recipient["slot"] <= previous_slot:
                raise ManifestValidationError("recipients: duplicate delivery or unordered slot")
            deliveries.add(delivery)
            previous_slot = recipient["slot"]
    if request is not None:
        if (manifest["request_digest"] != request.request_digest or target["name"] != request.target
                or len(pubs) != request.block_count):
            raise ManifestValidationError("manifest differs from the frozen request")
        for pub, block in zip(pubs, request.blocks):
            expected = {"pub_index": block.pub_index, "job_digest": block.job_digest,
                        "recipients": block.recipients_payload(), "n": block.n,
                        "shots": block.shots, "seed": block.seed}
            if any(pub[key] != item for key, item in expected.items()):
                raise ManifestValidationError("manifest PUB differs from its requested block")
    try:
        return json.loads(canonical_json(manifest))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ManifestValidationError("manifest is not canonical JSON") from exc


def compile_identity(manifest: Any) -> str:
    """Stable identity; no usage, provider metrics, settlement or receipt seals enter it."""
    return canonical_digest(validate_compile_manifest(manifest))


def build_compile_manifest(request: QSearchRequest, *, atlas_children: Sequence[str],
                           compiled_plans: Sequence[Mapping[str, Any]], target_snapshot_digest: str,
                           calibration_digest: str | None, profile: Mapping[str, Any],
                           compiler: Mapping[str, Any]) -> dict[str, Any]:
    """Bind inspected compiler outputs to a frozen request before submission.

    Each ``compiled_plans`` item is exactly ``compiled_plan_digest``, ``isa_digest``
    and logical-to-physical ``layout``. Missing evidence is an error, never a
    placeholder. Callers retain the resulting manifest as their expectation.
    """
    if len(atlas_children) != request.block_count or len(compiled_plans) != request.block_count:
        raise ManifestValidationError("expected one ATLAS child and compiled plan per PUB")
    pubs = []
    for index, (block, child, plan) in enumerate(zip(request.blocks, atlas_children, compiled_plans)):
        plan = _object(plan, {"compiled_plan_digest", "isa_digest", "layout"}, "compiled plan")
        pubs.append({"pub_index": index, "job_digest": block.job_digest,
                     "recipients": block.recipients_payload(), "n": block.n, "shots": block.shots,
                     "seed": block.seed, "atlas_child_digest": normalize_atlas_child_digest(child), **plan})
    return validate_compile_manifest({"schema": MANIFEST_SCHEMA, "request_digest": request.request_digest,
        "target": {"name": request.target, "snapshot_digest": target_snapshot_digest,
                   "calibration_digest": calibration_digest},
        "profile": dict(profile), "compiler": dict(compiler), "pubs": pubs}, request=request)


__all__ = ["MANIFEST_SCHEMA", "ManifestValidationError", "build_compile_manifest", "compile_identity",
           "normalize_atlas_child_digest", "validate_compile_manifest"]
