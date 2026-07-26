# Nano — Build Order

**Locked strategy: IR-first.** Nano does not start as a programming language. It starts as a
**deterministic intelligence execution graph** (Nano IR + reference runtime). The language becomes
the human-friendly way to author those graphs, later. This matches how LLVM and Qiskit evolved:
representation and execution first, surface syntax second.

Nano is the execution abstraction that lets a host trading platform tie together its risk gates,
provenance, deterministic-execution discipline, and runtime separation. Any platform with those
pieces can embed it; Aether ATS is the first consumer.

## The one architectural lock

```
Nano:      "Execute this intent."
Risk gate: "Is execution allowed?"
```

**Nano never places trades.** Strategies produce intents:

```json
{ "intent": "BUY", "asset": "BTC", "confidence": 0.91 }
```

A pluggable risk engine disposes: **components propose, gates decide.** This is enforced
structurally — the IR has no order/exchange primitive, only `Intent` nodes, and emitting intents
requires `intent.emit` in the module's effect manifest.

---

## Build order

### Milestone 1 — Nano IR  ✅

`nano/ir/` — a few primitives only:

| Node | Purpose |
|---|---|
| `Schedule(interval)` | when the graph evaluates ("5m") |
| `Condition(signal, operator, value)` | e.g. RSI < 30 |
| `Intent(action, asset, confidence)` | proposal, never an order |
| `Agent(name)` | named behavior blocks; `agent N { role research }` |

Target: this JSON **is** a runnable strategy:

```json
{
  "type": "Strategy",
  "nodes": [
    { "type": "Schedule",  "interval": "5m" },
    { "type": "Condition", "signal": "RSI", "operator": "<", "value": 30 },
    { "type": "Intent",    "action": "BUY", "asset": "BTC" }
  ]
}
```

### Milestone 2 — Reference interpreter  ✅

`nano/runtime/` — execute before you compile:

```python
result = interpreter.execute(strategy_graph, market_frame)
```

1. Load IR (manifest-checked). 2. Evaluate conditions against injected signal series
(no ambient clock, no lookahead). 3. Produce intents. 4. Log everything (replayable).

Pipeline shape: `price stream → condition → TRUE → Intent → [hand-off] → risk engine →
execution decision`. The hand-off is Milestone 5.

### Milestone 3 — Example corpus  ✅

`nano/examples/` — becomes the language test suite: `basic_rsi`, `momentum`, `mean_reversion`,
`volatility_guard`, `risk_manager`, `ai_agent`. Hand-written IR JSON now; the same examples must
compile from `.nano` source in Milestone 4 and produce identical IR.

### Milestone 4 — `.nano` syntax → IR  ✅

`nano/compiler/` — lexer, parser, compiler for the strategy subset:

```nano
strategy Momentum {
  every 5m {
    if RSI(14) < 30 { execute() }
  }
}
```

compiles to exactly the Milestone-1 JSON. Conformance rule: compiled IR replays identically to the
hand-written IR for every example.

### Milestone 5 — Risk-gate integration  ✅ (reference adapter in `nano/bridge/`)

First execution integration. Flow: `Nano IR → bridge (host-platform adapter) → risk engine →
execution decision`. The bridge loads IR, verifies the effect manifest, streams recorded market
frames through the interpreter, and forwards intents into the host platform's risk/release-gate
discipline. The backtester runs the same IR against historical frames — bit-identical replay is
the acceptance test. The reference adapter here defines the `DecisionGate` protocol any platform
can implement; Aether ATS is the first consumer.

An optional `ProvenanceGate` (`nano/bridge/provenance.py`) wraps any `DecisionGate` to bind
each decision to a signed, independently verifiable receipt — for platforms that need
non-repudiable proof a decision happened, not just a log line. Fully outside the language: a
`.nano` author can't see or reach it. Requires the optional `provenance` extra
(`pip install aether-nano[provenance]`); see `examples/provenance_signing_demo.py`.

### Milestone 6 — Editor tooling  ✅ engine layer (`nano/aethercode/`; extension packaging pending)

Not a whole IDE. The pure language-service engine first: syntax highlighting (semantic tokens),
diagnostics, IR preview (`if RSI < 30 { … }` → shows the compiled ConditionNode). Packaged as a
VS-Code-style extension by the host editor.

### Milestone 7 — Host-platform compiler inputs

Advanced compiler-input integrations (systems that discover computational patterns and feed them
to Nano) live in the host platform, not this repo.

### Where Nano++ starts

Much later, as an extension over the same IR (`Nano → Nano IR → Nano++`). Do not begin with
advanced computational features; the mistake would be building that syntax before the execution
graph is proven.

---

## Original 30-day plan (for the record)

| Week | Build | Exit |
|---|---|---|
| 1 | Nano package, IR schema, JSON serialization, basic interpreter | A JSON strategy executes deterministically |
| 2 | Lexer/parser, `.nano` files compile to IR | `if RSI < 30 { buy(BTC) }` works end-to-end |
| 3 | Risk-gate bridge, risk-layer hand-off, backtester | Nano strategies simulate against a host risk engine |
| 4 | Editor extension: highlighting + IR visualizer | Developer edits Nano with live IR preview |

## Alternate ordering (rejected)

For the record: building the bridge inside a host trading platform first, then extracting the
package, was rejected because the IR + runtime are ecosystem-wide — many runtimes and tools
consume them — so they belong in the standalone repo from day one, with the host platform as the
first *consumer*, not the owner.
