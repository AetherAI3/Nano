"""Opt-in packed receipts with caller-expected compiled provenance.

No service route activates this contract automatically. V2 retains its original
weaker provenance semantics and seal domain. V3 carries a public compile manifest
and uses a distinct domain; standalone verification always requires the caller's
independently retained expected manifest.
"""
from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Mapping
from typing import Any

from .canonical import canonical_digest, canonical_json, validate_digest
from .compile_manifest import (MAX_JSON_INTEGER, compile_identity, normalize_atlas_child_digest,
                               validate_compile_manifest)
from .receipt import ENERGY_TOLERANCE, MAX_REASON_CHARS, MIN_KEY_BYTES, ReceiptAuditError, SealError
from .receipt_v2 import atlas_request_envelope, request_receipt, results_digest
from .request import QSearchRequest

SCHEMA_V3 = "predator.qsearch.receipt.v3"
DOMAIN_V3 = b"predator.qsearch.hardware-receipt.v3\n"
SEAL_PREFIX_V3 = "hmac-sha256+hardware-v3:"
MAX_ACCOUNTING_REVISIONS = 64
_BASE_KEYS = {"schema", "receipt_version", "status", "request", "backend", "hardware", "blocks",
              "results_digest", "usage", "compile_manifest", "compile_identity", "atlas_receipt_sha256",
              "accounting_revision", "accounting_history"}
_BLOCK_KEYS = {"block_index", "pub_index", "job_digest", "recipients", "n", "shots", "seed", "counts",
               "counts_digest", "audit"}
_USAGE_KEYS = {"quantum_seconds", "usage_pending", "provider_sub_jobs", "provider_metrics"}


def _key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise ValueError(f"seal key must be at least {MIN_KEY_BYTES} bytes")
    return bytes(key)


def _check_usage(usage: Any) -> float | None:
    if not isinstance(usage, Mapping) or set(usage) != _USAGE_KEYS:
        raise ReceiptAuditError("usage: key set differs from the schema")
    seconds = usage["quantum_seconds"]
    if seconds is not None:
        try:
            finite = math.isfinite(seconds)
        except (TypeError, OverflowError):
            finite = False
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not finite or seconds < 0):
            raise ReceiptAuditError("usage: quantum seconds must be finite and nonnegative")
    if type(usage["usage_pending"]) is not bool or usage["usage_pending"] != (seconds is None or seconds == 0):
        raise ReceiptAuditError("usage: pending flag contradicts quantum seconds")
    subjobs = usage["provider_sub_jobs"]
    if subjobs is not None and (type(subjobs) is not int or not 0 <= subjobs <= MAX_JSON_INTEGER):
        raise ReceiptAuditError("usage: sub-job count must be a nonnegative integer")
    if usage["provider_metrics"] is not None and not isinstance(usage["provider_metrics"], Mapping):
        raise ReceiptAuditError("usage: provider metrics must be an object or null")
    canonical_json(usage)
    return seconds


