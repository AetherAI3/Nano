"""Bounded handwritten tokenizer for normalized intent text."""

from __future__ import annotations

import re
from typing import Tuple

from .lexicon import CatalogBundle, default_catalogs
from .tokens import IntentInputError, MAX_TOKENS, SourceSpan, Token

_TOKEN = re.compile(
    r"(?P<exchange>[a-z][a-z0-9_.-]{1,15}:[/$]?[a-z0-9._!-]{1,32})"
    r"|(?P<option>\$?[a-z]{1,6}\d{6}[cp]\d{8})"
    r"|(?P<future>/[a-z0-9]{1,6}|[a-z]{1,4}\d!)"
    r"|(?P<cashtag>\$[a-z][a-z0-9._-]{0,14})"
    r"|(?P<number>\d+(?:\.\d+)?)"
    r"|(?P<word>[a-z][a-z0-9._-]*)"
    r"|(?P<punct>[^\w\s])"
)

_KINDS = {
    "exchange": "security_shape",
    "option": "security_shape",
    "future": "security_shape",
    "cashtag": "security_shape",
    "number": "number",
    "word": "word",
    "punct": "punctuation",
}


def tokenize(
    normalized: str, catalogs: CatalogBundle | None = None
) -> Tuple[Token, ...]:
    """Tokenize normalized text, rejecting gaps and unbounded token streams."""

    bundle = catalogs or default_catalogs()
    tokens: list[Token] = []
    position = 0
    for match in _TOKEN.finditer(normalized):
        gap = normalized[position : match.start()]
        if gap and not gap.isspace():
            raise IntentInputError(
                f"unsupported character at normalized offset {position}"
            )
        position = match.end()
        text = match.group(0)
        roles = tuple(sorted({item.role for item in bundle.lookup(text)}))
        tokens.append(
            Token(
                kind=_KINDS[match.lastgroup or "punct"],
                text=text,
                span=SourceSpan(match.start(), match.end()),
                roles=roles,
            )
        )
        if len(tokens) > MAX_TOKENS:
            raise IntentInputError(
                f"intent source exceeds the {MAX_TOKENS}-token limit"
            )
    tail = normalized[position:]
    if tail and not tail.isspace():
        raise IntentInputError(
            f"unsupported character at normalized offset {position}"
        )
    return tuple(tokens)


__all__ = ["tokenize"]
