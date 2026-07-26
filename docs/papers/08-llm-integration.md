# 08 — LLM Integration

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Nano is the model-agnostic execution substrate beneath reasoning models — Claude, GPT, Gemini, or a local model all compile to the same IR, and the economics of compiled memory versus per-decision inference decide the architecture.

---

## Substrate, not wrapper

Most "LLM integration" in agent frameworks means binding to a vendor: an SDK, a function-calling schema, a prompt format. Nano integrates in the opposite direction. The contract between a reasoning model and Nano is a data format — `.nano` source or Nano IR JSON — and nothing else:

```
Claude ─┐
GPT ────┤
Gemini ─┼──►  Nano IR  ──►  Deterministic execution
Local ──┘
```

Any model that can emit a valid strategy participates fully. Validation, not vendor identity, is the admission criterion: `.nano` source passes through the shipped compiler ([`nano/compiler/`](../../nano/compiler/__init__.py)), and `StrategyGraph.from_dict` rejects unknown node types, malformed values, and manifest violations at load time regardless of who authored the program ([`nano/ir/graph.py`](../../nano/ir/graph.py)). The language is small enough that emitting it correctly is well within every current frontier and local model.

This direction of dependency matters strategically. Models improve monthly and vendors change terms; an execution substrate that depends on one model inherits its churn. Nano is designed so the reasoning layer is *swappable* and the execution layer is *permanent*: upgrade the model, keep every compiled behavior, every replay, every audit trail. The substrate outlives any particular intelligence that writes to it.

It also cuts the other way, usefully: because compiled artifacts are model-independent, a behavior reasoned out by an expensive frontier model can be *verified* by replay (paper [03](03-determinism.md)) and then executed forever at zero inference cost — or cross-checked by a second model reviewing the same IR. Reasoning becomes a procurement decision, not an architecture decision.

## The escalation contract

Integration is bidirectional. Downward, models compile decisions into IR. Upward, execution escalates novelty back to a model. The shipped memory layer implements the routing decision: `PatternStore.retrieve()` matches current observations against live, sufficiently confident compiled patterns and returns either matches (context for reasoning, or grounds to execute) or `escalate=True` ([`nano/memory/patterns.py`](../../nano/memory/patterns.py)). The model is consulted exactly when the compiled region of behavior runs out — and each consultation is an opportunity to compile a new pattern, shrinking future escalations.

## The economics

The argument for this architecture is ultimately arithmetic. Consider a decision point evaluated once per minute — a modest cadence for a trading loop, a monitoring agent, or a robot's supervisory layer. That is about 526,000 evaluations per year.

**Per-decision inference.** Suppose each evaluation costs one inference call at roughly 2,000 tokens round-trip (context plus reasoning plus output) and one second of latency. At representative frontier-model pricing on the order of $5–15 per million tokens blended, each decision costs around $0.01–0.03 and the year costs **$5,000–15,000 per decision point** — and 526,000 seconds (six days) of cumulative decision latency. Multiply by the dozens or hundreds of decision points in a real system.

**Compiled memory.** The same decision compiled to Nano costs one reasoning session up front — call it a few dollars of inference and a review cycle — and then each evaluation is a handful of float comparisons in the interpreter: microseconds, effectively zero marginal dollars. Escalations still cost full inference, but they occur at the *rate of novelty*, not the rate of decisions. If 2% of situations escalate, annual inference cost drops by roughly 50x; at 0.1%, by roughly 1,000x.

The precise constants vary by model, token counts, and cadence — and Nano's own runtime constants are honestly unmeasured at this stage (paper [11](11-performance.md)). But the *shape* is not in doubt, because it does not depend on the constants: per-decision inference scales cost and latency linearly with decisions made, while compiled memory scales them with novelty encountered. Any system that makes the same decisions repeatedly — which is to say, any production system — eventually crosses the line where compilation dominates.

Latency has a second, harsher constraint: some loops cannot wait for inference at all. A 100 ms control deadline does not admit a 1-second model call even if the tokens were free. For those loops, compiled execution is not the cheaper option; it is the only option, with inference relegated to the supervisory timescale.

## What this is not

Nano does not make models deterministic, does not cache model outputs, and does not claim compiled patterns are better than fresh reasoning — a compiled pattern is *yesterday's* reasoning, which is exactly why patterns carry confidence, expiry (`expires_at`), and provenance, and why admission is gated (papers [02](02-the-agentic-compiler.md), [04](04-provenance.md)). The claim is narrower: once reasoning is done and validated, re-deriving it per decision is waste. Nano is where validated reasoning goes to run.

---

*Previous: [07 — Nano vs. Pine Script](07-nano-vs-pine.md) · Next: [09 — Autonomous Systems](09-autonomous-systems.md)*
