# Nano build notes

> This is a record of the implementation sequence and architectural constraints. For the current API and maturity boundary, see [docs/architecture.md](docs/architecture.md), [docs/language.md](docs/language.md), and [docs/status.md](docs/status.md).

## The durable constraint

Nano strategies **propose**; a host gate **decides**. The language has no order, exchange, network, or external-actuation primitive. Its terminal runtime output is an `Intent`, which a host-owned `DecisionGate` may approve or reject.

```text
.nano rule -> StrategyGraph -> reference runtime -> Intent -> host DecisionGate -> Decision record
```

The effect manifest is a small strategy-IR boundary: intent-bearing graphs must declare `intent.emit`, and unknown effects are rejected at load time. It is not a general capability sandbox; the Python host process remains trusted.

## Implemented sequence

### 1. Strategy IR and validation

`nano/ir/` defines the versioned `StrategyGraph` representation. It supports a schedule, flat numeric conditions, intents, and metadata-only agent labels. A valid serialized strategy includes its version and effect manifest:

```json
{
  "type": "Strategy",
  "nanoIrVersion": "0.1.0",
  "name": "Momentum",
  "effects": ["intent.emit", "log.append"],
  "nodes": [
    {"type": "Schedule", "interval": "5m"},
    {"type": "Condition", "signal": "RSI", "operator": "<", "value": 30},
    {"type": "Intent", "action": "BUY", "asset": "BTC"}
  ]
}
```

### 2. Deterministic reference runtime

`nano/runtime/` evaluates validated strategy IR against an injected `MarketFrame`. The caller supplies timestamps and aligned signal series. The runtime emits intents and an ordered, in-memory event log; it does not fetch data or act externally.

### 3. Conformance corpus

`nano/examples/` contains paired source and IR fixtures. `nano/library/` extends that pattern with a trading-oriented strategy corpus. The tests compile pairs, validate IR, and replay reference execution.

### 4. .nano source compiler

`nano/compiler/` implements the locked v0.1 grammar with a handwritten lexer and recursive-descent parser. The compiler lowers source to canonical strategy IR; it does not implement static typing, optimization passes, or bytecode generation.

### 5. Host decision-gate bridge

`nano/bridge/` connects the reference runtime to a host-provided `DecisionGate` and records its decisions. `Backtester.verify_replay()` detects differences between two serialized bridge runs. The bridge remains a reference adapter: policy, persistence, and external action belong to the host.

### 6. Editor-service helpers

`nano/aethercode/` provides syntax/semantic tokens, diagnostics, and IR-preview functions. It is an engine layer, not a packaged editor extension.

## Adjacent experimental primitives

- `nano/memory/` provides standalone pattern retrieval and an `escalate` boolean; it is not a runtime model-routing system.
- `nano/loop/` provides separate loop-document validation, mutation-admission helpers, and a deterministic simulator protocol; it does not execute an autonomous loop.
- `nano/bridge/provenance.py` is an optional adapter that requires its external dependency and signs side-channel receipts.

## Work that remains outside the v0.1 implementation

The repository does not currently provide CLI commands, a type system, look-ahead analysis, an LLM runtime, automatic escalation, live data or exchange connectors, persistent core audit storage, general agent coordination, a loop executor, or real quantum-hardware dispatch.

The [design-note series](docs/papers/README.md) discusses some of these possible directions. It is not a substitute for the implemented contract.
