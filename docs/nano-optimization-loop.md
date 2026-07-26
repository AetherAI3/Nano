# Nano++ experimental loop primitives

**Status:** experimental data-model and admission primitives in `nano/loop/`; not an executable autonomous optimization system.

Nano++ is a research direction adjacent to the core strategy DSL. The code currently provides three narrowly scoped building blocks:

1. `LoopGraph` validates and serializes a separate loop document with ordered node references and effect declarations.
2. `admit_mutation()` makes a deterministic admission decision from caller-supplied candidate facts such as replay match, regression count, benchmark delta, and allowed capabilities.
3. `QuantumRuntime` defines a protocol and `SimulatorRuntime` returns deterministic, hash-derived reference results for a `QuantumJob`.

None of these modules executes a loop, invokes a model, performs benchmarking, mutates a running system, deploys changes, or dispatches work to a real quantum processor.

## What LoopGraph does

A `LoopGraph` is a validated, serializable document whose nodes have IDs, stage names, ordered input references, and free-form attributes. It rejects unknown stages, forward references, duplicate IDs, unknown effects, and stages that lack their required declared capability.

```text
Loop document -> LoopGraph.from_dict() -> validation / content_hash()
```

`LoopGraph.content_hash()` is deterministic for the loop document. This property belongs to the separate experimental loop representation; it does not make `StrategyGraph` content-addressed and does not provide loop execution or replay.

## Mutation admission

`Candidate` is input to `admit_mutation()`, not an artifact that Nano creates by itself. The helper checks, in order:

```text
replay match -> no regressions -> static flag -> benchmark gain -> capability ceiling
```

On success it returns an `admitted_pending_deploy` record. A host or operator must decide whether and how to deploy anything. This is useful as a small policy primitive, not evidence of autonomous self-modification.

## Simulator protocol

`SimulatorRuntime` implements `QuantumRuntime.submit(job)` as a pure, hash-derived reference calculation. It exists to make the interface testable without a vendor dependency. There is no real hardware connector, performance claim, or quantum advantage claim in this repository.

## Relationship to core Nano

The core `.nano` strategy pipeline is documented in [architecture.md](architecture.md). It compiles source into `StrategyGraph` and runs it over a `MarketFrame`. Nano++ does not share a runtime or executor with that path today.

For current maturity boundaries, see [status.md](status.md) and the [design-notes index](papers/README.md).
