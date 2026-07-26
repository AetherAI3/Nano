# 05 — Why Not Go?

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Go tells a computer how to compute; Nano tells an autonomous system when to act — they solve different problems, and a serious deployment will often use both.

---

## The question behind the question

"Why not just write this in Go?" is really asking: why does compiled decision-making need a language at all, when fast, reliable general-purpose languages exist? The answer is not that Go is deficient. Go is an excellent systems language — simple, fast, statically typed, with first-class concurrency. The answer is that a systems language and a decision language are optimizing for opposite guarantees.

## What a systems language guarantees

Go's contract with the programmer is about *computation*: given your description of how to compute, the toolchain produces an efficient, portable, memory-safe executable. To honor that contract, Go gives you the full capability set a program might need — goroutines and channels, the `net` package, `os` and `io`, `time.Now()`, `math/rand`, mutable package-level state. These are the tools of "how to compute," and Go's design makes them ergonomic on purpose.

Every one of those capabilities is a hole in the guarantee Nano exists to make.

- `time.Now()` means a function's behavior can depend on when it ran. Replay diverges.
- `math/rand` (or crypto randomness) means two runs need not agree at all.
- Goroutines mean scheduling order can change results; a data race is a nondeterminism generator even when it is not a bug.
- The `net` package means a "decision" can quietly depend on whatever a remote endpoint said at that instant — unrecorded, unreplayable.
- Mutable globals mean run *N* can behave differently because of run *N−1*.
- And nothing in Go stops a trading strategy from importing an exchange SDK and placing the order itself, bypassing every risk control that was supposed to stand in front of it.

You can, with discipline, write Go that avoids all of this — inject clocks, forbid goroutines in decision code, wrap I/O behind interfaces, review every import. But then the guarantee lives in your code review, not in your language. It holds until one contributor, one dependency, one refactor. Nano's position is that for autonomous decision-making, replayability must be a property of the *language*, not a property of the team.

## What a decision language guarantees

Nano's contract is about *decisions*: a compiled behavior, run against recorded inputs, produces exactly one possible result, and it cannot act on the world — it can only propose. The shipped implementation makes both halves concrete:

- The reference interpreter is a pure function of graph plus injected frame; no ambient clock, randomness, or I/O exists to reach for ([`nano/runtime/interpreter.py`](../../nano/runtime/interpreter.py)).
- The IR has no order, network, or actuation primitive. Its terminal output is a tuple of `Intent` values ([`nano/runtime/effects.py`](../../nano/runtime/effects.py)), and even emitting those requires `intent.emit` in the module's effect manifest, checked at load time ([`nano/ir/graph.py`](../../nano/ir/graph.py)).

In Go, forgetting to route an order through the risk engine is a bug. In Nano, it is unrepresentable. That difference — bug versus unrepresentable — is the entire justification for a separate language. Removing sockets, threads, ambient time, ambient entropy, and mutable state is not Nano lacking features Go has; it is Nano *charging for its guarantee*. Each removal buys a piece of bit-identical replay and provable propose-only behavior (papers [03](03-determinism.md), [12](12-security.md)).

## Expressiveness is the point, in both directions

Go can express any program, which is why it can prove almost nothing about a given program's decision behavior without reading it. Nano's strategy tier can express only schedules, conditions, and intents ([`nano/ir/nodes.py`](../../nano/ir/nodes.py)), which is why every Nano program is replayable, auditable, and gate-respecting *by construction*. A language's power to make guarantees is inversely related to its power to express arbitrary computation. Nano sits deliberately at the guarantee-heavy end; Go sits, correctly for its purpose, at the expressiveness-heavy end.

## Complementary, not competing

In a real deployment the two languages meet, and neither replaces the other. The market data ingester that feeds Nano its frames is exactly the kind of concurrent, network-heavy service Go is built for. The risk engine that disposes of Nano's intents (any host object implementing the `RiskEngine` protocol — [`nano/bridge/`](../../nano/bridge/__init__.py)), the gateway that talks to exchanges or actuators, the scheduler infrastructure around the runtime — systems software, all of it, and a fine fit for Go.

The boundary is clean because Nano's interface is data: IR JSON in, intents and logs out. A Go service can host a Nano runtime the way services host a regex engine or a SQL planner — an embedded, restricted evaluator for the part of the system where restriction is the value.

```
Go (or Rust, or Python):  feeds, gateways, gates, infrastructure — how to compute
Nano:                     the decision core — when to act, provably
```

Use Go to build the system. Use Nano to make the system's decisions answerable.

---

*Previous: [04 — Provenance](04-provenance.md) · Next: [06 — Why Not TypeScript (or Python)?](06-why-not-typescript.md)*
