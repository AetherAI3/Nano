"""Deterministic indicator kernels.

Every kernel is a pure function of its inputs: no ambient clock, no RNG, no
accumulated instance state. Same series in, same series out, bit-for-bit, which
is what lets a compiled strategy replay identically a year later.

**Cells may be absent.** A series cell is `None` when no value exists — either
the indicator has not warmed up yet, or the feed had a gap. Absence is never
filled in. Fabricating a warm-up value is the mirror image of look-ahead: both
invent data the strategy could not have had, and both inflate a backtest.

**Recursive indicators reset on a gap.** EMA, RSI, ATR, and OBV carry state
across bars, so they operate on *contiguous runs* of present values: a `None`
clears the accumulator and the kernel re-seeds from the next full window. The
alternative — smoothing across a hole as if it were not there — would make the
result depend on how the feed was chunked.

Conventions where the classic formula divides by zero are pinned here rather
than left to the caller, because an unpinned convention is a silent divergence
between two runtimes:

| Situation | Value |
|---|---|
| RSI with no losses in the window | `100.0` |
| ZSCORE / CCI with zero dispersion | `0.0` |
| STOCH_K with a flat range | `50.0` |
| WILLR with a flat range | `-50.0` |
| BB_PCT_B with zero band width | `0.5` |
| ROC from a zero base, VWAP with zero volume, BB_WIDTH on a zero mid | absent |
| SQRT of a negative | absent |
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Sequence, Tuple, Union

Cell = Optional[Union[float, bool]]
Series = Tuple[Cell, ...]
Scalar = Union[int, float, bool, None]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _floats(series: Sequence[Cell]) -> Tuple[Optional[float], ...]:
    """View a series as floats, preserving absence."""
    return tuple(None if c is None else float(c) for c in series)


def _cell(value: Union[Series, Scalar], index: int) -> Optional[float]:
    """Read `value` at `index`, broadcasting scalars across every bar."""
    if isinstance(value, tuple):
        cell = value[index] if index < len(value) else None
        return None if cell is None else float(cell)
    return None if value is None else float(value)


def _window(
    values: Tuple[Optional[float], ...], index: int, period: int
) -> Optional[Tuple[float, ...]]:
    """The `period` bars ending at `index`, or None if incomplete or gapped."""
    if index + 1 < period:
        return None
    window = values[index + 1 - period : index + 1]
    if any(v is None for v in window):
        return None
    return tuple(float(v) for v in window)


def _apply(fn, *args: Optional[float]) -> Optional[float]:
    if any(a is None for a in args):
        return None
    return fn(*args)


def _map1(values: Union[Series, Scalar], length: int, fn) -> Series:
    return tuple(_apply(fn, _cell(values, i)) for i in range(length))


def _map2(a: Union[Series, Scalar], b: Union[Series, Scalar], length: int, fn) -> Series:
    return tuple(_apply(fn, _cell(a, i), _cell(b, i)) for i in range(length))


# ---------------------------------------------------------------------------
# moving averages and dispersion
# ---------------------------------------------------------------------------


def sma(values: Series, period: int) -> Series:
    data = _floats(values)
    out: list[Cell] = []
    for i in range(len(data)):
        window = _window(data, i, period)
        out.append(None if window is None else sum(window) / period)
    return tuple(out)


def ema(values: Series, period: int) -> Series:
    """Alpha = 2/(period+1), seeded with the first complete `period`-bar mean."""
    data = _floats(values)
    alpha = 2.0 / (period + 1)
    out: list[Cell] = []
    run: list[float] = []
    state: Optional[float] = None
    for value in data:
        if value is None:
            run = []
            state = None
            out.append(None)
            continue
        run.append(value)
        if len(run) < period:
            out.append(None)
        elif len(run) == period:
            state = sum(run) / period
            out.append(state)
        else:
            state = alpha * value + (1.0 - alpha) * float(state)
            out.append(state)
    return tuple(out)


def wma(values: Series, period: int) -> Series:
    data = _floats(values)
    denominator = period * (period + 1) / 2.0
    out: list[Cell] = []
    for i in range(len(data)):
        window = _window(data, i, period)
        if window is None:
            out.append(None)
            continue
        weighted = sum(value * (offset + 1) for offset, value in enumerate(window))
        out.append(weighted / denominator)
    return tuple(out)


def stddev(values: Series, period: int) -> Series:
    """Population standard deviation — divides by `period`, not `period - 1`."""
    data = _floats(values)
    out: list[Cell] = []
    for i in range(len(data)):
        window = _window(data, i, period)
        if window is None:
            out.append(None)
            continue
        mean = sum(window) / period
        variance = sum((value - mean) ** 2 for value in window) / period
        out.append(variance ** 0.5)
    return tuple(out)


def zscore(values: Series, period: int) -> Series:
    data = _floats(values)
    means = sma(values, period)
    deviations = stddev(values, period)
    out: list[Cell] = []
    for i in range(len(data)):
        value, mean, deviation = data[i], means[i], deviations[i]
        if value is None or mean is None or deviation is None:
            out.append(None)
        elif float(deviation) == 0.0:
            out.append(0.0)
        else:
            out.append((value - float(mean)) / float(deviation))
    return tuple(out)


def rolling_sum(values: Series, period: int) -> Series:
    data = _floats(values)
    out: list[Cell] = []
    for i in range(len(data)):
        window = _window(data, i, period)
        out.append(None if window is None else sum(window))
    return tuple(out)


# ---------------------------------------------------------------------------
# momentum
# ---------------------------------------------------------------------------


def rsi(values: Series, period: int) -> Series:
    """Wilder's RSI. Seeds on the first `period` differences of a contiguous run."""
    data = _floats(values)
    out: list[Cell] = []
    gains: list[float] = []
    losses: list[float] = []
    previous: Optional[float] = None
    average_gain: Optional[float] = None
    average_loss: Optional[float] = None

    for value in data:
        if value is None:
            previous = None
            gains, losses = [], []
            average_gain = average_loss = None
            out.append(None)
            continue
        if previous is None:
            previous = value
            out.append(None)
            continue

        difference = value - previous
        previous = value
        gain = max(difference, 0.0)
        loss = max(-difference, 0.0)

        if average_gain is None:
            gains.append(gain)
            losses.append(loss)
            if len(gains) < period:
                out.append(None)
                continue
            average_gain = sum(gains) / period
            average_loss = sum(losses) / period
        else:
            average_gain = (average_gain * (period - 1) + gain) / period
            average_loss = (float(average_loss) * (period - 1) + loss) / period

        if average_loss == 0.0:
            out.append(100.0)
        else:
            rs = average_gain / float(average_loss)
            out.append(100.0 - 100.0 / (1.0 + rs))
    return tuple(out)


