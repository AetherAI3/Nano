# 01 — Why Nano

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Autonomous systems stop reasoning about the same thing over and over by compiling known reasoning into deterministic execution — Nano is the language, IR, and engine-governed runtime that makes that compilation possible.

---

## The orchestration crisis

The models got smart. The layer that runs them did not.

Foundation models now carry reasoning capability that would have been science fiction five years ago, yet the machinery used to orchestrate them in production — the layer that decides what runs, when, in what order, with what state — remains fragile, non-deterministic, and largely unauditable. The industry is trapped between two poles. On one side sit lightweight agent frameworks that surrender control flow to the model itself: the LLM decides which tool to call, which branch to take, which agent to hand off to, on every tick. On the other side sit heavyweight industrial workflow engines that deliver durable, exactly-once execution — at the cost of dedicated server fleets, event-sourcing databases, and operational complexity that dwarfs the agent being orchestrated.

Neither pole is acceptable for systems that act on the world. A system whose control flow is a sample from a probability distribution cannot be verified, cannot be replayed, and cannot answer *why did you do that?* with anything better than a shrug. A system that requires a distributed workflow cluster to check whether RSI dropped below 30 is not an agent; it is an ops burden.

Nano exists to resolve this dichotomy. It is deliberately neither pole: a compiled, engine-governed execution layer that is light enough to run as a pure-Python interpreter and strict enough that every run replays bit-for-bit.

## The central question

Watch any production agent for a week and you will see the same pattern: the overwhelming majority of its decisions are decisions it has already made. The market condition it evaluates at 09:35 is structurally identical to one it evaluated last Tuesday. The workflow branch it reasons through is the branch it reasoned through four hundred times this month. Yet the system pays for full inference every single time — in latency, in dollars, and in variance, because a probabilistic model asked the same question twice does not reliably give the same answer twice.

Human experts do not work this way. A radiologist does not re-derive anatomy for every scan; a chess master does not calculate known openings from first principles. Expertise *is* the compilation of reasoning into fast, reliable execution, with conscious deliberation reserved for the genuinely novel. Current agent architectures have no equivalent mechanism. They are permanently deliberating.

So the question Nano is built around is exactly one question:

> **How do autonomous systems stop reasoning about the same thing over and over?**

## Why prompting harder fails

The first instinct is always the same: fix it in the prompt. It does not hold.

A hand-authored prompt inside an agent pipeline is a load-bearing component with an unpredictable blast radius. It gatekeeps what context reaches downstream tools and how rationales pass between agents. When it drifts — and it drifts, because minor semantic shifts in user input or tool output change what the model does with the same template — the failure is silent. The pipeline does not crash. The retrieval step still returns relevant documents; the model just transforms them subtly wrong. The final answer still passes a loose string-match eval. The failure surfaces weeks later as an unexplained drop in task-completion rate for one customer cohort, a compliance question about a citation that supports nothing, a p99 latency spike traced to a model that quietly started producing three times the chain-of-thought it used to.

**Prompted orchestration** compounds this exponentially. When the model itself decides workflow transitions — which tool, which branch, which agent next — the application's control flow *is* a sequence of samples. That buys open-ended flexibility and destroys every formal guarantee at once: routing can hallucinate, loops can fail to terminate, and no two runs of the "same" workflow need agree. For anything safety-relevant — capital, infrastructure, healthcare — this is disqualifying by construction, not by degree.

## What compilation already solved — and what it didn't

The research community has made real progress on half of this problem. Prompt-compilation frameworks (DSPy is the canonical example) stop treating prompts as hand-tuned strings and start treating them as artifacts *searched for* by an optimizer: declare typed input/output signatures, define a success metric, and let the compiler traverse the prompt space — mutating instructions, reordering demonstrations, scoring candidates against a held-out set — until it finds a configuration that measurably wins. Reflective variants go further, using a second model to diagnose failures and propose improved instructions in a loop. Published results across these systems consistently show large accuracy gains over manual prompting, and the discipline they impose — typed module boundaries, metric-driven iteration, traceable module calls — is exactly the discipline production systems need.

