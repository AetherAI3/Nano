"""Public parse and compile pipeline for the Nano Intent sibling frontend."""

from __future__ import annotations

from typing import Any, Sequence

from .ast import IntentAST
from .entities import bind_entities
from .frames import parse_frame
from .frozen import thaw_json
from .ir import (
    NANO_INTENT_VERSION,
    ConfirmationPlan,
    IntentEntity,
    IntentIR,
    IntentStep,
    ResponsePlan,
    canonical_bytes,
    canonical_json,
)
from .lexicon import Catalog, CatalogBundle, default_catalogs
from .normalize import normalize
from .policy import plan_intent
from .receipts import build_receipt
from .tokenizer import tokenize


def _bundle(catalogs: CatalogBundle | Sequence[Catalog] | None) -> CatalogBundle:
    if catalogs is None:
        return default_catalogs()
    if isinstance(catalogs, CatalogBundle):
        return catalogs
    return CatalogBundle(tuple(catalogs))


def parse_intent(
    source: str, *, catalogs: CatalogBundle | Sequence[Catalog] | None = None
) -> IntentAST:
    """Normalize, tokenize, bind, and parse source into a typed Intent AST."""

    bundle = _bundle(catalogs)
    normalized = normalize(source)
    tokens = tokenize(normalized.value, bundle)
    entities = bind_entities(normalized.value, tokens, bundle)
    return parse_frame(
        source=normalized.source,
        source_hash=normalized.source_hash,
        normalized=normalized.value,
        tokens=tokens,
        entities=entities,
        catalogs=bundle,
    )


def compile_intent(
    source: str, *, catalogs: CatalogBundle | Sequence[Catalog] | None = None
) -> IntentIR:
    """Compile source into immutable, host-neutral Nano Intent IR 0.1.0."""

    bundle = _bundle(catalogs)
    ast = parse_intent(source, catalogs=bundle)
    plan = plan_intent(ast, bundle)
    receipt = build_receipt(ast, plan, bundle)
    return IntentIR(
        nano_intent_version=NANO_INTENT_VERSION,
        source_hash=ast.source_hash,
        normalized=ast.normalized,
        form=ast.form,
        confidence=ast.confidence,
        entities=tuple(
            IntentEntity(
                entity.kind,
                entity.raw,
                entity.canonical,
                (entity.span.start, entity.span.end),
            )
            for entity in ast.entities
        ),
        operation=plan.operation,
        response=ResponsePlan(plan.response_mode, plan.budget_class),
        steps=tuple(
            IntentStep(step.kind, step.capability, step.args) for step in plan.steps
        ),
        effects=plan.effects,
        confirmation=ConfirmationPlan(
            plan.confirmation_mode, plan.confirmation_reason
        ),
        receipt=receipt,
    )


def compile_to_dict(
    source: str, *, catalogs: CatalogBundle | Sequence[Catalog] | None = None
) -> dict[str, Any]:
    return compile_intent(source, catalogs=catalogs).to_dict()


def explain_intent(
    source: str, *, catalogs: CatalogBundle | Sequence[Catalog] | None = None
) -> str:
    """Render a stable, human-readable explanation of a compiled plan."""

    ir = compile_intent(source, catalogs=catalogs)
    receipt = ir.receipt
    lines = [
        f"normalized: {ir.normalized}",
        f"frame: {receipt.frame}",
        f"form: {ir.form}",
        f"confidence: {ir.confidence:.2f}",
        "entities:",
    ]
    if ir.entities:
        for entity in ir.entities:
            lines.append(
                f"  {entity.kind} {entity.raw!r} -> {thaw_json(entity.canonical)!r} "
                f"[{entity.span[0]}, {entity.span[1]}]"
            )
    else:
        lines.append("  (none)")
    lines.extend(
        (
            f"operation: {ir.operation}",
            f"response: {ir.response.mode} ({ir.response.budget_class})",
            f"effects: {', '.join(ir.effects) if ir.effects else '(none)'}",
            f"confirmation: {ir.confirmation.mode} - {ir.confirmation.reason}",
            f"reason codes: {', '.join(receipt.reason_codes)}",
        )
    )
    if ir.steps:
        lines.append("steps:")
        for index, step in enumerate(ir.steps, 1):
            lines.append(f"  {index}. {step.kind} -> {step.capability}")
    if receipt.warnings:
        lines.append("warnings:")
        lines.extend(f"  {warning}" for warning in receipt.warnings)
    if receipt.alternate_interpretations:
        lines.append("alternate interpretations:")
        for alternate in receipt.alternate_interpretations:
            lines.append(
                f"  {alternate['rank']}. {alternate['form']} / "
                f"{alternate['operation']} ({alternate['confidence']:.2f})"
            )
    return "\n".join(lines)


__all__ = [
    "canonical_bytes",
    "canonical_json",
    "compile_intent",
    "compile_to_dict",
    "explain_intent",
    "parse_intent",
]
