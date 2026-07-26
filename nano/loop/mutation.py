"""Deterministic Nano++ mutation-admission helper.

``admit_mutation`` evaluates caller-supplied candidate facts and returns an
admission record. It does not generate, compile, verify, or deploy a mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Candidate:
    """A proposed mutation to a loop, already compiled to a deterministic form."""

    name: str
    parent_hash: str
    replay_matches: bool        # replay against recorded inputs reproduces baseline
    benchmark_delta: float      # fractional improvement over parent (+0.12 = +12%)
    regressions: int            # failing regression cases
    static_ok: bool             # static analysis passed
    capabilities: Tuple[str, ...]  # effects the candidate would require
    provenance: str             # who produced it (e.g. "aqrc", "manual")


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    stage_reached: str
    reason: str
    artifact: Optional[dict] = None  # produced only on admission; never deployed here

    def to_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "stage_reached": self.stage_reached,
            "reason": self.reason,
            "artifact": self.artifact,
        }


_MIN_BENCHMARK_GAIN = 0.0  # must strictly improve to justify a live change


def admit_mutation(
    candidate: Candidate,
    *,
    allowed_capabilities: Tuple[str, ...] = (),
    min_gain: float = _MIN_BENCHMARK_GAIN,
) -> AdmissionDecision:
    """Run the candidate through Verification then Admission. Deterministic."""
    # --- Stage 3: Verification ------------------------------------------------
    if not candidate.replay_matches:
        return AdmissionDecision(
            False, "Verification", "replay does not reproduce baseline behavior"
        )
    if candidate.regressions > 0:
        return AdmissionDecision(
            False, "Verification", f"{candidate.regressions} regression(s) present"
        )
    if not candidate.static_ok:
        return AdmissionDecision(False, "Verification", "static analysis failed")
    if candidate.benchmark_delta <= min_gain:
        return AdmissionDecision(
            False,
            "Verification",
            f"benchmark delta {candidate.benchmark_delta:+.3f} does not clear the bar",
        )

    # --- Stage 4: Admission ---------------------------------------------------
    escalated = tuple(c for c in candidate.capabilities if c not in allowed_capabilities)
    if escalated:
        return AdmissionDecision(
            False,
            "Admission",
            f"capability escalation blocked: {list(escalated)}",
        )

    artifact = {
        "name": candidate.name,
        "parent_hash": candidate.parent_hash,
        "benchmark_delta": candidate.benchmark_delta,
        "capabilities": list(candidate.capabilities),
        "provenance": candidate.provenance,
        "status": "admitted_pending_deploy",  # a human/operator deploys, not this
    }
    return AdmissionDecision(True, "Admission", "all gates passed", artifact=artifact)
