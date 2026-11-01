"""Shared, deterministic compiler diagnostics for CLI and editor adapters.

Spans are point locations with exact 1-based Unicode-character coordinates.
An end equal to start makes no claim about a token's extent. IR-only failures
have no source span; only adapters retaining a legacy locator may supply one.
Only known source/IR rejection types are converted. Internal exceptions escape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from ..ir.graph import StrategyGraph
from ..ir.module import NanoModule
from ..ir.schema import (
    IRValidationError, ManifestViolation, NANO_IR_VERSION_BASELINE, TierViolation,
)
from .codegen import IRVersionError, compile_to_dict, source_hash
from .errors import NanoCompileError, NanoSyntaxError
from .parser import CompilerLimits, DEFAULT_COMPILER_LIMITS

DIAGNOSTIC_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class SourcePosition:
    line: int
    column: int

    def to_dict(self) -> dict:
        return {"line": self.line, "column": self.column}


@dataclass(frozen=True)
class SourceSpan:
    start: SourcePosition
    end: SourcePosition

    def to_dict(self) -> dict:
        return {"start": self.start.to_dict(), "end": self.end.to_dict()}


@dataclass(frozen=True)
class CompilerDiagnostic:
    code: str
    category: str
    severity: str
    message: str
    span: Optional[SourceSpan] = None
    expected: Optional[str] = None
    actual: Optional[str] = None
    hint: Optional[str] = None

    def __post_init__(self) -> None:
        if self.hint is not None and len(self.hint) > 240:
            object.__setattr__(self, "hint", self.hint[:240])

    def to_dict(self) -> dict:
        result = {
            "code": self.code, "category": self.category,
            "severity": self.severity, "message": self.message,
            "span": self.span.to_dict() if self.span is not None else None,
        }
        for name in ("expected", "actual", "hint"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result


@dataclass(frozen=True)
class CompileReport:
    source_digest: Optional[str]
    success: bool
    diagnostics: Tuple[CompilerDiagnostic, ...]
    ir_version: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "sourceDigest": self.source_digest, "success": self.success,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "irVersion": self.ir_version,
        }


def diagnostic_for_error(error: Exception) -> CompilerDiagnostic:
    """Convert a known compiler rejection without guessing its source position."""
    if isinstance(error, NanoCompileError):
        position = SourcePosition(error.line, error.column)
        return CompilerDiagnostic(
            code=error.code, category=error.category, severity="error",
            message=error.message, span=SourceSpan(position, position),
            expected=error.expected, actual=error.actual, hint=error.hint,
        )
    if isinstance(error, IRVersionError):
        code, category = "IR_VERSION_ERROR", "version"
    elif isinstance(error, (ManifestViolation, TierViolation)):
        code, category = "CAPABILITY_ERROR", "capability"
    elif isinstance(error, IRValidationError):
        code, category = "IR_VALIDATION_ERROR", "ir"
    else:
        raise TypeError(f"Unsupported diagnostic exception: {type(error).__name__}")
    return CompilerDiagnostic(code, category, "error", str(error))


def check_report(
    source: str, *, ir_version: Optional[str] = None,
    limits: CompilerLimits = DEFAULT_COMPILER_LIMITS,
) -> CompileReport:
    """Compile and validate source without executing a provider or any effects.

    The digest addresses exactly the UTF-8 source text supplied to this call.
    I/O and normalization are adapter responsibilities, not compiler behavior.
    """
    try:
        digest = source_hash(source)
    except UnicodeEncodeError as error:
        # Editor strings can contain an unpaired UTF-16 surrogate while being
        # edited. Such text has no UTF-8 digest; retain its real source location
        # rather than letting hashing bypass the diagnostic adapter.
        prefix = source[:error.start]
        rejection = NanoSyntaxError(
            "Source text must be encodable as UTF-8 (unpaired Unicode surrogate)",
            prefix.count("\n") + 1, error.start - prefix.rfind("\n"),
            expected="Unicode text encodable as UTF-8",
            actual=f"U+{ord(source[error.start]):04X}",
        )
        return CompileReport(None, False, (diagnostic_for_error(rejection),))
    try:
        document = compile_to_dict(source, ir_version=ir_version, limits=limits)
        if document["nanoIrVersion"] == NANO_IR_VERSION_BASELINE:
            StrategyGraph.from_dict(document)
        else:
            NanoModule.from_dict(document)
    except (NanoCompileError, IRValidationError) as error:
        return CompileReport(digest, False, (diagnostic_for_error(error),))
    return CompileReport(digest, True, (), document["nanoIrVersion"])