But note what these frameworks optimize: **the intelligence layer, not the execution layer.** A compiled prompt module is a better function. It is still a function with no durable runtime underneath it — nothing that coordinates multi-step work across models, tools, and fallbacks; nothing that survives a process crash; nothing that makes two executions of the same workflow provably identical. The compiler produced a perfect mathematical function and handed it to an execution environment built on hope.

Nano is aimed at that missing half — and at the interface between the halves. Reasoning (from a model, an optimizer, or a human) is *compiled into* Nano IR; from that point on, execution is the engine's problem, and the engine is deterministic.

## The false choice in orchestration

The current landscape offers a spectrum of state-management philosophies, each with a characteristic failure mode:

| Approach | Exemplars | State model | Characteristic failure |
|---|---|---|---|
| Conversational / emergent state | AutoGen, CrewAI | State *is* the accumulated chat history | Rollback is impossible; a hallucinated tool output can only be "forgotten" by more prompting; context growth eventually collapses the system |
| Explicit graph state | LangGraph | Typed state object with checkpointed transitions | Auditable, but heavy: high token overhead for chatty flows, and multi-agent debugging remains notoriously hard |
| Stateless handoff | Swarm-style routers | None — single-turn agent handoffs | No persistence, no observability; explicitly not for production |
| Durable workflow engines | Temporal | Database-backed event sourcing, exactly-once semantics | Bulletproof and operationally crushing: dedicated infrastructure, latency, and an ecosystem that is not LLM-native |
| Engine-governed DAGs | GraphBit and kin | Typed acyclic graph executed by a low-level engine; the LLM is a leaf function, never the router | Eliminates hallucinated routing and infinite loops — but strict acyclicity forecloses the self-refinement cycles advanced agents need |

The engine-governed row is the closest ancestor to Nano's position, and its core move is the right one: **take the model out of the orchestration loop entirely.** The model reasons inside tightly bounded, typed leaves; a deterministic engine owns every transition. What Nano adds to that move is the compilation thesis (the graph is *compiled from reasoning*, carrying provenance back to its author), an intent/gate boundary instead of direct actuation, and cycles where they are safe — the runtime's schedule loop is bounded and engine-owned, and unbounded refinement lives outside the deterministic core, in the gated optimization loop ([`nano/loop/`](../../nano/loop/)) where every proposed change passes admission control before any runtime loads it.

## The answer

Compile it. Concretely:

```
Reason once → Compile → Execute → Escalate only when necessary
```

1. **Reason once.** A reasoning model — Claude, GPT, Gemini, a local model; Nano is deliberately model-agnostic — works out how to handle a class of situation.
2. **Compile.** That decision is expressed as Nano IR: a small, validated, serializable execution graph. In the shipped implementation ([`nano/ir/graph.py`](../../nano/ir/graph.py)) a graph is a schedule, a set of conditions, and a set of intents, guarded by an effect manifest — authored either as IR JSON or as `.nano` source through the compiler ([`nano/compiler/`](../../nano/compiler/__init__.py)).
3. **Execute.** The deterministic runtime ([`nano/runtime/interpreter.py`](../../nano/runtime/interpreter.py)) evaluates the graph against injected inputs. Identical graph plus identical inputs yields a bit-identical result, including the audit log.
4. **Escalate only when necessary.** When observations match no compiled pattern, the system routes back to inference. The shipped memory layer ([`nano/memory/patterns.py`](../../nano/memory/patterns.py)) implements exactly this: `PatternStore.retrieve()` returns matched patterns or `escalate=True` — never a guess.

The inversion this produces is the whole point: **inference becomes the exception; execution becomes the default.** We call the compiled artifact *compiled memory* — not a cache of answers, but a cache of *decision procedures*, each carrying its confidence, its validity domain, and its provenance. Response caching cannot do this job: the value of "buy when RSI < 30 and volume is elevated" is not a cached answer, it is a *predicate over future observations*. You need a representation for the predicate, not the answer.

## Restrictions are the feature

Why a language, though? Why not write the fast path in Python?

