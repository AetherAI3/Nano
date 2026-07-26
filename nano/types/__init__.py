"""Nano's type system and semantic analysis.

Four modules, in dependency order:

* ``kinds`` — the type vocabulary and its unification rules.
* ``env`` — the symbol table: what a name is, and what it is allowed to do.
* ``lookahead`` — compile-time integer folding plus series-offset and period
  validation. The module that makes reading the future unrepresentable.
* ``checker`` — the pass that ties them together into a ``TypedProgram``.

Importing this package pulls in no runtime machinery: analysis is a pure
function from a parsed AST to a typed program, with no I/O anywhere in it.
"""

from ..compiler.errors import LookaheadError, NanoCompileError, NanoTypeError
from .checker import (
    Resolution,
    ResolvedFeed,
    ResolvedIndicator,
    ResolvedInfer,
    TypedProgram,
    check,
)
from .env import Scope, Symbol
from .kinds import (
    BOOL,
    CONFIDENCE,
    DURATION,
    FLOAT,
    INT,
    SERIES_BOOL,
    SERIES_FLOAT,
    STRING,
    VOID,
    Type,
    is_assignable,
    parse_type,
    record,
    series,
    type_names,
)
from .lookahead import fold_int, resolve_offset, resolve_period

__all__ = [
    "BOOL",
    "CONFIDENCE",
    "DURATION",
    "FLOAT",
    "INT",
    "LookaheadError",
    "NanoCompileError",
    "NanoTypeError",
    "Resolution",
    "ResolvedFeed",
    "ResolvedIndicator",
    "ResolvedInfer",
    "SERIES_BOOL",
    "SERIES_FLOAT",
    "STRING",
    "Scope",
    "Symbol",
    "Type",
    "TypedProgram",
    "VOID",
    "check",
    "fold_int",
    "is_assignable",
    "parse_type",
    "record",
    "resolve_offset",
    "resolve_period",
    "series",
    "type_names",
]
