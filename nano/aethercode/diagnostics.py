"""Editor adapter for the shared compiler diagnostic representation.

Source errors retain the compiler's exact coordinates. Positionless IR failures
retain the old line/column 1:1 locator for consumers of those legacy fields;
``span`` remains ``None`` so structured consumers never mistake it for evidence
of a source position. Valid programs of either supported IR version are accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from ..compiler.diagnostics import SourceSpan, check_report
from ..compiler.parser import CompilerLimits, DEFAULT_COMPILER_LIMITS

SEVERITY_ERROR = "error"


@dataclass(frozen=True)
class Diagnostic:
    line: int
    column: int
    message: str
    severity: str
    code: str = "SYNTAX_ERROR"
    category: str = "syntax"
    span: Optional[SourceSpan] = None
    expected: Optional[str] = None
    actual: Optional[str] = None
    hint: Optional[str] = None


def diagnostics(
    source: str, *, ir_version: Optional[str] = None,
    limits: CompilerLimits = DEFAULT_COMPILER_LIMITS,
) -> Tuple[Diagnostic, ...]:
    """Return known compiler rejections, leaving internal exceptions visible."""
    report = check_report(source, ir_version=ir_version, limits=limits)
    return tuple(
        Diagnostic(
            line=item.span.start.line if item.span is not None else 1,
            column=item.span.start.column if item.span is not None else 1,
            message=item.message, severity=item.severity,
            code=item.code, category=item.category, span=item.span,
            expected=item.expected, actual=item.actual, hint=item.hint,
        )
        for item in report.diagnostics
    )
