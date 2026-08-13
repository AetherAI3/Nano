# Nano vs. just writing it in Python

The short answer is: you can write the rule in Python.

For a small rule, Python is often the better choice. Nano is useful when the strategy itself needs to be a constrained, inspectable artifact with a defined execution contract: a validated `StrategyGraph`, deterministic reference execution, an ordered run log, and a host-controlled decision boundary.

The difference is not that Nano can express an `if` statement that Python cannot. The difference is what the strategy becomes and what the host can require around its execution.

## The same rule in Python

The Nano library contains a real Bollinger-band rule in
`nano/library/mean_reversion/bollinger_band_touch.nano`.

Its executable rule is:

- evaluate every hour;
- check `BB_PCT_B(20)`;
- when it is less than or equal to zero, emit a `BUY` intent for `SPY` with confidence `0.8`.

The same decision logic can be written directly in Python:

```python
def bollinger_band_touch(bb_pct_b):
    if bb_pct_b <= 0:
        return {
            "action": "BUY",
            "asset": "SPY",
            "confidence": 0.8,
        }

    return None
```

For this small piece of logic, Python is arguably simpler. There is no need to introduce a DSL merely to express a conditional.

## The same rule in Nano

The corresponding Nano strategy is:

```nano
strategy BollingerBandTouch {

    every 1h {

        if BB_PCT_B(20) <= 0 {

            buy(SPY, 0.8)

        }

    }

}
```

At the source level, this is not fundamentally more expressive than the Python version. The important difference appears after compilation.

The Nano source is represented as a structured strategy IR rather than only as executable host-language code.

## What the Nano version gives you

The additional value is not the conditional itself. It is the execution contract around the conditional.

### 1. A validated `StrategyGraph`

The Nano compiler represents the strategy as a structured IR. For the Bollinger-band example, the checked-in IR contains:

```json
{
  "type": "Strategy",
  "nanoIrVersion": "0.1.0",
  "name": "BollingerBandTouch",
  "effects": ["intent.emit", "log.append"],
  "nodes": [
    { "type": "Schedule", "interval": "1h" },
    { "type": "Condition", "signal": "BB_PCT_B", "operator": "<=", "value": 0 },
    { "type": "Intent", "action": "BUY", "asset": "SPY", "confidence": 0.8 }
  ]
}
```

This representation is validated when it is loaded. The IR loader checks the strategy type and version, requires a non-empty name and effect manifest, rejects unknown effects and node types, and enforces structural rules such as allowing at most one schedule.

It also checks that a graph containing intent nodes declares the corresponding `intent.emit` effect.

That makes the strategy structure explicit and machine-checkable rather than leaving its shape implicit in arbitrary host-language code.

### 2. Deterministic replay

Nano's reference runtime is designed around deterministic execution. A strategy is evaluated from a `StrategyGraph` and a `MarketFrame`, without relying on hidden wall-clock state, randomness, or network access.

The result can therefore be reproduced from the same strategy representation and input frame.

This matters when the host needs to verify that a previous evaluation can be replayed rather than merely rerunning an arbitrary Python program whose surrounding state may have changed.

The downstream `DecisionGate` is part of the same replay contract: its decision is required to be a deterministic function of the intent and input frame for replay verification to hold.

### 3. An ordered run log

The reference runtime records execution events as `LogEntry` values. Each entry contains an event, a timestamp, and a detail field.

The runtime therefore produces more than an action result. It can retain an ordered account of what happened during evaluation alongside the emitted intents.

This is useful when a host needs to inspect or audit an evaluation rather than only inspect its final output.

### 4. Host governance through `DecisionGate`

Nano does not place trades or directly perform external actions. Its runtime produces `Intent` values that represent proposals for a downstream host.

The bridge passes each intent to a host-provided `DecisionGate`:

```text
Nano strategy
     |
     v
   Intent
     |
     v
DecisionGate
     |
     v
  Decision
```

The gate decides whether an intent is approved or rejected. The bridge retains the intents, decisions, and log entries as part of the result.

This creates a clear boundary: Nano proposes; the host decides.

The host therefore remains responsible for the policy governing whether an intent should be acted upon and for any external side effects.

## Where plain Python is the better choice

Nano is not a replacement for Python.

Plain Python is the better choice when:

- the rule is small and only needs to run inside an existing application;
- the application needs arbitrary control flow or general-purpose programming;
- the implementation needs the wider Python ecosystem;
- the logic requires libraries or integrations that are already available in Python;
- introducing a separate DSL would add more complexity than value.

For example, the Bollinger-band conditional shown above is perfectly reasonable as a Python function when all that is required is to evaluate the condition and return a value.

Python can also provide validation, logging, replay, and governance. The difference is that an application built directly in Python has to define and enforce those contracts itself.

## The actual trade-off

The choice is therefore less about which language can express the rule and more about what representation the host wants to work with.

Python gives you a general-purpose programming environment:

```text
Python
  |
  v
Application-defined behavior
```

Nano gives you a constrained strategy representation with a defined path through compilation, validation, deterministic evaluation, logging, and host governance:

```text
Nano source
    |
    v
StrategyGraph
    |
    v
Reference runtime
    |
    +----> Intent
    |
    +----> ordered run log
    |
    v
DecisionGate
    |
    v
Host decision
```

For a one-off conditional, Python may be the better tool.

For a system that wants strategies to be represented, validated, replayed, logged, and governed through a defined host boundary, those additional constraints are the reason to consider Nano.