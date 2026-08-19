# Temporal and series primitives

Nano's computed indicators are deliberately a closed vocabulary. This note
records the G1 candidate arena and freezes the semantics of the selected pack.
All selected calls lower through the existing v1 `indicator` node. They add no
grammar, IR shape, ambient state, or access to future bars.

## Vocabulary audit

The starting registry has 35 deterministic computed indicators, but none can
remember a boolean event or summarize a boolean window. The resulting strategy
work shows three distinct gaps:

- `trend/supertrend_flip_long.nano` asks the host to publish a one-bar
  `SUPERTREND_FLIP_BULL` event even though Nano computes `SUPERTREND_DIR`; event
  recency and density remain awkward once the flip itself exists.
- `volume/obv_trend.nano` asks the host for an `OBV_SLOPE`. That is evidence for
  `LINREG_SLOPE`, but slope belongs with correlation in a later statistical
  pack rather than being mixed into this event pack.
- `risk/correlation_cluster_guard.nano` explicitly leaves cluster assignment
  and book exposure with the host. A two-series `CORREL` would unlock market
  pair rules, but it would not honestly replace host-owned portfolio clustering.

The event-volatility corpus is not evidence for moving its release calendar,
liquidity, or order-flow state into Nano. Those measurements are external host
facts. G1 only removes temporal approximations over series Nano already has.

## Idea arena

| Primitive | Strategies unlocked | Deterministic semantics | Warm-up | Gap behavior | Type signature | Look-ahead risk | Complexity | Recommendation |
|---|---|---|---:|---|---|---|---|---|
| `BARS_SINCE` | recent impulse/cross filters, cooldowns, event follow-through | `0` on true; increment on false; absent before first true | 0 | `None` clears event memory; absent until next true | `series<bool> -> series<float>` | Low: single forward scan | Low | **1 — ship** |
| `COUNT_TRUE` | k-of-n confirmation, persistence, bounded event frequency | count true cells in trailing window including current | `period - 1` | any gap in window makes result absent | `series<bool>, int -> series<float>` | Low: trailing window only | Low | **2 — ship** |
| `ALL_TRUE` | all-bars persistence | count equals `period` | `period - 1` | any gap in window makes result absent | `series<bool>, int -> series<bool>` | Low | Low | Reject: exactly `COUNT_TRUE(x, p) == p` |
| `ANY_TRUE` | recent occurrence in a fixed window | count is greater than zero | `period - 1` | any gap in window makes result absent | `series<bool>, int -> series<bool>` | Low | Low | Reject: exactly `COUNT_TRUE(x, p) > 0` |
| `RISING` | monotone MA/price trend filters | trailing `period` values strictly increase pairwise | `period - 1` | any gap in window makes result absent | `series<float>, int -> series<bool>` | Low | Low | **3 — ship with `FALLING`** |
| `FALLING` | monotone downtrend and deterioration filters | strict decreasing mirror of `RISING` | `period - 1` | any gap in window makes result absent | `series<float>, int -> series<bool>` | Low | Low | **3 — ship with `RISING`** |
| `PERCENTRANK` | adaptive ATR, volume, spread, and momentum regimes | fraction of prior `period` values `<=` current, normalized to 0..1 | `period` | current or prior-window gap makes result absent | `series<float>, int -> series<float>` | Low: current vs past only | Low | **4 — ship** |
| `LINREG_SLOPE` | noisy trend strength, OBV slope, drift filters | ordinary-least-squares slope over fixed x positions | `period - 1` | any gap in window makes result absent | `series<float>, int -> series<float>` | Low | Medium | Next statistical pack; pin period-1 convention first |
| `CORREL` | pair divergence, hedge and cross-market confirmation | Pearson correlation over one trailing paired window | `period - 1` | any gap in either window makes result absent | `series<float>, series<float>, int -> series<float>` | Low | Medium | Next statistical pack; pin zero-variance behavior first |
| `ADX`, `+DI`, `-DI` | directional trend regime and strength filters | one shared Wilder directional-movement recursion | convention-dependent | recursive state must reset and reseed | three `series<float>` calls over HLC and period | Low, but seed drift is material | High | Defer as its own convention-heavy pack |

The selected pack is the smallest set that covers event recency, event density,
monotone direction, and adaptive regime ranking. `ANY_TRUE` and `ALL_TRUE` add
spelling but no expressive power. Slope/correlation form a coherent later
statistical pack. ADX and the two directional indices must share one explicitly
chosen Wilder seed; shipping only one reading would make later results drift.

## Frozen selected semantics

### `BARS_SINCE(series<bool>) -> series<float>`

- A true cell returns `0.0`; following false cells return `1.0`, `2.0`, and so on.
- Cells before the first true event are `None`.
- A `None` cell returns `None` and forgets the prior event. Later false cells
  remain absent until another true event.
- Static warm-up is zero because the first bar can truthfully return zero.

Golden: `F, T, F, F, T, F -> None, 0, 1, 2, 0, 1`.

### `COUNT_TRUE(series<bool>, period) -> series<float>`

- The window contains the current cell and the preceding `period - 1` cells.
- A result exists only for a complete, gap-free window.
- True contributes one and false contributes zero. `period = 1` therefore
  returns `1.0` or `0.0` for a present current cell.

Golden at period 3: `T, F, T, T, F -> None, None, 2, 2, 2`.

### `RISING` and `FALLING`

- Nano chooses strict pairwise monotonicity across the trailing `period`
  observations. It does not mean merely that the current cell exceeds the
  preceding window's maximum/minimum.
- Equality breaks the condition. A gap makes every crossing window absent.
- `period = 1` is vacuously true for a present cell; static warm-up is
  `period - 1`.

This is explicit because platforms use “rising” for both one-bar direction and
current-versus-prior-extreme tests. Nano's period names a window, matching its
other rolling indicators.

### `PERCENTRANK(series<float>, period) -> series<float>`

- The comparison set is exactly the preceding `period` cells; the current cell
  is not ranked against itself.
- The result is `count(previous <= current) / period`. Ties are inclusive.
- The scale is `[0.0, 1.0]`, not a percentage from 0 to 100. This makes the
  regime expression `PERCENTRANK(ATR(...), 100) > 0.75` literal.
- The current cell and full prior window must be present, so static warm-up is
  `period`.

Golden at period 3: `4, 1, 3, 2, 5, 5, 0 -> None, None, None, 1/3, 1, 1, 0`.

## Causality and replay contract

Every output at index `i` is a pure function of input cells `0..i`. Focused
tests compare every full-series result with the result from each truncated
prefix, pin all warm-up/gap/boundary rules above, and execute one compiled
strategy twice against the same frame with identical receipts. No selected
primitive reads or confirms a future bar.

## Convention references

TradingView documents the two `BARS_SINCE` boundaries Nano also chooses: absent
before the first event and zero on the event bar. Its percent-rank documentation
describes the count of previous values at or below the current value. Nano uses
that ordering but normalizes to 0..1 instead of 0..100. “Rising” is not portable:
NinjaTrader documents it as a one-bar comparison, while other strategy texts use
the term for a longer run. Nano therefore freezes strict pairwise monotonicity
instead of claiming the conventions are interchangeable.

- [TradingView function FAQ (`ta.barssince`)](https://www.tradingview.com/pine-script-docs/faq/functions/)
- [TradingView reference (`percentrank`)](https://www.tradingview.com/pine-script-reference/v6/)
- [NinjaTrader `Rising()` reference](https://ninjatrader.com/support/helpguides/nt7/rising.htm)
