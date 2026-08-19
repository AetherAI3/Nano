"""Runtime enforcement of the `risk { ... }` block.

The normative semantics — which limits are enforced, what each one reads, where
the boundary sits, and why two limits are the host's to apply — are specified
once in `docs/language.md` ("Risk limits"). This module implements that
specification and does not restate it; a second prose copy would be a second
thing to keep true, and the two would drift.

What is worth saying here is the shape:

**Suppress, and nothing else.** A breach removes an actuating proposal from the
run and records why. It never rewrites one intent into another, never invents a
`PAUSE` the author did not write, and grants the module no effect it did not
already declare. Nano proposes and the host disposes — a gate that only ever
*reduces* what Nano proposes is the only kind that can live inside Nano.

**No ambient state.** A gate holds its limits and this run's signals. The VM
supplies how many actuating intents have already survived at the current
timestamp so an order-count limit cannot overbook one frame. Nothing carries
across frames, so enforcement is exactly as replayable as the rest of the run.

**Two tables, and every keyword in exactly one.** `ENFORCED_LIMITS` is what the
runtime applies; `HOST_ENFORCED_LIMITS` is what it announces it will not. A
keyword in neither is the failure this module exists to end, and a test holds the
union of the two to the grammar's vocabulary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from ..ir.module import NanoModule
from ..ir.schema import ACTUATING_INTENT_ACTIONS
from .effects import Intent, LogEntry
from .interpreter import MarketFrame

# Every host-supplied measurement is namespaced under this prefix. It contains a
# dot, which no Nano identifier may, so the measurement channel is unreachable
# from source and cannot collide with a feed signal a strategy reads by name.
# The names below are built from it rather than spelled out, so the property
# holds by construction instead of by review.
MEASUREMENT_PREFIX = "risk."

# Intents that ask the host to act in the market. Only these are gated: `PAUSE`
# and `OBSERVE` are how a strategy asks to be stopped and how it reports, and
# suppressing either would make a risk block less safe than no risk block. The
# split is declared in the schema so the type checker gates on the same three.
ACTUATING_ACTIONS = ACTUATING_INTENT_ACTIONS


@dataclass(frozen=True)
class LimitRule:
    """One enforceable limit: where its number comes from and how it breaches.

    `mode` is a tiny closed vocabulary rather than a callable, so the whole
    enforcement table stays readable as data:

    * `exceeds` — breach when the observation is strictly above the limit.
    * `reaches` — breach when the observation reaches the limit (a count whose
      value names the first unacceptable number).
    * `below` — breach when the observation is strictly below the limit.

    `observation` is the noun the log uses for the thing being measured, so a
    violation line reads as a sentence rather than as a signal name.

    `minimum_observation` is the host measurement's domain floor, where one
    exists. `includes_emitted_capacity` marks the one rule whose observed count
    must include actuating intents already accepted at this timestamp.
    """

    limit: str
    measurement: Optional[str]
    mode: str
    observation: str
    minimum_observation: Optional[float] = None
    includes_emitted_capacity: bool = False

    def breaches(self, observed: float, limit: float) -> bool:
        if self.mode == "exceeds":
            return observed > limit
        if self.mode == "reaches":
            return observed >= limit
        return observed < limit

    def band(self, limit: Any) -> str:
        """The allowed band, spelled one way for every log line that shows it."""
        if self.mode == "exceeds":
            return f"<= {limit}"
        if self.mode == "reaches":
            return f"< {limit}"
        return f">= {limit}"


def _measurement(name: str) -> str:
    return MEASUREMENT_PREFIX + name


# Order matters: it is the order simultaneous violations appear in the log, and
# it follows the schema's `RISK_LIMITS` so there is one declared vocabulary order
# rather than two that can drift. A test holds the two together.
ENFORCED_LIMITS: Tuple[LimitRule, ...] = (
    LimitRule("max_daily_loss", _measurement("daily_loss"), "exceeds", "daily loss"),
    LimitRule(
        "max_drawdown",
        _measurement("drawdown"),
        "exceeds",
        "drawdown",
        minimum_observation=0.0,
    ),
    LimitRule(
        "max_orders_per_day",
        _measurement("orders_today"),
        "reaches",
        "order count",
        minimum_observation=0.0,
        includes_emitted_capacity=True,
    ),
    LimitRule(
        "stop_trading_after_losses",
        _measurement("consecutive_losses"),
        "reaches",
        "consecutive losses",
        minimum_observation=0.0,
    ),
    # No measurement: the observation is the intent's own declared confidence.
    LimitRule("min_confidence", None, "below", "the intent's declared confidence"),
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


@dataclass(frozen=True)
class Violation:
    """One limit, breached once, with the sentence the log will carry."""

    limit: str
    detail: str


def _finite(value: Any) -> Optional[float]:
    """The value as a finite float, or None if it cannot serve as a measurement.

    Booleans are rejected along with strings and `None`. `bool` is a subclass of
    `int`, so without the guard `False` would arrive as a perfectly satisfied
    order count and `True` as a one-percent drawdown.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


