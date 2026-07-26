"""Baseline (v0.1.0) shape recovery — how the compiler picks an IR version.

Nano's central claim is that the artifact that backtested is the artifact that
trades is the artifact the audit replays. Byte-stability of compiled output is
therefore not a nicety; it is the claim. Twenty-one `.nano`/`_ir.json` pairs in
`nano/examples/` and `nano/library/` assert it, and Aether Code ships a vendored
snapshot pinned to `0.1.0`.

So v1.0 does not renumber everything. **The compiler emits the lowest IR version
that can express the program.** This module answers the only question that
decision needs: *is this program expressible in baseline IR?*

Baseline IR is a flat list — one schedule, an `and`-chain of
`SIGNAL <op> NUMBER` conditions, some intents, some agents. It has no way to
represent a second rule, an `else` branch, a computed series, a typed param, a
risk limit, or a reasoning call. A program that stays inside those limits
compiles to the same bytes it did before v1.0 existed. A program that reaches
past them becomes `1.0.0`, where those things have a representation.

`nano compile --ir-version` overrides the inference when a host wants one shape
regardless of content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..ir.schema import CONDITION_OPERATORS
from ..types.checker import ResolvedFeed, TypedProgram
from .ast import (
    ActionAst,
    AgentAst,
    Binary,
    Call,
    ConditionAst,
    Expr,
    Name,
    NumberLit,
)

# One source for the operator vocabulary: the IR schema, which is what a
# `Condition` node may actually carry.
_COMPARISON_OPS = CONDITION_OPERATORS


@dataclass(frozen=True)
class BaselineShape:
    """A program flattened into the baseline document's four node kinds."""

    interval: Optional[str]
    conditions: Tuple[ConditionAst, ...]
    actions: Tuple[ActionAst, ...]
    agents: Tuple[AgentAst, ...]


def _flatten_and_chain(expr: Expr) -> List[Expr]:
    """Split a left-associated `and` chain into its leaves, in source order."""
    if isinstance(expr, Binary) and expr.op == "and":
        return _flatten_and_chain(expr.left) + _flatten_and_chain(expr.right)
    return [expr]


def _as_feed_signal(expr: Expr, program: TypedProgram) -> Optional[str]:
    """The signal name, if `expr` reads a host-supplied feed signal.

    Both spellings count: a bare `RSI`, and the documented `RSI(14)` form whose
    integer argument is a contract with the feed rather than a computation.
    Membership is decided by what the checker resolved, not by re-guessing here.
    """
    if not isinstance(expr, (Name, Call)):
        return None
    resolution = program.resolution_of(expr)
    if isinstance(resolution, ResolvedFeed):
        return resolution.signal
    return None


def _as_condition(expr: Expr, program: TypedProgram) -> Optional[ConditionAst]:
    """Flatten one comparison into a baseline `Condition`, or give up."""
    if not isinstance(expr, Binary) or expr.op not in _COMPARISON_OPS:
        return None
    signal = _as_feed_signal(expr.left, program)
    if signal is None or not isinstance(expr.right, NumberLit):
        return None
    return ConditionAst(signal=signal, operator=expr.op, value=expr.right.value)


def baseline_shape(program: TypedProgram) -> Optional[BaselineShape]:
    """Flatten `program` into baseline shape, or return None if it does not fit.

    Every rejection below names a construct baseline IR has no node for. None of
    them is a shortcoming of this function — adding a case would mean inventing a
    representation, and a `0.1.0` document that a `0.1.0` reader cannot
    understand is worse than a `1.0.0` one.
    """
    strategy = program.strategy

    if strategy.tier != "nano":
        return None
    if strategy.params or strategy.inputs or strategy.lets:
        return None
    if strategy.risk is not None or strategy.signatures or strategy.routes:
        return None
    if any(agent.role is not None for agent in strategy.agents):
        return None
    if len(strategy.schedules) > 1:
        return None

    interval: Optional[str] = None
    conditions: Tuple[ConditionAst, ...] = ()
    actions: Tuple[ActionAst, ...] = ()

    if strategy.schedules:
        schedule = strategy.schedules[0]
        interval = schedule.interval
        if len(schedule.rules) > 1:
            return None

        if schedule.rules:
            rule = schedule.rules[0]
            if rule.otherwise:
                return None
            if not all(isinstance(s, ActionAst) for s in rule.then):
                return None

            flattened: List[ConditionAst] = []
            for leaf in _flatten_and_chain(rule.when):
                condition = _as_condition(leaf, program)
                if condition is None:
                    return None
                flattened.append(condition)
            if not flattened:
                # An unconditional rule -- `every 5m { observe() }` -- is a v1.0
                # addition. Baseline required an `if`, and its interpreter only
                # emits intents when at least one condition held.
                return None

            conditions = tuple(flattened)
            actions = tuple(s for s in rule.then if isinstance(s, ActionAst))

    return BaselineShape(
        interval=interval,
        conditions=conditions,
        actions=actions,
        agents=strategy.agents,
    )
