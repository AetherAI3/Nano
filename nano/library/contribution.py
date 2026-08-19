"""Reusable controls for validating Nano strategy-library contributions.

The command-line checker lives in ``scripts/check_contribution.py``.  The
controls below are library behavior, though: the conformance suite imports and
tests them, and installed/editable distributions must expose them through the
``nano`` package rather than relying on the repository-only ``scripts`` tree.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from nano.ir.graph import StrategyGraph
from nano.ir.module import NanoModule
from nano.runtime.interpreter import MarketFrame


__all__ = (
    "baseline_control_frames",
    "module_control_frame",
    "source_provenance_issues",
)


PROVENANCE_FIELD = "SOURCE:"


def _comparison_candidates(conditions: list[Any]) -> tuple[float, ...]:
    """Boundary, adjacent, midpoint, and exterior values for linear guards."""
    thresholds = sorted({float(condition.value) for condition in conditions})
    span = max(1.0, *(abs(value) for value in thresholds))
    candidates = {
        value
        for threshold in thresholds
        for value in (
            threshold,
            math.nextafter(threshold, -math.inf),
            math.nextafter(threshold, math.inf),
        )
    }
    candidates.update(
        (left + right) / 2.0
        for left, right in zip(thresholds, thresholds[1:])
    )
    candidates.update((thresholds[0] - span, thresholds[-1] + span))
    return tuple(sorted(candidates))


def baseline_control_frames(graph: StrategyGraph) -> tuple[MarketFrame, MarketFrame]:
    """Return paired fire/no-fire frames for an AND-only baseline graph.

    Repeated constraints on one signal are solved together. An impossible
    conjunction is rejected instead of being replayed as a deterministic no-op.
    """
    if not graph.conditions:
        raise ValueError("baseline control requires at least one condition")

    conditions_by_signal: dict[str, list[Any]] = defaultdict(list)
    for condition in graph.conditions:
        conditions_by_signal[condition.signal].append(condition)

    passing_values: dict[str, float] = {}
    for name, conditions in conditions_by_signal.items():
        value = next(
            (
                candidate
                for candidate in _comparison_candidates(conditions)
                if all(condition.evaluate(candidate) for condition in conditions)
            ),
            None,
        )
        if value is None:
            raise ValueError(f"constraints on signal {name!r} cannot all be true")
        passing_values[name] = value

    passing = {name: (value,) * 2 for name, value in passing_values.items()}
    failing = dict(passing)
    first_name = graph.conditions[0].signal
    first_conditions = conditions_by_signal[first_name]
    failing_value = next(
        candidate
        for candidate in _comparison_candidates(first_conditions)
        if not all(condition.evaluate(candidate) for condition in first_conditions)
    )
    failing[first_name] = (failing_value,) * 2
    timestamps = (0, 86400)
    return (
        MarketFrame(timestamps=timestamps, signals=passing),
        MarketFrame(timestamps=timestamps, signals=failing),
    )


def module_control_frame(module: NanoModule) -> MarketFrame:
    """Return a non-degenerate frame with two bars beyond module warm-up."""
    count = module.warmup + 2
    close = tuple(
        100.0
        + 0.05 * index
        + 6.0 * math.sin(index / 7.0)
        + 2.0 * math.sin(index / 3.0)
        for index in range(count)
    )
    open_ = tuple(
        value + (0.1 if index % 2 else -0.1)
        for index, value in enumerate(close)
    )
    candidates = {
        "open": open_,
        "high": tuple(max(o, c) + 0.5 for o, c in zip(open_, close)),
        "low": tuple(min(o, c) - 0.5 for o, c in zip(open_, close)),
        "close": close,
        "volume": tuple(1000.0 + 25.0 * (index % 7) for index in range(count)),
    }
    signals = {
        declaration.name: candidates.get(
            declaration.name,
            tuple(value + position for value in close),
        )
        for position, declaration in enumerate(module.inputs)
    }
    return MarketFrame(
        timestamps=tuple(86400 * bar for bar in range(count)),
        signals=signals,
    )


def source_provenance_issues(header: list[str]) -> tuple[str, ...]:
    """Validate only the mechanically knowable part of optional provenance."""
    source_lines = [
        line for line in header if line.startswith(f"// {PROVENANCE_FIELD}")
    ]
    if len(source_lines) > 1:
        return (
            f"comment header has more than one `// {PROVENANCE_FIELD}` line. "
            "Record one concise provenance claim, or omit it when the source "
            "is not known.",
        )
    if source_lines and not source_lines[0].partition(PROVENANCE_FIELD)[2].strip():
        return (
            f"`// {PROVENANCE_FIELD}` is empty. Name a source you can "
            "truthfully verify, or remove the field; absence means provenance "
            "was not recorded.",
        )
    return ()
