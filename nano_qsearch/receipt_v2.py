"""Shape and seal ``predator.qsearch.receipt.v2``: one receipt for the N circuit results of one request.

The body is the bench v2.0 receipt (Predator ``bench/qcomms/skt/i4_receipt_v2``,
``SPEC_RECEIPT_V2.md`` §3) plus the HW0 production additions (contract §5):
each block carries ``audit`` (``classical_min_energy``, ``best_sample_energy``,
``agree``, computed exactly like v1 ``hardware_receipt``: every raw sample is
re-energised and a best below the classical floor refuses the whole request);
``hardware`` adds ``provider_job_id``, ``dry_run``, ``max_execution_time`` and
``job_tags``; a top-level ``usage`` block is present iff ``backend.kind`` is
``ibm`` (reported by the service, not verified by the seal's reader).

Counts are keyed by the **raw display strings** (``msb_first``: ``bits[-1 - i]``
is ``x[i]``) of the ordered samples, so ``sum(counts) == shots`` per block.
v2 is all-or-nothing. A request that was never submitted is ``NOT_RUN``
(``blocks == []``, ``backend.kind == "none"``); there is no REFUSED status, so
a policy verdict is a NOT_RUN whose reason starts with ``refused:``. A request
that WAS submitted (a provider job id exists, amendment R3 H1) is never
NOT_RUN: a provider-terminal failure or a result that does not audit is
``FAILED`` (``blocks == []``, ``block_count == 0``, ``reason``, ``hardware``
with the provider job id, ``usage``; hardware seal).

Seal: HMAC-SHA256 over ``domain + canonical_json(body without seal)`` with
``b"predator.qsearch.hardware-receipt.v2\\n"`` / ``hmac-sha256+hardware-v2:``
for ``backend.kind == "ibm"`` and ``b"predator.qsearch.receipt.v2\\n"`` /
``hmac-sha256+v2:`` otherwise. Both differ from v1's domains and prefixes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import CanonicalizationError, canonical_digest, canonical_json
from .receipt import ENERGY_TOLERANCE, MAX_REASON_CHARS, MIN_KEY_BYTES, ReceiptAuditError, SealError
from .request import QSearchRequest

SCHEMA_V2 = "predator.qsearch.receipt.v2"
RECEIPT_VERSION = 2
HARDWARE_SEAL_PREFIX_V2 = "hmac-sha256+hardware-v2:"
SEAL_PREFIX_V2 = "hmac-sha256+v2:"
HARDWARE_DOMAIN_V2 = b"predator.qsearch.hardware-receipt.v2\n"
DOMAIN_V2 = b"predator.qsearch.receipt.v2\n"
HARDWARE_KIND = "ibm"
BIT_ORDER = "msb_first"
ATLAS_REQUEST_RECEIPT_SCHEMA = "AqrcQSearchRequestReceiptV1"
DRY_RUN_TARGETS = ("fake_fez",)
DEFAULT_REGISTER = "meas"            # the register compile_program measures into (build_nucleus_program)
MAX_JOB_TAGS = 16                    # Predator's verifier bound on hardware.job_tags
MAX_TAG_CHARS = 256
ZERO_USAGE_EVIDENCE_SCHEMA = "AqrcQSearchZeroUsageEvidenceV1"
ZERO_USAGE_API_VERSION = "2026-04-15"
ZERO_USAGE_KEYS = frozenset({"schema", "ledger_id", "request_id", "request_sha256", "client_request_digest",
                           "child_sha", "plan_sha", "job_id", "instance", "backend_name", "job_tags",
                           "provider_status", "usage_status", "evidence_sha256", "checked_at",
                           "provider_api_version", "evidence_source"})
_HEX = frozenset("0123456789abcdef")
_BITS = frozenset("01")


def _check_key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise ValueError(f"seal key must be at least {MIN_KEY_BYTES} bytes")
    return bytes(key)


def _seal_parts(body: Mapping[str, Any]) -> tuple[str, bytes]:
    backend = body.get("backend")
    kind = backend.get("kind") if isinstance(backend, Mapping) else None
    return (HARDWARE_SEAL_PREFIX_V2, HARDWARE_DOMAIN_V2) if kind == HARDWARE_KIND else (SEAL_PREFIX_V2, DOMAIN_V2)


def seal_receipt_v2(unsigned: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    """Seal ``unsigned`` under the v2 domain for its backend kind (any seal already present is replaced)."""
    key = _check_key(key)
    body = {k: v for k, v in unsigned.items() if k != "seal"}
    prefix, domain = _seal_parts(body)
    if prefix == HARDWARE_SEAL_PREFIX_V2 and not isinstance(body.get("hardware"), Mapping):
        raise ReceiptAuditError("an ibm receipt must carry its hardware block")
    mac = hmac.new(key, domain + canonical_json(body).encode("utf-8"), hashlib.sha256).hexdigest()
    return {**body, "seal": prefix + mac}


def verify_seal_v2(payload: Mapping[str, Any], key: bytes) -> None:
    """``SealError`` unless ``payload`` carries the v2 seal for its backend kind and it verifies."""
    key = _check_key(key)
    if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA_V2:
        raise SealError("not a v2 receipt")
    seal = payload.get("seal")
    body = {k: v for k, v in payload.items() if k != "seal"}
    prefix, domain = _seal_parts(body)
    if not isinstance(seal, str) or not seal.startswith(prefix):
        raise SealError("receipt carries no v2 seal for its backend kind")
    suffix = seal[len(prefix):]
    if len(suffix) != 64 or any(c not in _HEX for c in suffix):
        raise SealError("receipt seal is malformed")
    try:
        text = canonical_json(body)
    except (CanonicalizationError, RecursionError):
        raise SealError("receipt body is not canonical JSON") from None
    if not hmac.compare_digest(suffix, hmac.new(key, domain + text.encode("utf-8"), hashlib.sha256).hexdigest()):
        raise SealError("receipt seal does not verify")


def results_digest(blocks: Sequence[Mapping[str, Any]]) -> str:
    """``canonical_digest`` of the ordered list of block digests (bench §2)."""
    return canonical_digest([canonical_digest(block) for block in blocks])


def _reason(reason: str) -> str:
    if not isinstance(reason, str):
        raise TypeError("reason must be a str")
    return reason[:MAX_REASON_CHARS]


def not_run_v2(request: QSearchRequest, reason: str) -> dict[str, Any]:
    """NOT_RUN body: the request was not executed (or not completely); no blocks. Reason is class names only."""
    return {"schema": SCHEMA_V2, "receipt_version": RECEIPT_VERSION, "status": "NOT_RUN",
            "request": {"request_digest": request.request_digest, "block_count": 0},
            "backend": {"kind": "none"}, "blocks": [], "results_digest": results_digest([]),
            "reason": _reason(reason)}


def _finite_nonnegative(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        out = float(value)
    except OverflowError:
        return None
    return out if math.isfinite(out) and out >= 0 else None


def _zero_evidence_identity(request: QSearchRequest, proof: Any, *, job_id: Any,
                            backend_name: Any, tags: Any) -> bool:
    """The single strict identity/schema predicate for the ATLAS and sealed projections."""
    if not isinstance(proof, Mapping) or set(proof) != ZERO_USAGE_KEYS:
        return False
    def hex64(value: Any) -> bool:
        return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX
    evidence_digest = proof.get("evidence_sha256")
    fingerprint = proof.get("request_sha256")
    return (proof.get("schema") == ZERO_USAGE_EVIDENCE_SCHEMA
            and proof.get("provider_api_version") == ZERO_USAGE_API_VERSION
            and proof.get("evidence_source") == "ibm-runtime-job-metrics"
            and proof.get("usage_status") == "complete"
            and proof.get("provider_status") in ("CANCELLED", "ERROR")
            and proof.get("request_id") == request.request_id
            and proof.get("client_request_digest") == request.request_digest
            and hex64(fingerprint) and proof.get("child_sha") == fingerprint == proof.get("plan_sha")
            and proof.get("job_id") == job_id and isinstance(job_id, str) and bool(job_id)
            and proof.get("backend_name") == request.target == backend_name
            and isinstance(proof.get("instance"), str) and bool(proof["instance"].strip())
            and isinstance(proof.get("ledger_id"), str) and len(proof["ledger_id"]) == 32
            and set(proof["ledger_id"]) <= _HEX
            and isinstance(evidence_digest, str) and evidence_digest.startswith("sha256:")
            and hex64(evidence_digest[7:])
            and isinstance(proof.get("checked_at"), str) and bool(proof["checked_at"].strip())
            and isinstance(tags, list) and proof.get("job_tags") == tags
            and 1 <= len(tags) <= MAX_JOB_TAGS
            and all(isinstance(t, str) and 0 < len(t) <= MAX_TAG_CHARS for t in tags)
            and f"atlas-qsearch-req-{request.request_id}" in tags
            and f"atlas-ledger-{proof['ledger_id'][:16]}" in tags)


def confirmed_zero_receipt(request: QSearchRequest, receipt: Mapping[str, Any]) -> bool:
    """Recognize confirmed zero in an already authenticated Nano v2 receipt.

    Pure identity/shape validation: callers MUST first authenticate the hardware
    HMAC with ``verify_seal_v2(receipt, key)``. This function has no key and cannot
    authenticate a MAC. The sealed assertion originates in ATLAS's transition;
    neither a zero amount nor a marker in a mutable run record supplies authority.
    """
    if not isinstance(request, QSearchRequest) or not isinstance(receipt, Mapping):
        return False
    usage, hardware, backend, bound = (receipt.get(name) for name in ("usage", "hardware", "backend", "request"))
    if not all(isinstance(value, Mapping) for value in (usage, hardware, backend, bound)):
        return False
    proof = usage.get("zero_usage_evidence")
    seal = receipt.get("seal")
    if (receipt.get("schema") != SCHEMA_V2 or receipt.get("receipt_version") != RECEIPT_VERSION
            or receipt.get("status") != "FAILED" or receipt.get("blocks") != []
            or receipt.get("results_digest") != results_digest([])
            or bound.get("request_digest") != request.request_digest or type(bound.get("block_count")) is not int
            or bound["block_count"] != 0 or backend.get("kind") != HARDWARE_KIND
            or backend.get("id") != request.target or backend.get("dry_run") is not False
            or hardware.get("dry_run") is not False or request.target in DRY_RUN_TARGETS
            or hardware.get("purpose") != request.purpose
            or type(hardware.get("max_execution_time")) is not int
            or hardware["max_execution_time"] != request.max_execution_time
            or usage.get("usage_pending") is not False or _finite_nonnegative(usage.get("quantum_seconds")) != 0
            or not isinstance(seal, str) or not seal.startswith(HARDWARE_SEAL_PREFIX_V2)
            or len(seal[len(HARDWARE_SEAL_PREFIX_V2):]) != 64
            or set(seal[len(HARDWARE_SEAL_PREFIX_V2):]) - _HEX):
        return False
    return (_zero_evidence_identity(request, proof, job_id=hardware.get("provider_job_id"),
                                    backend_name=hardware.get("backend_name"), tags=hardware.get("job_tags"))
            and receipt.get("reason") == "provider: " + proof["provider_status"])


def confirmed_zero_evidence(request: QSearchRequest, atlas_receipt: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate ATLAS's authoritative zero transition, never infer finality from a zero.

    ATLAS owns provider authentication, instance binding and the durable accounting
    transition. This projection binds that transition to this exact client request
    before it enters Nano's hardware seal. Older ATLAS pins supply no projection.
    """
    proof = atlas_receipt.get("zero_usage_evidence")
    frozen = atlas_receipt.get("request")
    envelope = frozen.get("envelope") if isinstance(frozen, Mapping) else None
    if not isinstance(envelope, Mapping) or not _zero_evidence_identity(
            request, proof, job_id=atlas_receipt.get("job_id"), backend_name=atlas_receipt.get("backend_name"),
            tags=atlas_receipt.get("job_tags")):
        return None
    try:
        # ATLAS's fingerprint uses the stdlib's ASCII escaping, which is part of
        # its existing request contract (distinct from the client wire encoding).
        fingerprint = hashlib.sha256(json.dumps(dict(envelope), sort_keys=True, separators=(",", ":"),
                                                allow_nan=False).encode("utf-8")).hexdigest()
        valid = (atlas_receipt.get("status") == "FAILED" and atlas_receipt.get("dry_run") is False
                 and atlas_receipt.get("accounting_outcome") == "cancelled_no_usage"
                 and atlas_receipt.get("usage_final_status") == "complete"
                 and atlas_receipt.get("usage_pending") is False
                 and _finite_nonnegative(atlas_receipt.get("provider_final_usage_seconds")) == 0
                 and proof.get("provider_status") == atlas_receipt.get("provider_status")
                 and proof.get("request_id") == request.request_id == envelope.get("request_id")
                 and proof.get("client_request_digest") == request.request_digest == envelope.get("client_request_digest")
                 and proof.get("request_sha256") == fingerprint == frozen.get("request_sha256")
                 and envelope.get("recovery_tag") == f"atlas-qsearch-req-{request.request_id}")
        return dict(proof) if valid else None
    except (KeyError, TypeError, ValueError, RecursionError):
        return None


