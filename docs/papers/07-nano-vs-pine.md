# 07 — Nano vs. Pine Script and Trading DSLs

> **Design-note scope:** This essay may discuss proposals or research hypotheses beyond Nano v0.1.0. For the implemented contract, see [Architecture](../architecture.md), [Language](../language.md), and [Status](../status.md).


**Answer in one sentence:** Pine Script draws indicators and fires alerts inside a charting product; Nano compiles decisions into a gated, replayable execution substrate — they share a surface vocabulary and almost nothing else.

---

## The honest overlap

Because trading is Nano's first workload, the fastest wrong conclusion about Nano is "another Pine Script." The surface similarity is real: both let you express `if RSI < 30 then act`, both run over bar-by-bar market history, both target strategy authors rather than systems programmers — and Nano's strategy library ([`nano/library/`](../../nano/library/README.md)) is openly modeled on the ambition of Pine's public library. It is worth stating the overlap plainly so the differences land as architecture, not marketing.

Pine Script — and the family it represents: EasyLanguage, thinkScript, NinjaScript, MQL — is very good at its job. That job is *visualization and signaling inside a charting product*: compute a series, paint it, alert when it crosses a threshold, and, in strategy mode, simulate fills against the chart's history. Millions of traders get real value from exactly this.

## Different object, different contract

The difference is what a program *is* in each system.

**A Pine program is an indicator.** Its output is a drawn series or an alert or a broker-connected order. It lives inside one vendor's platform, computes what the platform feeds it, and its "backtest" is the platform's fill simulator — a distinct engine from live alert execution, with its own well-documented divergences (intrabar assumptions, repainting on `security()` calls, alert timing).

**A Nano program is a compiled decision.** Its output is not a drawing and not an order — it is a tuple of `Intent` values plus a complete audit log ([`nano/runtime/effects.py`](../../nano/runtime/effects.py)). The program is a serializable execution graph ([`nano/ir/graph.py`](../../nano/ir/graph.py)) that runs identically in backtest and live because there is only one execution engine: the same interpreter over a historical frame or a live frame (paper [03](03-determinism.md)).

Three structural commitments follow, and none of them exist in the Pine family:

**1. No order primitive.** Pine's strategy mode has `strategy.entry` and `strategy.exit`; MQL has `OrderSend`. The language itself can trade. Nano's IR contains no such node — not restricted, not permissioned: *absent*. The strategy-tier vocabulary is `Schedule`, `Condition`, `Intent`, `Agent` ([`nano/ir/nodes.py`](../../nano/ir/nodes.py)). A strategy that wants to act can only propose:

```json
{ "intent": "BUY", "asset": "BTC", "confidence": 0.91 }
```

**2. The intent/gate model.** Downstream of every intent stands a gate — a risk engine, a policy, a human — that disposes. Components propose; gates decide. This is the architectural lock recorded in [BUILD_ORDER.md](../../BUILD_ORDER.md): "Nano never places trades." The shipped bridge makes it concrete: `NanoBridge` forwards intents to any host object implementing the `RiskEngine` protocol, which approves or rejects each one with a recorded reason ([`nano/bridge/`](../../nano/bridge/__init__.py)). In Pine, risk management is more script inside the same script, written by the same author, bypassable by the same author. In Nano, the strategy author *cannot reach* execution, because the language has nothing to reach with.

**3. Effect manifests.** Every Nano module declares its capabilities up front, and the loader enforces the declaration: a graph containing `Intent` nodes without `intent.emit` in its manifest is rejected at load time as a `ManifestViolation` ([`nano/ir/graph.py`](../../nano/ir/graph.py)). An operator can know what a strategy is able to do by reading one list — before running a single tick. Pine has no equivalent concept; a script's capabilities are whatever the platform grants all scripts.

## Replay versus simulation

The deepest divide is epistemic. When a Pine backtest disagrees with live behavior, the author debugs the *difference between two engines*. When a Nano replay is run, there is nothing to disagree: execution is a pure function of graph plus injected frame, so historical and live execution are the same computation over different recorded inputs, and any run can be reproduced bit-for-bit, audit log included ([`nano/runtime/interpreter.py`](../../nano/runtime/interpreter.py)). "Why did the strategy fire at 14:35?" is answered by replaying 14:35, not by reconstructing what the simulator probably assumed.

This is also why Nano can support serious provenance (paper [04](04-provenance.md)) and gated AI self-improvement (paper [02](02-the-agentic-compiler.md)): a model-proposed strategy modification can be replayed against identical history and diffed decision-by-decision before any gate admits it. A charting DSL has no substrate on which that comparison could be exact.

## Different ceiling

Finally, scope. Pine is bounded by its product: it will always be a language for charts. Nano's trading vocabulary is the *first* vocabulary of a general execution architecture — the same IR, runtime discipline, and gate model apply to any Observe → Decide → Act → Record loop (paper [09](09-autonomous-systems.md)). A Pine script grows into a bigger Pine script. A Nano strategy is already the same kind of object as a robotics policy or an incident-response playbook: a compiled, gated, replayable decision.

Use Pine to see the market. Use Nano when the decision has to be executable, provable, and governed.

---

*Previous: [06 — Why Not TypeScript?](06-why-not-typescript.md) · Next: [08 — LLM Integration](08-llm-integration.md)*
