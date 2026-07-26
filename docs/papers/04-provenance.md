# 04 — Provenance: Protocol C

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Protocol C is Nano's cryptographic execution protocol — a chain of verifiable answers to who proposed a behavior, what compiled, what executed, who approved it, what changed, and when.

---

## Not a logging feature

It is tempting to describe provenance as "signed logs," and wrong. Logs describe what a system says it did. Protocol C is designed to make the execution lifecycle itself carry proof, so that at every stage of an autonomous system's operation there is a cryptographically checkable answer to six questions:

| Question | Attested fact |
|---|---|
| **Who proposed?** | The origin of a behavior — a named human, or a named model with its identity and the context of the reasoning session |
| **What compiled?** | The exact IR produced, bound to the proposal it came from |
| **What executed?** | The exact IR the runtime loaded, plus the injected inputs and the resulting intents and log |
| **Who approved?** | The gate decisions — which risk engine, policy, or human admitted the artifact or disposed of the intent |
| **What changed?** | The delta between one version of a behavior and its predecessor |
| **When?** | Ordering across all of the above |

The unit of trust is the execution chain: proposal → compilation → admission → execution → disposition, each link referencing the previous one. An intent that reaches an actuator can be traced back — through the gate that approved it, the graph that emitted it, the compilation that produced the graph — to the human or model that proposed the behavior in the first place.

## Why Nano can support this and most systems cannot

Two shipped properties make execution-level provenance meaningful rather than decorative.

**Content-addressability.** Nano IR is a small, canonical, serializable structure. `StrategyGraph.to_dict()` round-trips a validated graph to plain JSON ([`nano/ir/graph.py`](../../nano/ir/graph.py)), which means a graph has a stable content hash: "what compiled" and "what executed" can be identified by digest, not by filename or version string. Two parties holding the same hash hold the same behavior.

**Determinism.** Because execution is a pure function of graph and injected inputs (paper [03](03-determinism.md)), an attestation over `(graph hash, input frame, result)` is *verifiable*, not merely asserted: any verifier can re-execute and check that the claimed result is the only possible one. In a nondeterministic runtime, a signature over an execution attests only that someone observed it once. In Nano, it attests to a reproducible fact.

This is the deep reason provenance and determinism ship as one design. Cryptography binds statements to keys; determinism is what makes the statements themselves checkable.

## What is implemented, what is design

Honestly separated:

**Implemented today.** The provenance *field* is mandatory in the memory layer: every `Pattern` carries a `provenance` value (defaulting to `"manual"`), kept precisely so that machine-compiled patterns remain distinguishable from hand-authored ones and can be gated differently ([`nano/memory/patterns.py`](../../nano/memory/patterns.py)). The append-only execution log records every condition evaluation and intent emission with injected timestamps ([`nano/runtime/effects.py`](../../nano/runtime/effects.py)). Canonical serialization of graphs, the precondition for content addressing, works end to end. And the first cryptographic piece is shipped as a host-integration adapter: `ProvenanceRiskEngine` ([`nano/bridge/provenance.py`](../../nano/bridge/provenance.py)) wraps any risk gate so every disposition also produces a signed, independently re-verifiable Protocol-C receipt — deliberately outside the language, unreachable and unbypassable from `.nano` source, and refusing to degrade silently to unsigned logging if the signing package is absent.

**Design and roadmap.** The full chained-attestation format — signatures over proposals, compilations, and admissions, linked into one verifiable lifecycle chain — is specified but not built. It layers cleanly on the shipped substrate because the substrate was shaped for it: everything that needs signing is already an immutable, canonically serializable value.

## Who needs this

Protocol C is aimed at every domain where "the AI decided" is not an acceptable audit trail:

- **Finance.** Regulators and risk committees can verify which strategy version emitted an order intent, who approved its deployment, and replay the exact decision.
- **Healthcare.** A clinical workflow's automated steps carry proof of which protocol version ran, on what inputs, under whose sign-off.
- **Robotics and industrial automation.** Post-incident analysis traces an actuation back through the gate that approved it to the compiled policy and its author.
- **Enterprise automation.** Change management for agent behavior becomes reviewable: what changed between Tuesday's behavior and Wednesday's is a diff of content-addressed artifacts, not a diff of prompts.
- **AI governance.** Claims like "no unapproved model-generated behavior executed in production" become checkable properties of the chain rather than policy statements.

## The composition with gates

Protocol C completes Nano's propose/dispose architecture (paper [01](01-why-nano.md)). Gates decide whether a proposal runs; Protocol C proves that the gates were consulted and what they decided. Without provenance, a gated system asks for trust in its operators. With it, the discipline — components propose, gates decide, everything is recorded — becomes a property a third party can verify.

---

*Previous: [03 — Determinism](03-determinism.md) · Next: [05 — Why Not Go](05-why-not-go.md)*
