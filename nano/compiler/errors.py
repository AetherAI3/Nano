"""Compiler error types.

Invariant: every compile-time failure carries a 1-based line and column that
points at real source text. Downstream tooling (editor services, the LSP) relies on
these positions being exact — no error is ever raised without them.

Taxonomy (v1.0):

    NanoCompileError            any compile-time failure -- catch this
     └── NanoSyntaxError        lexing / parsing
          └── NanoTypeError     semantic analysis (types, arity, unknown names)
               └── LookaheadError  a read of future data

``NanoTypeError`` deliberately subclasses ``NanoSyntaxError`` rather than
sitting beside it. Hosts embedding the compiler (Aether Code's
``/agent/code/nano/compile``) already branch on ``NanoSyntaxError`` to surface
exact positions and fall back to a positionless line-1 message for anything
else; inheriting keeps type and look-ahead errors in the precise branch with no
host change. New code should catch ``NanoCompileError`` — it is the honest name
for "the compiler rejected this source".
"""

from __future__ import annotations


class NanoCompileError(ValueError):
    """`.nano` source failed to compile. Carries .line, .column, .message."""

    def __init__(self, message: str, line: int, column: int) -> None:
        super().__init__(f"{line}:{column}: {message}")
        self.message = message
        self.line = line
        self.column = column


class NanoSyntaxError(NanoCompileError):
    """Source is not well-formed: the lexer or parser rejected it."""


class NanoTypeError(NanoSyntaxError):
    """Source parses but violates the type system or a semantic rule."""


class LookaheadError(NanoTypeError):
    """Source attempts to read data from the future.

    Raised for any series access whose offset is not provably non-negative.
    Look-ahead is a correctness bug that silently inflates every backtest, so
    Nano makes it unrepresentable rather than merely detectable — see
    ``nano/types/lookahead.py``.
    """