class RiskGate:
    """Decides whether one proposed intent survives the module's risk limits.

    Built once per run. A module without a risk block gets an inert gate: nothing
    is reviewed and no `risk.*` entry reaches the log, so a strategy that
    declared no limits behaves exactly as it did before this module existed.

    The frame is optional, and neither `required_measurements` nor
    `declaration_log` consults it — a host, or the CLI, can ask a module what
    data it will need *before* it has any.
    """

    def __init__(
        self, limits: Mapping[str, Any], frame: Optional[MarketFrame] = None
    ) -> None:
        self._limits = dict(limits)
        self._signals = dict(frame.signals) if frame is not None else {}
        self._rules = tuple(
            rule for rule in ENFORCED_LIMITS if rule.limit in self._limits
        )
        self._unenforced = tuple(
            (name, why) for name, why in HOST_ENFORCED_LIMITS if name in self._limits
        )

    @staticmethod
    def for_module(
        module: NanoModule, frame: Optional[MarketFrame] = None
    ) -> "RiskGate":
        """The gate a module's `risk.limits` node describes.

        A document carrying two such nodes is rejected at load, matching the
        parser's refusal of two `risk` blocks, so at most one is ever found here.
        The first is taken rather than the last for the one caller that can get
        past the loader (`run_module(validate=False)`): a later declaration must
        never be able to loosen an earlier one.
        """
        nodes = module.of_op("risk.limits")
        if not nodes:
            return RiskGate({}, frame)
        declared = nodes[0].attrs.get("limits")
        return RiskGate(declared if isinstance(declared, Mapping) else {}, frame)

    # -- declaration -------------------------------------------------------

    def required_measurements(self) -> Tuple[str, ...]:
        """Frame signals the host must supply for this module's limits."""
        return tuple(
            rule.measurement for rule in self._rules if rule.measurement is not None
        )

    def declaration_log(self, timestamp: int) -> Tuple[LogEntry, ...]:
        """What the gate says about itself, once, at the top of the run."""
        entries: List[LogEntry] = []
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
        self, intent: Intent, bar: int, *, emitted_capacity: int = 0
    ) -> Tuple[Violation, ...]:
        """Every limit this intent breaches, in declared vocabulary order.

        An empty tuple means the intent is proposed. Non-actuating intents are
        never reviewed, so no limit can stop one. `emitted_capacity` counts only
        actuating intents that already survived at this intent's timestamp.
        """
        if not self._rules or intent.action not in ACTUATING_ACTIONS:
            return ()

        found: List[Violation] = []
        for rule in self._rules:
            # The declared value is shown exactly as the document spells it, here
            # and in `risk.armed` alike; only the comparison uses the coerced
            # float, so one limit never appears in the log under two spellings.
            declared = self._limits[rule.limit]
            limit = _finite(declared)
            observed, observation_error = self._observe(
                rule, intent, bar, emitted_capacity
            )
            if limit is None:
                # The loader requires a numeric, finite, in-range limit, so this
                # is reachable only through `run_module(validate=False)`. It
                # fails closed like any other unusable number.
                found.append(
                    Violation(
                        rule.limit,
                        f"{rule.limit}: declared limit is not a usable number",
                    )
                )
            elif observation_error is not None:
                found.append(
                    Violation(
                        rule.limit,
                        f"{rule.limit} {declared}: {observation_error} "
                        f"(fail-closed at bar {bar})",
                    )
                )
            elif observed is None:
                found.append(
                    Violation(
                        rule.limit,
                        f"{rule.limit} {declared}: {rule.observation} is "
                        f"unmeasured (fail-closed at bar {bar})",
                    )
                )
            elif rule.breaches(observed, limit):
                found.append(
                    Violation(
                        rule.limit,
                        f"{rule.limit} {declared}: {rule.observation} observed "
                        f"{observed} (allowed {rule.band(declared)})",
                    )
                )
        return tuple(found)

    def _observe(
        self, rule: LimitRule, intent: Intent, bar: int, emitted_capacity: int
    ) -> Tuple[Optional[float], Optional[str]]:
        """Return this bar's value and, separately, any domain violation."""
        if rule.measurement is None:
            observed = _finite(intent.confidence)
        else:
            series = self._signals.get(rule.measurement)
            # `MarketFrame` already refuses a series whose length does not match
            # the timeline, so the bounds half of this is unreachable through
            # the public constructor. It stays because a gate can be built from
            # any mapping, and an IndexError inside a risk check is the worst
            # place to learn that.
            if series is None or bar >= len(series):
                return None, None
            observed = _finite(series[bar])

        # Domains belong to rules, not to `_finite`: a negative daily loss is a
        # valid profit/no-loss observation, while negative drawdown and counts
        # are impossible measurements and therefore fail closed.
        if observed is None:
            return None, None
        if (
            rule.minimum_observation is not None
            and observed < rule.minimum_observation
        ):
            return (
                None,
                f"{rule.observation} observed {observed} outside valid domain "
                f">= {rule.minimum_observation}",
            )
        if rule.includes_emitted_capacity:
            if type(emitted_capacity) is not int or emitted_capacity < 0:
                return (
                    None,
                    "accepted intent capacity outside valid domain: "
                    "expected a nonnegative integer within finite numeric range",
                )
            try:
                observed += emitted_capacity
            except OverflowError:
                return (
                    None,
                    "accepted intent capacity outside valid domain: "
                    "expected a nonnegative integer within finite numeric range",
                )
            if not math.isfinite(observed):
                return (
                    None,
                    "accepted intent capacity outside valid domain: "
                    "expected a nonnegative integer within finite numeric range",
                )
        return observed, None

    def suppression_log(
        self, intent: Intent, violations: Sequence[Violation]
    ) -> Tuple[LogEntry, ...]:
        """One entry per breached limit, then the suppression itself.

        The suppression line carries the identifying fields `intent.emitted`
        carries. Without them, two intents withheld on the same bar are two
        byte-identical lines, and a host reading the log cannot tell which asset
        it was not asked to trade.
        """
        entries = [
            LogEntry(
                event="risk.violation", timestamp=intent.timestamp, detail=v.detail
            )
            for v in violations
        ]
        entries.append(
            LogEntry(
                event="intent.suppressed",
                timestamp=intent.timestamp,
                detail=(
                    f"{intent.action} asset={intent.asset} "
                    f"confidence={intent.confidence} withheld by risk limits: "
                    + ", ".join(v.limit for v in violations)
                ),
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
]