def roc(values: Series, period: int) -> Series:
    data = _floats(values)
    out: list[Cell] = []
    for i in range(len(data)):
        current = data[i]
        base = data[i - period] if i >= period else None
        if current is None or base is None or base == 0.0:
            out.append(None)
        else:
            out.append((current - base) / base * 100.0)
    return tuple(out)


def momentum(values: Series, period: int) -> Series:
    data = _floats(values)
    out: list[Cell] = []
    for i in range(len(data)):
        current = data[i]
        base = data[i - period] if i >= period else None
        out.append(None if current is None or base is None else current - base)
    return tuple(out)


def change(values: Series) -> Series:
    return momentum(values, 1)


# ---------------------------------------------------------------------------
# extremes
# ---------------------------------------------------------------------------


def highest(values: Series, period: int) -> Series:
    data = _floats(values)
    out: list[Cell] = []
    for i in range(len(data)):
        window = _window(data, i, period)
        out.append(None if window is None else max(window))
    return tuple(out)


def lowest(values: Series, period: int) -> Series:
    data = _floats(values)
    out: list[Cell] = []
    for i in range(len(data)):
        window = _window(data, i, period)
        out.append(None if window is None else min(window))
    return tuple(out)


# ---------------------------------------------------------------------------
# range and volatility
# ---------------------------------------------------------------------------


def true_range(high: Series, low: Series, close: Series) -> Series:
    highs, lows, closes = _floats(high), _floats(low), _floats(close)
    out: list[Cell] = []
    for i in range(len(highs)):
        previous_close = closes[i - 1] if i >= 1 else None
        if highs[i] is None or lows[i] is None or previous_close is None:
            out.append(None)
            continue
        out.append(
            max(
                float(highs[i]) - float(lows[i]),
                abs(float(highs[i]) - previous_close),
                abs(float(lows[i]) - previous_close),
            )
        )
    return tuple(out)


def atr(high: Series, low: Series, close: Series, period: int) -> Series:
    """Wilder-smoothed average true range, seeded with the first `period` TRs."""
    ranges = _floats(true_range(high, low, close))
    out: list[Cell] = []
    run: list[float] = []
    state: Optional[float] = None
    for value in ranges:
        if value is None:
            run = []
            state = None
            out.append(None)
            continue
        if state is None:
            run.append(value)
            if len(run) < period:
                out.append(None)
                continue
            state = sum(run) / period
        else:
            state = (state * (period - 1) + value) / period
        out.append(state)
    return tuple(out)


