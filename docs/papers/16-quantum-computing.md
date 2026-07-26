# 16 — Quantum Computing

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Quantum computing enters Nano as a research track, not a promise — the shipped Nano++ loop already models vendor-agnostic quantum jobs with a deterministic simulator backend, while real-hardware dispatch and any advantage claim remain squarely research.

---

## The claim, sized correctly

This paper makes the smallest defensible claim about quantum computing, because the field is littered with the other kind. The claim is: **if** quantum or hybrid hardware ever delivers advantage on the optimization problems autonomous systems actually face, Nano's architecture can dispatch those workloads through the same IR, gates, and provenance chain as everything else — and the work required to find out is an isolated research track that cannot destabilize the classical language.

Not claimed: quantum advantage for trading, any current quantum execution in this repository, or any timeline. [BUILD_ORDER.md](../../BUILD_ORDER.md) is explicit that quantum syntax before a proven execution graph would be the project's defining mistake, and the ordering there — AQRC integration only after Nano works, Nano++ only over the proven IR — exists to prevent it.

## Why quantum appears in an execution language at all

Inside every serious autonomous system sit combinatorial optimization problems: portfolio construction under constraints, fleet scheduling and routing, resource allocation, structural search over large configuration spaces. Classically these are solved with heuristics whose quality degrades with scale. They are also precisely the problem family — quadratic and higher-order combinatorial optimization — that quantum annealing and gate-model variational methods target.

An execution language for autonomous systems therefore faces a fork: either declare such workloads out of scope forever, or design the representation so a compute-intensive decision *step* can be dispatched to whatever backend solves it best. Nano takes the second fork, for a structural reason: the IR already separates *what a step computes* from *how the runtime executes it*. A graph node is a description; the interpreter is one way to evaluate it. That separation — the same one that will let a native runtime replace the Python reference interpreter (paper [11](11-performance.md)) — is the same one that lets a future optimization node be evaluated by a classical solver, a simulator, or quantum hardware, without the graph, its manifest, or its provenance changing shape.

## The pipeline under research

The Aether research stack around Nano sketches the path:

```
Problem (in Nano++ representation) → compilation to a computational path (AQRC)
    → backend dispatch: classical solver | simulator | quantum hardware
    → result re-enters the graph as an ordinary, logged observation
```

The first slice of this pipeline is implemented in this repository: the Nano++ optimization loop ships a vendor-agnostic quantum interface ([`nano/loop/quantum.py`](../../nano/loop/quantum.py)). Nano deliberately does not know IBM, IonQ, Azure, Rigetti, or D-Wave — it knows *quantum jobs*: a backend implements the `QuantumRuntime` protocol, the loop submits jobs and reads back a fidelity metric and distribution, and swapping hardware is swapping the runtime, never the loop. The reference backend, `SimulatorRuntime`, is fully deterministic — seeded by each job's content hash, never by ambient randomness — so loop replays stay bit-identical. Real-hardware backends, and any results obtained on them, remain research findings, not product claims.

## Determinism meets a probabilistic machine

The apparent contradiction deserves a direct answer: quantum measurement is irreducibly random — how does it enter a bit-identically replayable language? Through the same door as every other nondeterminism: **injected effects** (paper [13](13-design-principles.md)). Time and market data already enter as recorded inputs; a quantum backend's measurement outcomes enter identically — sampled once at execution, recorded, and read back on replay. A replayed execution does not re-query the QPU any more than it re-queries Tuesday's market. The shipped `SimulatorRuntime` demonstrates the discipline concretely by deriving its distribution from the job's content hash — no ambient entropy anywhere in the loop. Determinism in Nano was never "no randomness"; it is "no *unrecorded* randomness."

Gating also carries over with unusual force, because quantum computation has a property most compute lacks: it is expensive per shot and dispatched in discrete, priceable units. A quantum dispatch is a proposal like any other — subject to a gate that sees the estimated cost and expected value before hardware is engaged. The propose/dispose split turns out to be exactly the right control surface for a resource where an ungated loop can burn a budget in minutes.

## What would count as success

The research track has falsifiable exits. Success: a problem class from a real workload where a quantum-routed step, dispatched through Nano++ representation, beats the best classical baseline on solution quality per dollar at production scale. Failure — reported just as plainly: classical solvers win everywhere that matters, and Nano++ ships with classical optimization backends only. The architecture is indifferent to the outcome; that indifference is the design achievement. Nano does not bet on quantum computing. It ensures that no rearchitecture is needed if the bet elsewhere pays off.

---

*Previous: [15 — Closed-Loop Engineering](15-closed-loop-engineering.md) · Next: [17 — Heterogeneous Compute](17-heterogeneous-compute.md)*
