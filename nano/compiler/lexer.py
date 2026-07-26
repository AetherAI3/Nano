"""Lexer for `.nano` source (v0.1.0 surface).

Invariant: only the locked token vocabulary leaves this module. Anything
outside it — stray characters, half-written operators, malformed intervals or
number literals — fails here, immediately, with the 1-based line and column of
the offending text. Pure function of the source string; no I/O.
"""

from __future__ import annotations

from typing import Tuple

from .errors import NanoSyntaxError
from .tokens import Token

_PUNCT = {
    "{": "LBRACE",
    "}": "RBRACE",
    "(": "LPAREN",
    ")": "RPAREN",
    "[": "LBRACKET",
    "]": "RBRACKET",
    ",": "COMMA",
    ":": "COLON",
    ".": "DOT",
}

# Arithmetic operators. They share the OP token type with comparisons; the
# parser separates them by precedence, and editor tooling colours them alike.
_ARITHMETIC = frozenset("+-*/%")

_INTERVAL_UNITS = frozenset("smhd")

# Escapes recognised inside a string literal. Deliberately short: strings in
# Nano name escalation targets and signature inputs — they are not a text
# processing facility, and every extra escape is one more thing two runtimes can
# disagree about.
_STRING_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t"}


def _is_ident_start(ch: str) -> bool:
    return "a" <= ch <= "z" or "A" <= ch <= "Z" or ch == "_"


def _is_ident_char(ch: str) -> bool:
    return _is_ident_start(ch) or "0" <= ch <= "9"


def _is_digit(ch: str) -> bool:
    return "0" <= ch <= "9"


def tokenize(source: str) -> Tuple[Token, ...]:
    """Split source into tokens; always ends with a single EOF token."""
    tokens: list[Token] = []
    i = 0
    line = 1
    column = 1
    n = len(source)

    while i < n:
        ch = source[i]

        if ch == "\n":
            i += 1
            line += 1
            column = 1
            continue
        if ch in " \t\r":
            i += 1
            column += 1
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                i += 1
                column += 1
            continue

        start_line, start_column = line, column

        if ch in _PUNCT:
            tokens.append(Token(_PUNCT[ch], ch, start_line, start_column))
            i += 1
            column += 1
            continue

        if ch in "<>":
            if i + 1 < n and source[i + 1] == "=":
                tokens.append(Token("OP", ch + "=", start_line, start_column))
                i += 2
                column += 2
            else:
                tokens.append(Token("OP", ch, start_line, start_column))
                i += 1
                column += 1
            continue

        if ch in "=!":
            if i + 1 < n and source[i + 1] == "=":
                tokens.append(Token("OP", ch + "=", start_line, start_column))
                i += 2
                column += 2
                continue
            if ch == "=":
                # A lone `=` is a real token: it binds params and lets. Writing
                # it where a comparison belongs is a parser-level diagnostic, so
                # the error can name what was expected instead of just refusing
                # the character.
                tokens.append(Token("ASSIGN", "=", start_line, start_column))
                i += 1
                column += 1
                continue
            raise NanoSyntaxError(
                f"Unknown operator {ch!r} (expected one of <, <=, >, >=, ==, !=)",
                start_line,
                start_column,
            )

        if ch in _ARITHMETIC:
            tokens.append(Token("OP", ch, start_line, start_column))
            i += 1
            column += 1
            continue

        if ch == '"':
            # The token carries the *raw* span, quotes and escapes included, so
            # `len(value)` stays the source width editor highlighting needs.
            # Escapes are validated here and decoded by `decode_string`.
            j = i + 1
            while j < n and source[j] != '"':
                if source[j] == "\n":
                    raise NanoSyntaxError(
                        "Unterminated string literal (a string cannot span lines)",
                        start_line,
                        start_column,
                    )
                if source[j] == "\\":
                    if j + 1 >= n or source[j + 1] not in _STRING_ESCAPES:
                        raise NanoSyntaxError(
                            f"Unknown string escape {source[j : j + 2]!r} (expected "
                            f"one of {', '.join(sorted(_STRING_ESCAPES))})",
                            start_line,
                            start_column + (j - i),
                        )
                    j += 2
                    continue
                j += 1
            if j >= n:
                raise NanoSyntaxError(
                    'Unterminated string literal (missing closing \'"\')',
                    start_line,
                    start_column,
                )
            tokens.append(Token("STRING", source[i : j + 1], start_line, start_column))
            column += (j + 1) - i
            i = j + 1
            continue

        if _is_ident_start(ch):
            j = i
            while j < n and _is_ident_char(source[j]):
                j += 1
            text = source[i:j]
            tokens.append(Token("IDENT", text, start_line, start_column))
            column += j - i
            i = j
            continue

        if _is_digit(ch):
            j = i
            while j < n and _is_digit(source[j]):
                j += 1
            is_float = False
            if j < n and source[j] == ".":
                if j + 1 < n and _is_digit(source[j + 1]):
                    is_float = True
                    j += 1
                    while j < n and _is_digit(source[j]):
                        j += 1
                else:
                    raise NanoSyntaxError(
                        f"Malformed number literal {source[i : j + 1]!r} "
                        "(digits required after '.')",
                        start_line,
                        start_column,
                    )
            if j < n and _is_ident_char(source[j]):
                # A letter glued to a number is only legal as an interval:
                # an integer followed by exactly one unit in {s, m, h, d}.
                k = j
                while k < n and _is_ident_char(source[k]):
                    k += 1
                suffix = source[j:k]
                if not is_float and suffix in _INTERVAL_UNITS:
                    tokens.append(Token("INTERVAL", source[i:k], start_line, start_column))
                    column += k - i
                    i = k
                    continue
                raise NanoSyntaxError(
                    f"Malformed interval {source[i:k]!r} "
                    "(expected an integer followed by one of s, m, h, d)",
                    start_line,
                    start_column,
                )
            text = source[i:j]
            tokens.append(
                Token("FLOAT" if is_float else "INT", text, start_line, start_column)
            )
            column += j - i
            i = j
            continue

        raise NanoSyntaxError(f"Unexpected character {ch!r}", start_line, start_column)

    tokens.append(Token("EOF", "", line, column))
    return tuple(tokens)


def decode_string(raw: str) -> str:
    """Decode a STRING token's raw span into its value.

    The lexer already rejected unknown escapes and unterminated literals, so
    this only has to undo what it validated.
    """
    body = raw[1:-1]
    out: list[str] = []
    index = 0
    while index < len(body):
        if body[index] == "\\" and index + 1 < len(body):
            out.append(_STRING_ESCAPES[body[index + 1]])
            index += 2
            continue
        out.append(body[index])
        index += 1
    return "".join(out)
