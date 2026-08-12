# Macro-event signal contract

The `event_volatility/` library category consumes a **host event engine**: a deterministic runtime that owns the economic-release schedule, measures the post-release tape, and injects nonnegative bounded signal series into the Nano `MarketFrame`. Nano never fetches the release, never reads a clock, and never sees an order book — exactly as with every other host-provided signal.

This document is the contract those signals must satisfy. It is written from the strategy side: what each name means, what range the host must guarantee, and which measurements must be published as separate bull/bear pairs so baseline IR (which cannot carry negative literals) can express both directions.

## Design rules

1. **Nonnegative and bounded.** Every score is `0..1` (or `0/1` for flags, or a nonnegative ratio where noted). Signed quantities are forbidden; a signed surprise like `CORE_SURPRISE_Z = -1.4` must be published as the pair `CPI_COOL_SCORE = 0.86` / `CPI_HOT_SCORE = 0.0`.
2. **Bull/bear pairs are mutually exclusive by construction.** The host computes both sides from the same inputs and guarantees at most one side of a pair is above its qualification threshold at any tick.
3. **Missing is missing.** A signal the host cannot compute honestly (no release packet, not enough history, feed gap) is omitted from the frame. It is never filled with 0, a neutral constant, or a stale value. Nano's missing-signal semantics then make dependent conditions unknowable, and the strategy abstains.
4. **Versioned scales.** Any threshold or normalization constant (forecast-error scale, spread-stress baseline, whipsaw normalization) is versioned in the host. It is frozen during an event window and never silently retuned between events.
5. **The `(N)` annotation convention applies.** Event signals are bare names; the host owns every window and lookback.

## Reference points

- **Event anchor** — median executable mid from T-10s through T-1s relative to the scheduled release time.
- **Pre-event range** — high/low of a versioned T-5m window before the release.
- **First impulse** — maximum directional excursion from the event anchor during the initial observation window.

## Integrity and timing

| Signal | Range | Meaning |
| --- | --- | --- |
| `EVENT_READY` | 0/1 | A scheduled high-impact event window is armed and verified. |
| `ENTRY_WINDOW_OPEN` | 0/1 | Inside the host-enforced entry window (T+5s..T+180s in v1). |
| `RELEASE_CONFIRMED` | 0/1 | The release packet status is CONFIRMED from an execution-grade source. Calendar scrapes never set this. |
| `RELEASE_CONFLICT` | 0/1 | Any integrity failure: source conflict, CONFLICTED/LATE packet, schedule disagreement, failed payload hash. |
| `RELEASE_LATENCY_SCORE` | 0..1 | 1 = timely receipt relative to the provider publication timestamp. |
| `CALENDAR_INTEGRITY` | 0..1 | Agreement between the arming calendar and the release schedule source. |
| `SECONDS_SINCE_EVENT` | ≥0 | Whole seconds since the scheduled release. |

## Release interpretation (CPI family)

Computed deterministically from standardized forecast errors across headline and core fields; every component is preserved separately in the evidence packet.

| Signal | Range | Meaning |
| --- | --- | --- |
| `CPI_COOL_SCORE` | 0..1 | Cooler-than-consensus strength. Published beside `CPI_HOT_SCORE`; at most one is high. |
| `CPI_HOT_SCORE` | 0..1 | Hotter-than-consensus strength. |
| `CPI_HEADLINE_CORE_AGREEMENT` | 0..1 | Sign/magnitude agreement between headline and core surprises. |
| `CPI_REVISION_BULL_SCORE` | 0..1 | Bullish weight of prior-month revisions. |
| `CPI_REVISION_BEAR_SCORE` | 0..1 | Bearish weight of prior-month revisions. |
| `CPI_CONFLICT_SCORE` | 0..1 | Internal disagreement of the print (headline vs core vs revisions). |

## Price reaction