Because general-purpose languages permit too much. A Python fast path can open sockets, read the clock, consult global state, and race threads — so two runs of the "same" decision need not agree, and no one can prove after the fact why the system acted. (Papers [05](05-why-not-go.md) and [06](06-why-not-typescript.md) treat this in depth.)

There is a deeper design doctrine here, and it recurs everywhere robust systems are built: **deliberate restriction is what makes guarantees possible.** A type system restricts what programs you can write so it can promise what the surviving programs mean. A capability sandbox restricts what code can touch so it can promise what a compromise costs. An unconstrained orchestration layer — arbitrary loops, global mutation, dynamic routing — is maximally flexible and minimally verifiable: its execution paths cannot be serialized, its failures cannot be cleanly audited, and its behavior cannot be formally checked without wrapping it in an external monitoring apparatus larger than the system itself.

Nano takes the bet explicitly. The language removes sockets, ambient randomness, arbitrary I/O, threads, mutable globals, and any direct actuator or exchange API. Every removal exists because that capability destroys replayability. Time and entropy are injected, logged inputs. Every module carries an effect manifest checked at load time — a capability boundary the program cannot talk its way past, because the primitives it would need simply do not exist to compile. What remains is small, and the smallness is exactly what makes "why did the agent do that?" answerable with a replay instead of a shrug (paper [03](03-determinism.md)).

## Agents propose, gates decide

The second structural commitment: Nano programs cannot act. The terminal output of execution is a tuple of **intents** — proposals — plus an append-only log. A downstream gate (a risk engine, an approval policy, a human) disposes. Components propose; gates decide. This is enforced in the IR itself: there is no order or actuation primitive to compile, and emitting intents at all requires `intent.emit` in the module's effect manifest, checked at load time.

This same boundary governs self-improvement. An analysis loop may propose an optimized replacement graph; it cannot install one. Proposals pass admission gates — validation, replay-verification, human or policy sign-off — before any runtime loads them (paper [15](15-closed-loop-engineering.md)). **Nothing self-modifies silently.**

## A reliable citizen in agent networks

The orchestration layer is standardizing around shared protocols — Model Context Protocol for tool access, agent-to-agent messaging for cross-system coordination, OpenTelemetry GenAI conventions for tracing. Every one of these protocols embeds the same assumption: that each participating node executes its bounded tasks predictably. A node running on prompted orchestration or conversational state cannot honor that assumption; its unpredictability propagates through the network as deadlocks and silent divergence.

A Nano node can. Deterministic execution with an append-only audit log is, in effect, native event sourcing — every run *is* its own high-fidelity trace, structured enough to feed any conformant telemetry or evaluation pipeline without instrumenting the hot path. Determinism is not just a local safety property; it is what makes a node composable into networks larger than itself.

## Why trading first

Nano's first workload is trading strategy execution — not because trading is the product, but because trading is the most honest test environment available: inputs are deterministic time series, history is fully replayable, and outcomes are numerically measurable. A framework that claims "compiled decisions replay bit-identically and outperform per-decision inference" can be checked there without ambiguity. The architecture itself — Observe → Decide → Act → Record — generalizes to robotics, vehicles, security response, workflow automation, and beyond (paper [09](09-autonomous-systems.md)).

## What exists today

Honestly stated: the IR, the deterministic reference interpreter, the pattern memory layer, the `.nano` → IR compiler (`nano/compiler/`), the risk-gate bridge and backtester (`nano/bridge/`), the strategy library (`nano/library/`), editor language services (`nano/aethercode/`), and the Nano++ optimization loop with a deterministic quantum simulator backend (`nano/loop/`) are implemented and covered by the repository test suite. Every example compiles from `.nano` source to bit-identical IR and replays deterministically. The CLI (`nano compile` / `nano replay` / `nano visualize`) and real quantum-hardware dispatch remain design and research, tracked in [BUILD_ORDER.md](../../BUILD_ORDER.md) and paper [14](14-future-work.md). Comparative claims about other frameworks above describe their documented designs, not benchmarks we have run.

---

*Next: [02 — The Agentic Compiler](02-the-agentic-compiler.md), on what it means to compile decisions rather than algorithms.*
