"""Host-neutral response, capability, effect, and consent planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence, Tuple

from .ast import EntityNode, IntentAST
from .frozen import FrozenMap, thaw_json
from .lexicon import CatalogBundle

EFFECT_ORDER = (
    "ui.open",
    "ui.focus",
    "ui.navigate",
    "data.read",
    "llm.call",
    "external.write",
    "trade.request",
    "log.append",
)
ALLOWED_EFFECTS = frozenset(EFFECT_ORDER)
RESPONSE_MODES = frozenset(
    {
        "tile",
        "compact_answer",
        "agent_thread",
        "confirmation_preview",
        "refusal",
        "interpretations",
    }
)
BUDGET_CLASSES = frozenset({"none", "micro", "standard", "research"})
CONFIRMATION_MODES = frozenset(
    {"none", "submit_is_consent", "explicit_confirmation", "host_decides"}
)


@dataclass(frozen=True)
class PlannedStep:
    kind: str
    capability: str
    args: FrozenMap

    def __init__(self, kind: str, capability: str, args: dict[str, Any]) -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "args", FrozenMap(args))


@dataclass(frozen=True)
class IntentPlan:
    operation: str
    response_mode: str
    budget_class: str
    steps: Tuple[PlannedStep, ...]
    effects: Tuple[str, ...]
    confirmation_mode: str
    confirmation_reason: str
    reason_codes: Tuple[str, ...]


def _entity_pool(
    ast: IntentAST, entity_indexes: Sequence[int] | None = None
) -> list[EntityNode]:
    if entity_indexes is None:
        return list(ast.entities)
    return [ast.entities[index] for index in entity_indexes]


def _entities(
    ast: IntentAST,
    kind: str,
    entity_indexes: Sequence[int] | None = None,
) -> list[EntityNode]:
    return [
        entity
        for entity in _entity_pool(ast, entity_indexes)
        if entity.kind == kind
    ]


def _symbols(
    ast: IntentAST, entity_indexes: Sequence[int] | None = None
) -> list[str]:
    return [
        str(entity.canonical)
        for entity in _entities(ast, "security", entity_indexes)
    ]


def _topic(
    ast: IntentAST, entity_indexes: Sequence[int] | None = None
) -> str | None:
    topics = _entities(ast, "topic", entity_indexes)
    return str(topics[0].canonical) if topics else None


def _horizon_args(
    ast: IntentAST, entity_indexes: Sequence[int] | None = None
) -> dict[str, Any]:
    horizons = _entities(ast, "horizon", entity_indexes)
    if not horizons:
        return {}
    canonical = thaw_json(horizons[0].canonical)
    return canonical if isinstance(canonical, dict) else {"horizon": canonical}


def _capability(catalogs: CatalogBundle, key: str, fallback: str) -> str:
    return catalogs.capability(key) or fallback


def _ui_step(
    ast: IntentAST,
    catalogs: CatalogBundle,
    *,
    entity_indexes: Sequence[int] | None = None,
    action_override: str | None = None,
) -> PlannedStep:
    topic = _topic(ast, entity_indexes) or "workspace"
    symbols = _symbols(ast, entity_indexes)
    compare = len(symbols) >= 2
    action = action_override or ast.operator
    if ast.form == "compound_request":
        local_clause = next(
            (clause for clause in ast.clauses if clause.kind == "local_action"),
            None,
        )
        action = local_clause.operator if local_clause is not None else "open"
    kind = {
        "focus": "ui.focus",
        "switch": "ui.navigate",
        "add": "ui.navigate",
        "remove": "ui.navigate",
    }.get(action, "ui.open")
    key = f"ui.{topic}.compare" if compare else f"ui.{topic}"
    if action in ("add", "remove"):
        fallback = f"terminal.{topic}.{action}"
    elif action == "switch":
        fallback = f"terminal.workspace.switch.{topic}"
    else:
        fallback = (
            f"terminal.tile.{topic}_comparison"
            if compare
            else f"terminal.tile.{topic}"
        )
    args: dict[str, Any] = {"topic": topic}
    if compare:
        args["symbols"] = symbols
    elif symbols:
        args["symbol"] = symbols[0]
    if action not in ("open", "compare"):
        args["action"] = action
    return PlannedStep(kind, _capability(catalogs, key, fallback), args)


def _read_step(
    ast: IntentAST,
    catalogs: CatalogBundle,
    *,
    entity_indexes: Sequence[int] | None = None,
) -> PlannedStep:
    topic = _topic(ast, entity_indexes)
    symbols = _symbols(ast, entity_indexes)
    if topic is None and entity_indexes is not None:
        topic = _topic(ast)
    if not symbols and entity_indexes is not None:
        symbols = _symbols(ast)
    topic = topic or "market"
    horizon = _horizon_args(ast, entity_indexes)
    if topic == "earnings" and ast.form == "temporal_question":
        capability = _capability(
            catalogs, "data.earnings.calendar", "market.earnings.calendar"
        )
    elif topic == "volatility" and len(symbols) >= 2:
        capability = _capability(
            catalogs, "data.volatility.compare", "market.volatility.compare"
        )
    elif topic == "chart" or any(
        entity.raw in ("today", "since open")
        for entity in _entity_pool(ast, entity_indexes)
    ):
        capability = _capability(
            catalogs, "data.price.context", "market.price.context"
        )
    else:
        capability = f"market.{topic}.context"
    args: dict[str, Any] = {}
    if len(symbols) >= 2:
        args["symbols"] = symbols
    elif symbols:
        args["symbol"] = symbols[0]
    args.update(horizon)
    return PlannedStep("data.read", capability, args)


def _forecast_steps(ast: IntentAST, catalogs: CatalogBundle) -> list[PlannedStep]:
    symbols = _symbols(ast)
    base = {"symbol": symbols[0]} if symbols else {}
    horizon = _horizon_args(ast)
    return [
        PlannedStep(
            "data.read",
            _capability(
                catalogs, "data.earnings.history", "market.earnings.history"
            ),
            base,
        ),
        PlannedStep(
            "data.read",
            _capability(
                catalogs, "data.earnings.estimates", "market.earnings.estimates"
            ),
            {**base, **horizon},
        ),
    ]


def _llm_step(style: str) -> PlannedStep:
    return PlannedStep(
        "llm.call",
        "fast_nonthinking",
        {"answerStyle": style},
    )


def _effects(steps: Sequence[PlannedStep]) -> Tuple[str, ...]:
    present = {step.kind for step in steps}
    return tuple(effect for effect in EFFECT_ORDER if effect in present)


def plan_intent(ast: IntentAST, catalogs: CatalogBundle) -> IntentPlan:
    """Compile an AST into proposed host capabilities, never performing them."""

    steps: list[PlannedStep] = []
    reasons = list(ast.reason_codes)
    operation = ast.operator
    response_mode = "interpretations"
    budget = "none"
    confirmation_mode = "none"
    confirmation_reason = "no_consent_required"

    if ast.form == "ambiguous":
        operation = "interpret"
        confirmation_reason = "ambiguous_input"
        reasons.append("BUDGET_NONE_AMBIGUOUS")
    elif ast.form == "prohibited_or_unsupported":
        symbols = _symbols(ast)
        args = {"symbol": symbols[0]} if symbols else {}
        steps.append(PlannedStep("trade.request", "market.trade.request", args))
        operation = "trade_request"
        response_mode = "refusal"
        confirmation_mode = "host_decides"
        confirmation_reason = "host_policy_controls_trade_requests"
        reasons.extend(("NO_MODEL_FOR_REFUSAL", "BUDGET_NONE_PROHIBITED"))
    elif ast.form == "external_action_request":
        steps.append(
            PlannedStep(
                "external.write",
                f"host.external.{ast.operator}",
                {"action": ast.operator, "normalizedRequest": ast.normalized},
            )
        )
        response_mode = "confirmation_preview"
        confirmation_mode = "explicit_confirmation"
        confirmation_reason = "consequential_external_write"
        reasons.extend(("EXPLICIT_CONFIRMATION_REQUIRED", "BUDGET_NONE_PREVIEW"))
    elif ast.form in ("noun_phrase", "local_imperative"):
        steps.append(_ui_step(ast, catalogs))
        operation = "compare" if len(_symbols(ast)) >= 2 else ast.operator
        response_mode = "tile"
        reasons.extend(("LOCAL_CAPABILITY_ONLY", "BUDGET_NONE_LOCAL"))
    elif ast.form in ("temporal_question", "factual_question"):
        steps.extend((_read_step(ast, catalogs), _llm_step("structured_micro")))
        operation = "lookup"
        response_mode = "compact_answer"
        budget = "micro"
        confirmation_mode = "submit_is_consent"
        confirmation_reason = "clear_question"
        reasons.extend(("LLM_REQUIRED_FOR_LANGUAGE_RESPONSE", "BUDGET_MICRO_STRUCTURED"))
    elif ast.form == "compound_request":
        local_clauses = [
            clause
            for clause in ast.clauses
            if clause.kind == "local_action" and not clause.negated
        ]
        analysis_clause = next(
            (clause for clause in ast.clauses if clause.kind == "analysis"),
            None,
        )
        if ast.frame == "ordered_local_actions":
            steps.extend(
                _ui_step(
                    ast,
                    catalogs,
                    entity_indexes=clause.entity_indexes,
                    action_override=clause.operator,
                )
                for clause in local_clauses
            )
            operation = "compound"
            response_mode = "tile"
            reasons.extend(("LOCAL_CAPABILITY_ONLY", "BUDGET_NONE_LOCAL"))
            return IntentPlan(
                operation=operation,
                response_mode=response_mode,
                budget_class=budget,
                steps=tuple(steps),
                effects=_effects(steps),
                confirmation_mode=confirmation_mode,
                confirmation_reason=confirmation_reason,
                reason_codes=tuple(dict.fromkeys(reasons)),
            )
        if not ast.suppress_ui and local_clauses:
            local_clause = local_clauses[0]
            steps.append(
                _ui_step(
                    ast,
                    catalogs,
                    entity_indexes=local_clause.entity_indexes,
                    action_override=local_clause.operator,
                )
            )
        steps.extend(
            (
                _read_step(
                    ast,
                    catalogs,
                    entity_indexes=(
                        analysis_clause.entity_indexes
                        if analysis_clause is not None
                        else None
                    ),
                ),
                _llm_step("market_analysis"),
            )
        )
        operation = "compound"
        response_mode = "compact_answer"
        budget = "standard"
        confirmation_mode = "submit_is_consent"
        confirmation_reason = "clear_compound_question"
        reasons.extend(("ONE_COALESCED_LLM_CALL", "BUDGET_STANDARD_ANALYSIS"))
    elif ast.form == "analytical_request":
        if ast.operator == "forecast":
            steps.extend(_forecast_steps(ast, catalogs))
            steps.append(_llm_step("market_research"))
            operation = "forecast"
            response_mode = "compact_answer"
            budget = "research"
            reasons.extend(("LLM_REQUIRED_FOR_FORECAST_SYNTHESIS", "BUDGET_RESEARCH_FORECAST"))
        else:
            steps.extend((_read_step(ast, catalogs), _llm_step("market_analysis")))
            operation = "compare" if ast.operator == "compare" else "explain"
            response_mode = "compact_answer"
            budget = "standard"
            reasons.extend(("LLM_REQUIRED_FOR_ANALYSIS", "BUDGET_STANDARD_ANALYSIS"))
        confirmation_mode = "submit_is_consent"
        confirmation_reason = "clear_question"
    else:  # defensive: IntentAST already pins every known form
        raise ValueError(f"cannot plan intent form {ast.form!r}")

    effects = _effects(steps)
    if len([step for step in steps if step.kind == "llm.call"]) > 1:
        raise ValueError("Nano Intent 0.1 permits at most one llm.call")
    return IntentPlan(
        operation=operation,
        response_mode=response_mode,
        budget_class=budget,
        steps=tuple(steps),
        effects=effects,
        confirmation_mode=confirmation_mode,
        confirmation_reason=confirmation_reason,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


__all__ = [
    "ALLOWED_EFFECTS",
    "BUDGET_CLASSES",
    "CONFIRMATION_MODES",
    "EFFECT_ORDER",
    "IntentPlan",
    "PlannedStep",
    "RESPONSE_MODES",
    "plan_intent",
]
