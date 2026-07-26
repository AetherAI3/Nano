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

## Errors

The lexer and parser raise `NanoSyntaxError` with a 1-based line and column. IR load validation raises `IRValidationError` or `ManifestViolation` when the serialized strategy shape violates the supported contract.

For host integration and replay semantics, see [architecture.md](architecture.md).
