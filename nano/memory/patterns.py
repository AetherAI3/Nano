"""Standalone pattern-retrieval primitive.

A PatternStore matches caller-provided observations against stored conditions
and returns context plus an ``escalate`` flag. It does not invoke a model and
is not wired into the core compiler, interpreter, or bridge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from ..ir.nodes import ConditionNode


@dataclass(frozen=True)
class Pattern:
    name: str
    conditions: Tuple[ConditionNode, ...]
    context: Tuple[str, ...]
    confidence: float
    win_rate: Optional[float] = None
    expires_at: Optional[int] = None
    provenance: str = "manual"

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "Pattern":
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Pattern requires a non-empty 'name'")
        conditions_raw = data.get("conditions")
        if not isinstance(conditions_raw, list) or not conditions_raw:
            raise ValueError("Pattern requires a non-empty 'conditions' list")
        conditions = tuple(ConditionNode.from_dict(c) for c in conditions_raw)
        confidence = data.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise ValueError("Pattern 'confidence' must be numeric within [0, 1]")
        win_rate = data.get("win_rate")
        if win_rate is not None and not 0.0 <= float(win_rate) <= 1.0:
            raise ValueError("Pattern 'win_rate' must be within [0, 1]")
        return Pattern(
            name=name,
            conditions=conditions,
            context=tuple(data.get("context", ())),
            confidence=float(confidence),
            win_rate=None if win_rate is None else float(win_rate),
            expires_at=data.get("expires_at"),
            provenance=data.get("provenance", "manual"),
        )

    def is_live(self, now: int) -> bool:
        return self.expires_at is None or now < self.expires_at

    def matches(self, observations: Mapping[str, float]) -> bool:
        for condition in self.conditions:
            if condition.signal not in observations:
                return False
            if not condition.evaluate(float(observations[condition.signal])):
                return False
        return True

    def to_dict(self) -> dict:
        return {
            "pattern": self.name,
            "context": list(self.context),
            "confidence": self.confidence,
            "win_rate": self.win_rate,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class RetrievalResult:
    matched: Tuple[Pattern, ...]
    escalate: bool

    def to_dict(self) -> dict:
        return {
            "matched": [p.to_dict() for p in self.matched],
            "escalate": self.escalate,
        }


@dataclass(frozen=True)
class PatternStore:
    patterns: Tuple[Pattern, ...]

    @staticmethod
    def from_dicts(items: Sequence[Mapping[str, Any]]) -> "PatternStore":
        return PatternStore(patterns=tuple(Pattern.from_dict(i) for i in items))

    def retrieve(
        self,
        observations: Mapping[str, float],
        *,
        now: int,
        min_confidence: float = 0.0,
    ) -> RetrievalResult:
        """Known pattern -> context; no match -> an escalation flag for the caller."""
        matched = tuple(
            p
            for p in self.patterns
            if p.is_live(now) and p.confidence >= min_confidence and p.matches(observations)
        )
        return RetrievalResult(matched=matched, escalate=not matched)
