# 06 — Why Not TypeScript? (And Why Not Python?)

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** TypeScript models applications and Python models reasoning — Nano models deterministic decision graphs, and removing application machinery from the language is not missing functionality, it is removing uncertainty.

---

## TypeScript models applications

TypeScript is arguably the best language ever built for its actual job: modeling applications. An application is a long-lived, stateful, event-driven artifact — it holds mutable state, awaits network responses, reads and writes files, renders UI, and reacts to a user whose next action is unknowable. TypeScript's core machinery exists precisely to make that tractable: structural types over evolving object shapes, `async`/`await` over an event loop, promises for in-flight network calls, a vast ecosystem for HTTP, storage, and rendering.

Now list those same features from the standpoint of an autonomous system's decision core:

- **Mutable state** means the decision's output depends on history you did not record.
- **Async and the event loop** mean ordering depends on I/O timing; two runs interleave differently.
- **Networking** means the decision depends on what a remote service said at that millisecond — an input that was never captured and can never be replayed.
- **File and environment access** mean hidden inputs.
- **UI and events** mean the program's behavior is a function of an unrecorded human.

None of these are flaws in TypeScript. They are the definition of an application. But every one of them injects uncertainty into a decision, and uncertainty is exactly what an autonomous system's operators need removed. When Nano excludes sockets, ambient time, arbitrary I/O, threads, and mutable globals, it is not failing to reach feature parity with TypeScript — it is deleting the mechanisms by which a decision's inputs escape the record. **Removing them is not removing functionality; it is removing uncertainty.**

What remains is a different kind of object entirely: a deterministic decision graph. In the shipped IR, a program is a schedule, conditions, and intents behind an effect manifest ([`nano/ir/graph.py`](../../nano/ir/graph.py)); execution is a pure function of the graph and an injected frame ([`nano/runtime/interpreter.py`](../../nano/runtime/interpreter.py)). A TypeScript program is something you *run and observe*. A Nano program is something you can *replay and prove* (paper [03](03-determinism.md)).

There is also a place where the two meet naturally: TypeScript is a fine language for the application *around* Nano — the dashboard that visualizes execution graphs, the approval UI where a human disposes of intents, the editor tooling. The application layer models the humans; Nano models the decisions.

## Python models reasoning

Python deserves a more nuanced treatment, because Nano's own reference implementation is written in it, and because Python is where the reasoning side of the Nano loop naturally lives.

Python is excellent at exploration: notebooks, NumPy and pandas, model APIs, rapid iteration on an idea. When a quant or an ML engineer is *figuring out* a decision — testing hypotheses, fitting parameters, asking a model to reason — Python is the right tool, and Nano does not compete with it there at all.

The problem appears at the moment the figured-out decision goes into production and starts being made thousands or millions of times. Production Python decision code inherits everything TypeScript-style applications inherit — ambient time, ambient randomness, mutable module state, unrestricted imports (including, in trading, the exchange SDK that lets a strategy bypass its risk engine) — plus interpreter overhead on every evaluation. And the guarantee that a Python "pure function" stays pure is a code-review guarantee, not a language one.

Nano's division of labor is explicit:

```
Python (or any reasoning environment):  figure the decision out — once
Nano:                                   execute the decision — millions of times, identically
```

**Reason once, compile, execute millions of times.** The reasoning environment produces IR (or `.nano` source through the shipped compiler); the IR is validated at load (unknown nodes rejected, effect manifest enforced); the runtime executes it with no ability to reach back into Python's ambient capabilities. The exploratory freedom that makes Python ideal for reasoning is exactly what makes it wrong as the substrate of record for repeated autonomous decisions — and the restriction that makes Nano wrong for exploration is exactly what makes it right for execution.

## The general principle

The pattern across this paper and paper [05](05-why-not-go.md) is one principle applied three times. Go optimizes for computing efficiently; TypeScript optimizes for interacting with users and networks; Python optimizes for thinking fluidly. Each earns its power by admitting uncertainty — concurrency, events, dynamism. Nano occupies the one niche none of them can: the language whose programs are decisions, whose every input is recorded, whose every output is a proposal, and whose every execution can be reproduced exactly. Different layer, different contract, deliberately smaller language.

---

*Previous: [05 — Why Not Go?](05-why-not-go.md) · Next: [07 — Nano vs. Pine Script](07-nano-vs-pine.md)*
