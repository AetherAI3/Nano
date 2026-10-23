# nano_qsearch/job.py
"""Parse and validate a ``predator.qsearch.job.v1`` body into a ``QSearchJob``.

The body is Predator-M ``QSearchJobV1.to_payload()``. Validation is strict and
whole: every field is type-checked, the Q matrix must be square, symmetric and
finite, the angle vectors must have exactly ``p`` entries, and ``job_digest``
is recomputed with ``canonical_digest`` and must match. A job that fails here
gets no receipt at all (the service answers 400); a job that is valid but
over the service's own limits gets a REFUSED receipt bound to its digest.
"""
from __future__ import annotations

import math
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .canonical import canonical_digest, validate_digest

JOB_SCHEMA = "predator.qsearch.job.v1"
VARIANTS = ("auto", "ordinary", "joint5")
BACKEND_KINDS = ("auto", "mps", "statevector", "ibm")
TARGETS = ("heron_heavy_hex", "fake_fez", "ibm_fez")
CONVENTION = "symmetric_half"
_TOP_KEYS = frozenset({"schema", "compiled_search_digest", "variables", "qubo", "compile",
                       "backend", "limits", "audit", "job_digest"})


class JobValidationError(ValueError):
    """A job body that is not a valid ``predator.qsearch.job.v1``; ``code`` names the field group."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _mapping(value: Any, code: str, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise JobValidationError(code, f"{name} must be an object")
    return value


def _number(value: Any, code: str, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JobValidationError(code, f"{name} must be a finite number")
    try:
        out = float(value)
    except OverflowError:
        raise JobValidationError(code, f"{name} must be a finite number") from None
    if not math.isfinite(out):
        raise JobValidationError(code, f"{name} must be a finite number")
    return out


def _int(value: Any, code: str, name: str, *, minimum: int | None) -> int:
    """An int (bool refused); ``minimum=None`` skips the lower bound."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise JobValidationError(code, f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise JobValidationError(code, f"{name} must be an integer >= {minimum}")
    return value


def _choice(value: Any, choices: Sequence[str], code: str, name: str) -> str:
    if value not in choices:
        raise JobValidationError(code, f"{name} must be one of {list(choices)}")
    return value


