"""Nano Intent: deterministic natural-language plans beside the strategy DSL.

This package never imports, modifies, or extends ``nano.compiler``.  It emits an
independently versioned command plan which a host may strengthen, deny, or act
upon through its own capability mappings.
"""

from .ast import INTENT_FORMS, IntentAST
from .compiler import (
    compile_intent,
    compile_to_dict,
    explain_intent,
    parse_intent,
)
from .ir import (
    INTENT_COMPILER_NAME,
    INTENT_COMPILER_VERSION,
    NANO_INTENT_VERSION,
    SUPPORTED_INTENT_VERSIONS,
    IntentIR,
    IntentValidationError,
    canonical_bytes,
    canonical_json,
)
from .lexicon import Catalog, CatalogBundle, CatalogError, default_catalogs
from .normalize import normalize, normalize_text
from .tokens import IntentInputError, MAX_INPUT_CHARS, MAX_TOKENS

__all__ = [
    "Catalog",
    "CatalogBundle",
    "CatalogError",
    "INTENT_COMPILER_NAME",
    "INTENT_COMPILER_VERSION",
    "INTENT_FORMS",
    "IntentAST",
    "IntentIR",
    "IntentInputError",
    "IntentValidationError",
    "MAX_INPUT_CHARS",
    "MAX_TOKENS",
    "NANO_INTENT_VERSION",
    "SUPPORTED_INTENT_VERSIONS",
    "canonical_bytes",
    "canonical_json",
    "compile_intent",
    "compile_to_dict",
    "default_catalogs",
    "explain_intent",
    "normalize",
    "normalize_text",
    "parse_intent",
]
