"""Bounded repair gate for recorded candidate proposals (contract: policy.json).

At most one recorded candidate per case is considered. It is accepted only if
it equals the single correction that the allowlisted policy derives from the
first proposal alone; the policy never reads task expectations or hints.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .pipeline import Admission

POLICIES = ("none", "allowlist-v1")
TRIGGERS = {"quantum_command_json": frozenset({"json_syntax", "schema_mismatch"}),
            "nano_strategy_source": frozenset({"SYNTAX_ERROR"})}


@dataclass(frozen=True)
class RepairDecision:
    candidate: bool
    attempted: bool
    accepted: bool
    kind: str | None
    reason: str


def correction(proposal_kind: str, original: str, first: Admission) -> tuple[str, Any] | None:
    """Derive the single allowlisted correction, or None. Candidate 1 implements this."""
    return None


def decide(policy: str, proposal_kind: str, original: str, first: Admission,
           candidate: str | None) -> RepairDecision:
    if policy not in POLICIES:
        raise ValueError(f"unknown repair policy {policy!r}")
    if candidate is None:
        return RepairDecision(False, False, False, None, "no_candidate")
    if policy == "none":
        return RepairDecision(True, False, False, None, "policy_disabled")
    if first.category not in TRIGGERS.get(proposal_kind, frozenset()):
        return RepairDecision(True, False, False, None, "not_triggered")
    derived = correction(proposal_kind, original, first)
    if derived is None:
        return RepairDecision(True, True, False, None, "no_allowlisted_correction")
    return RepairDecision(True, True, False, derived[0], "candidate_differs")
