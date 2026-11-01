"""Diagnostics for `.nano` source.

Invariant: diagnostics never raises and never invents positions. Valid source
yields an empty tuple. A NanoSyntaxError becomes one diagnostic carrying the
compiler's exact 1-based line/column and message. IR-level validation failures
(grammatical programs the IR contract rejects) have no source position, so
they are anchored at line 1, column 1 — the strategy header — never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from ..compiler import NanoSyntaxError, compile_module
from ..ir.schema import IRValidationError

SEVERITY_ERROR = "error"


@dataclass(frozen=True)
class Diagnostic:
    line: int
    column: int
    message: str
    severity: str


def diagnostics(source: str) -> Tuple[Diagnostic, ...]:
    """Compile source and report failures as diagnostics; never raises."""
    try:
        # Editor validation accepts every supported language version. The
        # baseline-only compile_source entry point rejects valid v1 programs.
        compile_module(source)
    except NanoSyntaxError as error:
        return (
            Diagnostic(
                line=error.line,
                column=error.column,
                message=error.message,
                severity=SEVERITY_ERROR,
            ),
        )
    except IRValidationError as error:
        return (
            Diagnostic(line=1, column=1, message=str(error), severity=SEVERITY_ERROR),
        )
    return ()
