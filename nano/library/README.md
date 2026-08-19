# Nano strategy library

This directory is Nano's contribution-ready, trading-oriented **strategy library and conformance corpus** for the v0.1.0 language. It is a place to learn from familiar ideas, compare clear signal contracts, and contribute new source/IR pairs—not a live-strategy service or investment advice.

Every entry has two files:

- `<name>.nano` - source written in the locked Nano grammar
- `<name>_ir.json` - the canonical strategy IR expected from that source

`tests/test_library.py` verifies that each pair compiles to the expected IR, round-trips through `StrategyGraph`, and produces deterministic reference-runtime results. The library gives quant researchers a concrete way to meet the language: start with an idea they already recognize, make its host signal contract explicit, then let the tests preserve that contract.

## Learn, compare, contribute

The current library contains 34 strategies across eight categories — seven trading, one for deterministic watchdog controls. Browse an entry to see the source, its expected IR, and the feed convention it assumes. When you are ready, a well-documented strategy pair is the most direct contribution to Nano.

[Walk through a first contribution →](../../docs/first-contribution.md) · [Add a strategy →](../../CONTRIBUTING.md#add-a-strategy) · [Open a proposal →](https://github.com/AetherAI3/Nano/issues/new?template=strategy-library.yml)

One command checks an entry before you push it, and writes the `_ir.json` partner for you:

```bash
python scripts/check_contribution.py --write nano/library/<category>/<name>.nano
```

## Categories

| Category | Strategies |
| --- | --- |
| `momentum/` | `rsi_oversold_reversal`, `stochastic_oversold`, `williams_r_reversal`, `roc_momentum` |
| `mean_reversion/` | `bollinger_band_touch`, `zscore_reversion`, `cci_extreme` |
| `trend/` | `golden_cross`, `macd_histogram_flip`, `donchian_breakout` |
| `volatility/` | `atr_volatility_halt`, `bb_squeeze_breakout` |
| `volume/` | `volume_spike_confirmation`, `obv_trend` |
| `risk/` | `max_drawdown_breaker`, `daily_loss_limit`, `position_concentration_cap`, `correlation_cluster_guard`, `stale_data_halt`, `leverage_ceiling`, `consecutive_loss_circuit` |
| `event_volatility/` | `event_impulse_pullback_long`, `event_impulse_pullback_short`, `cpi_impulse_pullback_long`, `cpi_impulse_pullback_short`, `event_false_first_move_long`, `event_false_first_move_short`, `event_second_leg_long`, `event_second_leg_short`, `event_liquidity_halt`, `event_release_integrity_halt`, `event_whipsaw_halt` |
| `watchdog/` | `trusted_route_guard`, `credential_age_alert` |

## The comment header

Every entry opens with a `//` header, and it is not decoration — it is the part
a reviewer reads to decide whether the rule is *right*, as opposed to whether it
compiles. The source already says what the rule computes. The header says when
it is meant to fire and, more usefully, when it is wrong.

Five fields, all required, all checked by
`python scripts/check_contribution.py`:

| Field | What it answers |
| --- | --- |
| `REGIME:` | Which market or system state this is for — and, often more useful, which one it must **not** fire in |
| `CONDITIONS:` | What has to already be true before the rule is armed |
| `INVALIDATION:` | What makes it wrong. A rule nobody can disprove is not a rule |
| `SHAPE:` | The timeframe and the picture being described, and why that timeframe |
| `CALIBRATED ON:` | Where the numbers came from, and specifically what does *not* travel to another instrument or site |

A `NOT <other_entry>:` line is the convention for the near-neighbour problem:
name the existing entry yours is most likely to be confused with, and say what
distinguishes them. Every current entry carries one.

Signals get documented too. If your rule uses a signal already in the tables
below, the table is its definition. If it introduces a new one, define it in
your header — formula or data source, unit or range, normalization, lookback
convention — because the host has to implement it.

### Provenance

`SOURCE:` is optional and is how an entry credits an idea it did not invent:

```text
// SOURCE: Donchian channel breakout, as described publicly in trend-following
// literature. Translated to Nano; not derived from any proprietary code.
```

Use it whenever you are translating a publicly described idea. Leave it out when
the rule is your own. Do not transcribe proprietary strategy code — translate
the publicly described idea and say where it came from. Authorship itself is
already recorded by git and by the pull request, so there is no author field to
fill in.

## Signal conventions

Library entries compare **host-provided named signal series** with numeric literals, which is what keeps them on byte-stable baseline IR. Nano v1.0 *can* compute indicators — `RSI(close, 14)` and 32 others — but a library entry that did so would emit v1.0 IR and change its pinned fixture. Nano never fetches market data in either form. The names and transformations below are conventions a host data feed must implement.

| Pine-style expression | Nano signal | Feed convention |
| --- | --- | --- |
| `ta.rsi(close, 14)` | `RSI(14)` | Raw RSI, 0-100 |
| `ta.stoch(...)` %K | `STOCH_K(14)` | Raw %K, 0-100 |
| `ta.wpr(14)` | `WILLR_POS(14)` | Williams %R + 100 (0-100) |
| `ta.roc(close, 10)` | `ROC(10)` | Percent change over the window |
| Bollinger %B | `BB_PCT_B(20)` | `(close - lower) / (upper - lower)` |
| Bollinger width | `BB_WIDTH(20)` | `(upper - lower) / middle * 100` |
| z-score | `ZSCORE_NEG(20)` | Negated z-score |
| `ta.cci(20)` | `CCI_NEG(20)` | Negated CCI |
| `ta.crossover(sma50, sma200)` | `SMA_SPREAD(50)` | `SMA(50) - SMA(200)` |
| MACD histogram | `MACD_HIST(9)` | MACD line minus signal line |
| Donchian breakout | `DONCHIAN_POS(20)` | `(close - lower) / (upper - lower)` |
| `ta.atr(14) / close` | `ATR_PCT(14)` | ATR as a percentage of price |
| `ta.mom(close, 10)` | `MOM(10)` | `close - close[10]` |
| volume spike | `VOL_RATIO(20)` | `volume / sma(volume, 20)` |
| OBV trend | `OBV_SLOPE(20)` | Linear-regression slope of OBV |
| equity drawdown | `DRAWDOWN` | Portfolio drawdown percentage |

### Book-control signals (`risk/`)

Risk entries read **portfolio and infrastructure state**, not bar indicators. Every series is nonnegative and rises as the situation worsens, so a control is always a `>=` against a ceiling — the same direction on every rule, which is what makes a stack of them readable at a glance. These rules emit `pause` and `observe` only; none of them proposes a direction.

| Book measurement | Nano signal | Feed convention |
| --- | --- | --- |
| session loss | `DAY_LOSS_PCT` | Realised session loss as a positive percentage of starting equity; host owns the session boundary |
| largest position | `MAX_POSITION_PCT` | Largest single position as a percentage of gross exposure |
| cluster exposure | `CLUSTER_EXPOSURE_PCT` | Largest correlated-cluster exposure as a percentage of gross; host owns cluster assignment |
| feed freshness | `FEED_AGE_SEC` | Seconds since the last accepted tick |
| gross leverage | `GROSS_LEVERAGE` | Gross exposure divided by equity |
| losing streak | `CONSECUTIVE_LOSSES` | Count of consecutive losing closed decisions; host defines close and loss |

### Watchdog signals (`watchdog/`)

Watchdog rules read **host-measured system and policy state** — not markets at
all. They exist because the same three properties that make a trading rule
auditable (deterministic, replayable, unable to reach outside the host) are what
a security or compliance control needs most. See
[deterministic watchdogs and compliance controls](../../README.md#deterministic-watchdogs-and-compliance-controls)
for the boundary this sits inside.

They follow the `risk/` direction convention: every series is nonnegative and
rises as the situation worsens, so a control is always a `>=` against a ceiling.
A stack of them reads the same way top to bottom, which is the point. If your
natural measurement falls as things get worse — availability, remaining budget,
a health score — the host publishes the complement, and the header documents the
transform.

Watchdog rules emit `pause` and `observe` only. A rule proposing a direction is
a trading rule and belongs in another category; `tests/test_library.py` enforces
this.

| System measurement | Nano signal | Feed convention |
| --- | --- | --- |
| trusted route unavailable | `TRUSTED_ROUTE_DOWN` | 0 while the host's connectivity check verifies the route, 1 while it does not; host owns debouncing |
| credential age | `CREDENTIAL_AGE_DAYS` | Age in whole days of the oldest in-scope credential or signing key |

The categories are deliberately thin — two entries and two signals. Rules for
authentication-failure bursts, unsigned release artifacts, endpoint posture, and
release-approval thresholds are
[open issues](https://github.com/AetherAI3/Nano/labels/good%20first%20issue),
not omissions.

### Macro-event signals (`event_volatility/`)

Event strategies consume a **host event engine** rather than bar indicators: the host arms a scheduled release window, measures the post-release tape deterministically, and publishes nonnegative bounded scores. Bull and bear terms are always separate series (never one signed value), which is what keeps twin-armed branches mutually exclusive and inside baseline IR. The full measurement definitions live in [`docs/event_signal_contract.md`](../../docs/event_signal_contract.md).

| Event measurement | Nano signal | Feed convention |
| --- | --- | --- |
| armed event window | `EVENT_READY` | 0 or 1; scheduled high-impact window verified |
| entry window | `ENTRY_WINDOW_OPEN` | 0 or 1; host-enforced T+5s..T+180s |
| confirmed release | `RELEASE_CONFIRMED` | 0 or 1; release packet status CONFIRMED only |
| release integrity | `RELEASE_CONFLICT` | 0 or 1; any source/hash/schedule conflict |
| cool CPI print | `CPI_COOL_SCORE` | 0..1 from standardized forecast errors |
| hot CPI print | `CPI_HOT_SCORE` | 0..1; published beside `CPI_COOL_SCORE` |
| upward impulse | `UPSIDE_IMPULSE_ATR` | max upward excursion from event anchor / pre-event 1m ATR |
| downward impulse | `DOWNSIDE_IMPULSE_ATR` | max downward excursion from event anchor / pre-event 1m ATR |
| retracement hold | `RETRACE_HOLD_SCORE` | 1 - retracement/impulse, clamped 0..1 |
| failed breakout | `BREAK_FAILURE_SCORE` | 0..1; range break then deterministic reclaim |
| range break up/down | `PRE_EVENT_RANGE_BREAK_UP` / `_DOWN` | 0..1 against the versioned T-5m range |
| anchor reclaim/reject | `EVENT_ANCHOR_RECLAIM` / `EVENT_ANCHOR_REJECT` | 0..1 at the T-10s..T-1s median mid anchor |
| second leg | `SECOND_LEG_SCORE` | 0..1; impulse, compression, then fresh extreme, in order |
| cross-market agreement | `BULL_CROSS_CONFIRM` / `BEAR_CROSS_CONFIRM` | 0..1 MES/MNQ + flow agreement |
| order-flow confirmation | `BULL_FLOW_CONFIRM` / `BEAR_FLOW_CONFIRM` | 0..1 from the host futures feature fabric |
| liquidity normalized | `LIQUIDITY_OK` | 0 or 1; spread and depth renormalized |
| spread stress | `SPREAD_STRESS` | 0 = baseline, 1+ = severely stressed |
| tape churn | `WHIPSAW_SCORE` | 0..1 normalized anchor crossings in the window |

Two v0.1 details matter:

1. Baseline IR cannot carry a negative literal. The v1.0 grammar supports unary minus, but a library entry using it would leave baseline IR — so signals with negative natural ranges are still shifted (`WILLR_POS`) or negated (`ZSCORE_NEG`) by the feed. Document the transform in a `//` comment.
2. The parenthesized integer is documentation only. `RSI(14)` and `RSI` compile to the same `ConditionNode(signal="RSI", ...)`; the feed owns the actual lookback calculation.

## Intent boundary

```text
host MarketFrame -> Nano reference runtime -> Intent(s)
                 -> host DecisionGate -> Decision record(s)
```

A corpus strategy can emit `BUY`, `SELL`, `EXECUTE`, `PAUSE`, or `OBSERVE` intents. It cannot place a trade or call an external API. The host owns policy and any real-world action.

## Adding a strategy

[`docs/first-contribution.md`](../../docs/first-contribution.md) walks the whole
path once, with a real rule. The short version:

1. Choose an existing category or propose a new one.
2. Add `<name>.nano` using the [v0.1.0 subset](../../docs/language.md): one `every` block, one `if` rule, AND-chained conditions, and supported intent actions. Staying inside it is what keeps the checked-in IR byte-stable.
3. Write the [comment header](#the-comment-header) — five fields, plus a `NOT` line, plus any new signal's definition.
4. Generate the `_ir.json` partner and check everything at once:

   ```bash
   python scripts/check_contribution.py --write nano/library/<category>/<name>.nano
   ```

   `--write` produces the IR in the library's format, so nothing has to be
   hand-reflowed to match its neighbours. Re-run without `--write` and it should
   print `1 entry ready for review.`
5. Run `python -m pytest tests/test_library.py -q`.

The pair must compile to the checked-in IR, round-trip through the validator, and replay deterministically under the test frames before it is ready to merge. CI runs the same checker over the whole library on every pull request.
