# Run receipts

A Nano run already produces everything an audit needs: the intents proposed, the
escalations taken, and the ordered log behind both. What it did not produce was a
**document** — something with a version, a fixed serialization, and a written
promise about which bytes stay put across releases.

A *receipt* is that document. It is produced by `nano.runtime.receipt`, emitted by
`nano replay --report receipt`, and it is the artifact an external consumer should
build against.

```python
from nano.compiler import compile_module
from nano.runtime import build_receipt, canonical_bytes, receipt_digest, run_module

module = compile_module(source)
result = run_module(module, frame)

receipt = build_receipt(module, frame, result)
blob = canonical_bytes(receipt)          # the artifact
address = receipt_digest(receipt)        # sha256 over exactly those bytes
```

---

## 1. Canonical serialization

`canonical_bytes` is the **single source of truth** for turning a run into bytes.
`verify_run`, `Backtester.verify_replay`, and `nano replay --report receipt` all
call it. Two encoders would eventually disagree about a float, and the
disagreement would surface as a receipt that verifies in one tool and not in
another.

| Rule | Value | Why |
|---|---|---|
| Object key order | **sorted by Unicode code point** — Python's `sorted()` on `str`, which is what `json.dumps(sort_keys=True)` does | No fixed-order table to keep in step with the code, and immune to the order a dictionary happened to be built in |
| Array order | **preserved** | In a run log, order *is* the meaning |
| Whitespace | none — `separators=(",", ":")` | One artifact, not one per formatter |
| Non-ASCII | escaped — `ensure_ascii=True`, encoded as ASCII | Survives any transport, console codepage, or editor. A non-ASCII strategy name appears as `\uXXXX`, and the escaped form is what is hashed |
| Line terminator | **none** | The canonical form is the digested text and nothing else |
| Non-finite floats | **refused** (`ReceiptError`) | `NaN`/`Infinity` are not JSON; Python's encoder emits them anyway as bare tokens no conforming parser accepts |
| Absent members | **omitted**, never `null` | One spelling for "no value" |
| Object keys | must be strings | `json.dumps` silently coerces `{1: "a"}` to `{"1": "a"}`, which can collide with a real `"1"` key |

This continues the convention `NanoModule.content_hash` has always used
(`json.dumps(document, sort_keys=True, separators=(",", ":"))` in
`nano/ir/module.py`), so a `moduleHash` embedded in a receipt was computed the
same way the receipt around it was. The two deliberate additions are
`allow_nan=False` and the explicit ASCII encode.

### Not RFC 8785

This is **not** JCS ([RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)). JCS
sorts keys by UTF-16 code *unit*; Python sorts by code *point*, and the two
disagree for keys above U+FFFF. They also differ on number formatting. A JCS
implementation will not reproduce these bytes, so verify a Nano receipt with a
Nano-compatible encoder — or with `canonical_bytes` itself.

Reachable in practice: `frame_digest` uses signal names as object keys, and
`host` keys are whatever the caller passed.

### Newlines

