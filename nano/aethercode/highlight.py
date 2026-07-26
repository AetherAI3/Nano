"""Semantic tokens for `.nano` source.

Invariant: semantic_tokens never raises. It classifies whatever prefix of the
source the lexer can tokenize — syntactically invalid but lexable source still
yields its full token stream, and a lexer failure yields the tokens before the
offending text. Positions are the lexer's own 1-based line/column, unmodified,
so highlights always land on real source characters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from ..compiler import NanoSyntaxError, tokenize

# Token kinds. IDENT tokens are classified by value — the lexer has no keyword
# knowledge (the parser assigns keyword meaning by position), so the known
# keyword and action vocabularies are mirrored here for the editor's benefit.
KIND_KEYWORD = "keyword"
KIND_ACTION = "action"
KIND_INTERVAL = "interval"
KIND_NUMBER = "number"
KIND_STRING = "string"
KIND_OPERATOR = "operator"
KIND_PUNCTUATION = "punctuation"
KIND_IDENTIFIER = "identifier"

# There is deliberately no "indicator" or "type" kind here. Both would require
# knowing what a name *means*, and this function runs on partial, unparseable
# source where that is unknowable: `RSI` in `RSI(14) < 30` is a host-supplied
# feed signal, while `RSI(close, 14)` is a computed indicator, and the two are
# spelled identically until the checker has resolved them. Colouring both
# "indicator" would be wrong half the time. Semantic classification is the job of
# the checker-backed hover service, which has the types to do it honestly.

_KEYWORDS = frozenset(
    {
        "strategy",
        "tier",
        "every",
        "if",
        "else",
        "and",
        "or",
        "not",
        "agent",
        "param",
        "input",
        "output",
        "let",
        "risk",
        "signature",
        "route",
        "when",
        "otherwise",
        "escalate",
        "range",
        "role",
        "true",
        "false",
    }
)
_ACTIONS = frozenset({"buy", "sell", "execute", "pause", "observe"})

_TYPE_KINDS = {
    "INTERVAL": KIND_INTERVAL,
    "INT": KIND_NUMBER,
    "FLOAT": KIND_NUMBER,
    "STRING": KIND_STRING,
    "OP": KIND_OPERATOR,
    "ASSIGN": KIND_OPERATOR,
    "COLON": KIND_PUNCTUATION,
    "DOT": KIND_PUNCTUATION,
    "LBRACE": KIND_PUNCTUATION,
    "RBRACE": KIND_PUNCTUATION,
    "LPAREN": KIND_PUNCTUATION,
    "RPAREN": KIND_PUNCTUATION,
    "LBRACKET": KIND_PUNCTUATION,
    "RBRACKET": KIND_PUNCTUATION,
    "COMMA": KIND_PUNCTUATION,
}


@dataclass(frozen=True)
class SemanticToken:
    line: int
    column: int
    length: int
    kind: str


def _classify(token_type: str, value: str) -> str:
    if token_type == "IDENT":
        # Action names win over keywords where the two overlap: `execute` is a
        # keyword inside a `route` block but an action everywhere else, and
        # colouring the far more common form correctly is the better trade.
        if value in _ACTIONS:
            return KIND_ACTION
        if value in _KEYWORDS:
            return KIND_KEYWORD
        return KIND_IDENTIFIER
    # An unmapped token type would be a lexer/highlighter mismatch. Falling back
    # to `identifier` keeps the documented "never raises" contract, which the
    # Aether Code /tokens endpoint depends on, instead of turning a new token
    # type into a 500.
    return _TYPE_KINDS.get(token_type, KIND_IDENTIFIER)


def _offset_of(source: str, line: int, column: int) -> int:
    """Character offset of a 1-based (line, column) position."""
    offset = 0
    for _ in range(line - 1):
        newline = source.find("\n", offset)
        if newline < 0:
            return len(source)
        offset = newline + 1
    return min(offset + column - 1, len(source))


def semantic_tokens(source: str) -> Tuple[SemanticToken, ...]:
    """Classify source into editor highlight tokens; never raises."""
    text = source
    while True:
        try:
            tokens = tokenize(text)
            break
        except NanoSyntaxError as error:
            cut = _offset_of(text, error.line, error.column)
            if cut >= len(text):
                return ()
            text = text[:cut]
    return tuple(
        SemanticToken(
            line=token.line,
            column=token.column,
            length=len(token.value),
            kind=_classify(token.type, token.value),
        )
        for token in tokens
        if token.type != "EOF"
    )