def _usage(atlas_receipt: Mapping[str, Any], request: QSearchRequest) -> dict[str, Any]:
    """The service-reported usage block: raw provider seconds, pending flag, sub-jobs and metrics when known."""
    seconds = _finite_nonnegative(atlas_receipt.get("provider_final_usage_seconds"))
    sub_jobs = atlas_receipt.get("provider_sub_jobs")
    metrics = atlas_receipt.get("provider_metrics")
    proof = confirmed_zero_evidence(request, atlas_receipt) if seconds == 0 else None
    pending = seconds is None or (seconds == 0 and proof is None)
    try:                                               # informational: a non-canonical value is dropped, not sealed
        metrics = dict(metrics) if isinstance(metrics, Mapping) and canonical_json(metrics) else None
    except (CanonicalizationError, RecursionError):
        metrics = None
    return {"quantum_seconds": seconds, "usage_pending": pending,
            "provider_sub_jobs": sub_jobs if type(sub_jobs) is int and sub_jobs >= 0 else None,
            "provider_metrics": metrics, **({"zero_usage_evidence": proof} if proof is not None else {})}


def atlas_request_envelope(request: QSearchRequest, atlas_receipt: Any) -> Mapping[str, Any]:
    """Check the ATLAS request receipt's identity against ``request``; return its frozen request envelope.

    Identity only (schema, request id, PUB count, limit, target, purpose, recovery tag, provider job
    id, dry-run flag and, when ATLAS records it, Predator's request digest); the PUBs are checked by
    the OK path, since a FAILED receipt carries none.
    """
    if not isinstance(atlas_receipt, Mapping) or atlas_receipt.get("schema") != ATLAS_REQUEST_RECEIPT_SCHEMA:
        raise ReceiptAuditError("delegate result is not an ATLAS q-search request receipt")
    frozen = atlas_receipt.get("request")
    envelope = frozen.get("envelope") if isinstance(frozen, Mapping) else None
    if (not isinstance(envelope, Mapping) or envelope.get("request_id") != request.request_id
            or envelope.get("pub_count") != request.block_count
            or envelope.get("max_execution_time") != request.max_execution_time
            or envelope.get("target") != request.target or envelope.get("purpose") != request.purpose
            or not isinstance(envelope.get("recovery_tag"), str) or not envelope["recovery_tag"]
            or envelope.get("client_request_digest", request.request_digest) != request.request_digest):
        raise ReceiptAuditError("ATLAS request envelope differs from the v2 request")
    if (atlas_receipt.get("target") != request.target or atlas_receipt.get("backend_name") != request.target
            or not isinstance(atlas_receipt.get("job_id"), str) or not atlas_receipt["job_id"]):
        raise ReceiptAuditError("ATLAS request receipt target, backend name or job id is missing or differs")
    if atlas_receipt.get("dry_run") is not (request.target in DRY_RUN_TARGETS):
        raise ReceiptAuditError("ATLAS request receipt dry_run flag contradicts its target")
    children = envelope.get("children")
    if not isinstance(children, list) or len(children) != request.block_count:
        raise ReceiptAuditError("ATLAS request envelope must name one child per requested block")
    return envelope


