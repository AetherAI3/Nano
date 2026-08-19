# Nano language reference (v0.1.0)

Nano v0.1.0 is a compact DSL for scheduled threshold rules. It is intentionally not a general-purpose programming language.

## Valid example

```nano
strategy Momentum {
  agent Analyst

  every 5m {
    if RSI(14) < 30 and VOLUME > 1000000 {
      buy(BTC, 0.91)
      observe()
    }
  }
}
```

This program emits `BUY` and `OBSERVE` intents only when both injected signal values satisfy their comparisons. It does not calculate `RSI`, fetch market data, invoke `Analyst`, or place an order.

## Grammar

```text
program    := "strategy" IDENT "{" item* "}"
item       := schedule | agent
agent      := "agent" IDENT
schedule   := "every" INTERVAL "{" [rule] "}"
rule       := "if" condition ("and" condition)* "{" action+ "}"
condition  := IDENT ["(" INT ")"] OP NUMBER
action     := "buy" "(" IDENT ["," NUMBER] ")"
            | "sell" "(" IDENT ["," NUMBER] ")"
            | "execute" "(" ")"
            | "pause" "(" ")"
            | "observe" "(" ")"
```

`INTERVAL` is an integer followed by `s`, `m`, `h`, or `d` (for example `5m` or `1h`). `OP` is one of `<`, `<=`, `>`, `>=`, `==`, or `!=`. Numeric literals are non-negative integers or decimals.

## Semantic constraints

- A strategy may have **at most one** `every` block.
- An `every` block may have **at most one** `if` rule.
- Multiple conditions are joined with logical **AND** and are evaluated in order.
- A condition compares one named signal with one numeric literal. The right-hand side cannot be another identifier.
- The only actions are `buy`, `sell`, `execute`, `pause`, and `observe`.
- `buy` and `sell` require an asset identifier and accept an optional confidence value in `[0, 1]`.
- `agent Name` is parsed and stored as metadata, but has no runtime behavior in v0.1.0.

There are no variables, arithmetic, boolean `or`/`not`, function declarations, loops, imports, user-defined actions, I/O, or external API calls.

## Signals and lookback labels

Signals are supplied by the host in a `MarketFrame` mapping:

```python
MarketFrame(
    timestamps=(0, 300),
    signals={"RSI": (45.0, 22.0), "VOLUME": (900000.0, 1200000.0)},
)
```

Two forms exist, distinguished structurally rather than by heuristic. `RSI(14)` — one constant integer argument — is a **feed signal**: the host computes it and injects the series. `RSI(close, 14)` — a series argument — is **computed** by `nano/indicators/`. Nano never fetches data in either case; a feed supplies the frame.

The optional parenthesized integer in a condition is a source-level convention only: `RSI(14)` and `RSI` compile to the same `ConditionNode(signal="RSI", ...)`. Use the annotation to document the feed contract; do not expect the runtime to apply a lookback window.

## Compilation result

`compile_source()` emits a `StrategyGraph` that serializes to canonical JSON-like data:

```json
{
  "type": "Strategy",
  "nanoIrVersion": "0.1.0",
  "name": "Momentum",
  "effects": ["intent.emit", "log.append"],
  "nodes": [
    {"type": "Schedule", "interval": "5m"},
    {"type": "Condition", "signal": "RSI", "operator": "<", "value": 30},
    {"type": "Intent", "action": "BUY", "asset": "BTC", "confidence": 0.91}
  ]
}
```

The v0.1 compiler always emits the same two effect declarations. Load-time validation rejects unknown node/effect names and intent nodes without `intent.emit`. It does not implement general static typing, control-flow analysis, content hashing, or optimization passes.

## Runtime semantics

For each scheduled timestamp, the reference interpreter:

1. reads each required signal from the injected frame;
2. evaluates conditions in order, stopping at the first false condition;
3. emits every configured intent when all conditions pass; and
4. records ordered execution events in the returned result.

A strategy with no conditions emits no intents under the current interpreter. An invalid interval string supplied through raw IR may be rejected when the scheduler executes it rather than at IR load time.

## Risk limits (v1.0)

Everything above describes the v0.1.0 surface. `risk { ... }` is the one v1.0
construct documented here, because it is the one whose *runtime* behavior a
reader is most likely to assume wrongly. A strategy carrying a risk block
compiles to v1.0 IR.

```nano
strategy GuardedBreakout {
  risk {
    max_drawdown 0.05
    min_confidence 0.6
  }

  every 5m {
    if RSI(14) > 70 {
      sell(BTC, 0.8)
    }
  }
}
```

A limit is a name and one non-negative number. Each may be set at most once, and
a strategy may have at most one `risk` block.