The canonical form contains **no line terminator**. A writer that frames a
receipt as a file or as a [JSON Lines](https://jsonlines.org/) record appends
exactly one `\n`; that byte is framing, not content, and is **not** covered by
`receipt_digest`.

`nano replay --report receipt` writes the canonical bytes plus exactly one `\n`,
on every platform — it goes to the byte stream rather than through `print`,
whose text wrapper would rewrite that byte to `\r\n` on Windows. So the consumer
recipe is exact:

```sh
nano replay s.nano --data bars.csv --report receipt > receipt.json
# digest = sha256 of everything except the final byte
```

### Integers

JSON has one number type. Timestamps, bar counts, and warm-up counts are emitted
as JSON integers, and every one Nano produces is far inside ±2^53, so a
JavaScript `JSON.parse` consumer reads them exactly. A `host` value beyond that
range will lose precision in such a consumer — Nano will emit it faithfully, but
JavaScript cannot read it back.

### Floats

Floats use Python's `repr` shortest-round-trip formatting, which is stable across
CPython 3.9+ and platform-independent. Concretely, and asserted in
`tests/test_receipts.py`:

| Value | Bytes |
|---|---|
| `0.1 + 0.2` | `0.30000000000000004` |
| `1e308` | `1e+308` |
| `5e-324` | `5e-324` |
| `3.0` | `3.0` |
| `3` | `3` |
| `-0.0` | `-0.0` |
| `NaN`, `Infinity`, `-Infinity` | refused — `ReceiptError` |

Note the last four rows. `3.0` and `3` are equal in Python and distinct in the
bytes; so are `0.0` and `-0.0`. That is the point of comparing bytes.

### Why bytes and not dictionaries

Python holds `{"approved": True} == {"approved": 1}` and `{"x": 0.0} == {"x": -0.0}`.
Every determinism check in this repository used to be written as
`a.to_dict() != b.to_dict()`, which meant a decision gate that flipped between
`True` and `1`, or an arithmetic path that produced a negative zero on one pass,
replayed "bit-identically" while producing different bytes. Anything claiming
byte-stability now compares `canonical_bytes`.

---

## 2. What a receipt contains

```json
{
  "receiptVersion": 1,
  "identity": {
    "nanoVersion": "1.0.0",
    "irVersion": "1.0.0",
    "compiler": {"name": "nnc", "version": "1.0.0"},
    "module": "Drift",
    "moduleHash": "sha256:...",
    "tier": "nano",
    "effects": ["intent.emit", "log.append"],
    "warmupDeclared": 2,
    "reasoningRequired": false
  },
  "inputs": {
    "bars": 5,
    "firstTimestamp": 0,
    "lastTimestamp": 240,
    "signals": ["close"],
    "frameHash": "sha256:..."
  },
  "run": {
    "intents": [],
    "escalations": [],
    "log": [],
    "warmupBarsSkipped": 2
  },
  "provenance": {"sourceHash": "sha256:..."}
}
```

Worked examples are checked in under [`tests/golden/`](../tests/golden/).

### `identity` — executable identity

The four fields that answer *"what would run this again"*: `nanoVersion`,
`irVersion`, `compiler`, and `moduleHash`. `moduleHash` is the module's content
address: two modules share it exactly when they would execute identically.

A baseline (v0.1.0) graph lifts into a v1.0 module before it runs, and the
receipt records `irVersion: "1.0.0"` — the version that actually executed, not
the version the host handed in.

`params` appears here rather than under `inputs` because declared parameters are
part of the module, and changing one changes `moduleHash`.

`reasoningRequired` is `true` when the module contains an `infer`. A run that
consults a model is only reproducible against the same provider, and **the
receipt cannot identify the provider** — a host replaying a transcript should
record which transcript in `host`.

### `inputs` — the injected frame

`frameHash` is a content address over the timeline and every named series, so a
receipt records *which* data produced it without carrying the data. Signal
insertion order does not affect it; values do.

### `run` — the ordered log

`intents`, `escalations`, and `log` in emission order, plus `warmupBarsSkipped`.
A bar whose condition had not warmed up is counted here rather than being
silently treated as no-signal.

### `provenance` — where the module came from, unverified

`sourceHash` covers the `.nano` text. It is filed apart from `identity` on
purpose: a comment-only edit changes `sourceHash` and not `moduleHash`, and
collapsing the two would lose the distinction between "the file was edited" and
"the behavior was edited". Absent when the module carries no source hash.

### `host` — the caller's claims

Omitted unless supplied. This is the **only** section not derived from the run,
and the only place a wall clock, deployment id, operator, or provider transcript
id may appear. It is inside the digest — it is part of the record — but it is
outside the determinism guarantee, and its contents are claims rather than
findings.

---

## 3. Timestamps

**A deterministic run embeds no wall clock.** Every timestamp in a receipt came
out of the injected `MarketFrame`; nothing in `nano/runtime/receipt.py` can reach
a clock, entropy, the environment, or the network, and
`tests/test_determinism_guards.py` scans the whole `nano/` package with an AST
walk to keep that true rather than asserting it in prose.

A host that wants to record *when* a run happened puts it in `host`, where it is
visibly a claim.

The one deliberate exception in the codebase is Protocol-C signing
(`nano/bridge/provenance.py`), which carries a real wall clock and fresh entropy
**by design** — that is what makes a signature evidence of *when*. It lives
behind an optional third-party package, it is a side channel the bridge never
sees, and it never touches a base receipt.

---

## 4. Verification and drift

```python
from nano.runtime import differences, receipt_digest, verify_run

verify_run(module, frame)                 # runs twice, byte-compares, raises on drift
differences(before, after)                # ("/identity/moduleHash", "/inputs/frameHash")
```

`verify_run` raises `ReplayDivergence` naming the paths that drifted. A
divergence invalidates every number the run produced, so it fails rather than
warns.

`differences` is type-sensitive: `True` and `1` differ, and so do `0.0` and
`-0.0`, because they differ in the bytes. Detection alone would only say *that* a
run drifted; naming the path says *where*.

Drift is detected for each of the three things that can change:

| Change | What moves |
|---|---|
| Mutate the IR | `/identity/moduleHash` |
| Mutate an input bar | `/inputs/frameHash`, and usually `/run/...` |
| Mutate a declared param | `/identity/moduleHash`, `/identity/params/N/value` |

### Two different failures

`verify_run` and `Backtester.verify_replay` raise **two** exception types, and
they mean opposite things:

| Raised | Means |
|---|---|
| `ReplayDivergence` | The run is not reproducible. Two identical inputs gave different bytes. |
| `ReceiptError` (a `ValueError`) | The run produced a value the canonical form cannot represent — a non-finite float, a foreign scalar type, a non-string object key. The run may be perfectly deterministic. |

They are deliberately not merged. `except ReplayDivergence` will **not** catch a
`ReceiptError`; catch both if you want "the verification did not succeed".

### Behavior change in this release

`Backtester.verify_replay` and `nano replay --verify` used to compare the two
runs as Python dictionaries. They now compare canonical bytes, which is stricter
in two ways that can surface as new failures in existing, previously-passing
deployments:

- A gate that returns a **non-`bool`, non-`float` scalar** — `numpy.bool_`,
  `decimal.Decimal` from a NUMERIC database column — now raises `ReceiptError`
  instead of quietly comparing equal. Convert at the gate boundary:
  `bool(value)`, `float(value)`.
- A gate whose answer changes *type* between runs while keeping the same value
  (`True` one run, `1` the next) now raises `ReplayDivergence`. It always was
  nondeterministic; the old comparison could not see it, because Python holds
  `True == 1`.

Both are intentional. A comparison that cannot distinguish `true` from `1`
cannot underwrite a claim about bytes.

---

## 5. Authenticity — what a receipt is *not*

**An unsigned receipt is evidence of reproducibility, not of authenticity.**
Anyone can write one. It proves that *these bytes correspond to this module over
this frame*; it proves nothing about who produced them or when.

Binding a receipt to a signer is Protocol-C's job
(`nano.bridge.provenance.ProvenanceGate`). It is optional, it requires the
`aether-protocol-c` extra, and it is deliberately outside deterministic core
behavior. A base receipt is fully constructible with that package absent.

---

## 6. Stability contract

`receiptVersion` is an integer, currently **1**. It is bumped on any change to
the emitted shape, in the same commit that regenerates `tests/golden/`.

A **Nano version bump** also moves every golden receipt, because
`identity.nanoVersion` is part of the artifact. That is not a format change and
does not bump `receiptVersion`; regenerate the goldens with
`py -3.11 tests/regen_goldens.py`. The golden test distinguishes the two cases
in its failure message.

The member names of every section are pinned separately, in
`test_the_receipt_shape_is_pinned_to_this_version`. The goldens alone cannot
guard the shape, because they are regenerated as a matter of routine — a new
member could ride along in that regeneration unnoticed. Editing that pin is the
explicit act that means `receiptVersion` has to move.

### You may depend on

- The **serialization rules** in §1. They are the format.
- The **presence and meaning** of `receiptVersion`, `identity`, `inputs`, `run`,
  and — when the module carries a source hash — `provenance`.
- **`moduleHash` and `frameHash` being stable content addresses.** An exact value
  for a fixed source is pinned in `tests/test_receipts.py`, so it cannot change
  by accident.
- **Absent means absent.** The deterministic sections contain no JSON `null`.
- **Empty means empty.** `intents`, `escalations`, and `log` are always present,
  possibly as `[]`.
- **Array order.** Intents, escalations, and log entries are in emission order.
- **Additive growth within a version.** New *optional* members may appear in an
  existing object; nothing is removed or retyped without a version bump. Parse
  permissively.

### You may not depend on

- **`log` entry `detail` strings.** Free-form, human-facing, and expected to
  change. Read `event` and `timestamp`; treat `detail` as prose.
- **Node ids** (`n6`) appearing inside those strings. They are compiler output.
- **The set of `event` names.** New events may be added at any time.
- **`host`.** It is whatever the caller passed.
- **Anything in `nano replay --report json`.** That is a separate, older,
  unversioned CLI convenience; it is not the receipt and carries no stability
  promise.
- **Receipt digests comparing equal across Nano versions.** A version bump may
  legitimately change the bytes. Compare digests within a version, and compare
  `identity` across versions.

---

## 7. Boundaries and known sharp edges

- **A run that uses `infer` is only as deterministic as its provider.** The VM
  never constructs one, so a module cannot reach a model the caller did not hand
  it — but `reasoningRequired: true` means "reproducible *given the same
  provider*", and the receipt cannot check that it got one.
- **The effect manifest is order-sensitive.** `moduleHash` covers `effects` in
  declared order, so two manifests listing the same capabilities in a different
  order are different modules by hash. The receipt reports the declared order to
  stay consistent with `moduleHash` rather than papering over it.
- **An empty frame still logs `module.loaded` at timestamp `0`.** With no bars
  there is no first timestamp to report, and `0` is a placeholder rather than
  data. `inputs.bars` is `0` and `firstTimestamp` is absent, which is the
  reliable signal.
- **`host` is not validated** beyond being canonically encodable.

---

## 8. Recipes

Emit one receipt per run into a JSON Lines audit file:

```sh
nano replay strategy.nano --data bars.csv --report receipt >> audit.jsonl
```

Prove a strategy replays deterministically before trusting a backtest:

```sh
nano replay strategy.nano --data bars.csv --verify
```

The strategy below is the one the worked examples use:

```nano
strategy Drift {
    input close: series<float>
    let z = ZSCORE(close, 3)
    every 1m {
        if z > 0 {
            buy(BTC, 0.7)
        } else {
            observe()
        }
    }
}
```

---

Related: [Architecture](architecture.md) for where the runtime sits,
[Status](status.md) for what is implemented, and
[Security policy](../SECURITY.md) for the boundary of Nano's guarantees.
