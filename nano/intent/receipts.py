"""Explainability receipt construction for deterministic Intent compilation."""

from __future__ import annotations

from .ast import IntentAST
from .ir import INTENT_COMPILER_NAME, INTENT_COMPILER_VERSION, IntentReceipt
from .lexicon import CatalogBundle
from .policy import IntentPlan


def build_receipt(
    ast: IntentAST, plan: IntentPlan, catalogs: CatalogBundle
) -> IntentReceipt:
    unresolved = [
        entity.to_dict(include_binding=True)
        for entity in ast.entities
        if not entity.resolved
    ]
    return IntentReceipt(
        frame=ast.frame,
        reason_codes=plan.reason_codes,
        compiler={
            "name": INTENT_COMPILER_NAME,
            "version": INTENT_COMPILER_VERSION,
        },
        catalogs=catalogs.catalog_receipt(),
        source_hash=ast.source_hash,
        normalized=ast.normalized,
        tokens=[token.to_dict() for token in ast.tokens],
        entities=[entity.to_dict(include_binding=True) for entity in ast.entities],
        relations=[relation.to_dict() for relation in ast.relations],
        confidence_components=dict(ast.confidence_components),
        operation=plan.operation,
        response={"mode": plan.response_mode, "budgetClass": plan.budget_class},
        effects=plan.effects,
        confirmation={
            "mode": plan.confirmation_mode,
            "reason": plan.confirmation_reason,
        },
        warnings=ast.warnings,
        unresolved_entities=unresolved,
        alternate_interpretations=[
            item.to_dict() for item in ast.alternate_interpretations
        ],
    )


__all__ = ["build_receipt"]
