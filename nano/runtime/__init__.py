"""Nano runtimes.

``interpreter.execute`` is the reference semantics for baseline graphs and remains
the definition of correct behavior. ``vm.run_module`` executes v1.0 modules —
series, indicators, rules, routes, escalation — and is what every v1.0 consumer
uses. A conformance test holds the two to the same observable behavior on the
shared corpus, which is what keeps "the artifact that backtested is the artifact
that trades" true across an IR version boundary.
"""

from .effects import Intent, LogEntry
from .interpreter import (
    ExecutionResult,
    MarketFrame,
    RuntimeError_,
    SignalFrame,
    execute,
)
from .scheduler import interval_seconds, ticks
from .vm import Escalation, ModuleResult, ReasoningProvider, run_frames, run_module

__all__ = [
    "Escalation",
    "ExecutionResult",
    "Intent",
    "LogEntry",
    "MarketFrame",
    "ModuleResult",
    "ReasoningProvider",
    "RuntimeError_",
    "SignalFrame",
    "execute",
    "interval_seconds",
    "run_frames",
    "run_module",
    "ticks",
]
