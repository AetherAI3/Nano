# nano_qsearch/receipt.py
"""Shape and seal ``predator.qsearch.receipt.v1`` payloads.

Every receipt the service emits goes through ``seal_receipt``: HMAC-SHA256
over the canonical JSON of the unsigned body, carried as ``seal =
"hmac-sha256:<hex>"``. That is exactly what Predator-M
``QSearchReceiptV1.from_payload`` recomputes, so a receipt that verifies here
verifies there. Energies are compared with ``ENERGY_TOLERANCE``, never ``==``.
"""
from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Mapping
from typing import Any

from .canonical import CanonicalizationError, canonical_digest, canonical_json
from .job import QSearchJob

RECEIPT_SCHEMA = "predator.qsearch.receipt.v1"
SEAL_PREFIX = "hmac-sha256:"
ENERGY_TOLERANCE = 1e-9
MIN_KEY_BYTES = 16
TOP_SAMPLES = 8
MAX_REASON_CHARS = 200
_HEX = frozenset("0123456789abcdef")
_BITS = frozenset("01")
_ENGINE_KINDS = ("statevector", "mps")
SERVICE_NAME = "nano-qsearch"
_MALFORMED = "engine result is missing or malformed fields"


class SealError(ValueError):
    """The receipt seal is missing, malformed or does not verify."""


class ReceiptAuditError(ValueError):
    """An engine result that cannot be audited against its job; no OK receipt is built from it."""


def _check_key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise ValueError(f"seal key must be at least {MIN_KEY_BYTES} bytes")
    return bytes(key)


