"""Token vocabulary for `.nano` source.

Invariant: a token records exactly what the lexer saw and exactly where it saw
it (1-based line/column of the first character). Tokens are immutable; the
parser never re-inspects raw source, only this stream.
"""

from __future__ import annotations

from dataclasses import dataclass

# Token types emitted by the lexer. Keywords are not lexed specially — the
# parser gives IDENT tokens keyword meaning by position, which keeps the lexer
# free of grammar knowledge.
TOKEN_TYPES = frozenset(
    {
        "IDENT",  # [A-Za-z_][A-Za-z0-9_]*
        "INT",  # integer literal, e.g. 30
        "FLOAT",  # decimal literal, e.g. 0.91
        "STRING",  # quoted literal, e.g. "research-agent"
        "INTERVAL",  # INT + unit, e.g. 5m, 1h
        # Every operator shares one token type; the parser reads `value` to tell
        # `<` from `+`. Keeping arithmetic and comparison together means editor
        # tooling that maps OP -> "operator" needs no change as the grammar grows.
        "OP",  # < <= > >= == != + - * / %
        "ASSIGN",  # = -- distinct from ==, so `if x = 1` reports a real diagnostic
        "COLON",  # : in type annotations
        "DOT",  # . in signature-result field access
        "LBRACE",
        "RBRACE",
        "LPAREN",
        "RPAREN",
        "LBRACKET",  # [ opening a series offset
        "RBRACKET",
        "COMMA",
        "EOF",
    }
)


@dataclass(frozen=True)
class Token:
    type: str
    value: str
    line: int
    column: int
