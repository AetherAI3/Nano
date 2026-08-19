"""Runtime enforcement of the `risk { ... }` block.

A `risk` block used to parse, type-check, range-check, and land in the IR — and
then be read by nobody. This module is what makes the declaration true: it turns
a limit into a decision the VM makes at the moment an intent would be proposed,
and into log entries an auditor can read afterwards.

## What a risk limit is allowed to do

Suppress. Nothing else. A breach removes an actuating proposal from the run and
records why; it never rewrites an intent into a different one, never invents a
`PAUSE` the author did not write, and never gains any authority the module did
not already have. Nano proposes and the host disposes — a gate that *reduced*
what Nano proposes is the only kind of gate that can live inside Nano at all.

`PAUSE` and `OBSERVE` are therefore never suppressed. They are how a strategy
asks to be stopped and how it says what it saw; a breaker that silenced its own
halt because the book was in drawdown would be exactly backwards.

## Where the numbers come from

From the host, through the market frame, and from nowhere else. Nano does not
know your equity, your open orders, or how many losses you have taken in a row,
and it must not pretend to: every enforceable limit names one measurement series
the host supplies per bar, listed in `ENFORCED_LIMITS` below.

Those names are namespaced — `risk.drawdown`, not `drawdown`. A dot cannot occur
in a Nano identifier, so the measurement channel is unreachable from source and a
host measurement can never be confused with a feed signal a strategy reads by
name. That matters here more than most places: the strategy library reads a
`DRAWDOWN` feed expressed in *percent*, while `max_drawdown` is a *fraction of
equity*. Two conventions for one word is how a 5 percent stop becomes a 500
percent one, so the two never share a name.

## Boundaries, and what happens when a number is missing

**The allowed band is inclusive; a breach is strictly outside it.**
`max_drawdown 0.05` permits a drawdown of exactly 0.05 and breaches above it.
`min_confidence 0.6` permits exactly 0.6 and breaches below it.
`stop_trading_after_losses 3` is the one limit whose value names the first
*unacceptable* count rather than the last acceptable one — that is what "stop
after three losses" means — so its band is [0, 2] and it breaches at 3. The band
is still inclusive; only its upper edge is spelled differently.

**A measurement that is missing, absent, or not a finite number is a breach.**
Fail-closed, deliberately. NaN compares false against every threshold and
negative infinity compares below every threshold, so a naive `observed > limit`
would wave both through as "safe" — the two most dangerous inputs a risk gate can
be handed would be the two it ignored. A limit the host did not supply data for
is not a satisfied limit; it is an unverifiable one, and an unverifiable limit
stops the trade.

The same rule applies to `min_confidence`, which reads the intent's own declared
confidence and nothing else: `buy(BTC)` states no confidence, so it cannot be
shown to clear a minimum, so it does not. The `confidence` builtin is a separate
mechanism with a separate fallback chain, and letting it stand in here would let
an intent pass on the strength of a number its author never attached to it.

## The two limits this module refuses to enforce

`max_position_size` and `max_open_positions` cannot be honestly enforced from
anything Nano can see, and are listed in `HOST_ENFORCED_LIMITS` instead:

* A Nano intent carries an action, an asset, and a confidence. It carries **no
  order size**. Nothing in the language expresses the quantity a `buy` would put
  on, so Nano cannot tell whether accepting one would breach a position cap. It
  could refuse to trade while the *current* size is already over the cap, but
  that guard permits an arbitrarily large overage on the trade that causes it —
  it would put the words "max position size" on a control that does not bound
  position size.
* Nano equally cannot tell an opening trade from a closing one. A `sell` may
  flatten a long or open a short; a `buy` may start a position or add to one.
  Counting positions from that is guesswork with a number attached.

Both remain in the vocabulary, both still reach the IR, and the host — which does
know its book — can read them from `risk.limits` and apply them at the gate where
the size actually exists. What changes is that the run log now *says* Nano is not
applying them, so nobody infers enforcement from the declaration.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from ..ir.module import NanoModule
from .effects import LogEntry
from .interpreter import MarketFrame

# Prefix for every host-supplied risk measurement. A dot is not a legal character
# in a Nano identifier, so nothing a strategy can write reaches this namespace.
MEASUREMENT_PREFIX = "risk."

# Intents that ask the host to act in the market. Only these are gated; `PAUSE`
# and `OBSERVE` are how a strategy asks to be stopped and how it reports, and
# suppressing either would make a risk block less safe than no risk block.
ACTUATING_ACTIONS = frozenset({"BUY", "SELL", "EXECUTE"})


@dataclass(frozen=True)
class LimitRule:
    """One enforceable limit: where its number comes from and how it breaches.

    `mode` is deliberately a tiny closed vocabulary rather than a callable, so
    the whole enforcement table stays readable as data:

    * `exceeds` — breach when the observation is strictly above the limit.
    * `reaches` — breach when the observation reaches the limit (a count whose
      value names the first unacceptable number).
    * `below` — breach when the observation is strictly below the limit.
    """

    limit: str
    measurement: Optional[str]
    mode: str

    def breaches(self, observed: float, limit: float) -> bool:
        if self.mode == "exceeds":
            return observed > limit
        if self.mode == "reaches":
            return observed >= limit
        return observed < limit

    def band(self, limit: float) -> str:
        """The allowed band, spelled the way the log and the docs spell it."""
        if self.mode == "exceeds":
            return f"<= {limit}"
        if self.mode == "reaches":
            return f"< {limit}"
        return f">= {limit}"


# Order matters: it is the order simultaneous violations appear in the log, and
# it follows `RISK_LIMITS` so there is one declared vocabulary order rather than
# two that can drift. A test holds the two together.
ENFORCED_LIMITS: Tuple[LimitRule, ...] = (
    LimitRule("max_daily_loss", "risk.daily_loss", "exceeds"),
    LimitRule("max_drawdown", "risk.drawdown", "exceeds"),
    LimitRule("max_orders_per_day", "risk.orders_today", "exceeds"),
    LimitRule("stop_trading_after_losses", "risk.consecutive_losses", "reaches"),
    # No measurement: the observation is the intent's own declared confidence.
    LimitRule("min_confidence", None, "below"),
)

# Declared, carried to the host in the IR, and not enforced here. The reason
# travels with the name so the log can state it rather than gesture at docs.
HOST_ENFORCED_LIMITS: Tuple[Tuple[str, str], ...] = (
    (
        "max_position_size",
        "a Nano intent carries no order size, so the runtime cannot tell whether "
        "accepting one would breach a size cap — the host applies this limit "
        "where the size exists",
    ),
    (
        "max_open_positions",
        "a Nano intent cannot be told apart from a closing trade, so the runtime "
        "cannot count positions — the host applies this limit against its book",
    ),
)

_ENFORCED_BY_NAME = {rule.limit: rule for rule in ENFORCED_LIMITS}


def measurement_for(limit: str) -> Optional[str]:
    """The frame signal a limit reads, or None when it needs no host measurement."""
    rule = _ENFORCED_BY_NAME.get(limit)
    return rule.measurement if rule is not None else None


@dataclass(frozen=True)
class Violation:
    """One limit, breached once, with the sentence the log will carry."""

    limit: str
    detail: str


def _finite(value: Any) -> Optional[float]:
    """The value as a finite float, or None if it cannot serve as a measurement.

    Booleans are rejected along with strings and `None`: `True` is not a
    drawdown, and Python would happily float() it into 1.0.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


