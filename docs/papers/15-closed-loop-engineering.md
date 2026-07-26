# 15 — Closed-Loop Engineering

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Nano turns autonomous improvement into an engineering loop — execute, measure, propose, verify by replay, gate, recompile — where every iteration is evidence-backed and nothing changes without passing through a gate.

---

## From autonomous systems to autonomous engineering systems

The papers so far describe how a system *executes* compiled decisions. This one describes how a system *engineers* them — because the deeper ambition is not an agent that runs a fixed policy, but an engineering loop in which the system participates in improving its own behavior, under the same discipline a human engineering organization imposes on itself: measurement before change, review before deployment, rollback by construction.

The loop:

```
Execute (deterministic) → Measure (outcomes) → Propose (model reasons over evidence)
        ▲                                                  │
        │                                                  ▼
   Recompile ◄── Gate (admit / reject) ◄── Verify (replay old vs. new on identical history)
```

Every stage exists because a specific failure mode of naive "self-improving AI" has to be closed.

## Execute and measure: the evidence base

Improvement requires knowing what happened, exactly. Deterministic execution supplies it: every run produces intents plus a complete audit log, both replayable bit-for-bit ([`nano/runtime/interpreter.py`](../../nano/runtime/interpreter.py), paper [03](03-determinism.md)). In the trading proving ground, outcomes attach numerically — the memory layer's `Pattern` records `confidence` and `win_rate`, and carries `expires_at` so stale knowledge retires instead of silently rotting ([`nano/memory/patterns.py`](../../nano/memory/patterns.py)). The measurement stage is therefore not telemetry bolted on; it is the Record leg of the Observe → Decide → Act → Record loop, produced by the execution itself.

## Propose: the model as engineer, not operator

A reasoning model reviews the evidence and proposes a modification. The interaction pattern is the one sketched for ATS from the start:

```
Recommendation: false breakouts increased during low-liquidity periods.
Suggested modification:
+ condition volume_confirmation { volume > avgVolume * 1.5 }
Backtest: win rate 58% → 64%.
[Simulate] [Backtest] [Deploy] [Reject]
```

The critical framing: the model acts as an engineer submitting a change request, never as an operator with production access. Its proposal is an IR diff plus evidence — not an action. This is the intent/dispose split (paper [13](13-design-principles.md)) applied one level up: strategies propose intents; models propose strategies; gates dispose of both.

## Verify: replay is the code review

Here determinism pays its largest dividend. The proposed graph and the incumbent graph are replayed over identical historical frames — the same pure-function execution, so the comparison is exact: every decision where the two diverge is enumerable, with the observations that caused the divergence attached. "The new version is better" stops being a claim about aggregate statistics and becomes an inspectable decision-by-decision diff. No inference-loop architecture can offer this, because no two runs of an inference loop are exactly comparable even to themselves.

Content addressing (paper [13](13-design-principles.md)) makes the change itself precise: the proposal is a transition from hash A to hash B, and the provenance chain records who proposed it, what evidence supported it, and who approved it (paper [04](04-provenance.md)).

## Gate and recompile: nothing self-modifies silently

The gate — policy, risk engine, or human — admits or rejects. Admitted proposals recompile into the running system; rejected ones leave a recorded trace of what was considered and why it failed. Mandatory provenance keeps model-authored patterns distinguishable from hand-authored ones forever, so trust policies can differ by origin. The loop then closes: the new behavior executes, its outcomes are measured, and the next iteration begins.

Status, honestly: the loop's substrate is shipped — deterministic execution, the audit log, pattern memory with confidence, expiry, provenance, and explicit escalation — and so is the loop's skeleton: `nano/loop/` implements the observe → propose → compile → execute → measure → verify → admit lifecycle as deterministic, replayable IR, with gated mutation admission (`admit_mutation`). The surrounding automation — the model-driven proposal pipeline, the replay-diff harness, the gate UI — remains design (paper [14](14-future-work.md)).

## Why this loop generalizes

Nothing in the loop mentions markets. Any domain with the Observe → Decide → Act → Record cycle (paper [09](09-autonomous-systems.md)) supports the same engineering cycle above it: a robotics fleet proposing policy refinements from incident replays; a security platform tuning response playbooks against recorded campaigns; an enterprise agent tightening a workflow rule from a month of exception logs. Closed-loop engineering is what "autonomous system" should mean at maturity — not a system that acts without humans, but a system whose improvement process is so instrumented, so replayable, and so gated that delegating parts of it to machines is a controlled engineering decision rather than an act of faith.

---

*Previous: [14 — Future Work](14-future-work.md) · Next: [16 — Quantum Computing](16-quantum-computing.md)*
