# 11 — Performance

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Nano's performance claim is architectural — replacing per-decision inference with compiled execution removes orders of magnitude of latency and cost by construction — while the runtime's own constants remain deliberately unmeasured until there are benchmarks worth trusting.

---

## The performance claim Nano actually makes

It is easy to overclaim here, so the claim is stated precisely. Nano does not claim to be a fast interpreter — the shipped reference interpreter is unoptimized Python, chosen for auditability. Nano claims something categorically different: for repeated decisions, **the dominant cost in an agent system is inference, and compilation removes it from the hot path entirely.**

An inference-based decision costs, per decision: a network round trip, model queue time, and generation — hundreds of milliseconds to seconds of latency, and cents of metered cost. A compiled Nano decision costs, per decision: loading nothing (the graph is resident), evaluating a handful of numeric comparisons, and appending log entries — microseconds of CPU, zero marginal dollars. The gap between those two regimes is six to nine orders of magnitude in latency and effectively unbounded in relative cost. No amount of interpreter overhead in the compiled path closes a gap that large, which is why the architectural claim is robust even though the implementation is young. The full arithmetic, including escalation rates, is worked in paper [08](08-llm-integration.md).

## Where the compiled path spends its time

Three shipped components define the execution profile:

**The interpreter.** `execute()` is a straight loop: for each scheduled tick, evaluate conditions against the frame, short-circuiting on the first false; on all-true, emit intents ([`nano/runtime/interpreter.py`](../../nano/runtime/interpreter.py)). Work per tick is O(conditions), with condition evaluation a single float comparison ([`nano/ir/nodes.py`](../../nano/ir/nodes.py)). There is no allocation-heavy machinery in the decision itself; the append-only log dominates allocations, which is a defensible trade — the log *is* a product of execution, not overhead on it (paper [03](03-determinism.md)).

**The scheduler.** Ticks are derived from the injected timestamp series, not from timers or threads. Scheduling therefore costs an iteration, never a context switch, and — more importantly for the architecture — introduces no nondeterminism for performance to be traded against later.

**Pattern retrieval.** The memory layer's hot operation is `PatternStore.retrieve()`: filter live patterns by confidence threshold, then match each against current observations, where a match is again O(conditions) float comparisons ([`nano/memory/patterns.py`](../../nano/memory/patterns.py)). Retrieval is linear in the number of stored patterns today. That is the right implementation for a store measured in hundreds of patterns and a deliberately naive one for a store measured in millions — indexing strategies (by signal, by validity window) are an anticipated, unbuilt optimization. Crucially, retrieval is the *escalation gate*: every call it resolves without escalating is an inference call that never happens, so even a slow retrieval that beats a network round trip is winning by a large margin.

## What is honestly unmeasured

The repository currently ships correctness tests, not benchmarks. The following numbers do not exist yet, and no document in this series should be read as implying them:

- Interpreter throughput (ticks/second) on realistic strategy graphs and frame sizes.
- Pattern retrieval latency as a function of store size.
- End-to-end decision latency through the risk-gate bridge (`nano/bridge/`) under production load.
- Memory footprint of resident graphs and logs over long horizons.
- Any comparison between the Python reference interpreter and a future compiled or native runtime.

The reference interpreter's job is to be *obviously correct* — the executable specification against which faster runtimes will be validated by bit-identical replay, exactly as the shipped compiler is validated against the conformance corpus today. Optimizing ahead of that discipline would invert the project's own build order. When benchmarks land, they will measure the claims made here; until then, the honest statement is: the architectural gap is real by construction, the implementation constants are unknown by choice.

## Determinism as a performance asset

One further point deserves to be made, because it is usually missed: determinism, adopted for auditability, is also Nano's long-term performance strategy. A pure function of immutable inputs is the best case for every optimization that matters — results can be memoized by content hash (identical graph and frame imply identical result, so re-execution is a cache lookup), backtests parallelize perfectly across frames with no locks because there is no shared mutable state, and a future compiler can reorder or specialize aggressively because there are no hidden effects to preserve. Languages usually pay for auditability with speed. Nano's bet is that a language with no ambient state gets to keep both.

---

*Previous: [10 — The Nano Family](10-nano-family.md) · Next: [12 — Security](12-security.md)*