class RiskGate:
    """Decides whether one proposed intent survives the module's risk limits.

    Constructed once per run from the module's `risk.limits` node and the frame.
    A module without a risk block gets an inert gate: `armed` is false, `review`
    returns nothing, and no `risk.*` entry ever reaches the log — a strategy that
    declared no limits must behave exactly as it did before this module existed.
    """

    def __init__(self, limits: Mapping[str, Any], frame: MarketFrame) -> None:
        self._limits = dict(limits)
        self._frame = frame
        self._rules = tuple(
            rule for rule in ENFORCED_LIMITS if rule.limit in self._limits
        )
        self._unenforced = tuple(
            (name, why) for name, why in HOST_ENFORCED_LIMITS if name in self._limits
        )

    @staticmethod
    def for_module(module: NanoModule, frame: MarketFrame) -> "RiskGate":
        """Collect every `risk.limits` node in the module into one gate.

        The compiler emits at most one, but hand-written IR may carry several;
        merging in node order keeps a document with two blocks deterministic
        instead of silently honouring whichever one was found first.
        """
        limits: dict = {}
        for node in module.of_op("risk.limits"):
            declared = node.attrs.get("limits")
            if isinstance(declared, Mapping):
                limits.update(declared)
        return RiskGate(limits, frame)

    # -- declaration -------------------------------------------------------

    @property
    def armed(self) -> bool:
        """True when at least one limit is actually being enforced this run."""
        return bool(self._rules)

    def required_measurements(self) -> Tuple[str, ...]:
        """Frame signals the host must supply for this module's limits."""
        return tuple(
            rule.measurement for rule in self._rules if rule.measurement is not None
        )

    def declaration_log(self, timestamp: int) -> Tuple[LogEntry, ...]:
        """What the gate says about itself, once, at the top of the run."""
        entries: list[LogEntry] = []
        if self._rules:
            bands = ", ".join(
                f"{rule.limit} {rule.band(self._limits[rule.limit])}"
                for rule in self._rules
            )
            needs = ", ".join(self.required_measurements()) or "none"
            entries.append(
                LogEntry(
                    event="risk.armed",
                    timestamp=timestamp,
                    detail=f"{bands}; measurements: {needs}",
                )
            )
        for name, why in self._unenforced:
            entries.append(
                LogEntry(
                    event="risk.unenforced",
                    timestamp=timestamp,
                    detail=f"{name} {self._limits[name]}: not enforced by Nano — {why}",
                )
            )
        return tuple(entries)

    # -- decision ----------------------------------------------------------

    def review(
        self, action: str, confidence: Optional[float], bar: int
    ) -> Tuple[Violation, ...]:
        """Every limit this intent breaches, in declared vocabulary order.

        An empty tuple means the intent is proposed. Non-actuating intents are
        never reviewed, so they cannot be stopped by a limit.
        """
        if not self._rules or action not in ACTUATING_ACTIONS:
            return ()

        found: list[Violation] = []
        for rule in self._rules:
            limit = _finite(self._limits[rule.limit])
            observed, source = self._observe(rule, confidence, bar)
            if limit is None:
                # The IR validator requires a numeric limit, so this is only
                # reachable from a hand-built module. It fails closed like any
                # other unusable number.
                found.append(
                    Violation(
                        rule.limit,
                        f"{rule.limit}: declared limit is not a usable number",
                    )
                )
            elif observed is None:
                found.append(
                    Violation(
                        rule.limit,
                        f"{rule.limit} {limit}: {source} is unmeasured "
                        f"(fail-closed at bar {bar})",
                    )
                )
            elif rule.breaches(observed, limit):
                found.append(
                    Violation(
                        rule.limit,
                        f"{rule.limit} {limit}: {source} observed {observed} "
                        f"(allowed {rule.band(limit)})",
                    )
                )
        return tuple(found)

    def _observe(
        self, rule: LimitRule, confidence: Optional[float], bar: int
    ) -> Tuple[Optional[float], str]:
        """This bar's observation for one rule, plus what to call it in the log."""
        if rule.measurement is None:
            if confidence is None:
                return None, "the intent declares no confidence"
            return _finite(confidence), "intent confidence"

        series = self._frame.signals.get(rule.measurement)
        if series is None or bar >= len(series):
            return None, rule.measurement
        return _finite(series[bar]), rule.measurement

    def suppression_log(
        self, action: str, timestamp: int, violations: Sequence[Violation]
    ) -> Tuple[LogEntry, ...]:
        """One entry per breached limit, then the suppression itself."""
        entries = [
            LogEntry(event="risk.violation", timestamp=timestamp, detail=v.detail)
            for v in violations
        ]
        breached = ", ".join(v.limit for v in violations)
        entries.append(
            LogEntry(
                event="intent.suppressed",
                timestamp=timestamp,
                detail=f"{action} withheld by risk limits: {breached}",
            )
        )
        return tuple(entries)


__all__ = [
    "ACTUATING_ACTIONS",
    "ENFORCED_LIMITS",
    "HOST_ENFORCED_LIMITS",
    "MEASUREMENT_PREFIX",
    "LimitRule",
    "RiskGate",
    "Violation",
    "measurement_for",
]
