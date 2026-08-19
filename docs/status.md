# Nano status

Nano is an **alpha reference implementation**. This page distinguishes code that is present today from adjacent experiments and future work.

| Area | Status | Evidence and boundary |
| --- | --- | --- |
| `.nano` lexer, parser, and canonical code generation | Implemented | `nano/compiler/` parses the v1.0 grammar and produces `StrategyGraph` (baseline IR) or `NanoModule` (v1.0 IR). |
| Static typing and look-ahead protection | Implemented | `nano/types/` types `series<T>`, resolves indicator periods at compile time, and rejects any series offset that is negative or not a compile-time constant. |
| Computed indicators | Implemented | `nano/indicators/` ships 35 deterministic kernels with pinned degenerate-case conventions. The feed-signal form still works. |
| CLI | Implemented | `nano check / compile / replay / visualize / indicators / version` in `nano/cli/`. |
| Risk-limit enforcement | Implemented for five of seven limits | `nano/runtime/risk.py` withholds `BUY`/`SELL`/`EXECUTE` when `max_daily_loss`, `max_drawdown`, `max_orders_per_day`, `stop_trading_after_losses`, or `min_confidence` is breached, and logs `risk.violation` plus `intent.suppressed`. Every number comes from the host frame (`risk.drawdown`, `risk.daily_loss`, `risk.orders_today`, `risk.consecutive_losses`); a missing, absent, or non-finite measurement is a breach, not a pass. `PAUSE` and `OBSERVE` are never suppressed. See [language.md](language.md#risk-limits-v10). |
| `max_position_size` and `max_open_positions` | **Declared, not enforced by Nano** | These reach the IR and are logged as `risk.unenforced` at run time. Nano cannot enforce them honestly: an intent carries no order size, and Nano cannot tell an opening trade from a closing one, so neither cap is decidable from what the runtime can see. Hosts should read them from the `risk.limits` node and apply them at their own gate. This is a deliberate boundary, not a to-do. |
| Strategy IR validation | Implemented | `nano/ir/` validates the supported data shape and selected effect-manifest constraints. `StrategyGraph` is serializable but not content-addressed. |
| Reference interpreter and scheduler | Implemented | `nano/runtime/` evaluates injected `MarketFrame` data deterministically and returns intents plus an in-memory log. |
| Host decision-gate bridge and replay checker | Implemented | `nano/bridge/` forwards intents to a caller-provided `DecisionGate`; it never actuates externally. |
| Watchdog profile, artifact, receipt, and replay | Implemented | `nano/watchdog/` admits a rule under a restricted subset (`PAUSE`/`OBSERVE` only, no reasoning constructs), gates evaluation on declared-signal availability, and returns a replayable receipt. It reuses the existing compiler and VM — there is no second evaluator. It is not a scheduler, a host integration, or a decision gate, and it cannot enforce the `freshness_limit_ms` it records, because Nano reads no clock. See [watchdog_profile.md](watchdog_profile.md). |
| Source/IR conformance corpus and strategy corpus | Implemented | `nano/examples/` and `nano/library/` are paired source/IR test assets. |
| Editor-service helpers | Implemented | `nano/aethercode/` provides diagnostics, semantic tokens, and IR-preview functions. Packaging an editor extension is separate work. |
| Protocol-C provenance wrapper | Optional integration | `nano/bridge/provenance.py` requires its extra dependency and signs side-channel receipts outside the core runtime. |
| Pattern retrieval | Experimental primitive | `PatternStore` matches in-memory patterns and returns context plus an `escalate` boolean. It is not connected to a model or runtime routing path. |
| Loop validation, mutation admission, and simulator protocol | Experimental primitives | `nano/loop/` validates/hashes a separate loop document and gates caller-supplied candidate facts. It has no compiler, executor, deployment system, or live quantum backend. |

## Not implemented in this repository

- static typing, `Series<T>`, look-ahead protection, arithmetic, or indicator computation
- LLM calls, automatic escalation, confidence routing, or multi-agent coordination
- live market data, exchange/API clients, or order execution
- a policy or risk *engine*: nothing here tracks a book, a position, or an order. The `risk` block compares host-supplied measurements against declared limits and withholds proposals; it does not measure anything itself
- persistent core audit storage, full provenance chains, or input-data authentication
- a general strategy-DAG executor, autonomous loop runner, self-modifying deployment, or real quantum-hardware dispatch

## Reading the research material

The [paper series](papers/README.md) explores broader design hypotheses. It is intentionally more ambitious than the alpha runtime. Do not infer an implemented API, benchmark, or guarantee from a paper unless it is corroborated by the reference documentation and code.

## Compatibility posture

The public Python functions exported by `nano.compiler`, `nano.runtime`, and `nano.bridge` are the usable API surface for v0.1.0. The grammar and IR version are intentionally narrow while the conformance corpus establishes expected behavior. As an alpha project, breaking changes may occur before a stable release.
