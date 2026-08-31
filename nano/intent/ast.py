"""Typed, source-positioned Intent AST."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Tuple

from .frozen import freeze_json, thaw_json
from .tokens import SourceSpan, Token

INTENT_FORMS = (
    "noun_phrase",
    "local_imperative",
    "factual_question",
    "temporal_question",
    "analytical_request",
    "compound_request",
    "external_action_request",
    "prohibited_or_unsupported",
    "ambiguous",
)


@dataclass(frozen=True)
class EntityNode:
    kind: str
    raw: str
    canonical: Any
    span: SourceSpan
    catalog: str
    resolved: bool = True
    specificity: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical", freeze_json(self.canonical))

    def to_dict(self, *, include_binding: bool = False) -> dict[str, Any]:
        result = {
            "kind": self.kind,
            "raw": self.raw,
            "canonical": thaw_json(self.canonical),
            "span": self.span.to_list(),
        }
        if include_binding:
            result.update(
                {
                    "catalog": self.catalog,
                    "resolved": self.resolved,
                    "specificity": self.specificity,
                }
            )
        return result


@dataclass(frozen=True)
class Relation:
    kind: str
    head: str
    dependent: str
    span: SourceSpan

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "head": self.head,
            "dependent": self.dependent,
            "span": self.span.to_list(),
        }


@dataclass(frozen=True)
class IntentClause:
    kind: str
    operator: str
    span: SourceSpan
    entity_indexes: Tuple[int, ...] = ()
    negated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "operator": self.operator,
            "span": self.span.to_list(),
            "entityIndexes": list(self.entity_indexes),
            "negated": self.negated,
        }


@dataclass(frozen=True)
class AlternateInterpretation:
    rank: int
    form: str
    operation: str
    confidence: float
    reason_codes: Tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "form": self.form,
            "operation": self.operation,
            "confidence": self.confidence,
            "reasonCodes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class IntentAST:
    source: str
    source_hash: str
    normalized: str
    tokens: Tuple[Token, ...]
    entities: Tuple[EntityNode, ...]
    relations: Tuple[Relation, ...]
    clauses: Tuple[IntentClause, ...]
    form: str
    frame: str
    operator: str
    confidence: float
    confidence_components: Tuple[Tuple[str, float], ...]
    reason_codes: Tuple[str, ...]
    warnings: Tuple[str, ...] = ()
    alternate_interpretations: Tuple[AlternateInterpretation, ...] = ()
    suppress_ui: bool = False

    def __post_init__(self) -> None:
        if self.form not in INTENT_FORMS:
            raise ValueError(f"unknown intent form {self.form!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceHash": self.source_hash,
            "normalized": self.normalized,
            "tokens": [token.to_dict() for token in self.tokens],
            "entities": [entity.to_dict(include_binding=True) for entity in self.entities],
            "relations": [relation.to_dict() for relation in self.relations],
            "clauses": [clause.to_dict() for clause in self.clauses],
            "form": self.form,
            "frame": self.frame,
            "operator": self.operator,
            "confidence": self.confidence,
            "confidenceComponents": dict(self.confidence_components),
            "reasonCodes": list(self.reason_codes),
            "warnings": list(self.warnings),
            "alternateInterpretations": [
                alternate.to_dict() for alternate in self.alternate_interpretations
            ],
            "suppressUi": self.suppress_ui,
        }


__all__ = [
    "AlternateInterpretation",
    "EntityNode",
    "INTENT_FORMS",
    "IntentAST",
    "IntentClause",
    "Relation",
]
