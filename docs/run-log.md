# Run log

`nano replay --report json` writes the **run log**: the ordered account of one
run, meaning the intents it proposed, the escalations it took, and every event
behind both. This document freezes that output as **run log v1**: which members
are guaranteed, what each one means, how the bytes are produced, and what may
change between releases.

- Machine-readable shape: [`run-log.schema.json`](run-log.schema.json) (JSON Schema 2020-12).
- Worked examples, checked byte for byte: [`tests/fixtures/run_log/`](../tests/fixtures/run_log/).
- Proof: `tests/test_run_log_schema.py` and `tests/test_run_log_schema_encoding.py`.

The run log does **not** replace the [run receipt](receipts.md). The receipt is
the versioned, canonical, digestable artifact; archive and hash that. The run log
is the readable view of the same run, and its `intents`, `escalations`, and `log`
arrays are the receipt's `run` arrays element for element (§7).

---

## 1. Producing it

```sh
nano replay strategy.nano --data bars.csv --report json            # replayVerified: false
nano replay strategy.nano --data bars.csv --report json --verify   # replayVerified: true
```

There is no separate `nano run` command. `replay` is the CLI's only execution
path, so "the run" and "the replay" are the same thing here, with the same
document and the same rules.

The arrays come from `ModuleResult.to_dict()` (`nano/runtime/vm.py`), which uses
`Intent.to_dict()` and `LogEntry.to_dict()` (`nano/runtime/effects.py`) and
`Escalation.to_dict()`. The CLI adds the header members and encodes the result with
`nano.cli.commands.encode_run_log`. A Python host calling
`run_module(...).to_dict()` gets the same arrays, but no header and no bytes:
encoding is its own business.

A run that fails, whether through a compile error, unreadable data, a missing
signal, an empty date, or a failed `--verify`, writes **no** run log. It prints a
diagnostic on stderr and exits non-zero. Partial documents do not exist.

---

## 2. The document

Members appear in exactly this order:

