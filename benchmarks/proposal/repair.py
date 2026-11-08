"""Bounded repair gate for recorded candidate proposals (contract: policy.json).

At most one recorded candidate per case is considered. It is accepted only if
it equals the single correction that the allowlisted policy derives from the
first proposal alone; the policy never reads task expectations or hints.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nano.fuzzing.diagnostic_repair import run_repair_fixture

from .decode import DecodeError, strict_json
from .pipeline import Admission, diagnostic_envelope
from .verifier import CONSTANTS, strict_equal

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


def _trailing_commas(text: str) -> list[int]:
    """Offsets of commas outside strings followed only by whitespace and a closer."""
    sites, in_string, escaped = [], False, False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == ",":
            rest = text[index + 1:].lstrip(" \t\r\n")
            if rest[:1] in ("}", "]"):
                sites.append(index)
    return sites


def _decoded_object(text: str) -> dict[str, Any] | None:
    try:
        value = strict_json(text)
    except DecodeError:
        return None
    return value if isinstance(value, dict) else None


def _json_syntax(text: str) -> tuple[str, Any] | None:
    sites = _trailing_commas(text)
    if len(sites) == 1:
        repaired = _decoded_object(text[:sites[0]] + text[sites[0] + 1:])
        return ("json_trailing_comma", repaired) if repaired is not None else None
    if not sites:
        repaired = _decoded_object(text + "}")
        return ("json_unclosed_object", repaired) if repaired is not None else None
    return None


def _constant_field(text: str) -> tuple[str, Any] | None:
    document = _decoded_object(text)
    if document is None:
        return None
    missing = [key for key in CONSTANTS if key not in document]
    if len(missing) != 1:
        return None
    return "constant_field", {**document, missing[0]: CONSTANTS[missing[0]]}


def _strategy_rparen(source: str) -> tuple[str, Any] | None:
    # The #65 fixture policy; its task oracle is neutralised so no expectation is read here.
    result = run_repair_fixture(source, diagnostic_envelope, task_oracle=lambda _source: False)
    return ("nano_missing_rparen", result["finalSource"]) if result["corrections"] == 1 else None


def correction(proposal_kind: str, original: str, first: Admission) -> tuple[str, Any] | None:
    """Derive the single allowlisted correction from the first proposal alone, or None."""
    if proposal_kind == "quantum_command_json" and first.category == "json_syntax":
        return _json_syntax(original)
    if proposal_kind == "quantum_command_json" and first.category == "schema_mismatch":
        return _constant_field(original)
    if proposal_kind == "nano_strategy_source" and first.category == "SYNTAX_ERROR":
        return _strategy_rparen(original)
    return None


def _matches(proposal_kind: str, derived: Any, candidate: str) -> bool:
    if proposal_kind == "nano_strategy_source":
        return candidate == derived
    repaired = _decoded_object(candidate)
    return repaired is not None and strict_equal(repaired, derived)


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
    if not _matches(proposal_kind, derived[1], candidate):
        return RepairDecision(True, True, False, derived[0], "candidate_differs")
    return RepairDecision(True, True, True, derived[0], "accepted")
