# 17 — Heterogeneous Compute

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** One IR, many backends — Nano's long-term execution model dispatches the same validated graph to the reference interpreter, native runtimes, distributed clusters, or specialized hardware, with the choice of backend invisible to the graph's meaning and guarantees.

---

## The problem heterogeneity solves

An autonomous system's decision workloads do not share a performance profile. A supervisory rule ticking every five minutes is happy in an interpreter. A backtest sweeping ten years of frames across a thousand parameter variants wants a cluster. A control-adjacent decision on a 100 ms deadline wants a compiled native path. A combinatorial optimization step may one day want an accelerator or a quantum backend (paper [16](16-quantum-computing.md)). Forcing all of these through one execution engine means either overbuilding the simple case or starving the demanding one.

The conventional answer is one codebase per regime — the Python research version, the C++ production port, the Spark job for the sweep — with "should be equivalent" as the integration strategy. Every team that has run that architecture knows where it fails: the ports drift, and the version that made the money is not quite the version that passed the review.

Nano's answer is the compiler community's answer, applied to decisions: **one artifact, many lowerings.** The IR is the single source of truth for what a behavior *is*; backends differ only in how fast, how parallel, and on what silicon it runs.

```
                       Nano IR (validated, content-addressed)
                                      │
        ┌──────────────┬──────────────┼──────────────┬─────────────────┐
        ▼              ▼              ▼              ▼                 ▼
  Reference       Native runtime   Distributed    Accelerated     Quantum/hybrid
  interpreter     (low latency)    (backtests,    (batch numeric   (optimization
  (shipped —      [design]         sweeps)        evaluation)      steps)
  the spec)                        [design]       [design]         [research]
```

## Conformance: replay is the contract

What keeps heterogeneous backends honest is the property the rest of the series keeps returning to: execution is a pure function of graph plus injected frame ([`nano/runtime/interpreter.py`](../../nano/runtime/interpreter.py), paper [03](03-determinism.md)). That gives backend equivalence an unusually strong definition — not "passes the same test suite" but **bit-identical results**: for every graph and frame, a conforming backend must produce exactly the intents and log the reference interpreter produces.

This is why the reference interpreter is written for clarity rather than speed (paper [11](11-performance.md)): it is the executable specification. A native runtime, a vectorized batch evaluator, or a distributed executor is validated by replaying the example corpus and diffing bytes. The conformance rule already governs the language front end — `.nano` source must compile to IR that replays identically to hand-written IR ([BUILD_ORDER.md](../../BUILD_ORDER.md)) — and the same rule extends to every backend. Drift between environments stops being a code-review concern and becomes a mechanically checkable one.

Determinism also does the heavy lifting for *distribution* specifically. Executions over disjoint frames share no mutable state, so a parameter sweep or a fleet-wide backtest parallelizes with no coordination beyond scattering inputs and gathering results — and any anomalous result from any worker is individually replayable on a laptop. The scheduling problem for massive execution graphs (a Nano++ concern — paper [10](10-nano-family.md)) is a real engineering problem, but it is scheduling *pure work*, the easiest kind there is.

## Guarantees that travel with the artifact

Backend choice must never weaken the security or audit story, and the design makes that structural rather than aspirational:

- **The manifest travels.** A graph's effect manifest is part of the artifact; every backend enforces the same load-time validation ([`nano/ir/graph.py`](../../nano/ir/graph.py)). Moving a strategy from interpreter to cluster grants it nothing.
- **Identity travels.** Content addressing means "what ran on the cluster" and "what ran locally" are provably the same behavior — one hash (paper [13](13-design-principles.md)).
- **Provenance travels.** Attestations bind to the content hash, not the venue, so the Protocol C chain (paper [04](04-provenance.md)) spans backends without modification: what compiled, what executed, who approved — wherever it executed.
- **The gate stays put.** Backends evaluate; they never dispose. Intents from a thousand distributed workers flow to the same gate discipline as intents from one interpreter.

## Status

Sized honestly: the reference interpreter is the only IR execution backend that exists, joined by the loop's deterministic quantum-job simulator (`SimulatorRuntime` in [`nano/loop/quantum.py`](../../nano/loop/quantum.py)) as the first non-interpreter compute target. Native, distributed, and accelerated runtimes are design; real quantum hardware dispatch is research (papers [14](14-future-work.md), [16](16-quantum-computing.md)). What is *not* deferred is the property that makes them all possible — the IR/runtime separation, pure-function execution, and byte-level conformance criterion are shipped and tested today. Heterogeneous compute is not a pivot Nano might make later; it is the payoff of decisions already locked in.

---

*Previous: [16 — Quantum Computing](16-quantum-computing.md) · Back to the [series index](README.md)*
