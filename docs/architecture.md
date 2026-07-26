# Nano architecture

> This page describes the code in the repository today. It does not describe proposed LLM routing, live execution, or a general-purpose agent platform.

## Core flow

```text
.nano source
    -> nano.compiler (tokenize, parse, codegen)
    -> StrategyGraph (validated, serializable IR)
    -> nano.runtime.execute(graph, MarketFrame)
    -> Intent(s) + ordered per-run LogEntry values
    -> optional NanoBridge
    -> host DecisionGate
    -> Decision record(s)
```

The core is deliberately small. It is a Python reference implementation for deterministic evaluation of threshold rules over host-injected numeric signal series.

## The compiler and strategy IR

`nano/compiler/` contains a handwritten lexer and recursive-descent parser. `compile_source()` turns a valid `.nano` program into an immutable `StrategyGraph`.

`StrategyGraph` is a flat, validated strategy representation:

- one optional `ScheduleNode`;
- zero or more `ConditionNode` values;
- zero or more `IntentNode` values; and
- zero or more metadata-only `AgentNode` values.

It is called a graph for the project's conceptual model, but the current `StrategyGraph` has no edges or branching topology. At runtime, all conditions gate all intents. `StrategyGraph.from_dict()` validates the version, known node types, known effect names, and the requirement that an intent-bearing strategy declares `intent.emit`. It is not a type checker, an optimizer, or a content-addressed artifact.

## Deterministic reference execution

`nano/runtime/` evaluates a graph against a `MarketFrame`:

- timestamps and signal series are injected by the caller;
- the scheduler expands the strategy interval over those timestamps;
- conditions are evaluated in source/IR order and short-circuit on the first failure;
- if every condition passes, the runtime emits each configured intent; and
- the result contains immutable intents and an ordered, in-memory event log.

The interpreter has no network, wall clock, random-number source, or side-effecting action path. Identical graphs and frames produce identical reference results. This does not authenticate input data or sandbox the Python host process.

## Governance boundary

`nano/runtime/` never acts on an intent. `nano/bridge/NanoBridge` forwards each emitted `Intent` to a host-defined `DecisionGate` and records the returned `Decision` in the bridge result.

The host owns policy, external action, persistence, and feed integrity. A `DecisionGate` is a protocol, not a built-in risk engine or exchange integration. For bridge-level replay, the gate must behave deterministically; `Backtester.verify_replay()` runs the same graph/frames/gate twice and raises when the serialized reports differ.

`nano/bridge/provenance.py` is an optional host adapter. When its external dependency is installed, it can sign decision receipts as a side channel. It is outside the language and deliberately uses time/entropy for signing, so it is not part of the base deterministic result.

## Supporting modules

| Module | Current role | Important boundary |
| --- | --- | --- |
| `nano/aethercode/` | Pure diagnostics, semantic-token, and IR-preview helpers. | Not a packaged LSP or IDE integration. |
| `nano/memory/` | In-memory pattern matching that returns context and an `escalate` boolean. | It is not called by the compiler, interpreter, or bridge and does not call a model. |
| `nano/loop/` | Separate experimental `LoopGraph` validator, mutation-admission helper, and deterministic simulator protocol. | There is no loop compiler or executor, model integration, deployment system, or QPU connector. |
| `nano/examples/` | Paired `.nano` source and expected-IR conformance fixtures. | They are language test assets, not generic runnable programs. |
| `nano/library/` | Paired, trading-oriented strategy corpus. | It demonstrates syntax and test conformance; it is not a registry or investment advice. |

## Trust boundaries

Nano provides a narrow execution contract, not a complete security boundary:

- **Nano validates strategy IR.** It does not validate the correctness or provenance of market data.
- **Nano proposes intents.** The host gate decides whether to approve them; external actuation is the host's responsibility.
- **The reference runtime is deterministic.** A nondeterministic gate, mutable host environment, or different input frame can change a larger system's outcome.
- **The base log is in memory.** Durable audit storage and cryptographic proof require host integration.

See [language.md](language.md) for the source contract and [status.md](status.md) for maturity boundaries.
