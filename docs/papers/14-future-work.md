# 14 — Future Work

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** The build order is deliberate — with the IR, interpreter, compiler front end, risk-gate bridge, and editor services shipped, the remaining work is the CLI, the cognitive execution layer, and real-hardware dispatch on the quantum research track — each stage proven on the shipped IR before the next begins.

---

## The rule that orders everything

[BUILD_ORDER.md](../../BUILD_ORDER.md) locks the strategy: IR-first, representation and execution before surface syntax, exactly as LLVM and Qiskit evolved. Milestones 1–6 are shipped — IR, reference interpreter, conformance corpus, the `.nano` → IR compiler, the risk-gate bridge and backtester, editor language services — along with the memory layer and the Nano++ optimization loop. What follows is the remaining work, in build order, with its acceptance criterion. Nothing below should be read as available today.

## 1. Developer experience: the CLI

**Goal: a meaningful result in under a minute.**

```
pip install aether-nano
nano compile strategy.json     # validate IR, report manifest and content hash
nano replay strategy.json frame.json    # execute; print intents + audit log; verify bit-identical re-run
nano visualize strategy.json   # render the execution graph
```

None of these commands exist yet — today the library is driven from Python (`compile_source(...)`, `interpreter.execute(graph, frame)`, `NanoBridge`). But the sixty-second path is the adoption thesis: a developer should validate a graph, replay it deterministically, and *see* the decision structure before reading a single paper. `replay` is the flagship — running an execution twice and printing "results identical, byte-for-byte" is the product demo that no inference-loop framework can perform. `visualize` matters for a different reason: compiled decisions are small graphs, and a rendered graph is the honest UI for "what will this system do?"

## 2. The front end: shipped, with typing still to come

The `.nano` surface language — lexer, parser, codegen ([`nano/compiler/`](../../nano/compiler/__init__.py)) — ships today, compiling

```nano
strategy Momentum {
  every 5m {
    if RSI(14) < 30 { execute() }
  }
}
```

to bit-identical Milestone-1 JSON, verified by the conformance corpus (`basic_rsi`, `momentum`, `mean_reversion`, `volatility_guard`, `risk_manager`, `ai_agent`) — each example exists as `.nano` source and hand-written IR that must match byte-for-byte. What remains is the richer type layer: `Series<T>` typing, turning look-ahead — peeking at future values — into a compile error rather than a backtest artifact. The front end earns nothing new by design: it is a human-friendly way to author graphs the IR already executes.

## 3. Runtime integration and hardening

The bridge and backtester ship (`nano/bridge/`): IR loads manifest-checked, recorded frames stream through the interpreter, and intents forward to any host `RiskEngine` — **bit-identical replay of a `BridgeResult` is the acceptance test**, and an optional Protocol-C adapter signs every disposition. Remaining hardening: production deployment against a live risk engine, audit log persistence at scale, release-gate automation, and the first real benchmarks (paper [11](11-performance.md) lists what is currently unmeasured). Editor language services (semantic tokens, diagnostics, IR preview — `nano/aethercode/`) ship; a full LSP and replay debugger remain.

## 4. The cognitive execution layer

The layer that makes the escalation architecture first-class at runtime: confidence routing (execute when compiled confidence clears the threshold, escalate to a reasoning model when it does not), pattern admission through gates, and semantic indexing of compiled behavior. The shipped memory layer is its foundation — `PatternStore.retrieve()` already returns matched patterns or `escalate=True` ([`nano/memory/patterns.py`](../../nano/memory/patterns.py)) — and the loop's gated admission (`admit_mutation` in `nano/loop/`) is the first shipped piece — but the runtime routing loop and the model integration remain design. This is the stage where "inference becomes the exception" stops being an architecture diagram and becomes a measurable escalation rate.

## 5. The quantum research track

Last by design. The research question: certain optimization workloads inside autonomous systems — portfolio construction, large scheduling problems, combinatorial search — may eventually route to quantum or hybrid backends. The representation layer is in place: the Nano++ loop ships a vendor-agnostic `QuantumRuntime` protocol with a deterministic, content-seeded `SimulatorRuntime` reference backend ([`nano/loop/quantum.py`](../../nano/loop/quantum.py)) — Nano knows quantum *jobs*, never vendors. Real-hardware dispatch, and any advantage claim, remain research (papers [10](10-nano-family.md), [16](16-quantum-computing.md), [17](17-heterogeneous-compute.md)). This track is research, with research's honest uncertainty: it may reshape Nano++, and it may conclude that classical backends win everywhere that matters. The IR is designed so either outcome is fine.

## What will not change

Future work extends the substrate; it does not renegotiate it. The six principles of paper [13](13-design-principles.md) — determinism, propose-only execution, IR-first, injected effects, content addressing, gated change — bind every item on this list. A CLI flag that broke replay, a language feature that reached the network, a cognitive layer that self-admitted patterns: each is already rejected, in advance, by design.

---

*Previous: [13 — Design Principles](13-design-principles.md) · Next: [15 — Closed-Loop Engineering](15-closed-loop-engineering.md)*
