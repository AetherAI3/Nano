"""`.nano` -> Nano IR compiler.

The compiler is the only path from surface syntax to IR, and this module is its
stable public API. Downstream tooling — the editor services in
``nano/aethercode/``, the CLI, and Aether Code's language-service endpoints —
consumes exactly this surface, so names here are kept and added to, never
repurposed.

Which entry point to use:

| Want | Call |
|---|---|
| The IR document a host should store | ``compile_to_dict`` |
| An executable module (either version) | ``compile_module`` |
| A baseline ``StrategyGraph`` | ``compile_source`` |
| Types and diagnostics without IR | ``check_source`` |

``compile_to_dict`` emits the lowest IR version that can express the program, so a
v0.1.0-era strategy still compiles to byte-identical v0.1.0 output. Pass
``ir_version`` to force a shape.

Catch ``NanoCompileError`` to handle any compile failure; ``NanoSyntaxError``,
``NanoTypeError``, and ``LookaheadError`` narrow it, and every one of them carries
an exact 1-based line and column.
"""

from .codegen import (
    EFFECTS_V0_1_0,
    IRVersionError,
    ast_to_dict,
    check_source,
    compile_module,
    compile_source,
    compile_to_dict,
    required_ir_version,
    source_hash,
)
from .errors import (
    LookaheadError, NanoCapabilityError, NanoCompileError, NanoResourceLimitError,
    NanoSyntaxError, NanoTypeError,
)
from .diagnostics import (
    DIAGNOSTIC_SCHEMA_VERSION, CompileReport, CompilerDiagnostic, SourcePosition,
    SourceSpan, check_report, diagnostic_for_error,
)
from .lexer import decode_string, tokenize
from .parser import CompilerLimits, DEFAULT_COMPILER_LIMITS, MAX_COMPILER_NESTING, parse
from .tokens import Token

__all__ = [
    "EFFECTS_V0_1_0",
    "IRVersionError",
    "LookaheadError",
    "NanoCapabilityError",
    "NanoCompileError",
    "NanoResourceLimitError",
    "CompilerLimits",
    "DEFAULT_COMPILER_LIMITS",
    "MAX_COMPILER_NESTING",
    "NanoSyntaxError",
    "NanoTypeError",
    "Token",
    "DIAGNOSTIC_SCHEMA_VERSION",
    "CompileReport",
    "CompilerDiagnostic",
    "SourcePosition",
    "SourceSpan",
    "ast_to_dict",
    "check_source",
    "check_report",
    "compile_module",
    "compile_source",
    "compile_to_dict",
    "decode_string",
    "diagnostic_for_error",
    "parse",
    "required_ir_version",
    "source_hash",
    "tokenize",
]
