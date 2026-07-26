"""Indicator layer — typed signatures plus deterministic reference kernels.

Two halves that stay separable on purpose: ``registry`` is the compile-time
contract the type checker reads (arity, parameter kinds, warm-up length), and
``compute`` is the runtime math. A host that only compiles never imports the
kernels; a host that swaps in vectorised kernels keeps the same signatures.
"""

from .compute import Cell, Series, UnknownIndicator, evaluate
from .registry import INDICATORS, IndicatorSpec, is_indicator, lookup, names

__all__ = [
    "Cell",
    "INDICATORS",
    "IndicatorSpec",
    "Series",
    "UnknownIndicator",
    "evaluate",
    "is_indicator",
    "lookup",
    "names",
]
