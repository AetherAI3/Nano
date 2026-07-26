# The Nano Paper Series

Seventeen short papers, each answering one question about Nano — the compiled execution architecture for autonomous engineering systems. Read [01](01-why-nano.md) first; the rest stand alone.

## The core thesis

| # | Paper | One line |
|---|---|---|
| 01 | [Why Nano](01-why-nano.md) | Autonomous systems stop re-reasoning by compiling known reasoning into deterministic execution. |
| 02 | [The Agentic Compiler](02-the-agentic-compiler.md) | Nano compiles decisions, not algorithms — and moves the LLM from inside the hot loop to above it. |
| 03 | [Determinism](03-determinism.md) | "Why did the agent do that?" is answered by bit-identical replay, not reconstruction (implemented). |
| 04 | [Provenance: Protocol C](04-provenance.md) | A cryptographic execution protocol: who proposed, what compiled, what executed, who approved, what changed, when. |

## Positioning

| # | Paper | One line |
|---|---|---|
| 05 | [Why Not Go?](05-why-not-go.md) | Go tells a computer how to compute; Nano tells an autonomous system when to act — complementary layers. |
| 06 | [Why Not TypeScript?](06-why-not-typescript.md) | TS models applications and Python models reasoning; Nano models deterministic decision graphs — removing uncertainty, not functionality. |
| 07 | [Nano vs. Pine Script](07-nano-vs-pine.md) | Pine draws indicators; Nano compiles gated, replayable decisions — no order primitive, intents plus effect manifests. |

## Scope

| # | Paper | One line |
|---|---|---|
| 08 | [LLM Integration](08-llm-integration.md) | Model-agnostic substrate: any reasoning model compiles to the same IR; the economics of compiled memory decide the architecture. |
| 09 | [Autonomous Systems](09-autonomous-systems.md) | The Observe → Decide → Act → Record loop generalizes everywhere; ATS trading is the first workload, not the product. |
| 10 | [The Nano Family](10-nano-family.md) | Nano / Nano+ / Nano++ — deterministic, adaptive, and computational execution over one compiler and one IR. |

## Engineering

| # | Paper | One line |
|---|---|---|
| 11 | [Performance](11-performance.md) | The win is architectural — execution replaces inference; the runtime's own constants are honestly unmeasured. |
| 12 | [Security](12-security.md) | Capability restriction by construction: absent primitives, effect manifests, load-time validation, admission gates. |
| 13 | [Design Principles](13-design-principles.md) | Six non-negotiable rules — determinism, intent/dispose, IR-first, injected effects, content addressing, gated change. |
| 14 | [Future Work](14-future-work.md) | With IR, compiler, bridge, and editor services shipped: the CLI, `Series<T>` typing, cognitive execution layer, quantum hardware dispatch — in that order. |

## The horizon

| # | Paper | One line |
|---|---|---|
| 15 | [Closed-Loop Engineering](15-closed-loop-engineering.md) | Execute → measure → propose → verify by replay → gate → recompile: self-improvement as a governed engineering loop. |
| 16 | [Quantum Computing](16-quantum-computing.md) | A research track, sized honestly: if quantum backends ever win on real optimization workloads, the IR is ready to dispatch them. |
| 17 | [Heterogeneous Compute](17-heterogeneous-compute.md) | One IR, many backends — interpreter to cluster to accelerator — with bit-identical replay as the conformance contract. |

---

Grounding: papers cite the shipped implementation (`nano/ir/`, `nano/runtime/`, `nano/memory/`, `nano/compiler/`, `nano/bridge/`, `nano/library/`, `nano/aethercode/`, `nano/loop/` — 338 tests) and mark everything else as design, roadmap, or research. Build sequence: [BUILD_ORDER.md](../../BUILD_ORDER.md).