def _block(request: QSearchRequest, index: int, pub: Any, child_sha256: Any) -> tuple[dict[str, Any], str]:
    """One v2 block from one ATLAS PUB receipt; returns ``(block, register)``."""
    job, want = request.jobs[index], request.blocks[index]
    if not isinstance(pub, Mapping) or pub.get("pub_index") != index or pub.get("child_sha256") != child_sha256:
        raise ReceiptAuditError(f"PUB {index} is out of order or bound to another child")
    raw = pub.get("raw_samples")
    if (pub.get("bit_order") != BIT_ORDER or not isinstance(pub.get("register"), str) or not pub["register"]
            or not isinstance(raw, list) or len(raw) != want.shots or pub.get("shots") != len(raw)
            or any(not isinstance(bits, str) or len(bits) != want.n or set(bits) - _BITS for bits in raw)):
        raise ReceiptAuditError(f"PUB {index} samples do not match the block's shots and width")
    counts = dict(Counter(raw))
    energies = {bits: job.energy([int(c) for c in reversed(bits)]) for bits in counts}   # display → x0-first
    best, floor = min(energies.values()), want.classical_min_energy
    if best < floor - ENERGY_TOLERANCE:
        raise ReceiptAuditError(f"PUB {index} best_sample_energy is below the classical floor")
    block = {"block_index": index, "pub_index": index, "job_digest": want.job_digest,
             "recipients": want.recipients_payload(), "n": want.n, "shots": want.shots, "seed": want.seed,
             "counts": counts, "counts_digest": canonical_digest(counts),
             "audit": {"classical_min_energy": floor, "best_sample_energy": float(best),
                       "agree": abs(best - floor) <= ENERGY_TOLERANCE}}
    return block, pub["register"]