def _validate_body(body: Mapping[str, Any], *, expected_manifest: Any,
                   expected_request: QSearchRequest | None = None) -> dict[str, Any]:
    if not isinstance(body, Mapping):
        raise ReceiptAuditError("not a v3 receipt object")
    if expected_request is not None and not isinstance(expected_request, QSearchRequest):
        raise ReceiptAuditError("expected_request must be a parsed QSearchRequest")
    if body.get("schema") != SCHEMA_V3 or type(body.get("receipt_version")) is not int or body["receipt_version"] != 3:
        raise ReceiptAuditError("not a v3 receipt")
    status = body.get("status")
    if status not in ("OK", "FAILED") or set(body) != _BASE_KEYS | ({"reason"} if status == "FAILED" else set()):
        raise ReceiptAuditError("receipt status or key set differs from the v3 schema")
    manifest = validate_compile_manifest(body["compile_manifest"])
    expected = validate_compile_manifest(expected_manifest, request=expected_request)
    identity = compile_identity(expected)
    if compile_identity(manifest) != identity or body["compile_identity"] != identity:
        raise ReceiptAuditError("receipt compile identity differs from the caller's expected manifest")
    request = body["request"]
    if (not isinstance(request, Mapping) or set(request) != {"request_digest", "block_count"}
            or request["request_digest"] != expected["request_digest"] or type(request["block_count"]) is not int):
        raise ReceiptAuditError("receipt request differs from the expected manifest")
    backend, hardware = body["backend"], body["hardware"]
    if (not isinstance(backend, Mapping) or set(backend) != {"kind", "id", "dry_run"}
            or backend["kind"] != "ibm" or backend["id"] != expected["target"]["name"]
            or type(backend["dry_run"]) is not bool or backend["dry_run"] != (backend["id"] == "fake_fez")):
        raise ReceiptAuditError("receipt backend differs from the expected target")
    hardware_keys = {"backend_name", "provider_job_id", "bit_order", "register", "purpose", "dry_run",
                     "max_execution_time", "job_tags"}
    if (not isinstance(hardware, Mapping) or set(hardware) != hardware_keys
            or hardware["backend_name"] != backend["id"] or hardware["dry_run"] is not backend["dry_run"]
            or hardware["bit_order"] != "msb_first" or hardware["purpose"] not in ("qsearch", "qcomms")
            or type(hardware["max_execution_time"]) is not int or not 1 <= hardware["max_execution_time"] <= 600
            or any(not isinstance(hardware[name], str) or not 0 < len(hardware[name]) <= 256 for name in ("register", "provider_job_id"))):
        raise ReceiptAuditError("receipt hardware block is malformed")
    tags = hardware["job_tags"]
    if not isinstance(tags, list) or not 1 <= len(tags) <= 16 or any(not isinstance(t, str) or not 0 < len(t) <= 256 for t in tags):
        raise ReceiptAuditError("receipt hardware tags are malformed")
    if expected_request is not None and (hardware["purpose"] != expected_request.purpose
            or hardware["max_execution_time"] != expected_request.max_execution_time):
        raise ReceiptAuditError("receipt purpose or execution limit differs from the expected request")
    if expected_request is not None:
        prefixes = ["atlas-qsearch-req-" + expected_request.request_id]
        if expected_request.block_count == 1:
            prefixes.append("atlas-qsearch-" + expected_request.request_id)
        if not any(tags[0].startswith(prefix) for prefix in prefixes):
            raise ReceiptAuditError("receipt recovery tag differs from the expected request")
    blocks = body["blocks"]
    if not isinstance(blocks, list) or request["block_count"] != len(blocks):
        raise ReceiptAuditError("receipt block count is malformed")
    if status == "FAILED":
        if blocks or backend["dry_run"] or not isinstance(body["reason"], str) or not body["reason"] or len(body["reason"]) > MAX_REASON_CHARS:
            raise ReceiptAuditError("a failed receipt must name a submitted job and contain no verified blocks")
    elif len(blocks) != len(expected["pubs"]):
        raise ReceiptAuditError("receipt is missing expected PUBs")
    for index, (block, pub) in enumerate(zip(blocks, expected["pubs"])):
        if not isinstance(block, Mapping) or set(block) != _BLOCK_KEYS:
            raise ReceiptAuditError("receipt block key set differs from the schema")
        for field in ("block_index", "pub_index", "n", "shots", "seed"):
            if type(block[field]) is not int:
                raise ReceiptAuditError("receipt block index, width, shots and seed must be integers")
        if block["block_index"] != index or any(block[field] != pub[field] for field in ("pub_index", "job_digest", "recipients", "n", "shots", "seed")):
            raise ReceiptAuditError("receipt block differs from its expected ordered PUB")
        if canonical_json(block["recipients"]) != canonical_json(pub["recipients"]):
            raise ReceiptAuditError("receipt recipient types differ from the expected ordered PUB")
        counts = block["counts"]
        if (not isinstance(counts, Mapping) or not counts or any(not isinstance(bits, str) or len(bits) != pub["n"]
                or set(bits) - {"0", "1"} or type(count) is not int or count < 1 for bits, count in counts.items())
                or sum(counts.values()) != pub["shots"] or block["counts_digest"] != canonical_digest(counts)):
            raise ReceiptAuditError("receipt counts differ from the expected shots or width")
        audit = block["audit"]
        if (not isinstance(audit, Mapping) or set(audit) != {"classical_min_energy", "best_sample_energy", "agree"}
                or type(audit["agree"]) is not bool):
            raise ReceiptAuditError("receipt audit is malformed")
        for field in ("classical_min_energy", "best_sample_energy"):
            if isinstance(audit[field], bool) or not isinstance(audit[field], (int, float)) or not math.isfinite(audit[field]):
                raise ReceiptAuditError("receipt audit energy must be finite")
        delta = audit["best_sample_energy"] - audit["classical_min_energy"]
        if delta < -ENERGY_TOLERANCE or audit["agree"] != (abs(delta) <= ENERGY_TOLERANCE):
            raise ReceiptAuditError("receipt audit contradicts its classical floor")
        if expected_request is not None and audit["classical_min_energy"] != expected_request.blocks[index].classical_min_energy:
            raise ReceiptAuditError("receipt classical floor differs from the expected request")
        if expected_request is not None:
            job = expected_request.jobs[index]
            best = min(job.energy([int(c) for c in reversed(bits)]) for bits in counts)
            if abs(best - audit["best_sample_energy"]) > ENERGY_TOLERANCE:
                raise ReceiptAuditError("receipt best energy differs from the expected request's counts")
    if body["results_digest"] != results_digest(blocks):
        raise ReceiptAuditError("receipt results digest differs")
    validate_digest(body["atlas_receipt_sha256"], field="atlas_receipt_sha256")
    _check_usage(body["usage"])
    revision, history = body["accounting_revision"], body["accounting_history"]
    if (type(revision) is not int or not 0 <= revision <= MAX_ACCOUNTING_REVISIONS
            or not isinstance(history, list) or len(history) != revision):
        raise ReceiptAuditError("accounting revision/history differs")
    canonical_json(body)
    return expected


