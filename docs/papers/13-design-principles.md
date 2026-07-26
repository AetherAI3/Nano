# 13 — Design Principles

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Six principles govern every Nano decision — determinism, the intent/dispose split, IR-first construction, injected effects, content addressing, and gated self-improvement — and each is a rule that resolves design arguments before they start.

---

A language earns coherence not from its features but from the rules that decide what features it refuses. These are Nano's six, each stated as the question it answers.

## 1. Determinism — "why did it do that?"

Every execution must be reproducible bit-for-bit from its recorded inputs. This is the root principle; the other five either serve it or build on it. The shipped interpreter states it as a contract — pure function of graph plus frame, no ambient clock, no ambient randomness, no I/O ([`nano/runtime/interpreter.py`](../../nano/runtime/interpreter.py)) — and paper [03](03-determinism.md) develops it fully. Design test: *if a proposed feature can make two runs on identical inputs differ, it does not enter the language, whatever it is worth.*

## 2. The intent/dispose split — "who is allowed to act?"

Nano programs propose; they never act. The terminal output of execution is a tuple of `Intent` values ([`nano/runtime/effects.py`](../../nano/runtime/effects.py)); disposition belongs to a gate outside the language — a risk engine, a policy, a human. The IR enforces this by omission (no order or actuation primitive exists to compile) and by declaration (emitting intents requires `intent.emit` in the manifest — [`nano/ir/graph.py`](../../nano/ir/graph.py)). [BUILD_ORDER.md](../../BUILD_ORDER.md) records it as the one architectural lock: components propose, gates decide. Design test: *any construct that would let a strategy reach the world directly is rejected, including convenient ones.*

## 3. IR-first — "what is the real artifact?"

The IR is the product; surface syntax is a convenience layered on later. Nano began as a validated, serializable execution graph with a reference interpreter — the LLVM and Qiskit path — and the `.nano` language will be judged by one conformance rule: source must compile to IR that replays identically to hand-written IR for every example. This ordering keeps every consumer honest: tooling, runtimes, gates, and provenance all target one artifact format, and the language can never quietly diverge from what actually executes. Design test: *any capability must exist and be proven in the IR before it earns syntax.*

## 4. Injected effects — "where do inputs come from?"

Everything the outside world contributes — time, market data, entropy when it arrives — enters as an explicit, recorded input, never as an ambient capability. `MarketFrame` carries the timeline and aligned signal series; the scheduler iterates recorded ticks; a missing signal is an error, not a fetch. Injection is what makes determinism *practical* rather than merely declared: replay is possible because every input was, by construction, captured. It also collapses the live/backtest distinction — same code path, different frames. Design test: *if the runtime would need to ask the environment for something mid-execution, the design is wrong; the answer must arrive in the frame.*

## 5. Content addressing — "which behavior is this, exactly?"

A compiled behavior is identified by what it is, not what it is called. Because graphs serialize canonically (`to_dict` round-trips a validated graph), a behavior has a stable content hash: "the strategy that ran Tuesday" is a digest, not a filename that may have been overwritten. This underwrites reproducible builds, exact version diffs, memoized re-execution, and the provenance chain — a signature over a content hash attests to the actual behavior (paper [04](04-provenance.md)). Pinned package hashes extend the same rule to dependencies. Design test: *no identity by mutable reference; two parties holding the same hash must hold the same behavior.*

## 6. Gated self-improvement — "how does the system change?"

Nano is built for systems that improve their own behavior — a model observes performance, proposes a modified graph, and the proposal is evidence-backed: replayed against identical history, diffed decision-by-decision, carrying its provenance (mandatory on every `Pattern` — [`nano/memory/patterns.py`](../../nano/memory/patterns.py)). But improvement is a *lifecycle* event, never a runtime one: nothing self-modifies silently, and every admitted change passes a gate with a human or policy behind it. Adaptation between executions; determinism within each (paper [10](10-nano-family.md)). Design test: *any learning mechanism that would alter behavior without passing an admission gate is rejected.*

## How the six compose

The principles are not independent; they form one argument. Injected effects make determinism achievable. Determinism makes replay exact. Exact replay plus content addressing make provenance verifiable rather than asserted. Verifiable provenance plus the intent/dispose split make gates meaningful — a gate can know precisely what it is admitting and what it later approved. And meaningful gates are what make self-improvement safe. Remove any one principle and the chain breaks: mutable identity breaks provenance, ambient inputs break replay, direct actuation breaks gating. This interdependence is why the principles are non-negotiable rather than preferences — and why the papers on Go, TypeScript, and Pine ([05](05-why-not-go.md), [06](06-why-not-typescript.md), [07](07-nano-vs-pine.md)) keep returning to the same point: general-purpose languages cannot adopt this chain piecemeal, because each link only holds when all of them do.

---

*Previous: [12 — Security](12-security.md) · Next: [14 — Future Work](14-future-work.md)*