| Limit | Unit | Allowed while | Measurement |
|---|---|---|---|
| `max_daily_loss` | fraction of equity | `risk.daily_loss <= limit` | `risk.daily_loss` |
| `max_drawdown` | fraction of equity | `risk.drawdown <= limit` | `risk.drawdown` |
| `max_orders_per_day` | count | `risk.orders_today <= limit` | `risk.orders_today` |
| `stop_trading_after_losses` | consecutive losses | `risk.consecutive_losses < limit` | `risk.consecutive_losses` |
| `min_confidence` | confidence in [0, 1] | `intent confidence >= limit` | the intent itself |
| `max_position_size` | fraction of equity | — | **not enforced by Nano** |
| `max_open_positions` | count | — | **not enforced by Nano** |

### What enforcement does

When an actuating intent — `buy`, `sell`, or `execute` — is about to be proposed,
every enforced limit is checked against that bar. A breach **withholds the
proposal** and records `risk.violation` and `intent.suppressed` in the run log. A
violation never rewrites one intent into another and never grants the module an
effect it did not already declare; the host still decides everything that
survives, and never sees what did not.

`pause` and `observe` are never suppressed. They are how a strategy asks to be
stopped and how it reports what it saw — a breaker that silenced its own halt
because the book was in drawdown would be worse than no breaker. `escalate` is
not gated either: a breached limit is a reason to ask for help, not to stop
asking.

`min_confidence` is checked against the intent's own declared confidence, and
absence fails closed — so a floor beside a bare `buy(BTC)` would propose nothing,
ever, at any input. **The compiler rejects that pairing rather than letting it
run**, naming the line and column of the action:

```text
buy() declares no confidence, so it can never satisfy min_confidence 0.6 and
would be suppressed at every bar — give it one, as `buy(BTC, 0.9)`, or drop
the limit
```

The same applies to `execute()`, which has no confidence argument in the grammar
at all, so it cannot appear in a strategy that declares a floor. Watch for
`min_confidence 0`: it is the natural spelling of "no floor", it is inside the
allowed range, and it suppresses everything — so it is rejected too.

### Units, and the boundary

Fractions are fractions. `max_drawdown 0.05` is five percent, and `0.05` is
rejected as a percentage nowhere: `max_daily_loss 2` is a compile error, not a
200 percent stop. Note that this is a *different convention* from the `DRAWDOWN`
feed signal used by the strategy library, which is expressed in percent. The two
never meet, because the risk gate reads only `risk.drawdown`.

**The allowed band is inclusive; a breach is strictly outside it.** A drawdown of
exactly `0.05` under `max_drawdown 0.05` is permitted; anything above it is not.
An intent whose confidence is exactly `0.6` under `min_confidence 0.6` is
permitted. `stop_trading_after_losses 3` is the one limit whose number names the
first *unacceptable* count rather than the last acceptable one — "stop after
three losses" — so its band is 0 to 2 and it breaches at 3.

### Where the numbers come from

From the host, per bar, through the same `MarketFrame` that carries every other
signal:

```python
MarketFrame(
    timestamps=(0, 300),
    signals={"RSI": (45.0, 78.0), "risk.drawdown": (0.01, 0.07)},
)
```

Nano does not read your account, your positions, or your order history, and it
has no clock of its own to decide when a day rolled over — `risk.orders_today` is
a number the host maintains and reports. The `risk.` prefix contains a dot, which
no Nano identifier may, so a measurement can never collide with a signal a
strategy reads by name.

**A limit whose measurement is missing, absent at that bar, or not a finite
number is a breach, not a pass.** NaN compares false against every threshold and
negative infinity compares below every threshold, so treating either as "safe"
would ignore the two most dangerous values a gate can be handed. A boolean is not
a number either: `False` would otherwise arrive as a perfectly satisfied count.

Because a missing measurement withholds everything, `nano replay` refuses a data
file that does not carry the columns a strategy's limits read, rather than
reporting a run that proposed nothing. When a replay does withhold something, the
text report says so and the full account is in the log — `nano replay --report
json` prints it.

### The two Nano will not enforce

`max_position_size` and `max_open_positions` still parse, still range-check, and
still travel to the host in the IR. Nano does not apply them, and says so with a
`risk.unenforced` log entry naming each one.

The reason is that neither is decidable from what Nano can see. A Nano intent
carries an action, an asset, and a confidence — it carries no order size, so
nothing in the language says how much a `buy` would put on. Nano equally cannot
tell an opening trade from a closing one: a `sell` may flatten a long or open a
short. A guard built on either guess would carry a name it does not honour, and
an unenforced limit that announces itself is safer than an enforced-looking one
that does not. The host knows its book; these belong at its gate.

## Errors

The lexer and parser raise `NanoSyntaxError` with a 1-based line and column. IR load validation raises `IRValidationError` or `ManifestViolation` when the serialized strategy shape violates the supported contract.

For host integration and replay semantics, see [architecture.md](architecture.md).
