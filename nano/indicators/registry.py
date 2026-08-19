"""Indicator signatures — the typed contract between source and computation.

Until v1.0 Nano never computed an indicator: `RSI(14) < 30` named a signal the
host feed supplied, and the `(14)` was documentation. That **feed-signal form**
still works and still compiles to a bare `Condition` node. v1.0 adds the
**computed form** — `EMA(price, 20)`, where `price` is a declared `input` and
Nano derives the series itself, deterministically, from data already in the
frame.

The two forms are distinguished structurally, never by heuristic:

    RSI(14)          one static integer, no series  -> feed signal, host-supplied
    RSI(close, 14)   a series argument              -> computed by nano/indicators

Three parameter kinds, and the difference matters:

* `series<float>` — history-consuming. The argument must really be a series; a
  scalar cannot satisfy it, because the kernel needs prior bars.
* `int` — a **period**. Must be a compile-time constant so warm-up length is
  known before any data arrives. Periods never lift over series.
* `float` — a plain runtime value. May be scalar or series; if any `float`
  argument is a series the whole call lifts and evaluates elementwise.

`lookback` is how many bars must elapse before a value exists. It is a function
of the resolved period arguments, not a constant, because MACD's warm-up depends
on three of them. The VM refuses to emit an intent from an unwarmed bar, which
is the other half of look-ahead safety: never peek forward, never fabricate
backward.

**This module is a leaf.** Parameter and return types are the canonical *type
spellings* (`"series<float>"`), not `nano.types` objects, and the checker parses
them with `parse_type`. Importing the type system here would close a cycle —
`indicators` -> `types` -> `checker` -> `indicators` — whose failure depends on
which package a caller happens to import first. A declarative table that the
type system interprets has no such ordering hazard, and it keeps the registry
readable as data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

# Canonical type spellings, matching nano.types.kinds.Type.__str__.
INT = "int"
FLOAT = "float"
SERIES_FLOAT = "series<float>"
SERIES_BOOL = "series<bool>"

# A lookback rule maps the call's resolved period arguments to a warm-up length.
LookbackRule = Callable[[Tuple[int, ...]], int]


@dataclass(frozen=True)
class IndicatorSpec:
    """One indicator's type signature, warm-up rule, and documentation."""

    name: str
    params: Tuple[str, ...]
    returns: str
    lookback: LookbackRule
    doc: str

    @property
    def arity(self) -> int:
        return len(self.params)

    @property
    def period_indices(self) -> Tuple[int, ...]:
        """Positions of the `int` parameters — the ones that must be constant."""
        return tuple(i for i, p in enumerate(self.params) if p == INT)

    @property
    def series_indices(self) -> Tuple[int, ...]:
        """Positions that require a genuine series argument."""
        return tuple(i for i, p in enumerate(self.params) if p.startswith("series<"))

    @property
    def lifts(self) -> bool:
        """True when a scalar `float` parameter may receive a series and lift."""
        return any(p == FLOAT for p in self.params)

    def signature_text(self) -> str:
        """`EMA(series<float>, int) -> series<float>` — for hovers and `--emit types`."""
        return f"{self.name}({', '.join(self.params)}) -> {self.returns}"


def _fixed(n: int) -> LookbackRule:
    return lambda _periods: n


def _first_period(offset: int = 0) -> LookbackRule:
    """Warm-up derived from the first period argument, plus `offset`."""
    return lambda periods: (periods[0] + offset if periods else 0)


def _max_period(offset: int = 0) -> LookbackRule:
    """Warm-up derived from the largest period argument, plus `offset`."""
    return lambda periods: (max(periods) + offset if periods else 0)


def _macd_signal_lookback(periods: Tuple[int, ...]) -> int:
    """MACD signal/histogram warm up once the line is warm, then again for its EMA."""
    fast, slow, signal = periods[0], periods[1], periods[2]
    return max(fast, slow) - 1 + signal - 1


