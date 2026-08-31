"""Deterministic entity and relation binding for market intent text."""

from __future__ import annotations

import re
from typing import Iterable, Sequence, Tuple

from .ast import EntityNode
from .lexicon import CatalogBundle
from .tokens import SourceSpan, Token

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_OPTION = re.compile(r"^\$?([a-z]{1,6})(\d{6})([cp])(\d{8})$")
_EXCHANGE = re.compile(r"^([a-z][a-z0-9_.-]{1,15}):([/$]?[a-z0-9._!-]+)$")
_GENERIC_SECURITY = re.compile(r"^[a-z][a-z0-9.-]{0,5}$")


def _overlaps(span: SourceSpan, spans: Iterable[SourceSpan]) -> bool:
    return any(span.start < used.end and used.start < span.end for used in spans)


def _phrase_span(tokens: Sequence[Token], start: int, length: int) -> SourceSpan:
    return SourceSpan(tokens[start].span.start, tokens[start + length - 1].span.end)


def _bind_catalog_phrases(
    normalized: str,
    tokens: Sequence[Token],
    catalogs: CatalogBundle,
    role: str,
) -> list[EntityNode]:
    aliases = sorted(
        {
            match.alias
            for catalog in catalogs.catalogs
            for alias, candidate_role, _ in catalog.aliases
            if candidate_role == role
            for match in catalogs.lookup(alias, role)
        },
        key=lambda item: (-len(item.split()), item),
    )
    found: list[EntityNode] = []
    used: list[SourceSpan] = []
    words = [token.text for token in tokens]
    for alias in aliases:
        parts = alias.split()
        for index in range(0, len(tokens) - len(parts) + 1):
            if words[index : index + len(parts)] != parts:
                continue
            span = _phrase_span(tokens, index, len(parts))
            if _overlaps(span, used):
                continue
            match = catalogs.lookup(alias, role)[0]
            found.append(
                EntityNode(
                    kind=role,
                    raw=normalized[span.start : span.end],
                    canonical=match.canonical,
                    span=span,
                    catalog=match.catalog,
                    resolved=True,
                    specificity=len(parts) * 100 + len(alias),
                )
            )
            used.append(span)
    return found


def _horizons(normalized: str, tokens: Sequence[Token]) -> list[EntityNode]:
    found: list[EntityNode] = []
    words = [token.text for token in tokens]
    units = {
        "day": "days",
        "days": "days",
        "week": "weeks",
        "weeks": "weeks",
        "month": "months",
        "months": "months",
        "quarter": "quarters",
        "quarters": "quarters",
        "qtr": "quarters",
        "qtrs": "quarters",
        "year": "years",
        "years": "years",
    }
    for index, word in enumerate(words):
        if word not in ("next", "previous", "prior", "last"):
            continue
        count = 1
        unit_index = index + 1
        if unit_index < len(tokens):
            number = words[unit_index]
            if tokens[unit_index].kind == "number" and number.isdigit():
                count = int(number)
                unit_index += 1
            elif number in _NUMBER_WORDS:
                count = _NUMBER_WORDS[number]
                unit_index += 1
        if unit_index >= len(tokens) or words[unit_index] not in units:
            continue
        if not 1 <= count <= 120:
            continue
        sign = -1 if word in ("previous", "prior", "last") else 1
        span = SourceSpan(tokens[index].span.start, tokens[unit_index].span.end)
        found.append(
            EntityNode(
                kind="horizon",
                raw=normalized[span.start : span.end],
                canonical={units[words[unit_index]]: count * sign},
                span=span,
                catalog="core",
                resolved=True,
                specificity=300,
            )
        )
    phrases = (
        (("year", "over", "year"), {"comparison": "year_over_year"}),
        (("since", "open"), {"period": "since_open"}),
    )
    for parts, canonical in phrases:
        for index in range(len(tokens) - len(parts) + 1):
            if tuple(words[index : index + len(parts)]) != parts:
                continue
            span = _phrase_span(tokens, index, len(parts))
            found.append(
                EntityNode(
                    kind="horizon",
                    raw=normalized[span.start : span.end],
                    canonical=canonical,
                    span=span,
                    catalog="core",
                    resolved=True,
                    specificity=250,
                )
            )
    for index, word in enumerate(words):
        if word not in ("today", "tomorrow"):
            continue
        span = tokens[index].span
        found.append(
            EntityNode(
                kind="time",
                raw=word,
                canonical=word,
                span=span,
                catalog="core",
                resolved=True,
                specificity=200,
            )
        )
    return found


def _canonical_security(text: str, catalogs: CatalogBundle) -> tuple[str, bool, str]:
    match = catalogs.lookup(text, "security")
    if match:
        return match[0].canonical, True, match[0].catalog
    option = _OPTION.match(text)
    if option:
        return "".join(option.groups()).upper(), False, "market"
    exchange = _EXCHANGE.match(text)
    if exchange:
        return f"{exchange.group(1).upper()}:{exchange.group(2).lstrip('$').upper()}", False, "market"
    return text.lstrip("$").upper(), False, "market"


def _security_context(tokens: Sequence[Token], index: int, topic_spans: Sequence[SourceSpan]) -> bool:
    token = tokens[index]
    if token.kind == "security_shape" or "security" in token.roles:
        return True
    if not _GENERIC_SECURITY.match(token.text):
        return False
    if any(abs(token.span.end - span.start) <= 2 or abs(span.end - token.span.start) <= 2 for span in topic_spans):
        return True
    nearby = {candidate.text for candidate in tokens[max(0, index - 3) : index + 4]}
    return bool(
        nearby
        & {
            "than",
            "versus",
            "vs",
            "compare",
            "buy",
            "sell",
            "trade",
            "open",
            "show",
            "view",
        }
    )


def bind_entities(
    normalized: str, tokens: Sequence[Token], catalogs: CatalogBundle
) -> Tuple[EntityNode, ...]:
    """Bind typed entities, leaving host-validation-required symbols explicit."""

    topics = _bind_catalog_phrases(normalized, tokens, catalogs, "topic")
    temporal = _horizons(normalized, tokens)
    occupied = [entity.span for entity in topics + temporal]
    topic_spans = [entity.span for entity in topics]
    securities: list[EntityNode] = []
    excluded_roles = {
        "imperative",
        "interrogative",
        "analytical",
        "external_action",
        "trade",
        "conjunction",
        "comparison",
        "negation",
        "qualifier",
        "ui_noun",
        "filler",
        "time",
        "time_unit",
        "topic",
    }
    for index, token in enumerate(tokens):
        if _overlaps(token.span, occupied):
            continue
        if token.kind not in ("word", "security_shape"):
            continue
        if set(token.roles) & excluded_roles and "security" not in token.roles:
            continue
        if not _security_context(tokens, index, topic_spans):
            continue
        canonical, resolved, catalog = _canonical_security(token.text, catalogs)
        securities.append(
            EntityNode(
                kind="security",
                raw=normalized[token.span.start : token.span.end],
                canonical=canonical,
                span=token.span,
                catalog=catalog,
                resolved=resolved,
                specificity=500 if token.kind == "security_shape" else 400,
            )
        )
    return tuple(sorted((*securities, *topics, *temporal), key=lambda item: (item.span.start, -item.specificity, item.kind)))


__all__ = ["bind_entities"]
