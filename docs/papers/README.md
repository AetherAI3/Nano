# Nano design notes and research directions

> **Important:** this series explores the broader Nano thesis. It is not the language specification, an API reference, or evidence that a capability is implemented. For current behavior, start with the [architecture](../architecture.md), [language reference](../language.md), and [status](../status.md).

These short essays record the rationale behind the project, possible future directions, and trade-offs worth testing. Some arguments intentionally extend beyond the alpha Python reference runtime. When a paper differs from the code or reference documentation, the code and tests define the current contract.

## Core ideas

| # | Paper | Question it explores |
| --- | --- | --- |
| 01 | [Why Nano](01-why-nano.md) | Why might deterministic compiled rules complement model-driven systems? |
| 02 | [The Agentic Compiler](02-the-agentic-compiler.md) | What would it mean to compile decisions rather than invoke a model every time? |
| 03 | [Determinism](03-determinism.md) | Why does deterministic replay matter for a reference runtime? |
| 04 | [Provenance: Protocol C](04-provenance.md) | How could an optional host integration add signed decision receipts? |

## Positioning and scope

| # | Paper | Question it explores |
| --- | --- | --- |
| 05 | [Why Not Go?](05-why-not-go.md) | Where might a narrow decision DSL fit beside general-purpose languages? |
| 06 | [Why Not TypeScript?](06-why-not-typescript.md) | What is different about modeling constrained decisions? |
| 07 | [Nano vs. Pine Script](07-nano-vs-pine.md) | How do the strategy examples compare with indicator-oriented scripting? |
| 08 | [LLM Integration](08-llm-integration.md) | What could model integration look like? *(Not implemented in Nano v0.1.0.)* |
| 09 | [Autonomous Systems](09-autonomous-systems.md) | Which broader system shapes motivate the project? |
| 10 | [The Nano Family](10-nano-family.md) | What is the proposed Nano / Nano+ / Nano++ framing? |

## Engineering and research tracks

| # | Paper | Question it explores |
| --- | --- | --- |
| 11 | [Performance](11-performance.md) | What should future benchmarks measure? |
| 12 | [Security](12-security.md) | Which constraints can the strategy IR enforce, and where are host boundaries? |
| 13 | [Design Principles](13-design-principles.md) | Which architectural constraints should guide future work? |
| 14 | [Future Work](14-future-work.md) | Which capabilities are still unimplemented or experimental? |
| 15 | [Closed-Loop Engineering](15-closed-loop-engineering.md) | What might governed self-improvement require? |
| 16 | [Quantum Computing](16-quantum-computing.md) | When might specialized optimization backends be worth researching? |
| 17 | [Heterogeneous Compute](17-heterogeneous-compute.md) | What could portable execution across backends mean? |

## Reading responsibly

The shipped repository includes a small strategy DSL, serializable strategy IR, deterministic reference interpreter, host decision-gate bridge, test corpus, and several experimental primitives. It does **not** include an LLM runtime, automatic escalation, general loop executor, live exchange integration, or real quantum dispatch. Use [status.md](../status.md) as the concise capability map.