| Member | Type | Meaning |
|---|---|---|
| `strategy` | string | Module name from the `strategy` declaration. |
| `moduleHash` | `sha256:<64 hex>` | Content address of the executable IR (`NanoModule.content_hash()`). Two modules share it exactly when they would execute identically. |
| `sourceHash` | `sha256:<64 hex>` | sha256 of the `.nano` text after UTF-8 decoding (BOM stripped) and newline normalisation, so a CRLF checkout and an LF checkout hash the same. Changes on a comment edit; `moduleHash` does not. |
| `bars` | integer ≥ 1 | Bars in the frame that ran, after any `--date` filter. |
| `rowsRead` | integer ≥ 1 | Rows read from the data file before filtering. |
| `rowsFiltered` | integer ≥ 0 | Rows the `--date` filter dropped. Always `rowsRead == bars + rowsFiltered`. |
| `warmupDeclared` | integer ≥ 0 | The module's declared warm-up, in bars. |
| `replayVerified` | boolean | `true` only when `--verify` ran the module twice and the two receipts were byte-identical. `false` means *not checked*, never *failed*, because a failed verification writes no run log. |
| `intents` | array of [intent](#intent) | Proposals that survived the risk gate. |
| `escalations` | array of [escalation](#escalation) | Hand-offs to a reasoning layer. |
| `log` | array of [log entry](#log-entry), non-empty | The audit log. The first entry is always `module.loaded`. |
| `warmup_bars_skipped` | integer ≥ 0 | Scheduled evaluations of top-level rules skipped because the condition had no value yet. Equal to the number of `condition.unwarmed` entries. It is summed over rules, so with two rules it can exceed `bars`. |

Every member is always present. No member is ever `null`. The top-level object,
and every object inside the arrays, is **closed** in v1: the schema sets
`additionalProperties: false`, and a v1 producer emits nothing else.

The one naming inconsistency, camelCase headers next to snake_case
`warmup_bars_skipped`, is historical and frozen. Renaming it would break every
existing consumer for no gain in meaning. The receipt spells the same number
`run.warmupBarsSkipped`.

### Intent

```json
{"intent": "BUY", "timestamp": 120, "asset": "BTC", "confidence": 0.7}
```

| Member | Presence | Meaning |
|---|---|---|
| `intent` | always | One of `BUY`, `SELL`, `EXECUTE`, `PAUSE`, `OBSERVE`. |
| `timestamp` | always | Bar the proposal belongs to (§5). |
| `asset` | only when the action names one | Asset symbol from source. Omitted, never `null`, otherwise. |
| `confidence` | only when declared | The literal from source, range-checked to `[0, 1]` at compile time and emitted **as written**: `buy(BTC, 1)` gives `1`, `buy(BTC, 1.0)` gives `1.0`. |

This is also the shape `nano package test` fixtures compare (`{intent, timestamp, asset?, confidence?}`).

### Escalation

```json
{"escalate": "Desk", "timestamp": 180, "isAgent": true, "reason": "route Trusted: guard not satisfied"}
```

| Member | Meaning |
|---|---|
| `escalate` | Target: a declared agent name when `isAgent` is `true`, or a quoted host target when it is `false`. |
| `timestamp` | Bar of the hand-off. |
| `isAgent` | Boolean; never `0`/`1`. |
| `reason` | Informative prose. Today it is `rule body` for an `escalate` statement inside a rule, and `route <Name>: guard not satisfied` or `route <Name>: guard has no value` for a route fallback. Display it; do not parse it across versions. |

Each escalation is mirrored by an `llmre.escalated` log entry, with detail
`<escalate>: <reason>`, in the same relative order.

### Log entry

```json
{"event": "intent.emitted", "timestamp": 120, "detail": "BUY asset=BTC"}
```

| Member | Meaning |
|---|---|
| `event` | Dotted lower-case name (`^[a-z][a-z_]*(\.[a-z][a-z_]*)+$`). |
| `timestamp` | Bar the event belongs to (§5). |
| `detail` | **Always a string.** Human-facing prose (§3). |

`detail` is the run log's only open extension point, and it is open as *prose*,
not as structure. It is never an object or array. Its text is byte-stable for
identical inputs within one Nano version, so golden files may pin it. It is not a
parse target across versions: wording, node ids (`n11`), and the order of clauses
may change in any release.

---

## 3. Events

The VM emits these events in run log v1. The set is **open**: a later minor
release may add an event, so consumers must ignore names they do not know. The
list is kept equal to the emitters in `nano/runtime/vm.py` and
`nano/runtime/risk.py`, to `$defs.knownEvent` in the schema, and to this table by
`test_known_events_equal_the_vm_emitters_the_schema_and_the_doc`.

Detail formats are **informative**. They describe today's text so a reader can
make sense of a log, not a grammar to parse.

<!-- run-log-events:start -->
| Event | When | Timestamp | Detail today (informative) |
|---|---|---|---|
| `module.loaded` | Once, first entry | first bar | `<name> tier=<tier> effects=[...] warmup=<n>` |
| `risk.armed` | Once, if the module enforces any risk limit | first bar | `<limit> <band>, ...; measurements: <signals or none>` |
| `risk.unenforced` | Once per declared limit Nano does not enforce | first bar | `<limit> <value>: not enforced by Nano — <why>` |
| `infer.skipped` | Once per `infer` when no provider was supplied (always the case for `nano replay`) | first bar | `<Signature>: no reasoning provider supplied` |
| `infer.invalid` | Signature output contract unusable (once), or one bar's response off-contract | first bar, or that bar | `'<Signature>': <reason code>` |
| `infer.failed` | The provider raised on a bar | that bar | `'<Signature>': provider_exception:<ExceptionType>` |
| `infer.called` | The provider answered a bar on-contract | that bar | `<Signature> -> [<sorted output names>]` |
| `condition.unwarmed` | A scheduled top-level rule fired but its condition had no value | that tick | `<node> has no value at bar <i>` |
| `condition.evaluated` | A scheduled top-level rule fired and its condition had a value | that tick | `<node> -> True` or `<node> -> False` |
| `intent.emitted` | An intent survived the risk gate and was appended to `intents` | that tick | `<ACTION> asset=<asset or None>` |
| `risk.violation` | One per limit an intent breached, in declared vocabulary order | that tick | `<limit> <value>: <measurement> observed <x> (allowed <band>)` |
| `intent.suppressed` | After the violations, once per withheld intent | that tick | `<ACTION> asset=<asset> confidence=<c> withheld by risk limits: <limits>` |
| `llmre.escalated` | Every escalation, from a rule body or a route fallback | that tick or bar | `<target>: <reason>` |
| `route.executed` | A route guard held on a bar | that bar | `<Route> -> <execute label>` |
<!-- run-log-events:end -->

`infer.invalid`, `infer.failed` and `infer.called` need a reasoning provider,
and `nano replay` never supplies one. They appear only in logs a Python host
produces with `run_module(..., provider=...)`. That log has the same shape and
validates against the same schema (`test_provider_only_events_fit_the_same_schema`).

Other Nano surfaces build `LogEntry` values too: the baseline interpreter
(`strategy.loaded`), the watchdog evaluator (`watchdog.*`), and the host bridge.
They use the same three-member entry shape, but they are not `nano replay` output
and are outside this table.

---

## 4. Bytes

`encode_run_log` is the single encoder. The rules:

| Rule | Value |
|---|---|
| Layout | `json.dumps(document, indent=2)`: two-space indent, one member or element per line, `,` at line end, `": "` between key and value, `[]` for an empty array. |
| Key order | **Fixed by the emitters**, as listed in §2. Not sorted. |
| Array order | Emission order (§6). |
| Non-ASCII | Escaped as `\uXXXX` (`ensure_ascii`). The bytes are pure ASCII; for example, the em dash in `risk.unenforced` details leaves as `—`. |
| Line terminator | Exactly one `\n` after the closing brace, **on every platform**. Never `\r\n`. |
| Integers | Exact decimal digits, any magnitude, sign kept. Timestamps and counts are always JSON integers, never `120.0`. |
| Floats | Python's shortest round-trip `repr`, which is locale-independent: `0.30000000000000004`, `1e-07`, `1.0`, `5e-324`. `1` and `1.0` stay distinct. |
| Non-finite numbers | Cannot occur. The only non-integer member is `confidence`, which the compiler range-checks to `[0, 1]`, so `NaN` and `Infinity` are refused before a run exists. |
| Absent | Omitted (`asset`, `confidence`), never `null`. |

Consequences a consumer may rely on: two runs of the same Nano version over the
same strategy file and the same data file, with the same arguments, write
**byte-identical** run logs. This holds on any OS, in any time zone, under any
locale, and for any hash seed. Parsing a run log and re-encoding it with
`encode_run_log` gives back the same bytes.

JSON Schema cannot express key order, array order, or layout, so those are pinned
by tests rather than by the schema: `test_report_reproduces_the_golden_bytes`,
`test_golden_is_lf_only_ascii_and_reencodes_to_itself`,
`test_record_key_order_is_fixed_by_the_emitters_not_by_sorting`,
`test_float_formatting_is_shortest_round_trip_repr`, and
`test_large_and_negative_integer_timestamps_are_emitted_exactly`.

### Behavior change: the line terminator

Before run log v1 was frozen, `--report json` was printed through the console's
text wrapper, which on Windows rewrites each `\n` to `\r\n`. The same run
therefore produced different bytes on Windows and POSIX. It now leaves through
the byte stream, as the receipt already did (see [receipts.md §1](receipts.md#newlines)),
so every platform gets the LF-only bytes POSIX always produced. No member, value,
order, or indentation changed. A Windows consumer that compared raw bytes against
a CRLF capture should recapture once. A consumer that parsed the JSON sees no
difference.

---

## 5. Timestamps

**Every timestamp in a run log is a bar time from the injected frame. None is a
wall-clock reading.** This holds in a run and in a replay alike, because they are
the same code path.

- Data timestamps become integer epoch seconds at load time. An epoch integer is
  taken as is; an ISO-8601 string is read as **UTC** unless it carries an offset.
  The reader's time zone never matters (`nano/data/frames.py`).
- Rows are sorted by timestamp and duplicates are refused, so bar `i` has one
  well-defined time.
- A rule's events carry the time of the tick that fired. A route's events carry
  the time of the bar being guarded. Run-level events (`module.loaded`,
  `risk.armed`, `risk.unenforced`, `infer.skipped`, and a contract-level
  `infer.invalid`) carry the **first bar's** time.
- Schedules align to the injected timeline, not to the clock: a schedule fires on
  the first bar and then on each bar at least one interval after its previous
  firing (`nano/runtime/scheduler.py`).
- `--date` filters by the UTC calendar date of each bar.
- `run_module` over an empty frame stamps `module.loaded` with `0` as a
  placeholder (see [receipts.md §7](receipts.md)). The CLI never shows this,
  because it refuses an empty frame before running.

Nothing on the run-log path imports a clock. `tests/test_determinism_guards.py`
guards that statically across `nano/`, and `test_no_wall_clock_reaches_the_report`
checks it dynamically: it replaces `time.time`, the monotonic and performance
clocks, and the date-time class with absurd values and requires the golden bytes
unchanged. A host that wants to record *when* a run happened belongs in the
receipt's `host` section, where the value is visibly a claim.

---

## 6. Ordering

All three arrays are in **emission order**. Emission order is deterministic, but
it is **not timestamp order**:

1. `module.loaded`, then `risk.armed` and `risk.unenforced` (declaration order).
2. `infer.*` events, while signals are derived, one signature call site at a time
   over every bar.
3. Each top-level rule in compiler entry order (source order of the `every`
   blocks, then of the rules inside each block). Each rule walks **its whole
   timeline** before the next rule starts. Within one tick, the order is
   `condition.*`, then for each statement in the taken branch:
   `risk.violation`…`intent.suppressed` or `intent.emitted`, and
   `llmre.escalated`.
4. Each `route` in source order, over every bar.

So a strategy with two rules logs the first rule's bars `0…n`, then the second
rule's bars `0…n`, and routes come last even when they are declared first. The
fixture `run_log_audit.json` shows exactly this, and
`test_arrays_are_in_emission_order_not_timestamp_order` keeps it so. A consumer
that wants a timeline sorts by `timestamp` itself, using a **stable** sort, so
events at one time keep their causal order.

`intents` and `escalations` follow the same order as their mirroring `intent.emitted`
and `llmre.escalated` log entries (`test_documented_invariants_hold`).

---

## 7. Relationship to the run receipt

| Run log member | Receipt path |
|---|---|
| `intents`, `escalations`, `log` | `run.intents`, `run.escalations`, `run.log`, identical canonical bytes per array |
| `warmup_bars_skipped` | `run.warmupBarsSkipped` |
| `moduleHash` | `identity.moduleHash` |
| `strategy` | `identity.module` |
| `warmupDeclared` | `identity.warmupDeclared` |
| `sourceHash` | `provenance.sourceHash` |
| `bars` | `inputs.bars` |
| `rowsRead`, `rowsFiltered`, `replayVerified` | not in the receipt (data-file and CLI facts, not run facts) |

`test_run_arrays_are_the_receipt_run_arrays` holds this table true for every
fixture.

| | Run log | Receipt |
|---|---|---|
| Purpose | Read and diff a run | Archive, hash, and verify a run |
| Encoding | indented, emitter key order, LF-terminated | canonical: sorted keys, no whitespace (receipts.md §1) |
| Digest | none defined; do not hash it as an identity | `receipt_digest` over canonical bytes |
| Identity fields | `moduleHash`, `sourceHash` | full `identity`, `inputs.frameHash` |
| Version marker | out of band (§8) | in band, `receiptVersion` |

The receipt remains the single authority for canonical bytes. When a consumer
needs a content address, a signature, or a cross-tool comparison, it should use
`--report receipt`. [receipts.md §6](receipts.md#6-stability-contract) lists
`--report json` under *you may not depend on*. From the receipt's side that is
still right: the run log is not the receipt and carries none of the receipt's
guarantees. This document adds the run log's own, narrower contract, which
covers its shape and its bytes.

---

## 8. Versioning and compatibility

This is **run log v1**. The schema says so in its `title` and in
`"x-runLogVersion": 1`. The schema's `$id` is
`https://github.com/AetherAI3/Nano/docs/run-log.schema.json`.

**There is no in-band version member, by decision.** Adding one would change
every existing run log's bytes and member set, and it would break consumers that
reject unknown members, which is exactly what the closed v1 schema tells them to
do. The rule is instead: **a run log without `runLogVersion` is v1.** If a
breaking change is ever needed, v2 adds `"runLogVersion": 2` as its first new
member. Its presence is the signal, and v1 documents never carry it.

| Change | Allowed in v1? |
|---|---|
| New `event` name | Yes. The event set is open; consumers ignore unknown names. Update §3, the schema's `knownEvent`, and the goldens in the same change. |
| Different `detail` or escalation `reason` wording, different node ids | Yes, in any release. Regenerate the goldens; detail is prose. |
| Different hash values after a compiler or IR change | Yes. `moduleHash` addresses the IR, which may legitimately move between Nano versions; compare it within a version. |
| Add, remove, rename, or retype any member, or make one conditional | **No.** That is v2. |
| Change key order, indentation, escaping, float or integer formatting, or the line terminator | **No.** That is v2. |
| Change the meaning or order of an array | **No.** That is v2. |

A golden regeneration (`python tests/test_run_log_schema.py`) is the explicit
act that accepts a byte change. Do it only for an allowed change from the table
above, and say which one in the commit message.

### You may depend on

- Every member in §2 being present, typed, and non-null, and no others appearing.
- Byte-identical output for identical inputs on one Nano version, on any platform (§4).
- Timestamps being bar times from the data, never the clock (§5).
- Emission order, and the mirroring between `intents`/`escalations` and the log (§6).
- `log[0].event == "module.loaded"`, `rowsRead == bars + rowsFiltered`, and
  `warmup_bars_skipped` equalling the `condition.unwarmed` count.
- The arrays matching the receipt's `run` arrays (§7).

### You may not depend on

- The text of `detail` or of an escalation `reason`, or node ids inside them.
- The event set being closed.
- Timestamps being sorted.
- Run log bytes being equal across Nano versions, or being a content address.
  Use the receipt for that.
