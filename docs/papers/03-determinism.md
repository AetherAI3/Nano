# 03 — Determinism

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Nano is deterministic so that "why did the agent do that?" has an exact, mechanically checkable answer — every execution can be replayed bit-for-bit from its recorded inputs.

---

## The question determinism answers

When an autonomous system does something surprising, the operator's first question is always the same: *why did it do that?* In most agent stacks the honest answer is a reconstruction — logs, a model transcript, a best guess about what the sampler did. In Nano the answer is a replay. Feed the same graph and the same recorded inputs back through the runtime and you get the same intents and the same log, byte for byte. The explanation is not a narrative; it is a reproduction.

This property is not aspirational. It is the design contract of the shipped reference interpreter, stated in its module docstring and enforced by its structure:

> Executes a validated StrategyGraph against an injected MarketFrame. Pure function of its inputs: identical graph + identical frame => identical result, bit-for-bit. No ambient clock, no ambient randomness, no I/O.
> — [`nano/runtime/interpreter.py`](../../nano/runtime/interpreter.py)

The test suite exercises this replay guarantee alongside the rest of the runtime, and the conformance corpus extends it to the compiler: `.nano` source must compile to IR that replays identically to the hand-written IR for every example.

## How determinism is achieved

Determinism is not a testing discipline in Nano; it is what remains when the sources of nondeterminism are removed from the language.

**Time is an injected input.** The runtime never calls a clock. `MarketFrame` carries an explicit tuple of timestamps, and the scheduler iterates over those recorded ticks — the program observes time, it never asks for it. Two consequences follow immediately: live execution and historical replay are the *same code path* over different frames, and there is no way for a strategy to behave differently at replay time because "now" changed.

**Entropy is an injected input.** There is no random primitive in the IR and no ambient RNG in the runtime. Where randomness is genuinely needed — the quantum simulator backend in the optimization loop — it is seeded by the job's content, never by ambient entropy, so loop replays stay bit-identical ([`nano/loop/quantum.py`](../../nano/loop/quantum.py)).

**Data is an injected input.** The interpreter performs no I/O. All observations come from the frame passed to `execute()`; a signal absent from the frame is a runtime error, not a network fetch. `MarketFrame` validates on construction that every signal series is exactly aligned with the timestamp series, so there is no partially observed state to diverge on.

**State cannot leak between runs.** IR nodes, graphs, intents, and log entries are all frozen dataclasses; graphs never mutate after load ([`nano/ir/nodes.py`](../../nano/ir/nodes.py)). There are no mutable globals to accumulate history, no threads to interleave, and no caches whose warmth could alter results.

**The log is part of the output.** `execute()` returns an `ExecutionResult` containing both the intents and a complete append-only log — every condition evaluation with its observed value and outcome, every intent emission, each stamped with the injected timestamp. Because the log is produced by the same pure function, the *audit trail itself* replays bit-identically. An auditor does not have to trust that the log matches the run; regenerating it is the check.

## What determinism buys

**Exact debugging.** A misbehaving strategy is debugged by replaying the frame that triggered it. There is no "could not reproduce."

**Honest backtests.** Because live and historical execution share one code path, a backtest is not a simulation of the strategy — it is the strategy. Look-ahead is attacked at the same level: the interpreter observes signals only at the current tick index, and the planned `Series<T>` typing makes peeking at future values a compile error rather than a code-review hope.

**Meaningful audit.** A regulator, a risk officer, or a post-incident review can verify — not merely be told — what the system evaluated and why it proposed what it proposed. Provenance (paper [04](04-provenance.md)) builds on this: signatures over nondeterministic behavior would attest to little.

**Trustworthy self-improvement.** When a model proposes a modified strategy, the old and new graphs can be replayed over identical history and compared decision-by-decision. Without determinism, an observed improvement might be sampling noise; with it, the diff in behavior is exact.

## The price, paid deliberately

Determinism is why Nano removes what it removes: sockets, wall clocks, ambient randomness, arbitrary I/O, threads, mutable globals. Each is a capability a general-purpose language treats as essential, and each makes bit-identical replay impossible. Nano treats their absence as the feature (papers [05](05-why-not-go.md), [06](06-why-not-typescript.md), [12](12-security.md)). The result is a language that cannot express most programs — and can prove everything about the programs it does express.

---

*Previous: [02 — The Agentic Compiler](02-the-agentic-compiler.md) · Next: [04 — Provenance](04-provenance.md)*
