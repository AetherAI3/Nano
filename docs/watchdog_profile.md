# The Watchdog profile

> This page describes `nano/watchdog/`, which is in the repository today. It does not describe a scheduler, a host integration, or a decision gate — none of those are here, and [what this is not](#what-this-is-not) says so explicitly.

A **Watchdog** is an ordinary Nano rule admitted under a narrower contract. It reads numbers a host publishes, recognises a policy condition, and proposes a bounded response that somebody else decides about:

```text
host observations
    -> validated Nano rule
    -> deterministic evaluation
    -> proposed Intent(s)
    -> host DecisionGate
    -> recorded Decision
```

Nano owns the first four steps. The last two are the host's, and nothing in `nano/watchdog/` can reach them.

There is no second language here and no second runtime. `compile_watchdog` is `nano.compiler.compile_module` plus an admission check; `evaluate_watchdog` is one call to `nano.runtime.run_module` with a gate in front and a record behind. A parallel evaluator would eventually disagree with the first one, and the disagreement would surface as a control that behaved one way in review and another way in production.

## The profile

| | |
| --- | --- |
| **Permits** | host-published numeric signals, `param`/`input`/`let` declarations, series offsets, the deterministic indicator kernels, arithmetic, comparison, `and`/`or`/`not`, one `every` block, `if`/`else`, `risk { … }`, `agent`, and the log |
| **Proposes** | `PAUSE` and `OBSERVE` — nothing else |
| **Forbids** | `BUY`, `SELL`, `EXECUTE`, `llm.call`, `llmre.escalate`, `infer`, `signature`, `route`, `sign.emit`, and any tier above `nano` |
| **Cannot reach** | a network, a filesystem, an external tool, a clock, a random source, or an action with consequences |

The last row is not a restriction Watchdogs add. It is a property of the language: Nano has no I/O primitive to restrict.

### The opcode check is an allow-list

`WATCHDOG_OPS` names every opcode a Watchdog may contain. An opcode added to `nano/ir/module.py` tomorrow is denied here until somebody admits it deliberately — the posture the effect manifest already takes, and it fails closed as the language grows rather than the day after.

### Models may author a Watchdog; they may never sit inside one

Nothing stops a model writing the source, reviewing it, or explaining a run log afterwards. What the profile denies is a stochastic stage *inside* an evaluation. A rule containing one cannot be replayed, and a control that cannot be replayed cannot be audited.

The language already provides the marker: a `tier nano` module cannot contain a reasoning construct, so an auditor reading the tier off the artifact does not have to read the graph to know there is no model in the loop.

## The signal contract, and the reason it exists

With no `input` declarations, an unknown name is a host feed signal. That is what makes `if RSI(14) < 30` work with no preamble, and it must keep working — see [`nano/types/env.py`](../nano/types/env.py). The cost is stated there too: *a typo becomes a signal that never arrives.*

For a directional strategy that is a missed trade somebody eventually notices. For a control it is worse, because a rule that never fires and a rule with nothing to report look identical from outside. `QUEUE_SATURATION_PTC` compiles perfectly well, passes review, deploys, and reports nothing for a year.

So a Watchdog declares what it reads, and admission fails if the rule reaches for anything else:

```python
SATURATION = WatchdogSignalSpecV1(
    name="QUEUE_SATURATION_PCT",
    unit="percent",
    source="host queue telemetry",
    required=True,
    freshness_limit_ms=60_000,
    description="Host work-queue depth as a percentage of its configured ceiling.",
    value_domain="0..100",
    normalization="depth / ceiling * 100, clamped to 0..100",
)
```

Two conventions are worth stating because getting them wrong is quiet:

- **A boolean is exactly `0` or `1`.** Not `0/1/2`, not `-1` for unknown. An unknown value is an *absent* value, and absence has its own channel.
- **Do not flatten an unordered vocabulary onto one number.** Encoding `unknown=0, degraded=1, healthy=2` invents the claim that degraded sits between the other two, and every `>=` written against it inherits the invention. Publish independent booleans instead. Nano cannot detect this — the numbers look identical to it — so it belongs in review.

`freshness_limit_ms` is a bound on the **host**. Nano reads no clock and cannot tell a fresh value from a stale one. Requiring the field means the obligation is written down and delegated rather than assumed.

## Missing input is not zero

This is the rule the rest of the module is built around.

`run_module` treats an absent cell as "not warmed up yet" and skips the bar — correct for a strategy, where a bar with no data is a bar you skip. A control cannot skip. If the number that would have decided whether to hold is not there, the honest answer is `INPUT_UNAVAILABLE` and a list of what was missing, not a silence that reads like calm.

A signal is unavailable when the frame does not carry it, or when its value **at the last bar** is absent. `NaN` and positive or negative infinity are not JSON numbers and are normalized once to that same absence spelling before hashing or evaluation. The last bar is the observation the proposal is about. A gap earlier in the frame is history the VM already refuses to fire on and records as `condition.unwarmed` — absence *now* means the rule cannot judge now; absence *then* does not. A non-finite value therefore cannot turn a failed comparison into a quiet `OK` receipt or leak a bare non-JSON token into an artifact.

The gate covers every signal the contract marks `required`, **and** every signal the rule actually reads. `required=False` describes the host contract, not the rule's dependency — otherwise it would be a way to talk the evaluator into comparing against a value it does not have.

`unavailable_policy` is declared on the artifact and stamped on every receipt:

| Policy | Meaning |
| --- | --- |
| `HOLD` | The host should treat the watched operation as not cleared to proceed. |
| `OBSERVE_ONLY` | The host should record and surface the gap without holding. |

**Nano does not act on it.** On an unavailable frame it proposes nothing and says why; the host reads the state and the policy and decides. Synthesising a `PAUSE` that no rule matched would mean Nano manufacturing policy, which is the boundary this design exists to hold. There is deliberately no third option meaning "carry on as though the value were zero".

## Evaluation states

Exactly one lands on every receipt.

| State | Meaning |
| --- | --- |
| `OK` | The rule was evaluated against a readable frame. |
| `INPUT_UNAVAILABLE` | A required or referenced signal was absent. The rule did not run. |
| `WATCHDOG_INVALID` | The artifact failed its integrity check or the profile. The rule did not run. |
| `WATCHDOG_EVALUATION_FAILED` | The runtime raised. Defence in depth behind the availability gate. |

### "No intent" is never a health claim

An `OK` receipt proposing nothing means **only** that no configured condition matched on a valid frame. It is not evidence that the thing being watched is fine.

Three receipts propose nothing, and they are three different answers:

| Fixture | State | Proposals | What it actually says |
| --- | --- | --- | --- |
| `normal` | `OK` | none | The rule ran and nothing matched. |
| `recovered` | `OK` | none | The rule ran, an earlier bar had a gap, nothing matched now. |
| `missing-input` | `INPUT_UNAVAILABLE` | none | The rule could not run. Nothing is known. |

A host reading only `proposed_intents` cannot tell them apart. `input_state` is the field that carries the difference, which is the reason it exists. `WatchdogReceiptV1.is_evaluated` is deliberately not named `is_healthy`: it says the evaluation happened, not that what was evaluated is well.

## The artifact

`compile_watchdog` returns a `WatchdogArtifactV1` — the record of one admitted rule.

| Field | Source |
| --- | --- |
| `watchdog_id`, `revision`, `risk_class`, `unavailable_policy` | supplied by the caller |
| `name`, `cadence`, `allowed_intents` | **derived from the compiled module** |
| `source_hash` | `sha256` over the `.nano` text |
| `canonical_ir_hash` | `NanoModule.content_hash()` |
| `signals`, `required_signals` | the declared contract |

The derived fields are derived on purpose. A field a caller can set is a claim; a field read out of the artifact it describes is a fact.

`canonical_ir_hash` is a real content address over the canonical graph. `NanoModule` provides one; **`StrategyGraph` does not** — [`docs/status.md`](status.md) records that it "is serializable but not content-addressed" — which is why a Watchdog is always a module, and why the binding check below is worth anything.

`artifact_hash()` covers the exact canonical serialized bytes of the document, with the hash field itself excluded: a document cannot commit to its own digest.

The module is carried on the artifact but is not part of the serialized document. `evaluate_watchdog` re-derives its content hash before running it, so an artifact whose IR was swapped underneath it becomes `WATCHDOG_INVALID` rather than executing.

A Watchdog declares **exactly one cadence**. A record that averaged two schedules into a single field would be a record that is wrong; a rule needing two rhythms is two Watchdogs, which is also how it should be paused, revised, and audited.

## The receipt

`WatchdogReceiptV1` answers four questions: *what was observed, which exact rule evaluated it, what matched, and what was proposed.*

- **What was observed** — `input_frame`, `input_frame_hash`, `frame_timestamp`, `missing_inputs`
- **Which rule** — `watchdog_id`, `watchdog_revision`, `source_hash`, `ir_hash`, `nano_version`
- **What matched** — `execution_log`, the ordered `LogEntry` sequence the VM produced
- **What was proposed** — `proposed_intents`, `input_state`, `unavailable_policy`

Every key is present on every receipt, including the null ones. An audit record with a stable key set diffs cleanly; one that drops fields when they are empty makes "this run had no missing inputs" and "this run predates the field" look the same.

`evaluation_id` is a **content address** over the artifact hash and the frame hash, not a nonce. The same rule over the same frame is the same evaluation. A sampled id would make byte-identical replay impossible; a store that needs uniqueness adds its own key beside it.

`created_at` is **injected** by the caller, like every other time value in Nano. A receipt that stamped `time.time()` on itself could never be replayed.

There is no gate decision here and no record of an effect. Those are the host's half of the boundary; a host attaches its decision beside the receipt, keyed by `evaluation_id`.

## Replay

```python
replayed = replay_watchdog(artifact, receipt)   # raises WatchdogReplayMismatch
```

The claim is narrow and total: the same artifact over the canonical input-frame snapshot produces a byte-identical receipt — the same proposals, in the same order, with the same ordered log. Replay compares canonical bytes, not Python dictionary equality (`True == 1` and `0.0 == -0.0` make that weaker than the serialized claim).

Replay **rebuilds the frame from the receipt** rather than reusing the caller's object. Handing the original frame back would prove only that the evaluator is a function, which is already true and already tested. Reading it back out of `input_frame` proves what an audit needs: the receipt on its own reproduces the decision, with no live system standing by to re-answer questions about the inputs.

A mismatch is an integrity failure, not a warning. It means an input is not what the record says it was, or something in the evaluation path is reading a value nobody injected — and either way the receipts already archived cannot be trusted.

Replay ends where Nano ends, at the proposal. A host verifying its own gate replays that half with [`Backtester.verify_replay`](../nano/bridge/backtester.py), a different check with a different failure mode. Neither substitutes for the other.

## Isolation

A malformed Watchdog must not take down the ones beside it. `evaluate_watchdog` never raises for a fault in the rule: an integrity or profile failure becomes `WATCHDOG_INVALID`, a runtime fault becomes `WATCHDOG_EVALUATION_FAILED`, and both arrive as receipts a host can file. The reason is in the `execution_log`.

## Worked example

The sample rule is deliberately not a market rule — a Watchdog reads whatever numbers a host can publish honestly:

```nano
strategy QueueSaturationWatchdog {

    agent OpsDesk

    every 1m {

        if QUEUE_SATURATION_PCT >= 90 {

            observe()

        }

    }

}
```

```python
from nano.runtime import SignalFrame
from nano.watchdog import compile_watchdog, evaluate_watchdog, replay_watchdog

artifact = compile_watchdog(
    source,
    watchdog_id="queue-saturation",
    revision=3,
    signals=(SATURATION,),
    risk_class="operational-advisory",
    unavailable_policy="HOLD",
)

frame = SignalFrame(
    timestamps=(0, 60, 120),
    signals={"QUEUE_SATURATION_PCT": (41.0, 44.0, 96.0)},
)

receipt = evaluate_watchdog(artifact, frame)
receipt.input_state        # "OK"
receipt.proposed_intents   # (Intent(action="OBSERVE", timestamp=120),)

replay_watchdog(artifact, receipt)   # reproduces the receipt byte for byte
```

Drop the last reading and the answer changes shape rather than going quiet:

```python
blind = evaluate_watchdog(
    artifact,
    SignalFrame(timestamps=(0, 60, 120),
                signals={"QUEUE_SATURATION_PCT": (41.0, 44.0, None)}),
)
blind.input_state         # "INPUT_UNAVAILABLE"
blind.missing_inputs      # ("QUEUE_SATURATION_PCT",)
blind.proposed_intents    # ()  -- and this is NOT the same as normal
```

## Frame terminology

`SignalFrame` is an alias of `MarketFrame`, not a second type — `SignalFrame is MarketFrame` holds. A frame is a timeline plus named numeric series, and nothing about that shape is market-specific. At the Watchdog boundary, timestamps and series must use exact built-in list/tuple containers and `signals` an exact built-in dict. They are copied once into an immutable snapshot used by the frame hash, receipt, and VM; a subclass cannot return one observation to the digest and another to evaluation.

It is an alias rather than a rename so the class keeps its original `__name__`: reprs, pickles, and existing diagnostics do not shift under any current consumer. New non-market code should reach for `SignalFrame`; every existing import keeps working unchanged.

## What this is not

- **Not a scheduler.** Nothing here decides when to evaluate. The host calls `evaluate_watchdog` and supplies the frame.
- **Not a host integration.** There is no connector to CI, a server, a queue, or any other system. Nano receives observations and returns proposals.
- **Not a decision gate.** `proposed_intents` is a proposal. `nano/bridge/` shows the shape a host gate takes; the policy inside it is the host's.
- **Not freshness enforcement.** `freshness_limit_ms` is declared and carried. Nano reads no clock and cannot check it.
- **Not durable storage.** Receipts are in-memory values. Archiving, signing, and retention are host concerns.
- **Not a health monitor.** A Watchdog reports what its rule matched. It never reports that something is fine.