def _job_tags(atlas_receipt: Mapping[str, Any], envelope: Mapping[str, Any]) -> list[str]:
    """The provider job tags ATLAS reports (recovery tag first, then e.g. its ledger tag), else the recovery tag."""
    tags = atlas_receipt.get("job_tags")
    if (isinstance(tags, list) and 1 <= len(tags) <= MAX_JOB_TAGS and tags[0] == envelope["recovery_tag"]
            and all(isinstance(t, str) and 0 < len(t) <= MAX_TAG_CHARS for t in tags)):
        return list(tags)
    return [envelope["recovery_tag"]]


def _hardware(request: QSearchRequest, atlas_receipt: Mapping[str, Any], envelope: Mapping[str, Any], *,
              register: str, purpose: str) -> dict[str, Any]:
    return {"backend_name": atlas_receipt["backend_name"], "provider_job_id": atlas_receipt["job_id"],
            "bit_order": BIT_ORDER, "register": register, "purpose": purpose,
            "dry_run": atlas_receipt["dry_run"], "max_execution_time": request.max_execution_time,
            "job_tags": _job_tags(atlas_receipt, envelope)}


def _finished(body: dict[str, Any]) -> dict[str, Any]:
    try:
        canonical_json(body)                 # fail here, not at sealing, on a non-canonical provider value
    except (TypeError, CanonicalizationError, RecursionError):
        raise ReceiptAuditError("ATLAS request receipt is missing or malformed fields") from None
    return body