def seal_receipt(unsigned: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    key = _check_key(key)
    body = {k: v for k, v in unsigned.items() if k != "seal"}
    mac = hmac.new(key, canonical_json(body).encode("utf-8"), hashlib.sha256).hexdigest()
    return {**body, "seal": SEAL_PREFIX + mac}


def verify_seal(payload: Mapping[str, Any], key: bytes) -> None:
    key = _check_key(key)
    if not isinstance(payload, Mapping):
        raise SealError("receipt is not a JSON object")
    seal = payload.get("seal")
    if not isinstance(seal, str) or not seal.startswith(SEAL_PREFIX):
        raise SealError("receipt carries no hmac-sha256 seal")
    suffix = seal[len(SEAL_PREFIX):]
    if len(suffix) != 64 or any(c not in _HEX for c in suffix):
        raise SealError("receipt seal is malformed")
    body = {k: v for k, v in payload.items() if k != "seal"}
    try:
        text = canonical_json(body)
    except (CanonicalizationError, RecursionError):
        raise SealError("receipt body is not canonical JSON") from None
    expected = hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(suffix, expected):
        raise SealError("receipt seal does not verify")


def _base(job: QSearchJob, status: str) -> dict[str, Any]:
    return {"schema": RECEIPT_SCHEMA, "job_digest": job.job_digest, "status": status}


def _reason(reason: str) -> str:
    if not isinstance(reason, str):
        raise TypeError("reason must be a str")
    return reason[:MAX_REASON_CHARS]


def not_run_receipt(job: QSearchJob, reason: str) -> dict[str, Any]:
    """NOT_RUN body. Callers pass ``"<stage>: <ExceptionClassName>"``, never ``str(exc)``; capped at 200 chars."""
    return {**_base(job, "NOT_RUN"), "backend": {"kind": "none"}, "reason": _reason(reason)}


def refused_receipt(job: QSearchJob, reason: str) -> dict[str, Any]:
    """REFUSED body. Callers pass ``"<stage>: <ExceptionClassName>"`` or a fixed policy
    sentence, never ``str(exc)``; capped at 200 chars."""
    return {**_base(job, "REFUSED"), "backend": {"kind": "none"}, "reason": _reason(reason)}


def energy_gap(best: float, floor: float) -> float:
    return (best - floor) / abs(floor) if abs(floor) > 0.0 else 0.0


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _audit_sample(job: QSearchJob, row: Any) -> float:
    """Check one ``[bits, count, energy]`` row against the job; return its energy."""
    if not isinstance(row, (list, tuple)) or len(row) != 3:
        raise ReceiptAuditError("engine sample is not [bits, count, energy]")
    bits, count, energy = row
    if not isinstance(bits, str) or len(bits) != job.n or any(c not in _BITS for c in bits):
        raise ReceiptAuditError("engine sample bits do not match the job")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ReceiptAuditError("engine sample count is not a positive integer")
    if not _finite_number(energy):
        raise ReceiptAuditError("engine sample energy is not a finite number")
    if abs(job.energy([int(c) for c in bits]) - energy) > ENERGY_TOLERANCE:
        raise ReceiptAuditError("engine sample energy does not match the job")
    return float(energy)


def _audit_result(job: QSearchJob, result: Mapping[str, Any]) -> None:
    """Refuse an engine result whose kind, samples or best energy cannot be re-derived from ``job``."""
    if result.get("kind") not in _ENGINE_KINDS:
        raise ReceiptAuditError("engine kind is not statevector or mps")
    truncated = result.get("truncated")
    if not (truncated is True or truncated is False or truncated is None):
        raise ReceiptAuditError("engine truncated is not true, false or null")
    if result["kind"] == "statevector" and truncated is not None:
        raise ReceiptAuditError("engine truncated must be null for statevector")
    best = result.get("best_sample_energy")
    if not _finite_number(best):
        raise ReceiptAuditError("engine best_sample_energy is not a finite number")
    samples = result.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ReceiptAuditError("engine samples must be a non-empty list")
    energies = [_audit_sample(job, row) for row in samples]
    if abs(best - min(energies)) > ENERGY_TOLERANCE:
        raise ReceiptAuditError("engine best_sample_energy is not the minimum sample energy")
    if best < job.classical_min_energy - ENERGY_TOLERANCE:
        raise ReceiptAuditError("engine best_sample_energy is below the classical floor")


def ok_receipt(job: QSearchJob, result: Mapping[str, Any]) -> dict[str, Any]:
    """Build the OK (or TRUNCATED) unsigned body from an engine result (see plan Task 3).

    The result is audited first (``ReceiptAuditError`` on any violation): every
    sample's energy is recomputed from the job, the best energy must be their
    minimum and may not undercut the job's classical floor. A result that is not
    a mapping, or that lacks or mistypes a field copied into the receipt, is
    refused the same way.
    """
    if not isinstance(result, Mapping):
        raise ReceiptAuditError(_MALFORMED)
    _audit_result(job, result)
    try:
        return _ok_body(job, result)
    except (KeyError, TypeError, AttributeError):
        raise ReceiptAuditError(_MALFORMED) from None


def _ok_body(job: QSearchJob, result: Mapping[str, Any]) -> dict[str, Any]:
    floor = job.classical_min_energy
    best = float(result["best_sample_energy"])
    agree = abs(best - floor) <= ENERGY_TOLERANCE
    samples = [list(row) for row in result["samples"]]
    status = "TRUNCATED" if result.get("truncated") is True else "OK"
    return {
        **_base(job, status),
        "compiled_plan_digest": result["compiled_plan_digest"],
        "variant": result["variant"],
        "nuclei": [list(nucleus) for nucleus in result["nuclei"]],
        "compile": dict(result["compile"]),
        "metrics": dict(result["metrics"]),
        "backend": {
            "kind": result["kind"], "id": result["backend_id"],
            "chi_max": result["chi_max"], "chi_reached": result["chi_reached"],
            "chi_source": result.get("chi_source"), "truncated": result["truncated"],
            "shots": result["shots"], "seed": result["seed"], "wall_s": result["wall_s"],
            "attempts": result["attempts"],
        },
        "samples": {
            "bit_order": "x0_first",
            "distribution_digest": canonical_digest([[bits, count] for bits, count, _ in samples]),
            "top": [{"bits": bits, "count": count, "energy": energy} for bits, count, energy in samples[:TOP_SAMPLES]],
        },
        "audit": {
            "classical_min_energy": floor, "best_sample_energy": best, "agree": agree,
            "energy_gap_best": energy_gap(best, floor),
            "mean_energy_sample": result["mean_energy_sample"],
            "mean_energy_ideal": result["mean_energy_ideal"],
            "tvd_ideal": result["tvd_ideal"],
            "exact_min_energy": result["exact_min_energy"],
        },
        "service": {"name": SERVICE_NAME, "engine": "nano_qsearch._engine"},
    }


__all__ = ["ENERGY_TOLERANCE", "RECEIPT_SCHEMA", "ReceiptAuditError", "SEAL_PREFIX", "SealError", "energy_gap",
           "not_run_receipt", "ok_receipt", "refused_receipt", "seal_receipt", "verify_seal"]
