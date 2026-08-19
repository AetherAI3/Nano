# Nano status

Nano is an **alpha reference implementation**. This page distinguishes code that is present today from adjacent experiments and future work.

| Area | Status | Evidence and boundary |
| --- | --- | --- |
| `.nano` lexer, parser, and canonical code generation | Implemented | `nano/compiler/` parses the v1.0 grammar and produces `StrategyGraph` (baseline IR) or `NanoModule` (v1.0 IR). |
| Static typing and look-ahead protection | Implemented | `nano/types/` types `series<T>`, resolves indicator periods at compile time, and rejects any series offset that is negative or not a compile-time constant. |
| Computed indicators | Implemented | `nano/indicators/` ships 35 deterministic kernels with pinned degenerate-case conventions. The feed-signal form still works. |
| CLI | Implemented | `nano check / compile / replay / visualize / indicators / version` in `nano/cli/`. |
| Risk-limit enforcement | Partial | A `risk { ... }` block parses, range-checks, and reaches the IR, but nothing enforces the limits at run time yet. |
| Strategy IR validation | Implemented | `nano/ir/` validates the supported data shape and selected effect-manifest constraints. `StrategyGraph` is serializable but not content-addressed. |
| Reference interpreter and scheduler | Implemented | `nano/runtime/` evaluates injected `MarketFrame` data deterministically and returns intents plus an in-memory log. |
| Host decision-gate bridge and replay checker | Implemented | `nano/bridge/` forwards intents to a caller-provided `DecisionGate`; it never actuates externally. |
| Source/IR conformance corpus and strategy corpus | Implemented | `nano/examples/` and `nano/library/` are paired source/IR test assets. |
| Editor-service helpers | Implemented | `nano/aethercode/` provides diagnostics, semantic tokens, and IR-preview functions. Packaging an editor extension is separate work. |
| Protocol-C provenance wrapper | Optional integration | `nano/bridge/provenance.py` requires its extra dependency and signs side-channel receipts outside the core runtime. |
| Pattern retrieval | Experimental primitive | `PatternStore` matches in-memory patterns and returns context plus an `escalate` boolean. It is not connected to a model or runtime routing path. |
| Loop validation, mutation admission, and simulator protocol | Experimental primitives | `nano/loop/` validates/hashes a separate loop document and gates caller-supplied candidate facts. It has no compiler, executor, deployment system, or live quantum backend. |

## Not implemented in this repository

- static typing, `Series<T>`, look-ahead protection, arithmetic, or indicator computation
- LLM calls, automatic escalation, confidence routing, or multi-agent coordination
- live market data, exchange/API clients, order execution, or a built-in policy/risk engine
- persistent core audit storage, full provenance chains, or input-data authentication
- a general strategy-DAG executor, autonomous loop runner, self-modifying deployment, or real quantum-hardware dispatch

## Reading the research material

The [paper series](papers/README.md) explores broader design hypotheses. It is intentionally more ambitious than the alpha runtime. Do not infer an implemented API, benchmark, or guarantee from a paper unless it is corroborated by the reference documentation and code.

## Compatibility posture

The public Python functions exported by `nano.compiler`, `nano.runtime`, and `nano.bridge` are the usable API surface for v0.1.0. The grammar and IR version are intentionally narrow while the conformance corpus establishes expected behavior. As an alpha project, breaking changes may occur before a stable release.