def failed_v2(request: QSearchRequest, atlas_receipt: Mapping[str, Any], *, purpose: str,
              reason: str) -> dict[str, Any]:
    """FAILED body: the request WAS submitted (a provider job id exists) but yields no verified blocks.

    A provider-terminal failure (ERROR / CANCELLED / max time) or a result that does not audit. Same
    top-level keys as OK plus ``reason``; ``blocks == []``, ``block_count == 0``; ``hardware`` names the
    provider job and ``usage`` reports what the provider charged. Sealed with the hardware seal.
    """
    envelope = atlas_request_envelope(request, atlas_receipt)
    if atlas_receipt.get("dry_run") is not False:
        raise ReceiptAuditError("only a submitted (non-dry-run) request can be FAILED")
    pubs = atlas_receipt.get("pubs")
    register = (pubs[0].get("register") if isinstance(pubs, list) and pubs and isinstance(pubs[0], Mapping)
                else atlas_receipt.get("register"))
    register = register if isinstance(register, str) and register else DEFAULT_REGISTER
    try:
        body = {
            "schema": SCHEMA_V2, "receipt_version": RECEIPT_VERSION, "status": "FAILED",
            "request": {"request_digest": request.request_digest, "block_count": 0},
            "backend": {"kind": HARDWARE_KIND, "id": atlas_receipt["backend_name"], "dry_run": False},
            "hardware": _hardware(request, atlas_receipt, envelope, register=register, purpose=purpose),
            "blocks": [], "results_digest": results_digest([]), "usage": _usage(atlas_receipt, request),
            "reason": _reason(reason),
        }
    except (KeyError, TypeError):
        raise ReceiptAuditError("ATLAS request receipt is missing or malformed fields") from None
    return _finished(body)


