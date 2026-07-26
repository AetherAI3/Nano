# 02 — The Agentic Compiler

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Nano's compiler does not translate algorithms into machine code — it extracts repeatable decisions from a reasoning model and fixes them into a deterministic, auditable execution graph.

---

## Compiling decisions, not algorithms

A conventional compiler takes a description of *how to compute* and lowers it to instructions a machine executes. The programmer already knows the algorithm; the compiler's job is fidelity and speed.

Nano's compiler has a different input and a different job. Its input is a *decision* — a policy a reasoning model has worked out: under these observed conditions, propose this action, with this confidence, valid in this domain. Its job is to fix that decision into a form that is deterministic, inspectable, replayable, and gated. The output is not machine code; it is Nano IR, an execution graph whose strategy-tier vocabulary is four node types: `Schedule`, `Condition`, `Intent`, and `Agent` ([`nano/ir/nodes.py`](../../nano/ir/nodes.py)).

This is why we say Nano extracts **repeatable intelligence**. The model's reasoning about *why* RSI below 30 with elevated volume is a buying opportunity stays with the model. What compiles is the operational residue of that reasoning — the predicate and the proposal — stripped of everything that cannot be replayed.

## The pipeline

```
Reasoning model (any) → Compile → Nano IR → Deterministic execution → Escalation on novelty
```

- **Reasoning.** Any model. Nano takes no dependency on a vendor; the compilation target is the same IR whether the reasoning came from Claude, GPT, Gemini, or a local model (paper [08](08-llm-integration.md)).
- **Compilation.** Either path ends in validated IR. Emitted IR JSON is checked by `StrategyGraph.from_dict`: unknown node types are rejected, effect manifests are enforced (`intent.emit` is required before any `Intent` node is admitted), and malformed values fail at load time, never at runtime ([`nano/ir/graph.py`](../../nano/ir/graph.py)). `.nano` source goes through the shipped compiler front end — lexer, parser, codegen ([`nano/compiler/`](../../nano/compiler/__init__.py)) — under the conformance rule that compiled IR must match hand-written IR bit-for-bit across the example corpus.
- **Execution.** The reference interpreter runs the graph as a pure function of graph plus injected frame ([`nano/runtime/interpreter.py`](../../nano/runtime/interpreter.py)). No inference happens on this path at all.
- **Escalation.** When the observed situation matches no compiled pattern, the system returns to the model. The shipped `PatternStore.retrieve()` makes this explicit: its result is either matched patterns or `escalate=True` ([`nano/memory/patterns.py`](../../nano/memory/patterns.py)).

## Against the Think→Tool loop

The dominant agent architecture today is:

```
Observe → LLM thinks → LLM picks a tool → Tool runs → Observe → LLM thinks again → …
```

The model sits inside the loop, consulted on every iteration. This design has real virtues — it needs no compilation step and handles novelty by default — but it has four structural costs:

1. **Latency.** Every decision waits on an inference round trip, typically hundreds of milliseconds to seconds. Control loops that tick every few seconds spend most of their budget thinking about things they have already thought about.
2. **Cost.** Inference is metered per token, per decision, forever. A decision made a million times costs a million inferences.
3. **Variance.** Sampling-based inference does not guarantee the same output for the same input. The system's behavior on identical situations drifts.
4. **Opacity.** The answer to "why did the agent do that?" is a transcript of stochastic reasoning, not a checkable artifact.

Nano's architecture inverts the loop:

```
Observe → Compiled pattern match → Execute deterministically
                    │
                    └─ no match → LLM reasons → (optionally) compile the new decision
```

The model moves from *inside* the hot loop to *above* it. It is consulted at the boundary of the known, and each consultation can permanently enlarge the compiled region. Over time the system's marginal cost per decision trends toward the cost of evaluating a predicate — while the Think→Tool system's marginal cost stays constant.

The comparison is not adversarial. The Think→Tool loop is exactly what Nano escalates *to*. The claim is narrower and stronger: repeated decisions do not belong inside an inference loop, any more than a hot function belongs inside an interpreter when a compiler is available.

## What makes the compilation trustworthy

A decision compiled by an AI is a decision that must be gated before it runs. Three mechanisms carry that weight:

- **Load-time validation as the security boundary.** Graphs that violate their manifest or contain unknown constructs are rejected before execution, not discovered mid-run.
- **Provenance.** Every compiled pattern records where it came from — `provenance` is a mandatory field on `Pattern`, so machine-compiled patterns stay distinguishable from hand-authored ones and can be gated accordingly (paper [04](04-provenance.md)).
- **Admission gates.** Nothing self-deploys. A compiled workflow is a proposal; deployment passes through the same propose/dispose discipline as the intents it will one day emit — implemented for the optimization loop in `admit_mutation` ([`nano/loop/mutation.py`](../../nano/loop/mutation.py)).

---

*Previous: [01 — Why Nano](01-why-nano.md) · Next: [03 — Determinism](03-determinism.md)*
