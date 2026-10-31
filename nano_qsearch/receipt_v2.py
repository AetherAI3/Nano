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
v2 is all-or-nothing: a request that did not produce every block is
``NOT_RUN`` with ``blocks == []`` and ``backend.kind == "none"``; there is no
REFUSED status in v2, so a policy verdict is a NOT_RUN whose reason starts
with ``refused:``.

Seal: HMAC-SHA256 over ``domain + canonical_json(body without seal)`` with
``b"predator.qsearch.hardware-receipt.v2\\n"`` / ``hmac-sha256+hardware-v2:``
for ``backend.kind == "ibm"`` and ``b"predator.qsearch.receipt.v2\\n"`` /
``hmac-sha256+v2:`` otherwise. Both differ from v1's domains and prefixes.
"""
from __future__ import annotations

import hashlib
import hmac
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


def _usage(atlas_receipt: Mapping[str, Any]) -> dict[str, Any]:
    """The service-reported usage block: raw provider seconds, pending flag, sub-jobs and metrics when known."""
    seconds = _finite_nonnegative(atlas_receipt.get("provider_final_usage_seconds"))
    sub_jobs = atlas_receipt.get("provider_sub_jobs")
    metrics = atlas_receipt.get("provider_metrics")
    pending = seconds is None or seconds == 0          # zero is pending, never free (Predator checks exactly this)
    try:                                               # informational: a non-canonical value is dropped, not sealed
        metrics = dict(metrics) if isinstance(metrics, Mapping) and canonical_json(metrics) else None
    except (CanonicalizationError, RecursionError):
        metrics = None
    return {"quantum_seconds": seconds, "usage_pending": pending,
            "provider_sub_jobs": sub_jobs if type(sub_jobs) is int and sub_jobs >= 0 else None,
            "provider_metrics": metrics}


def atlas_request_envelope(request: QSearchRequest, atlas_receipt: Any) -> Mapping[str, Any]:
    """Check the ATLAS request receipt's identity against ``request``; return its frozen request envelope."""
    if not isinstance(atlas_receipt, Mapping) or atlas_receipt.get("schema") != ATLAS_REQUEST_RECEIPT_SCHEMA:
        raise ReceiptAuditError("delegate result is not an ATLAS q-search request receipt")
    frozen = atlas_receipt.get("request")
    envelope = frozen.get("envelope") if isinstance(frozen, Mapping) else None
    if (not isinstance(envelope, Mapping) or envelope.get("request_id") != request.request_id
            or envelope.get("pub_count") != request.block_count
            or envelope.get("max_execution_time") != request.max_execution_time
            or envelope.get("target") != request.target or envelope.get("purpose") != request.purpose
            or not isinstance(envelope.get("recovery_tag"), str) or not envelope["recovery_tag"]):
        raise ReceiptAuditError("ATLAS request envelope differs from the v2 request")
    if (atlas_receipt.get("target") != request.target or atlas_receipt.get("backend_name") != request.target
            or not isinstance(atlas_receipt.get("job_id"), str) or not atlas_receipt["job_id"]):
        raise ReceiptAuditError("ATLAS request receipt target, backend name or job id is missing or differs")
    if atlas_receipt.get("dry_run") is not (request.target in DRY_RUN_TARGETS):
        raise ReceiptAuditError("ATLAS request receipt dry_run flag contradicts its target")
    children = envelope.get("children")
    pubs = atlas_receipt.get("pubs")
    if (not isinstance(children, list) or not isinstance(pubs, list) or len(pubs) != request.block_count
            or len(children) != request.block_count):
        raise ReceiptAuditError("ATLAS request receipt must carry exactly one PUB per requested block")
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


def request_receipt(request: QSearchRequest, atlas_receipt: Mapping[str, Any], *, purpose: str) -> dict[str, Any]:
    """OK v2 body from an ATLAS ``AqrcQSearchRequestReceiptV1`` (FakeFez dry run or ibm_fez); unsigned."""
    envelope = atlas_request_envelope(request, atlas_receipt)
    built = [_block(request, i, pub, child) for i, (pub, child)
             in enumerate(zip(atlas_receipt["pubs"], envelope["children"]))]
    registers = {register for _, register in built}
    if len(registers) != 1:
        raise ReceiptAuditError("PUBs of one request must share one measurement register")
    blocks = [block for block, _ in built]
    backend_name = atlas_receipt["backend_name"]
    try:
        body = {
            "schema": SCHEMA_V2, "receipt_version": RECEIPT_VERSION, "status": "OK",
            "request": {"request_digest": request.request_digest, "block_count": len(blocks)},
            "backend": {"kind": HARDWARE_KIND, "id": backend_name, "dry_run": atlas_receipt["dry_run"]},
            "hardware": {"backend_name": backend_name, "provider_job_id": atlas_receipt["job_id"],
                         "bit_order": BIT_ORDER, "register": registers.pop(), "purpose": purpose,
                         "dry_run": atlas_receipt["dry_run"], "max_execution_time": request.max_execution_time,
                         "job_tags": [envelope["recovery_tag"]]},
            "blocks": blocks, "results_digest": results_digest(blocks), "usage": _usage(atlas_receipt),
        }
        canonical_json(body)                 # fail here, not at sealing, on a non-canonical provider value
    except (KeyError, TypeError, CanonicalizationError):
        raise ReceiptAuditError("ATLAS request receipt is missing or malformed fields") from None
    return body


__all__ = ["ATLAS_REQUEST_RECEIPT_SCHEMA", "BIT_ORDER", "DOMAIN_V2", "HARDWARE_DOMAIN_V2", "HARDWARE_SEAL_PREFIX_V2",
           "RECEIPT_VERSION", "SCHEMA_V2", "SEAL_PREFIX_V2", "atlas_request_envelope", "not_run_v2",
           "request_receipt", "results_digest", "seal_receipt_v2", "verify_seal_v2"]