def provider_failure_reason(atlas_receipt: Mapping[str, Any]) -> str:
    """``provider: <terminal status>`` for a FAILED ATLAS receipt.

    The provider's status name only (ERROR, CANCELLED, ...); the provider's free-text error
    message stays in ATLAS's ledger receipt, as Nano reasons never carry provider or OS text.
    """
    status = atlas_receipt.get("provider_status")
    ok = isinstance(status, str) and 0 < len(status) <= 40 and status.replace("_", "").isalnum()
    return "provider: " + (status if ok else "job failed")


def request_receipt(request: QSearchRequest, atlas_receipt: Mapping[str, Any], *, purpose: str) -> dict[str, Any]:
    """OK (or, for a provider-terminal failure, FAILED) v2 body from an ATLAS ``AqrcQSearchRequestReceiptV1``."""
    envelope = atlas_request_envelope(request, atlas_receipt)
    if atlas_receipt.get("status") == "FAILED":
        return failed_v2(request, atlas_receipt, purpose=purpose, reason=provider_failure_reason(atlas_receipt))
    pubs = atlas_receipt.get("pubs")
    if not isinstance(pubs, list) or len(pubs) != request.block_count:
        raise ReceiptAuditError("ATLAS request receipt must carry exactly one PUB per requested block")
    built = [_block(request, i, pub, child) for i, (pub, child) in enumerate(zip(pubs, envelope["children"]))]
    registers = {register for _, register in built}
    if len(registers) != 1:
        raise ReceiptAuditError("PUBs of one request must share one measurement register")
    blocks = [block for block, _ in built]
    try:
        body = {
            "schema": SCHEMA_V2, "receipt_version": RECEIPT_VERSION, "status": "OK",
            "request": {"request_digest": request.request_digest, "block_count": len(blocks)},
            "backend": {"kind": HARDWARE_KIND, "id": atlas_receipt["backend_name"],
                        "dry_run": atlas_receipt["dry_run"]},
            "hardware": _hardware(request, atlas_receipt, envelope, register=registers.pop(), purpose=purpose),
            "blocks": blocks, "results_digest": results_digest(blocks), "usage": _usage(atlas_receipt, request),
        }
    except (KeyError, TypeError):
        raise ReceiptAuditError("ATLAS request receipt is missing or malformed fields") from None
    return _finished(body)


__all__ = ["ATLAS_REQUEST_RECEIPT_SCHEMA", "BIT_ORDER", "DOMAIN_V2", "HARDWARE_DOMAIN_V2", "HARDWARE_SEAL_PREFIX_V2",
           "DEFAULT_REGISTER", "RECEIPT_VERSION", "SCHEMA_V2", "SEAL_PREFIX_V2", "atlas_request_envelope", "failed_v2",
           "not_run_v2", "provider_failure_reason", "request_receipt", "results_digest", "seal_receipt_v2",
           "confirmed_zero_receipt",
           "verify_seal_v2"]