def _mac(body: Mapping[str, Any], key: bytes) -> str:
    return SEAL_PREFIX_V3 + hmac.new(key, DOMAIN_V3 + canonical_json(body).encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_mac(payload: Mapping[str, Any], key: bytes) -> None:
    seal = payload.get("seal")
    body = {k: v for k, v in payload.items() if k != "seal"}
    if not isinstance(seal, str) or not hmac.compare_digest(seal, _mac(body, key)):
        raise SealError("receipt carries no valid v3 hardware seal")


def _verify_history(body: Mapping[str, Any], key: bytes) -> None:
    """Reconstruct each prior sealed body from its accounting snapshot and prefix."""
    history = body["accounting_history"]
    previous_seconds = None
    for index, snapshot in enumerate(history):
        if not isinstance(snapshot, Mapping) or set(snapshot) != {"usage", "atlas_receipt_sha256", "seal", "receipt_digest"}:
            raise ReceiptAuditError("accounting snapshot is malformed")
        seconds = _check_usage(snapshot["usage"])
        validate_digest(snapshot["atlas_receipt_sha256"], field="historical atlas_receipt_sha256")
        prior = {**body, "usage": snapshot["usage"], "atlas_receipt_sha256": snapshot["atlas_receipt_sha256"],
                 "accounting_revision": index, "accounting_history": history[:index], "seal": snapshot["seal"]}
        _verify_mac(prior, key)
        if canonical_digest(prior) != snapshot["receipt_digest"]:
            raise ReceiptAuditError("accounting snapshot digest differs from its prior signed receipt")
        if previous_seconds is not None and (seconds is None or seconds < previous_seconds):
            raise ReceiptAuditError("accounting quantum seconds regressed")
        previous_seconds = seconds
    current = _check_usage(body["usage"])
    if history and (current is None or current <= 0 or (previous_seconds is not None and current < previous_seconds)):
        raise ReceiptAuditError("accounting settlement must be positive and cannot regress")


def request_receipt_v3(request: QSearchRequest, atlas_receipt: Mapping[str, Any], *,
                       expected_manifest: Any, purpose: str) -> dict[str, Any]:
    """Create a body only when ATLAS evidence matches the caller-approved compile identity."""
    manifest = validate_compile_manifest(expected_manifest, request=request)
    envelope = atlas_request_envelope(request, atlas_receipt)
    if ([normalize_atlas_child_digest(child) for child in envelope["children"]]
            != [pub["atlas_child_digest"] for pub in manifest["pubs"]]):
        raise ReceiptAuditError("ATLAS children differ from the expected compile manifest")
    pubs = atlas_receipt.get("pubs")
    if isinstance(pubs, list):
        for pub in pubs:
            if not isinstance(pub, Mapping) or type(pub.get("pub_index")) is not int:
                raise ReceiptAuditError("ATLAS PUB index must be an integer")
    if purpose != request.purpose:
        raise ReceiptAuditError("receipt purpose differs from the frozen request")
    body = request_receipt(request, atlas_receipt, purpose=purpose)
    body.update(schema=SCHEMA_V3, receipt_version=3, compile_manifest=manifest,
                compile_identity=compile_identity(manifest), atlas_receipt_sha256=canonical_digest(atlas_receipt),
                accounting_revision=0, accounting_history=[])
    _validate_body(body, expected_manifest=manifest, expected_request=request)
    return body


def seal_receipt_v3(unsigned: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    """Seal the explicitly constructed v3 body, including its entire provenance and history."""
    key = _key(key)
    body = {k: v for k, v in unsigned.items() if k != "seal"}
    _validate_body(body, expected_manifest=body.get("compile_manifest"))
    _verify_history(body, key)
    # Detach nested caller-owned objects before returning the signed document.
    import json
    body = json.loads(canonical_json(body))
    return {**body, "seal": _mac(body, key)}


def verify_receipt_v3(payload: Mapping[str, Any], key: bytes, *, expected_manifest: Any,
                      expected_request: QSearchRequest | None = None) -> None:
    """Verify seal, ordered provenance, counts and prior accounting against an independent expectation.

    V2 receipts are rejected; reading them under the v2 API never upgrades their
    provenance claim. Verification authenticates the issuing host's claim, not
    the provider's samples or usage as independent third-party facts.
    """
    key = _key(key)
    try:
        if not isinstance(payload, Mapping) or "seal" not in payload:
            raise SealError("receipt carries no v3 seal")
        _verify_mac(payload, key)
        body = {k: v for k, v in payload.items() if k != "seal"}
        _validate_body(body, expected_manifest=expected_manifest, expected_request=expected_request)
        _verify_history(body, key)
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        if isinstance(exc, SealError):
            raise
        raise SealError(str(exc)) from None


def reseal_usage_v3(payload: Mapping[str, Any], key: bytes, *, expected_manifest: Any,
                     usage: Mapping[str, Any], atlas_receipt_sha256: str | None = None,
                     expected_request: QSearchRequest | None = None) -> dict[str, Any]:
    """Append a positive, nonregressing accounting revision; preserve every old seal and usage block."""
    verify_receipt_v3(payload, key, expected_manifest=expected_manifest, expected_request=expected_request)
    seconds, old_seconds = _check_usage(usage), _check_usage(payload["usage"])
    if seconds is None or seconds <= 0 or (old_seconds is not None and seconds < old_seconds):
        raise ReceiptAuditError("settlement must be positive and cannot regress")
    if payload["accounting_revision"] >= MAX_ACCOUNTING_REVISIONS:
        raise ReceiptAuditError("accounting history limit reached")
    snapshot = {"usage": payload["usage"], "atlas_receipt_sha256": payload["atlas_receipt_sha256"],
                "seal": payload["seal"], "receipt_digest": canonical_digest(payload)}
    body = {k: v for k, v in payload.items() if k != "seal"}
    body.update(usage=dict(usage), accounting_revision=body["accounting_revision"] + 1,
                accounting_history=[*body["accounting_history"], snapshot])
    if atlas_receipt_sha256 is not None:
        validate_digest(atlas_receipt_sha256, field="atlas_receipt_sha256")
        body["atlas_receipt_sha256"] = atlas_receipt_sha256
    return seal_receipt_v3(body, key)


__all__ = ["DOMAIN_V3", "SCHEMA_V3", "SEAL_PREFIX_V3", "MAX_ACCOUNTING_REVISIONS", "request_receipt_v3",
           "seal_receipt_v3", "verify_receipt_v3", "reseal_usage_v3"]
