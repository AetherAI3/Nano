"""Nano runtimes.

``interpreter.execute`` is the reference semantics for baseline graphs and remains
the definition of correct behavior. ``vm.run_module`` executes v1.0 modules —
series, indicators, rules, routes, escalation — and is what every v1.0 consumer
uses. A conformance test holds the two to the same observable behavior on the
shared corpus, which is what keeps "the artifact that backtested is the artifact
that trades" true across an IR version boundary.

``risk`` holds the one thing the VM does *not* do freely: a module's declared
risk limits are evaluated there, and a breach withholds an actuating intent and
says so in the log. Measurements come from the host's frame; the daily order cap
also adds actuating intents already accepted at the same timestamp. That local,
deterministic capacity never carries across frames,
so enforcement stays as replayable as the rest of the run — see that module for
which limits are enforced, which two are the host's to apply, and why.
"""

from .effects import Intent, LogEntry
from .interpreter import (
    ExecutionResult,
    MarketFrame,
    RuntimeError_,
    SignalFrame,
    execute,
)
from .risk import ENFORCED_LIMITS, HOST_ENFORCED_LIMITS, RiskGate
from .scheduler import interval_seconds, ticks
from .vm import Escalation, ModuleResult, ReasoningProvider, run_frames, run_module

__all__ = [
    "ENFORCED_LIMITS",
    "Escalation",
    "ExecutionResult",
    "HOST_ENFORCED_LIMITS",
    "Intent",
    "LogEntry",
    "MarketFrame",
    "ModuleResult",
    "ReasoningProvider",
    "RiskGate",
    "RuntimeError_",
    "SignalFrame",
    "execute",
    "interval_seconds",
    "run_frames",
    "run_module",
    "ticks",
]
