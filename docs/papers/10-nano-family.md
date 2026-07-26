# 10 — The Nano Family

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Nano, Nano+, and Nano++ are three capability tiers over one compiler and one IR — deterministic execution, adaptive execution, and large-scale computational execution — so a user grows without ever migrating.

---

## Why tiers instead of one big language

A language that tried to serve a first-time strategy author, a multi-agent systems engineer, and a distributed-optimization researcher with a single feature surface would fail all three: the beginner drowns in capability, the researcher chafes at guardrails, and the auditor can no longer tell what a given program is allowed to do.

Nano's answer is tiering by *capability*, not by dialect. There is one compiler and one IR; a tier is a bounded vocabulary of constructs — and therefore a bounded set of effects a program at that tier can declare. Since every graph already carries an explicit effect manifest checked at load time ([`nano/ir/graph.py`](../../nano/ir/graph.py)), tier boundaries are enforceable the same way everything else in Nano is enforced: structurally, before execution, not by convention.

```
Nano      →  deterministic execution        "What should the system do?"
Nano+     →  adaptive execution             "How should the system reason and coordinate?"
Nano++    →  computational execution        "How should complex computation be built and run?"

              one compiler · one IR · increasing capability
```

## Nano — the deterministic execution language

The base tier is what this repository implements today: schedules, conditions, and intents, executing as a pure function of injected inputs, replaying bit-identically, proposing rather than acting ([`nano/ir/nodes.py`](../../nano/ir/nodes.py), [`nano/runtime/interpreter.py`](../../nano/runtime/interpreter.py)). This is the tier of papers [01](01-why-nano.md)–[03](03-determinism.md): compiled decisions, gated execution, exact audit.

It is deliberately the *whole product* for most users. A trading strategy, a monitoring policy, or a workflow rule needs nothing above it — and everything above it inherits its guarantees, because the higher tiers compile to the same IR discipline rather than escaping it.

## Nano+ — adaptive execution

Nano+ adds the machinery of adaptation: compiled memory, confidence routing, multi-agent coordination, and gated learning. This is where the escalation architecture becomes first-class — patterns with confidence, expiry, and provenance; retrieval that either supplies context or escalates to a reasoning model; agents as named behavior blocks coordinating through the same intent/gate discipline.

Status, honestly: the memory layer ships today — `Pattern`, `PatternStore`, and confidence-thresholded retrieval with explicit escalation ([`nano/memory/patterns.py`](../../nano/memory/patterns.py)) — and the `Agent` node exists in the IR. Confidence routing in the runtime and multi-agent constructs remain design; the learning lifecycle has its first implemented piece in the gated mutation admission of the optimization loop ([`nano/loop/mutation.py`](../../nano/loop/mutation.py)).

The critical property is what Nano+ does *not* change: adaptation happens between executions, never during one. A Nano+ system learns by compiling new patterns through admission gates — each individual execution remains as deterministic and replayable as base Nano. Adaptivity lives in the lifecycle; determinism lives in the run.

## Nano++ — computational execution

Nano++ is the research and heavy-compute tier: distributed optimization, massive execution graphs, high-performance scheduling, and research workloads, including consuming computational paths discovered by AQRC. Where Nano asks "when should the system act?", Nano++ asks how large, structured computation should be constructed, optimized, and executed — while still emitting into the same IR and the same provenance chain.

Status: the first Nano++ component is implemented — the autonomous optimization loop ([`nano/loop/`](../../nano/loop/__init__.py)): the observe → propose → compile → execute → measure → verify → admit lifecycle as deterministic, replayable IR, with a vendor-agnostic `QuantumRuntime` protocol and a content-seeded deterministic `SimulatorRuntime`. Distributed and high-performance runtimes remain design; [BUILD_ORDER.md](../../BUILD_ORDER.md)'s rule stands — the advanced tier grows only over the proven execution graph.

## One compiler, one IR — why it matters

The tiers are a growth path, not three products:

- **No migration cliff.** A strategy written on day one remains valid when its author adopts Nano+ memory or Nano++ optimization. Capability is added around the artifact, not by rewriting it.
- **Uniform audit.** A regulator or reviewer reads one IR, one manifest format, one log format, regardless of tier. A Nano++ research workload is exactly as replayable and provenance-carrying as a beginner's RSI rule (paper [04](04-provenance.md)).
- **Shared tooling.** The interpreter, the planned CLI (`nano compile` / `nano replay` / `nano visualize`), the visualizer, and the gates serve all tiers, because there is only one artifact format to serve.
- **Honest layering.** Each tier's claims rest on the tier below. Nano+ adaptation is trustworthy because Nano execution is deterministic; Nano++ scale is auditable because both lower tiers preserve the manifest and provenance discipline.

The family, in one sentence: the same compiled decision, given memory, then given scale — never given a different foundation.

---

*Previous: [09 — Autonomous Systems](09-autonomous-systems.md) · Next: [11 — Performance](11-performance.md)*
