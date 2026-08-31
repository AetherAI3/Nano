"""Deterministic sentence-frame parsing over bound tokens and entities."""

from __future__ import annotations

from typing import Sequence, Tuple

from .ast import (
    AlternateInterpretation,
    EntityNode,
    IntentAST,
    IntentClause,
    Relation,
)
from .lexicon import CatalogBundle, suggestions
from .tokens import SourceSpan, Token


def _has_role(token: Token, role: str) -> bool:
    return role in token.roles


def _canonical(token: Token, role: str, catalogs: CatalogBundle) -> str:
    matches = catalogs.lookup(token.text, role)
    return matches[0].canonical if matches else token.text


def _first(tokens: Sequence[Token], role: str) -> int | None:
    return next((index for index, token in enumerate(tokens) if _has_role(token, role)), None)


def _entities_of(entities: Sequence[EntityNode], kind: str) -> list[EntityNode]:
    return [entity for entity in entities if entity.kind == kind]


def _clause_span(tokens: Sequence[Token], start: int, end: int | None = None) -> SourceSpan:
    if not tokens:
        return SourceSpan(0, 0)
    final = len(tokens) if end is None else max(start + 1, end)
    return SourceSpan(tokens[start].span.start, tokens[final - 1].span.end)


def _warnings(
    tokens: Sequence[Token],
    entities: Sequence[EntityNode],
    catalogs: CatalogBundle,
) -> Tuple[str, ...]:
    occupied = {
        index
        for index, token in enumerate(tokens)
        if any(
            token.span.start < entity.span.end and entity.span.start < token.span.end
            for entity in entities
        )
    }
    found: list[str] = []
    for index, token in enumerate(tokens):
        if index in occupied or token.kind != "word" or token.roles:
            continue
        candidates = suggestions(token.text, catalogs)
        if candidates:
            found.append(
                f"UNRESOLVED_WORD:{token.text}:SUGGESTIONS={','.join(candidates)}"
            )
    for entity in entities:
        if entity.kind == "security" and not entity.resolved:
            found.append(f"HOST_SECURITY_VALIDATION_REQUIRED:{entity.canonical}")
    return tuple(found)


def _relations(
    entities: Sequence[EntityNode],
    *,
    form: str,
    normalized_length: int,
    clauses: Sequence[IntentClause],
) -> Tuple[Relation, ...]:
    groups: list[list[EntityNode]] = []
    if form == "compound_request" and clauses:
        for clause in clauses:
            selected = [entities[index] for index in clause.entity_indexes]
            if selected:
                groups.append(selected)
    if not groups:
        groups = [list(entities)]

    horizons = [
        entity for entity in entities if entity.kind in ("horizon", "time")
    ]
    found: list[Relation] = []
    for group in groups:
        securities = _entities_of(group, "security")
        topics = _entities_of(group, "topic")
        for security in securities:
            for topic in topics:
                found.append(
                    Relation(
                        "subject_topic",
                        str(security.canonical),
                        str(topic.canonical),
                        SourceSpan(
                            min(security.span.start, topic.span.start),
                            max(security.span.end, topic.span.end),
                        ),
                    )
                )
        if len(securities) >= 2:
            found.append(
                Relation(
                    "compared_with",
                    str(securities[0].canonical),
                    str(securities[1].canonical),
                    SourceSpan(securities[0].span.start, securities[1].span.end),
                )
            )
    for horizon in horizons:
        found.append(
            Relation(
                "bounded_by",
                "request",
                horizon.raw,
                horizon.span,
            )
        )
    if form in ("factual_question", "temporal_question", "analytical_request"):
        found.append(
            Relation(
                "question_targets",
                form,
                str(topics[0].canonical) if topics else "subject",
                SourceSpan(0, normalized_length),
            )
        )
    return tuple(found)


