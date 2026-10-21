# nano/loop/qsearch_receipt.py
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
from collections.abc import Mapping
from typing import Any

from .qsearch_canonical import canonical_digest, canonical_json
from .qsearch_job import QSearchJob

RECEIPT_SCHEMA = "predator.qsearch.receipt.v1"
SEAL_PREFIX = "hmac-sha256:"
ENERGY_TOLERANCE = 1e-9
MIN_KEY_BYTES = 16
TOP_SAMPLES = 8
_HEX = frozenset("0123456789abcdef")
SERVICE_NAME = "nano-qsearch"


class SealError(ValueError):
    """The receipt seal is missing, malformed or does not verify."""


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
    seal = payload.get("seal")
    if not isinstance(seal, str) or not seal.startswith(SEAL_PREFIX):
        raise SealError("receipt carries no hmac-sha256 seal")
    suffix = seal[len(SEAL_PREFIX):]
    if len(suffix) != 64 or any(c not in _HEX for c in suffix):
        raise SealError("receipt seal is malformed")
    body = {k: v for k, v in payload.items() if k != "seal"}
    expected = hmac.new(key, canonical_json(body).encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(suffix, expected):
        raise SealError("receipt seal does not verify")


def _base(job: QSearchJob, status: str) -> dict[str, Any]:
    return {"schema": RECEIPT_SCHEMA, "job_digest": job.job_digest, "status": status}


def not_run_receipt(job: QSearchJob, reason: str) -> dict[str, Any]:
    return {**_base(job, "NOT_RUN"), "backend": {"kind": "none"}, "reason": reason}


def refused_receipt(job: QSearchJob, reason: str) -> dict[str, Any]:
    return {**_base(job, "REFUSED"), "backend": {"kind": "none"}, "reason": reason}


def energy_gap(best: float, floor: float) -> float:
    return (best - floor) / abs(floor) if abs(floor) > 0.0 else 0.0


def ok_receipt(job: QSearchJob, result: Mapping[str, Any]) -> dict[str, Any]:
    """Build the OK (or TRUNCATED) unsigned body from an engine result (see plan Task 3)."""
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
            "chi_max": result["chi_max"], "chi_reached": result["chi_reached"], "truncated": result["truncated"],
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
        "service": {"name": SERVICE_NAME, "engine": "nano.loop._qsearch_engine"},
    }


__all__ = ["ENERGY_TOLERANCE", "RECEIPT_SCHEMA", "SEAL_PREFIX", "SealError", "energy_gap",
           "not_run_receipt", "ok_receipt", "refused_receipt", "seal_receipt", "verify_seal"]
