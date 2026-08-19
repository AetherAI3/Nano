"""Nano reference interpreter (Milestone 2).

Executes a validated StrategyGraph against an injected MarketFrame. Pure
function of its inputs: identical graph + identical frame => identical result,
bit-for-bit. No ambient clock, no ambient randomness, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence, Tuple

from ..ir.graph import StrategyGraph
from .effects import Intent, LogEntry
from .scheduler import ticks


class RuntimeError_(Exception):
    """Execution failed against the provided market frame."""


@dataclass(frozen=True)
class MarketFrame:
    """Injected market data: a timeline and named signal series aligned to it."""

    timestamps: Tuple[int, ...]
    signals: Mapping[str, Sequence[float]]

    def __post_init__(self) -> None:
        for name, series in self.signals.items():
            if len(series) != len(self.timestamps):
                raise ValueError(
                    f"Signal {name!r} has {len(series)} points for "
                    f"{len(self.timestamps)} timestamps"
                )

    def observe(self, signal: str, index: int) -> float:
        if signal not in self.signals:
            raise RuntimeError_(f"Signal {signal!r} not present in market frame")
        return float(self.signals[signal][index])


# A frame is a timeline plus named numeric series. Nothing about that shape is
# market-specific, and non-market callers (see ``nano/watchdog/``) read queue
# depth or credential age through exactly the same contract. ``SignalFrame`` is
# the name to reach for in that code.
#
# It is an alias rather than a rename, deliberately: the class keeps its original
# ``__name__``, so reprs, pickles, and the "not present in market frame"
# diagnostic above do not shift under any existing consumer. One type, two names
# — a parallel frame class would eventually want a parallel evaluator, and two
# evaluators disagree.
SignalFrame = MarketFrame


@dataclass(frozen=True)
class ExecutionResult:
    intents: Tuple[Intent, ...]
    log: Tuple[LogEntry, ...]

    def to_dict(self) -> dict:
        return {
            "intents": [i.to_dict() for i in self.intents],
            "log": [e.to_dict() for e in self.log],
        }


def execute(graph: StrategyGraph, frame: MarketFrame) -> ExecutionResult:
    """Run the graph over the frame; return intents + a complete audit log."""
    interval = graph.schedule.interval if graph.schedule is not None else "1s"
    log: list[LogEntry] = [
        LogEntry(
            event="strategy.loaded",
            timestamp=frame.timestamps[0] if frame.timestamps else 0,
            detail=f"{graph.name} effects={list(graph.effects)}",
        )
    ]
    intents: list[Intent] = []

    for index, ts in ticks(interval, frame.timestamps):
        all_true = True
        for condition in graph.conditions:
            observed = frame.observe(condition.signal, index)
            passed = condition.evaluate(observed)
            log.append(
                LogEntry(
                    event="condition.evaluated",
                    timestamp=ts,
                    detail=(
                        f"{condition.signal} {condition.operator} {condition.value} "
                        f"observed={observed} -> {passed}"
                    ),
                )
            )
            if not passed:
                all_true = False
                break

        if all_true and graph.conditions:
            for intent_node in graph.intents:
                intent = Intent(
                    action=intent_node.action,
                    timestamp=ts,
                    asset=intent_node.asset,
                    confidence=intent_node.confidence,
                )
                intents.append(intent)
                log.append(
                    LogEntry(
                        event="intent.emitted",
                        timestamp=ts,
                        detail=f"{intent.action} asset={intent.asset}",
                    )
                )

    return ExecutionResult(intents=tuple(intents), log=tuple(log))