def parse_frame(
    *,
    source: str,
    source_hash: str,
    normalized: str,
    tokens: Sequence[Token],
    entities: Sequence[EntityNode],
    catalogs: CatalogBundle,
) -> IntentAST:
    """Build a typed AST from grammatical roles and structural relations."""

    token_tuple = tuple(tokens)
    entity_tuple = tuple(entities)
    securities = _entities_of(entities, "security")
    topics = _entities_of(entities, "topic")
    horizons = _entities_of(entities, "horizon")
    lexical_warnings = _warnings(tokens, entities, catalogs)
    has_unresolved_word = any(
        token.kind == "word"
        and not token.roles
        and not any(
            token.span.start < entity.span.end and entity.span.start < token.span.end
            for entity in entities
        )
        for token in tokens
    )

    local_index = _first(tokens, "imperative")
    interrogative_index = _first(tokens, "interrogative")
    analytical_index = _first(tokens, "analytical")
    external_index = _first(tokens, "external_action")
    trade_index = _first(tokens, "trade")
    conjunction_index = _first(tokens, "conjunction")
    negation_index = _first(tokens, "negation")

    suppress_ui = bool(
        local_index is not None
        and negation_index is not None
        and negation_index < local_index
        and analytical_index is not None
        and analytical_index > local_index
    )
    left_entity_indexes: Tuple[int, ...] = ()
    right_entity_indexes: Tuple[int, ...] = ()
    if conjunction_index is not None:
        conjunction = tokens[conjunction_index].span
        left_entity_indexes = tuple(
            index
            for index, entity in enumerate(entities)
            if entity.span.end <= conjunction.start
        )
        right_entity_indexes = tuple(
            index
            for index, entity in enumerate(entities)
            if entity.span.start >= conjunction.end
        )

    form = "ambiguous"
    frame = "unresolved_fragment"
    operator = "interpret"
    reasons: list[str] = []
    clauses: list[IntentClause] = []

    if trade_index is not None:
        form = "prohibited_or_unsupported"
        frame = "trade_request"
        operator = "trade_request"
        reasons = ["TRADE_VERB", "HOST_GOVERNED_PROHIBITION"]
        clauses.append(
            IntentClause(
                "prohibited_action",
                _canonical(tokens[trade_index], "trade", catalogs),
                _clause_span(tokens, trade_index),
                tuple(range(len(entities))),
            )
        )
    elif external_index is not None and (
        external_index == 0 or all("filler" in token.roles for token in tokens[:external_index])
    ):
        form = "external_action_request"
        frame = "consequential_external_action"
        operator = _canonical(tokens[external_index], "external_action", catalogs)
        reasons = ["CONSEQUENTIAL_VERB", "EXTERNAL_WRITE_BOUNDARY"]
        clauses.append(
            IntentClause(
                "external_action",
                operator,
                _clause_span(tokens, external_index),
                tuple(range(len(entities))),
            )
        )
    elif suppress_ui:
        form = "analytical_request"
        frame = "negated_local_then_analysis"
        operator = _canonical(tokens[analytical_index], "analytical", catalogs)
        reasons = ["EXPLICIT_NEGATION", "UI_STEP_SUPPRESSED", "ANALYTICAL_VERB"]
        clauses.extend(
            (
                IntentClause(
                    "local_action",
                    _canonical(tokens[local_index], "imperative", catalogs),
                    _clause_span(tokens, local_index, analytical_index),
                    tuple(range(len(entities))),
                    negated=True,
                ),
                IntentClause(
                    "analysis",
                    operator,
                    _clause_span(tokens, analytical_index),
                    tuple(range(len(entities))),
                ),
            )
        )
    elif (
        local_index is not None
        and analytical_index is None
        and conjunction_index is not None
        and left_entity_indexes
        and right_entity_indexes
        and any(entities[index].kind == "topic" for index in left_entity_indexes)
        and any(entities[index].kind == "topic" for index in right_entity_indexes)
    ):
        form = "compound_request"
        frame = "ordered_local_actions"
        operator = "compound_local"
        local_operator = _canonical(tokens[local_index], "imperative", catalogs)
        reasons = ["LOCAL_IMPERATIVE", "ORDERED_CONJUNCTION", "MULTIPLE_LOCAL_ACTIONS"]
        clauses.extend(
            (
                IntentClause(
                    "local_action",
                    local_operator,
                    _clause_span(tokens, local_index, conjunction_index),
                    left_entity_indexes,
                ),
                IntentClause(
                    "local_action",
                    local_operator,
                    _clause_span(tokens, conjunction_index + 1),
                    right_entity_indexes,
                ),
            )
        )
    elif (
        local_index is not None
        and analytical_index is not None
        and conjunction_index is not None
        and local_index < conjunction_index < analytical_index
    ):
        form = "compound_request"
        frame = "ordered_local_analysis"
        operator = "compound"
        reasons = ["LOCAL_IMPERATIVE", "ORDERED_CONJUNCTION", "ANALYTICAL_VERB"]
        clauses.extend(
            (
                IntentClause(
                    "local_action",
                    _canonical(tokens[local_index], "imperative", catalogs),
                    _clause_span(tokens, local_index, conjunction_index),
                    left_entity_indexes,
                ),
                IntentClause(
                    "analysis",
                    _canonical(tokens[analytical_index], "analytical", catalogs),
                    _clause_span(tokens, analytical_index),
                    right_entity_indexes,
                ),
            )
        )
    elif local_index is not None and (
        local_index == 0 or all("filler" in token.roles for token in tokens[:local_index])
    ):
        form = "local_imperative"
        frame = "security_topic_local_imperative"
        operator = _canonical(tokens[local_index], "imperative", catalogs)
        reasons = ["LOCAL_IMPERATIVE"]
        clauses.append(
            IntentClause(
                "local_action",
                operator,
                _clause_span(tokens, local_index),
                tuple(range(len(entities))),
            )
        )
    elif interrogative_index is not None:
        question = _canonical(tokens[interrogative_index], "interrogative", catalogs)
        if question == "when":
            form = "temporal_question"
            frame = "security_topic_temporal"
            operator = "lookup"
            reasons = ["TEMPORAL_INTERROGATIVE"]
        elif question in ("why", "how"):
            form = "analytical_request"
            operator = "compare" if len(securities) >= 2 or any("comparison" in token.roles for token in tokens) else "explain"
            frame = "security_topic_comparison" if operator == "compare" else "security_topic_analysis"
            reasons = ["CAUSAL_INTERROGATIVE", "COMPARISON_RELATION"] if operator == "compare" else ["CAUSAL_INTERROGATIVE"]
        else:
            form = "factual_question"
            frame = "security_topic_factual"
            operator = "lookup"
            reasons = ["FACTUAL_INTERROGATIVE"]
        clauses.append(
            IntentClause(
                "question",
                question,
                _clause_span(tokens, interrogative_index),
                tuple(range(len(entities))),
            )
        )
    elif analytical_index is not None:
        analytical = _canonical(tokens[analytical_index], "analytical", catalogs)
        forecast = analytical == "forecast" or any(
            _canonical(token, "analytical", catalogs) == "forecast"
            for token in tokens
            if "analytical" in token.roles
        )
        comparison = len(securities) >= 2 or any(
            "comparison" in token.roles for token in tokens
        )
        form = "analytical_request"
        if forecast:
            frame = "security_topic_forecast"
            operator = "forecast"
            reasons = ["ANALYTICAL_VERB", "FORECAST_TOPIC"]
            if horizons:
                reasons.append("EXPLICIT_HORIZON")
        elif comparison:
            frame = "security_topic_comparison"
            operator = "compare"
            reasons = ["ANALYTICAL_VERB", "COMPARISON_RELATION"]
        else:
            frame = "security_topic_analysis"
            operator = analytical
            reasons = ["ANALYTICAL_VERB"]
        clauses.append(
            IntentClause(
                "analysis",
                operator,
                _clause_span(tokens, analytical_index),
                tuple(range(len(entities))),
            )
        )
    elif topics and securities and not has_unresolved_word:
        form = "noun_phrase"
        if len(securities) >= 2:
            frame = "security_comparison_noun_phrase"
            operator = "compare"
            reasons = ["NOUN_PHRASE", "MULTIPLE_SECURITIES", "TOPIC"]
        else:
            frame = "security_topic_noun_phrase"
            operator = "open"
            reasons = ["NOUN_PHRASE", "SECURITY", "TOPIC"]
        clauses.append(
            IntentClause(
                "noun_phrase",
                operator,
                SourceSpan(0, len(normalized)),
                tuple(range(len(entities))),
            )
        )

    if topics:
        reasons.append("MARKET_TOPIC")
    if securities and "SECURITY" not in reasons:
        reasons.append("SECURITY")

    confidence_by_form = {
        "noun_phrase": 0.98,
        "local_imperative": 0.99,
        "factual_question": 0.96,
        "temporal_question": 0.97,
        "analytical_request": 0.98 if operator == "forecast" else 0.96,
        "compound_request": 0.97,
        "external_action_request": 0.99,
        "prohibited_or_unsupported": 1.0,
        "ambiguous": 0.35,
    }
    confidence = confidence_by_form[form]
    lexical = 1.0 if not lexical_warnings else 0.75
    structure = 0.35 if form == "ambiguous" else 0.98
    binding = 1.0 if topics and securities else (0.8 if entities else 0.25)

    alternates: list[AlternateInterpretation] = []
    if form == "ambiguous":
        candidate_words = [
            candidate
            for token in tokens
            if token.kind == "word" and not token.roles
            for candidate in suggestions(token.text, catalogs)
        ]
        if candidate_words:
            alternates.append(
                AlternateInterpretation(
                    1,
                    "local_imperative",
                    "open",
                    0.42,
                    ("FUZZY_ORDINARY_VOCABULARY", f"SUGGESTED_{candidate_words[0].upper()}"),
                )
            )
        if securities:
            alternates.append(
                AlternateInterpretation(
                    len(alternates) + 1,
                    "noun_phrase",
                    "choose_topic",
                    0.4,
                    ("SECURITY_WITHOUT_TOPIC",),
                )
            )
        alternates.append(
            AlternateInterpretation(
                len(alternates) + 1,
                "ambiguous",
                "request_clarification",
                0.3,
                ("INSUFFICIENT_STRUCTURE",),
            )
        )
        reasons = ["AMBIGUOUS_INPUT", "NO_EXECUTABLE_FRAME"]

    relations = list(
        _relations(
            entities,
            form=form,
            normalized_length=len(normalized),
            clauses=clauses,
        )
    )
    if form == "compound_request":
        local_only = frame == "ordered_local_actions"
        relations.append(
            Relation(
                "precedes",
                "local_action_1" if local_only else "local_action",
                "local_action_2" if local_only else "analysis",
                SourceSpan(0, len(normalized)),
            )
        )

    return IntentAST(
        source=source,
        source_hash=source_hash,
        normalized=normalized,
        tokens=token_tuple,
        entities=entity_tuple,
        relations=tuple(relations),
        clauses=tuple(clauses),
        form=form,
        frame=frame,
        operator=operator,
        confidence=confidence,
        confidence_components=(
            ("binding", round(binding, 2)),
            ("lexical", round(lexical, 2)),
            ("structure", round(structure, 2)),
        ),
        reason_codes=tuple(dict.fromkeys(reasons)),
        warnings=lexical_warnings,
        alternate_interpretations=tuple(alternates),
        suppress_ui=suppress_ui,
    )


__all__ = ["parse_frame"]
