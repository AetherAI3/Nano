# 09 — Autonomous Systems

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Every autonomous system runs the same loop — Observe, Decide, Act, Record — and Nano compiles the Decide stage wherever that loop exists, with autonomous trading (ATS) as the first workload rather than the product.

---

## The universal loop

Strip any autonomous system to its skeleton and the same cycle appears:

```
Observe → Decide → Act → Record
   ▲                        │
   └────────────────────────┘
```

A trading system observes market frames, decides whether conditions warrant action, proposes orders, and records fills. A warehouse robot observes sensor state, decides on a motion, actuates, and logs telemetry. A security platform observes events, decides whether they constitute an incident, responds, and writes the case record. The domains share no APIs, no data formats, no vendors — but they share the loop, and the loop is where the engineering pain concentrates: the Decide stage is the part that is expensive to run, hard to audit, and dangerous to get wrong.

Nano is scoped to exactly that stage. It does not observe (inputs are injected as recorded frames — [`nano/runtime/interpreter.py`](../../nano/runtime/interpreter.py)), it does not act (outputs are intents a downstream gate disposes of — [`nano/runtime/effects.py`](../../nano/runtime/effects.py), [`nano/bridge/`](../../nano/bridge/__init__.py)), and the Record stage falls out for free (the append-only log is part of the execution result, and replays bit-identically). By refusing to own three of the four stages, Nano can make hard guarantees about the one it owns.

This scoping is what makes the architecture portable. Nothing in the IR knows what a market is. A `Condition` compares a named signal to a threshold; whether the signal is `RSI`, `joint_torque_3`, `failed_logins_per_min`, or `reactor_temp` is the frame provider's business. The domain list below is not a roadmap of ports to build — it is the same node vocabulary fed different observations.

## The domains

- **Robotics.** Supervisory policies — when to slow, stop, re-plan, request help — compiled and replayable, with actuation gated by the safety controller. Post-incident analysis becomes replay, not forensics.
- **Autonomous vehicles.** The behavioral layer's repeatable decisions run compiled at deadline-safe latency; genuinely novel scenes escalate to heavier reasoning off the critical path.
- **Cybersecurity.** Detection-and-response playbooks as gated decision graphs: the system proposes containment, the gate (policy or analyst) disposes, and every response is provable after the fact.
- **Workflow automation and enterprise agents.** The 95% of an agent's day that is routine executes deterministically at near-zero cost; the model handles exceptions. Behavior change becomes a reviewable diff of content-addressed artifacts (paper [04](04-provenance.md)).
- **Manufacturing.** Quality and scheduling decisions with full audit lineage; a nonconformance traces to the exact compiled rule and inputs that admitted the part.
- **Cloud orchestration.** Scaling, failover, and remediation policies that can be replayed against the incident that triggered them — ending "why did the autoscaler do that?" archaeology.
- **Scientific pipelines.** Analysis decision points compiled and content-addressed, making the pipeline itself reproducible, not just its code.

Different stakes, same requirements: repeated decisions, bounded latency, mandatory auditability, and a hard line between proposing an action and taking it.

## ATS: the first workload

Nano's first workload is autonomous trading — and the framing matters: **first workload, not product**. Trading was chosen because it is the most demanding honest test environment available for a compiled execution architecture:

1. **Deterministic inputs.** Market history is a fixed time series. Frames are perfectly recordable, so the injected-input model is exercised without simulation infrastructure.
2. **Replayable history.** Years of data exist to replay compiled decisions against. The claim "backtest and live are the same computation" is checkable to the byte (paper [03](03-determinism.md)) — and the shipped bridge and backtester ([`nano/bridge/`](../../nano/bridge/__init__.py)) run exactly that acceptance test.
3. **Measurable outcomes.** P&L, win rate, drawdown — numbers, not vibes. Whether a compiled pattern is better than per-decision inference is a measurement, not an opinion. The memory layer's `win_rate` field exists for exactly this ([`nano/memory/patterns.py`](../../nano/memory/patterns.py)).
4. **Real adversarial stakes.** Markets punish look-ahead, overfitting, and silent nondeterminism with money. A framework whose guarantees survive trading has had its guarantees actually tested.
5. **The gate discipline is non-negotiable.** No serious trading operation lets strategy code place orders directly, so the intent/gate lock ([BUILD_ORDER.md](../../BUILD_ORDER.md)) is validated by an environment that already demands it.

A framework proven in a forgiving domain proves little. One whose determinism, gating, and economics survive live markets carries that proof into every other loop on the list — which is why the integration path runs Nano IR → risk-gate bridge → execution decision first, and the generalization second.

---

*Previous: [08 — LLM Integration](08-llm-integration.md) · Next: [10 — The Nano Family](10-nano-family.md)*