# ---------------------------------------------------------------------------
# oscillators over (high, low, close)
# ---------------------------------------------------------------------------


def stoch_k(high: Series, low: Series, close: Series, period: int) -> Series:
    highs, lows = highest(high, period), lowest(low, period)
    closes = _floats(close)
    out: list[Cell] = []
    for i in range(len(closes)):
        top, bottom, current = highs[i], lows[i], closes[i]
        if top is None or bottom is None or current is None:
            out.append(None)
            continue
        span = float(top) - float(bottom)
        out.append(50.0 if span == 0.0 else (current - float(bottom)) / span * 100.0)
    return tuple(out)


def willr(high: Series, low: Series, close: Series, period: int) -> Series:
    highs, lows = highest(high, period), lowest(low, period)
    closes = _floats(close)
    out: list[Cell] = []
    for i in range(len(closes)):
        top, bottom, current = highs[i], lows[i], closes[i]
        if top is None or bottom is None or current is None:
            out.append(None)
            continue
        span = float(top) - float(bottom)
        out.append(-50.0 if span == 0.0 else -(float(top) - current) / span * 100.0)
    return tuple(out)


def cci(high: Series, low: Series, close: Series, period: int) -> Series:
    highs, lows, closes = _floats(high), _floats(low), _floats(close)
    typical: Tuple[Cell, ...] = tuple(
        None
        if highs[i] is None or lows[i] is None or closes[i] is None
        else (float(highs[i]) + float(lows[i]) + float(closes[i])) / 3.0
        for i in range(len(closes))
    )
    typical_floats = _floats(typical)
    means = sma(typical, period)
    out: list[Cell] = []
    for i in range(len(typical_floats)):
        window = _window(typical_floats, i, period)
        current, mean = typical_floats[i], means[i]
        if window is None or current is None or mean is None:
            out.append(None)
            continue
        deviation = sum(abs(value - float(mean)) for value in window) / period
        out.append(
            0.0 if deviation == 0.0 else (current - float(mean)) / (0.015 * deviation)
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# volume
# ---------------------------------------------------------------------------


def obv(close: Series, volume: Series) -> Series:
    closes, volumes = _floats(close), _floats(volume)
    out: list[Cell] = []
    previous: Optional[float] = None
    state: Optional[float] = None
    for i in range(len(closes)):
        current, size = closes[i], volumes[i]
        if current is None or size is None:
            previous = None
            state = None
            out.append(None)
            continue
        if previous is None:
            previous = current
            out.append(None)
            continue
        if state is None:
            state = 0.0
        if current > previous:
            state += size
        elif current < previous:
            state -= size
        previous = current
        out.append(state)
    return tuple(out)


def vwap(price: Series, volume: Series, period: int) -> Series:
    prices, volumes = _floats(price), _floats(volume)
    out: list[Cell] = []
    for i in range(len(prices)):
        price_window = _window(prices, i, period)
        volume_window = _window(volumes, i, period)
        if price_window is None or volume_window is None:
            out.append(None)
            continue
        total_volume = sum(volume_window)
        if total_volume == 0.0:
            out.append(None)
            continue
        notional = sum(p * v for p, v in zip(price_window, volume_window))
        out.append(notional / total_volume)
    return tuple(out)


# ---------------------------------------------------------------------------
# MACD family
# ---------------------------------------------------------------------------


def macd_line(values: Series, fast: int, slow: int) -> Series:
    return _map2(ema(values, fast), ema(values, slow), len(values), lambda a, b: a - b)


def macd_signal(values: Series, fast: int, slow: int, signal: int) -> Series:
    return ema(macd_line(values, fast, slow), signal)


def macd_hist(values: Series, fast: int, slow: int, signal: int) -> Series:
    line = macd_line(values, fast, slow)
    return _map2(line, ema(line, signal), len(values), lambda a, b: a - b)


# ---------------------------------------------------------------------------
# Bollinger family
# ---------------------------------------------------------------------------


def bb_middle(values: Series, period: int) -> Series:
    return sma(values, period)


def _bollinger(
    values: Series, period: int, mult: Union[Series, Scalar], *, sign: float
) -> Series:
    middle, deviations = sma(values, period), stddev(values, period)
    out: list[Cell] = []
    for i in range(len(values)):
        mid, deviation, multiple = middle[i], deviations[i], _cell(mult, i)
        if mid is None or deviation is None or multiple is None:
            out.append(None)
        else:
            out.append(float(mid) + sign * multiple * float(deviation))
    return tuple(out)


def bb_upper(values: Series, period: int, mult: Union[Series, Scalar]) -> Series:
    return _bollinger(values, period, mult, sign=+1.0)


def bb_lower(values: Series, period: int, mult: Union[Series, Scalar]) -> Series:
    return _bollinger(values, period, mult, sign=-1.0)


def bb_pct_b(values: Series, period: int, mult: Union[Series, Scalar]) -> Series:
    data = _floats(values)
    upper = bb_upper(values, period, mult)
    lower = bb_lower(values, period, mult)
    out: list[Cell] = []
    for i in range(len(data)):
        value, top, bottom = data[i], upper[i], lower[i]
        if value is None or top is None or bottom is None:
            out.append(None)
            continue
        span = float(top) - float(bottom)
        out.append(0.5 if span == 0.0 else (value - float(bottom)) / span)
    return tuple(out)


def bb_width(values: Series, period: int, mult: Union[Series, Scalar]) -> Series:
    middle = sma(values, period)
    upper = bb_upper(values, period, mult)
    lower = bb_lower(values, period, mult)
    out: list[Cell] = []
    for i in range(len(values)):
        mid, top, bottom = middle[i], upper[i], lower[i]
        if mid is None or top is None or bottom is None or float(mid) == 0.0:
            out.append(None)
        else:
            out.append((float(top) - float(bottom)) / float(mid) * 100.0)
    return tuple(out)


# ---------------------------------------------------------------------------
# crosses
# ---------------------------------------------------------------------------


def _cross(left: Series, right: Series, *, above: bool) -> Series:
    a, b = _floats(left), _floats(right)
    out: list[Cell] = []
    for i in range(len(a)):
        if (
            i == 0
            or a[i] is None
            or b[i] is None
            or a[i - 1] is None
            or b[i - 1] is None
        ):
            out.append(None)
            continue
        if above:
            out.append(a[i] > b[i] and a[i - 1] <= b[i - 1])
        else:
            out.append(a[i] < b[i] and a[i - 1] >= b[i - 1])
    return tuple(out)


def crossover(left: Series, right: Series) -> Series:
    return _cross(left, right, above=True)


def crossunder(left: Series, right: Series) -> Series:
    return _cross(left, right, above=False)


# ---------------------------------------------------------------------------
# elementwise scalar maths
# ---------------------------------------------------------------------------


def absolute(values: Union[Series, Scalar], *, length: int) -> Series:
    return _map1(values, length, abs)


def square_root(values: Union[Series, Scalar], *, length: int) -> Series:
    return _map1(values, length, lambda v: None if v < 0.0 else v ** 0.5)


def minimum(
    a: Union[Series, Scalar], b: Union[Series, Scalar], *, length: int
) -> Series:
    return _map2(a, b, length, min)


def maximum(
    a: Union[Series, Scalar], b: Union[Series, Scalar], *, length: int
) -> Series:
    return _map2(a, b, length, max)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

# Kernels that need the frame length because a scalar argument may have to be
# broadcast across every bar; the others derive length from their series input.
_LENGTH_AWARE = frozenset({"ABS", "SQRT", "MIN", "MAX"})

_KERNELS: Dict[str, Callable[..., Series]] = {
    "SMA": sma,
    "EMA": ema,
    "WMA": wma,
    "STDDEV": stddev,
    "ZSCORE": zscore,
    "SUM": rolling_sum,
    "RSI": rsi,
    "ROC": roc,
    "MOM": momentum,
    "CHANGE": change,
    "HIGHEST": highest,
    "LOWEST": lowest,
    "TR": true_range,
    "ATR": atr,
    "STOCH_K": stoch_k,
    "WILLR": willr,
    "CCI": cci,
    "OBV": obv,
    "VWAP": vwap,
    "MACD_LINE": macd_line,
    "MACD_SIGNAL": macd_signal,
    "MACD_HIST": macd_hist,
    "BB_MIDDLE": bb_middle,
    "BB_UPPER": bb_upper,
    "BB_LOWER": bb_lower,
    "BB_PCT_B": bb_pct_b,
    "BB_WIDTH": bb_width,
    "CROSSOVER": crossover,
    "CROSSUNDER": crossunder,
    "ABS": absolute,
    "SQRT": square_root,
    "MIN": minimum,
    "MAX": maximum,
}


class UnknownIndicator(KeyError):
    """No kernel is registered under that name."""


def evaluate(name: str, args: Sequence[object], *, length: int) -> Series:
    """Run the kernel for `name`. `length` is the frame's bar count.

    The type checker has already validated arity, parameter kinds, and period
    constancy, so a failure here is a compiler bug rather than a user error.
    """
    kernel = _KERNELS.get(name)
    if kernel is None:
        raise UnknownIndicator(name)
    if name in _LENGTH_AWARE:
        return kernel(*args, length=length)
    return kernel(*args)
