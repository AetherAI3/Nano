# 12 — Security

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Nano's security model is capability restriction by construction — effect manifests, no actuation primitives in the language, load-time validation, and admission gates — so least privilege is a property of the artifact, not a policy around it.

---

## The threat model

An execution substrate for autonomous systems faces three distinct adversaries. First, the **malicious or compromised strategy**: code (increasingly, model-generated code) that tries to act beyond its mandate — exfiltrate data, bypass risk controls, act directly on the world. Second, the **buggy strategy**: no intent to harm, but behavior its author did not foresee. Third, the **untrusted compiler**: when an LLM writes the program, the program's author is itself part of the attack surface, and "review the code carefully" does not scale to machine-generated behavior arriving continuously.

Conventional stacks answer all three with perimeter controls: sandboxes, network policies, code review, runtime monitors. Nano's position is that the strongest control is the one the language makes unnecessary: **a capability that does not exist in the language cannot be abused, cannot be misused, and does not need to be monitored.** Restrictions are the feature.

## Layer 1: capabilities absent from the language

The base layer is what Nano removed. There is no socket, no file handle, no process spawn, no thread, no ambient clock or entropy, no mutable global — and, decisively for the first proving ground, **no exchange or actuator API**. The IR's complete vocabulary is Schedule, Condition, Intent, Agent ([`nano/ir/nodes.py`](../../nano/ir/nodes.py)); the runtime's complete output is intents plus a log ([`nano/runtime/effects.py`](../../nano/runtime/effects.py)).

The security consequence is categorical rather than probabilistic. A malicious Nano strategy cannot exfiltrate data because there is nothing to exfiltrate *with*. It cannot place an order because the language cannot express one — the intent/gate lock in [BUILD_ORDER.md](../../BUILD_ORDER.md) is enforced structurally, and the worst a hostile graph can do is *propose* aggressively to a gate that is free to say no. This collapses the code-review problem for AI-generated programs: a reviewer (human or automated) of a Nano graph never has to answer "what could this code secretly do?" The answer is fixed by the language: evaluate conditions, propose intents.

## Layer 2: effect manifests — declared capability, enforced at load

Within the language's small capability space, modules must still declare what they use. Every strategy carries an `effects` manifest, and the loader enforces it before anything runs: a graph is rejected if its manifest is missing or empty, if it declares effects outside the known set, or if it contains `Intent` nodes without declaring `intent.emit` — a `ManifestViolation` at load time ([`nano/ir/graph.py`](../../nano/ir/graph.py)).

This gives operators least privilege in its most useful form: *legible* privilege. What a module can do is a short list in its artifact — readable by a human, checkable by a machine, diffable across versions, and eventually signable (paper [04](04-provenance.md)). As the effect vocabulary grows with the language, the manifest becomes the single choke point where new capability is granted deliberately rather than acquired ambiently.

Load-time enforcement is itself a security decision worth naming: validation is the boundary, so violations are rejected before execution, never discovered mid-run. There is no window in which a noncompliant graph was partially executed.

## Layer 3: admission gates — nothing self-deploys

The outer layer governs the lifecycle. Two gate families stand between a proposed behavior and the world:

- **Admission gates** decide whether a compiled artifact may be loaded at all. This is where AI self-improvement is made safe: a model-proposed strategy modification is replayed against history (exactly comparable, thanks to determinism — paper [03](03-determinism.md)), reviewed with its provenance attached, and only then admitted. Nothing self-modifies silently. The full gate pipeline is design; the substrate it needs — validated loading, mandatory provenance on patterns ([`nano/memory/patterns.py`](../../nano/memory/patterns.py)), canonical serialization — is shipped.
- **Disposition gates** decide whether an emitted intent becomes an action. The risk engine, policy layer, or human that receives intents applies its own limits, budgets, and vetoes, outside the strategy author's reach.

Components propose; gates decide — at both the artifact level and the action level.

## What Nano does not secure

Scope honesty: Nano secures the decision core, not the system around it. The frame provider can feed poisoned observations; the gate can be misconfigured; the host Python process is not sandboxed against its own operator; and the reasoning model can be prompt-injected into proposing bad strategies — which the gates must catch, since load-time validation checks form, not wisdom. These belong to the surrounding system's threat model. What Nano guarantees is narrower and checkable: whatever enters through the frame and whatever intelligence authored the graph, the artifact can only evaluate, propose, and log — and everything it does can be replayed, byte for byte, in front of an auditor.

---

*Previous: [11 — Performance](11-performance.md) · Next: [13 — Design Principles](13-design-principles.md)*