| Signal | Range | Meaning |
| --- | --- | --- |
| `UPSIDE_IMPULSE_ATR` | ≥0 | Max upward excursion from the event anchor divided by the pre-event 1m ATR. |
| `DOWNSIDE_IMPULSE_ATR` | ≥0 | Max downward excursion from the event anchor divided by the pre-event 1m ATR. |
| `IMPULSE_EFFICIENCY` | 0..1 | Net displacement divided by total path length. |
| `RETRACE_HOLD_SCORE` | 0..1 | `1 - retracement / impulse`, clamped. |
| `BREAK_FAILURE_SCORE` | 0..1 | Excursion outside the pre-event range followed by a deterministic close/reclaim back through the boundary. |
| `EVENT_ANCHOR_RECLAIM` | 0..1 | Strength of a reclaim of the event anchor from below. |
| `EVENT_ANCHOR_REJECT` | 0..1 | Strength of a rejection at the event anchor from above. |
| `SECOND_LEG_SCORE` | 0..1 | Raised only after impulse, controlled retrace, 30-90s compression, and a fresh break of the first-impulse extreme have occurred **in order**. |
| `OVERSHOOT_SCORE` | 0..1 | Excursion relative to the historical event-move distribution. |
| `VOLATILITY_COMPRESSION` | 0..1 | Contraction of realized range after the first impulse. |
| `PRE_EVENT_RANGE_BREAK_UP` | 0..1 | Upside break of the pre-event range. |
| `PRE_EVENT_RANGE_BREAK_DOWN` | 0..1 | Downside break of the pre-event range. |
| `WHIPSAW_SCORE` | 0..1 | Normalized event-anchor crossings and direction flips in the trailing observation window. 0 = one-way tape, 1 = pure churn. |

## Liquidity and flow

Consumed from the host's existing futures feature fabric; never rederived by the event engine.

| Signal | Range | Meaning |
| --- | --- | --- |
| `LIQUIDITY_OK` | 0/1 | Spread and depth renormalized against the pre-event baseline. |
| `SPREAD_STRESS` | ≥0 | Executable spread relative to pre-event baseline; 0 = normal, 1+ = severe. |
| `DEPTH_COLLAPSE_SCORE` | 0..1 | Loss of top-of-book depth vs baseline. |
| `BULL_FLOW_CONFIRM` | 0..1 | Bullish futures order-flow confirmation. |
| `BEAR_FLOW_CONFIRM` | 0..1 | Bearish futures order-flow confirmation. |
| `ABSORPTION_SCORE` | 0..1 | Passive absorption of aggressive flow. |
| `QUEUE_DEPLETION_SCORE` | 0..1 | Queue consumption rate at the touch. |
| `VOLUME_ACCELERATION` | ≥0 | Volume rate vs pre-event baseline. |
| `CANCEL_STRESS` | 0..1 | Cancellation intensity vs baseline. |

## Cross-market

Published only when the underlying markets are genuinely live and entitled; absent otherwise.

| Signal | Range | Meaning |
| --- | --- | --- |
| `BULL_CROSS_CONFIRM` | 0..1 | MES/MNQ and flow agreement on the upside. |
| `BEAR_CROSS_CONFIRM` | 0..1 | MES/MNQ and flow agreement on the downside. |
| `MES_MNQ_AGREEMENT` | 0..1 | Directional agreement between the two index futures. |
| `NQ_RELATIVE_STRENGTH` | 0..1 | NQ outperforming ES on the event path. |
| `NQ_RELATIVE_WEAKNESS` | 0..1 | NQ underperforming ES on the event path. |

## Options context

Grounding/weighting evidence only; options signals never independently create execution authority in v1.

| Signal | Range | Meaning |
| --- | --- | --- |
| `OPTIONS_FLOW_AVAILABLE` | 0/1 | A real options evidence plane is live. |
| `OPTIONS_UNUSUAL_SCORE` | 0..100 | Deterministic unusualness score. |
| `OPTIONS_BULL_SCORE` / `OPTIONS_BEAR_SCORE` | 0..1 | Directional options positioning. |
| `OPTIONS_IV_SHOCK` | ≥0 | Pre-release IV elevation. |
| `OPTIONS_IV_CRUSH` | 0..1 | Post-release IV collapse. |
| `GAMMA_POSITIVE_SCORE` / `GAMMA_NEGATIVE_SCORE` | 0..1 | Dealer gamma regime, only with provider-backed Greeks. |
| `CALL_WALL_DISTANCE_SCORE` / `PUT_WALL_DISTANCE_SCORE` | 0..1 | Proximity to major walls. |

## Control strategies

`event_liquidity_halt`, `event_release_integrity_halt`, and `event_whipsaw_halt` are published Nano strategies that emit `PAUSE`. They are auditable explanations, not the only safety mechanism: the host must enforce the same gates directly, and controls are never candidates for an execution slot.
