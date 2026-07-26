"""Nano IR — the contract between the compiler and every runtime.

Two live document versions, one executable form. ``StrategyGraph`` loads baseline
(v0.1.0) documents and is the reference semantics; ``NanoModule`` loads v1.0.0 and
is what runtimes actually evaluate. A baseline graph lifts into a module via
``StrategyGraph.to_module()``, so there is one evaluator and the two versions
cannot drift apart. See ``schema.py`` for how a version gets chosen.
"""

from typing import Any, Mapping

from .graph import StrategyGraph
from .module import (
    ARITHMETIC_OPS,
    COMPARISON_OPS,
    COMPILER_NAME,
    COMPILER_VERSION,
    DETERMINISM_CONTRACT,
    OPS,
    InputDecl,
    IRNode,
    NanoModule,
    OpSpec,
    ParamDecl,
    canonical_effects,
)
from .nodes import AgentNode, ConditionNode, IntentNode, ScheduleNode
from .schema import (
    NANO_IR_VERSION,
    NANO_IR_VERSION_1_0,
    NANO_IR_VERSION_BASELINE,
    NANO_IR_VERSION_LATEST,
    SUPPORTED_IR_VERSIONS,
    IRValidationError,
    ManifestViolation,
    TierViolation,
)

__all__ = [
    "ARITHMETIC_OPS",
    "AgentNode",
    "COMPARISON_OPS",
    "COMPILER_NAME",
    "COMPILER_VERSION",
    "ConditionNode",
    "DETERMINISM_CONTRACT",
    "IRNode",
    "IRValidationError",
    "InputDecl",
    "IntentNode",
    "ManifestViolation",
    "NANO_IR_VERSION",
    "NANO_IR_VERSION_1_0",
    "NANO_IR_VERSION_BASELINE",
    "NANO_IR_VERSION_LATEST",
    "NanoModule",
    "OPS",
    "OpSpec",
    "ParamDecl",
    "SUPPORTED_IR_VERSIONS",
    "ScheduleNode",
    "StrategyGraph",
    "TierViolation",
    "canonical_effects",
    "load",
    "load_module",
]


def load(document: Mapping[str, Any]):
    """Load a Nano IR document of either version.

    Dispatches on `nanoIrVersion` so a caller holding an unknown document does not
    have to guess which loader to reach for. Returns a ``StrategyGraph`` for
    baseline and a ``NanoModule`` for v1.0; both validate on the way in.
    """
    version = document.get("nanoIrVersion")
    if version == NANO_IR_VERSION_BASELINE:
        return StrategyGraph.from_dict(document)
    if version == NANO_IR_VERSION_1_0:
        return NanoModule.from_dict(document)
    raise IRValidationError(
        f"nanoIrVersion {version!r} unsupported "
        f"(expected one of {', '.join(SUPPORTED_IR_VERSIONS)})"
    )


def load_module(document: Mapping[str, Any]) -> NanoModule:
    """Load any Nano IR document as an executable ``NanoModule``.

    Baseline documents are lifted on the way through, so a runtime only ever needs
    one code path regardless of which version it was handed.
    """
    loaded = load(document)
    if isinstance(loaded, StrategyGraph):
        return loaded.to_module()
    return loaded