def _angles(value: Any, p: int, name: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != p:
        raise JobValidationError("COMPILE", f"{name} must list exactly p={p} finite numbers")
    return tuple(_number(v, "COMPILE", name) for v in value)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return types.MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class QSearchJob:
    compiled_search_digest: str
    variables: tuple[str, ...]
    q: tuple[tuple[float, ...], ...]
    offset: float
    variant: str
    p: int
    gammas: tuple[float, ...]
    betas: tuple[float, ...]
    opt_level: int
    compile_seed: int
    target: str
    backend_kind: str
    chi_max: int
    shots: int
    backend_seed: int
    max_variables: int
    max_wall_s: int
    max_qpu_s: int
    classical_min_energy: float
    job_digest: str
    payload: Mapping[str, Any]

    @property
    def n(self) -> int:
        return len(self.variables)

    def energy(self, bits: Sequence[int]) -> float:
        """``xᵀ Q x + offset`` for ``bits`` in variable order."""
        if len(bits) != self.n or any(b not in (0, 1) for b in bits):
            raise ValueError("assignment length or alphabet differs from the job")
        total = self.offset
        for i, xi in enumerate(bits):
            if xi:
                row = self.q[i]
                total += sum(row[j] for j, xj in enumerate(bits) if xj)
        return total


def _parse_variables(body: Mapping[str, Any]) -> tuple[str, ...]:
    variables = body["variables"]
    if (not isinstance(variables, list) or not variables
            or any(not isinstance(v, str) or not v for v in variables)
            or len(set(variables)) != len(variables)):
        raise JobValidationError("VARIABLES", "variables must be a nonempty list of unique nonempty strings")
    return tuple(variables)


def _parse_qubo(body: Mapping[str, Any], n: int) -> tuple[tuple[tuple[float, ...], ...], float]:
    """Return ``(q, offset)``; ``q`` must be square over ``n`` variables, finite and exactly symmetric."""
    qubo = _mapping(body["qubo"], "QUBO", "qubo")
    if set(qubo) != {"q", "offset", "convention"}:
        raise JobValidationError("QUBO", "qubo must have exactly q, offset, convention")
    if qubo["convention"] != CONVENTION:
        raise JobValidationError("QUBO", f"qubo.convention must be {CONVENTION}")
    offset = _number(qubo["offset"], "QUBO", "qubo.offset")
    rows = qubo["q"]
    if not isinstance(rows, list) or len(rows) != n or any(not isinstance(r, list) or len(r) != n for r in rows):
        raise JobValidationError("QUBO", "qubo.q must be a square matrix over the variables")
    q = tuple(tuple(_number(v, "QUBO", "qubo.q entry") for v in row) for row in rows)
    for i in range(n):
        for j in range(i + 1, n):
            if q[i][j] != q[j][i]:   # Predator writes both halves from one value
                raise JobValidationError("QUBO", f"qubo.q is not symmetric at ({i},{j})")
    return q, offset


def _parse_compile(body: Mapping[str, Any]) -> tuple[str, int, tuple[float, ...], tuple[float, ...], int, int, str]:
    """Return ``(variant, p, gammas, betas, opt_level, seed, target)``."""
    comp = _mapping(body["compile"], "COMPILE", "compile")
    if set(comp) != {"variant", "p", "gammas", "betas", "opt_level", "seed", "target", "training"}:
        raise JobValidationError("COMPILE", "compile has unexpected or missing keys")
    variant = _choice(comp["variant"], VARIANTS, "COMPILE", "compile.variant")
    p = _int(comp["p"], "COMPILE", "compile.p", minimum=1)
    gammas = _angles(comp["gammas"], p, "compile.gammas")
    betas = _angles(comp["betas"], p, "compile.betas")
    opt_level = _int(comp["opt_level"], "COMPILE", "compile.opt_level", minimum=0)
    if opt_level > 3:
        raise JobValidationError("COMPILE", "compile.opt_level must be 0..3")
    seed = _int(comp["seed"], "COMPILE", "compile.seed", minimum=None)
    target = _choice(comp["target"], TARGETS, "COMPILE", "compile.target")
    if comp["training"] != "none":
        raise JobValidationError("COMPILE", "only compile.training == 'none' is defined")
    return variant, p, gammas, betas, opt_level, seed, target


def _parse_backend(body: Mapping[str, Any]) -> tuple[str, int, int, int]:
    """Return ``(kind, chi_max, shots, seed)``."""
    back = _mapping(body["backend"], "BACKEND", "backend")
    if set(back) != {"kind", "chi_max", "shots", "seed"}:
        raise JobValidationError("BACKEND", "backend has unexpected or missing keys")
    kind = _choice(back["kind"], BACKEND_KINDS, "BACKEND", "backend.kind")
    chi_max = _int(back["chi_max"], "BACKEND", "backend.chi_max", minimum=1)
    shots = _int(back["shots"], "BACKEND", "backend.shots", minimum=1)
    seed = _int(back["seed"], "BACKEND", "backend.seed", minimum=None)
    return kind, chi_max, shots, seed


def _parse_limits(body: Mapping[str, Any], n: int) -> tuple[int, int, int]:
    """Return ``(max_variables, max_wall_s, max_qpu_s)``; ``n`` must fit ``max_variables``."""
    lim = _mapping(body["limits"], "LIMITS", "limits")
    if set(lim) != {"max_variables", "max_wall_s", "max_qpu_s"}:
        raise JobValidationError("LIMITS", "limits has unexpected or missing keys")
    max_variables = _int(lim["max_variables"], "LIMITS", "limits.max_variables", minimum=1)
    max_wall_s = _int(lim["max_wall_s"], "LIMITS", "limits.max_wall_s", minimum=0)
    max_qpu_s = _int(lim["max_qpu_s"], "LIMITS", "limits.max_qpu_s", minimum=0)
    if n > max_variables:
        raise JobValidationError("LIMITS", f"{n} variables exceed limits.max_variables={max_variables}")
    return max_variables, max_wall_s, max_qpu_s


def _parse_audit(body: Mapping[str, Any]) -> float:
    """Return ``classical_min_energy``."""
    audit = _mapping(body["audit"], "AUDIT", "audit")
    if set(audit) != {"classical_min_energy"}:
        raise JobValidationError("AUDIT", "audit must have exactly classical_min_energy")
    return _number(audit["classical_min_energy"], "AUDIT", "audit.classical_min_energy")


def parse_job(body: Any) -> QSearchJob:
    """Validate ``body`` completely, then return the typed job (never partial)."""
    body = _mapping(body, "SCHEMA", "job")
    if body.get("schema") != JOB_SCHEMA:
        raise JobValidationError("SCHEMA", f"schema must be {JOB_SCHEMA}")
    unknown = set(body) - _TOP_KEYS
    missing = _TOP_KEYS - set(body)
    if unknown or missing:
        raise JobValidationError("SCHEMA", f"unknown keys {sorted(unknown)}, missing keys {sorted(missing)}")
    try:
        compiled_search_digest = validate_digest(body["compiled_search_digest"], field="compiled_search_digest")
    except ValueError as exc:
        raise JobValidationError("DIGEST", str(exc)) from exc

    variables = _parse_variables(body)
    n = len(variables)
    q, offset = _parse_qubo(body, n)
    variant, p, gammas, betas, opt_level, compile_seed, target = _parse_compile(body)
    backend_kind, chi_max, shots, backend_seed = _parse_backend(body)
    max_variables, max_wall_s, max_qpu_s = _parse_limits(body, n)
    classical_min_energy = _parse_audit(body)

    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    expected = canonical_digest(unsigned)
    if body["job_digest"] != expected:
        raise JobValidationError("DIGEST", "job_digest does not match the canonical body")

    return QSearchJob(
        compiled_search_digest=compiled_search_digest, variables=tuple(variables), q=q, offset=offset,
        variant=variant, p=p, gammas=gammas, betas=betas, opt_level=opt_level, compile_seed=compile_seed,
        target=target, backend_kind=backend_kind, chi_max=chi_max, shots=shots, backend_seed=backend_seed,
        max_variables=max_variables, max_wall_s=max_wall_s, max_qpu_s=max_qpu_s,
        classical_min_energy=classical_min_energy, job_digest=expected, payload=_deep_freeze(body),
    )


__all__ = ["BACKEND_KINDS", "CONVENTION", "JOB_SCHEMA", "JobValidationError", "QSearchJob", "TARGETS",
           "VARIANTS", "parse_job"]