_SPECS: Tuple[IndicatorSpec, ...] = (
    # -- moving averages and dispersion ------------------------------------
    IndicatorSpec(
        "SMA", (SERIES_FLOAT, INT), SERIES_FLOAT, _first_period(-1),
        "Simple moving average over the last `period` bars.",
    ),
    IndicatorSpec(
        "EMA", (SERIES_FLOAT, INT), SERIES_FLOAT, _first_period(-1),
        "Exponential moving average, alpha = 2/(period+1), seeded with the "
        "first `period`-bar SMA.",
    ),
    IndicatorSpec(
        "WMA", (SERIES_FLOAT, INT), SERIES_FLOAT, _first_period(-1),
        "Linearly weighted moving average; the newest bar carries weight `period`.",
    ),
    IndicatorSpec(
        "STDDEV", (SERIES_FLOAT, INT), SERIES_FLOAT, _first_period(-1),
        "Population standard deviation over the last `period` bars.",
    ),
    IndicatorSpec(
        "ZSCORE", (SERIES_FLOAT, INT), SERIES_FLOAT, _first_period(-1),
        "(value - SMA) / STDDEV over `period` bars; 0 when dispersion is 0.",
    ),
    IndicatorSpec(
        "SUM", (SERIES_FLOAT, INT), SERIES_FLOAT, _first_period(-1),
        "Rolling sum over the last `period` bars.",
    ),
    # -- momentum -----------------------------------------------------------
    IndicatorSpec(
        "RSI", (SERIES_FLOAT, INT), SERIES_FLOAT, _first_period(),
        "Wilder's relative strength index, 0..100.",
    ),
    IndicatorSpec(
        "ROC", (SERIES_FLOAT, INT), SERIES_FLOAT, _first_period(),
        "Rate of change as a percentage over `period` bars.",
    ),
    IndicatorSpec(
        "MOM", (SERIES_FLOAT, INT), SERIES_FLOAT, _first_period(),
        "Absolute change over `period` bars: value - value[period].",
    ),
    IndicatorSpec(
        "CHANGE", (SERIES_FLOAT,), SERIES_FLOAT, _fixed(1),
        "Bar-over-bar difference: value - value[1].",
    ),
    # -- extremes -----------------------------------------------------------
    IndicatorSpec(
        "HIGHEST", (SERIES_FLOAT, INT), SERIES_FLOAT, _first_period(-1),
        "Highest value over the last `period` bars.",
    ),
    IndicatorSpec(
        "LOWEST", (SERIES_FLOAT, INT), SERIES_FLOAT, _first_period(-1),
        "Lowest value over the last `period` bars.",
    ),
    # -- range and volatility -----------------------------------------------
    IndicatorSpec(
        "TR", (SERIES_FLOAT, SERIES_FLOAT, SERIES_FLOAT), SERIES_FLOAT, _fixed(1),
        "True range from (high, low, close).",
    ),
    IndicatorSpec(
        "ATR", (SERIES_FLOAT, SERIES_FLOAT, SERIES_FLOAT, INT), SERIES_FLOAT,
        _first_period(),
        "Average true range from (high, low, close), Wilder-smoothed.",
    ),
    IndicatorSpec(
        "SUPERTREND", (SERIES_FLOAT, SERIES_FLOAT, SERIES_FLOAT, INT, FLOAT),
        SERIES_FLOAT, _first_period(),
        "SuperTrend trailing line from (high, low, close): the ATR(`period`) band, "
        "`mult` wide, that the current trend is riding.",
    ),
    IndicatorSpec(
        "SUPERTREND_DIR", (SERIES_FLOAT, SERIES_FLOAT, SERIES_FLOAT, INT, FLOAT),
        SERIES_BOOL, _first_period(),
        "True while the SuperTrend line sits below price, False while above. "
        "A flip is this value differing from the previous bar's.",
    ),
    # -- oscillators over (high, low, close) --------------------------------
    IndicatorSpec(
        "STOCH_K", (SERIES_FLOAT, SERIES_FLOAT, SERIES_FLOAT, INT), SERIES_FLOAT,
        _first_period(-1),
        "Stochastic %K over `period` bars, 0..100.",
    ),
    IndicatorSpec(
        "WILLR", (SERIES_FLOAT, SERIES_FLOAT, SERIES_FLOAT, INT), SERIES_FLOAT,
        _first_period(-1),
        "Williams %R over `period` bars, -100..0.",
    ),
    IndicatorSpec(
        "CCI", (SERIES_FLOAT, SERIES_FLOAT, SERIES_FLOAT, INT), SERIES_FLOAT,
        _first_period(-1),
        "Commodity channel index over `period` bars.",
    ),
    # -- volume -------------------------------------------------------------
    IndicatorSpec(
        "OBV", (SERIES_FLOAT, SERIES_FLOAT), SERIES_FLOAT, _fixed(1),
        "On-balance volume from (close, volume); starts at 0 on the first warm bar.",
    ),
    IndicatorSpec(
        "VWAP", (SERIES_FLOAT, SERIES_FLOAT, INT), SERIES_FLOAT, _first_period(-1),
        "Rolling volume-weighted average price from (price, volume) over `period`.",
    ),
    # -- MACD family --------------------------------------------------------
    IndicatorSpec(
        "MACD_LINE", (SERIES_FLOAT, INT, INT), SERIES_FLOAT, _max_period(-1),
        "EMA(fast) - EMA(slow).",
    ),
    IndicatorSpec(
        "MACD_SIGNAL", (SERIES_FLOAT, INT, INT, INT), SERIES_FLOAT,
        _macd_signal_lookback,
        "EMA(signal) of the MACD line.",
    ),
    IndicatorSpec(
        "MACD_HIST", (SERIES_FLOAT, INT, INT, INT), SERIES_FLOAT,
        _macd_signal_lookback,
        "MACD line minus its signal line.",
    ),
    # -- Bollinger family ---------------------------------------------------
    IndicatorSpec(
        "BB_MIDDLE", (SERIES_FLOAT, INT), SERIES_FLOAT, _first_period(-1),
        "Bollinger middle band: SMA(period).",
    ),
    IndicatorSpec(
        "BB_UPPER", (SERIES_FLOAT, INT, FLOAT), SERIES_FLOAT, _first_period(-1),
        "Bollinger upper band: SMA + mult * STDDEV.",
    ),
    IndicatorSpec(
        "BB_LOWER", (SERIES_FLOAT, INT, FLOAT), SERIES_FLOAT, _first_period(-1),
        "Bollinger lower band: SMA - mult * STDDEV.",
    ),
    IndicatorSpec(
        "BB_PCT_B", (SERIES_FLOAT, INT, FLOAT), SERIES_FLOAT, _first_period(-1),
        "Position within the bands: (value - lower) / (upper - lower).",
    ),
    IndicatorSpec(
        "BB_WIDTH", (SERIES_FLOAT, INT, FLOAT), SERIES_FLOAT, _first_period(-1),
        "Band width as a percentage of the middle band.",
    ),
    # -- crosses ------------------------------------------------------------
    IndicatorSpec(
        "CROSSOVER", (SERIES_FLOAT, SERIES_FLOAT), SERIES_BOOL, _fixed(1),
        "True on the bar where the first series crosses above the second.",
    ),
    IndicatorSpec(
        "CROSSUNDER", (SERIES_FLOAT, SERIES_FLOAT), SERIES_BOOL, _fixed(1),
        "True on the bar where the first series crosses below the second.",
    ),
    # -- elementwise scalar maths (lift over series) ------------------------
    IndicatorSpec("ABS", (FLOAT,), FLOAT, _fixed(0), "Absolute value."),
    IndicatorSpec(
        "SQRT", (FLOAT,), FLOAT, _fixed(0),
        "Square root. A negative input yields no value for that bar.",
    ),
    IndicatorSpec("MIN", (FLOAT, FLOAT), FLOAT, _fixed(0), "Lesser of two values."),
    IndicatorSpec("MAX", (FLOAT, FLOAT), FLOAT, _fixed(0), "Greater of two values."),
)

INDICATORS: Dict[str, IndicatorSpec] = {spec.name: spec for spec in _SPECS}


def lookup(name: str) -> Optional[IndicatorSpec]:
    return INDICATORS.get(name)


def names() -> Tuple[str, ...]:
    return tuple(sorted(INDICATORS))


def is_indicator(name: str) -> bool:
    return name in INDICATORS
