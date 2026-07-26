# Pre-production hardening sweep — 2026-07-26

A polish pass over the Nano v1.0 implementation. No new language features, no
spec expansion. The objective was to find the places where the code did not yet
do what the code said it did.

Four read-only audits ran in parallel — structural drift, governance boundaries,
diagnostic and CLI consistency, documentation drift — followed by a mutation
sweep that disabled each safety guard in turn to see whether any test noticed.

**Tests: 280 → 338.** Every finding below was verified by execution, not by
inspection.

---

## The finding that mattered

`nano/ir/module.py` opens by claiming that manifest violations, tier violations,
cycles, and future-reading offsets are *"enforced at load time, not discovered at
run time"*. The mutation sweep disabled each of those checks and re-ran the full
suite. **Six survived**:

| Disabled guard | Suite result |
|---|---|
| effect manifest as a capability grant | 280 passed |
| tier gating | 280 passed |
| forward-reference / cycle rejection | 280 passed |
| negative `series.index` offset | 280 passed |
| fastmath refusal | 280 passed |
| baseline tier restriction in version inference | 280 passed |

"Load-time validation is the security boundary" was an untested assertion. The
checks existed and worked; nothing would have told us if they stopped working.
`tests/test_module.py` now covers all of it, and every one of those mutations is
killed.

Worse, the boundary was **reachable around**. `NanoModule` is a frozen dataclass,
so in-process code could construct one directly and skip `from_dict` entirely —
and the governance audit did exactly that, building a module whose manifest
granted only `log.append` and running three `BUY` intents through the VM. The
identical document, loaded properly, is rejected.

Fixed by making the claim true rather than softening it: `NanoModule.validate()`
re-runs load-time validation, and `run_module` calls it before executing. One
pass over the nodes, against an evaluation that is nodes × bars.

---

## Governance

| Claim | Verdict |
|---|---|
| A Nano program cannot act on the world | **Holds, structurally.** The entire stdlib surface of `nano/` is `argparse, csv, hashlib, json, re, sys, os, dataclasses, datetime, pathlib, typing`. No socket, no subprocess, no `eval`, no `pickle`. Enforced by absence. |
| Effects are a capability grant | **Was bypassable** via direct construction → fixed by `validate()`. |
| The runtime cannot execute invalid IR | **Was false** for two paths → fixed. |
| Tier gates constructs | Holds at both compile time and load time. |
| No look-ahead | Holds at three independent layers, now tested at each. |
| fastmath refused; clock and entropy injected | Holds. No ambient time or randomness anywhere in `nano/`. |
| The CLI cannot skip safety checks | Holds. Every command routes through `check_source` / `compile_module` / `compile_to_dict`. |

Two asymmetries closed:

- **`compile_to_dict` did not round-trip through the loader**, while
  `compile_module` did — so `nano compile -o ir.json` wrote a document the
  validator had never seen. This contradicted a comment in that same file:
  *"A compiler that trusts its own output is a compiler whose invariants drift."*
- **`StrategyGraph.to_module()`** built a `NanoModule` directly. Now validated
  through the same path as everything else.

---

## Robustness

Chaos-testing the CLI with malformed input found two traceback paths:

- A **non-UTF-8 source file** crashed instead of reporting. `UnicodeDecodeError`
  is a `ValueError`, not an `OSError`, so `except OSError` missed it — a *missing*
  file got a clean diagnostic while binary junk produced a stack trace.
- The same gap existed in `nano/data/frames.py` for market data. `load_frame` now
  funnels every failure through `FeedError`, so a caller handling bad data does
  not also have to handle two unrelated exception types.

Also: **Ctrl-C** exits 130 with `interrupted` rather than a traceback, and a
**broken pipe** (`nano compile x.nano | head -1`) exits cleanly instead of raising
during interpreter shutdown.

Traced and verified clean: permission errors, a directory passed as a file, empty
files, empty CSVs, malformed dates, unknown flags, unknown subcommands.

---

## CLI correctness

| Defect | Fix |
|---|---|
| `replay` and `visualize` caught only `NanoCompileError`, but `compile_module` round-trips through the loader — so `IRValidationError` escaped as a traceback while `compile` reported it cleanly | one shared `_compile` helper; all three behave identically |
| `--emit types` and `--emit plan` **accepted `-o`, ignored it, and exited 0 having written nothing** | every emit mode routes through `_write_or_print` |
| `check` abandoned remaining files at the first unreadable one, so **the exit code depended on argument order** | every file is attempted; a rejected program outranks an unreadable one |
| the `--verify` second run sat outside the error guard | both runs share it |
| a malformed `--date` cost a full compile before erroring | parsed first |

Exit codes reclassified to mean something:

| Situation | Was | Now | Why |
|---|---|---|---|
| malformed `--date` | 3 (IO) | 2 (usage) | nothing failed to be read |
| unknown indicator name | 1 | 2 (usage) | a bad argument value |
| forced IR version cannot hold the program | 1 | 2 (usage) | the program is fine; the flag was wrong |
| data file lacks a required signal | 3 (IO) | 1 | both inputs read fine; they do not fit each other |
| no rows for the requested date | 3 (IO) | 1 | the read succeeded |

`nano indicators ""` used to print the entire list — a truthiness check where an
identity check belonged.

---

## Simplification

| Duplication | Resolution |
|---|---|
| `_is_plain_int` defined twice, byte-identical including its docstring | one public `is_plain_int` in `nano/types/lookahead.py` |
| the comparison-operator set written out four times | one source: `CONDITION_OPERATORS` in `nano/ir/schema.py` |
| `_NAMED_OPS` defined in both `nano/ir/module.py` and `nano/cli/render.py` | one exported `NAMED_OPS` |
| `canonical_effects()` — never called from any path | removed |
| `CompiledIR` type alias — never used | removed |

Also added: duplicate entries in an effect manifest are now rejected. Two
byte-different documents granting the same capability would otherwise make
`moduleHash` depend on manifest spelling rather than meaning.

---

## Documentation

The docs described the previous release. The worst offender was
`CONTRIBUTING.md`, which told contributors that *"`nano compile` / `nano replay` /
`nano visualize` are designed but not built"* — inviting someone to rebuild
shipped, tested code.

Three documented examples did not compile, and had not for several releases:

| Location | Problem |
|---|---|
| `README.md` showcase | `observe market` — the grammar requires `observe()`; `average` was undefined |
| `README.md` roadmap prose | `buy when RSI < 30` — `when` is only legal inside a `route` block |
| `BUILD_ORDER.md` exit criterion | `buy()` — an asset is required |

Rather than only fix them, `tests/test_docs.py` now **compiles every fenced
`nano` block in the repository** and checks the advertised test count against the
suite. A block may opt out with `// doc: illustrative`, which is explicit and
greppable. Documentation drift of this kind is now a test failure.

Corrected throughout: stale counts (including a `121` the 173→280 sweep would
have missed), `RiskEngine` → `DecisionGate`, `ProvenanceRiskEngine` →
`ProvenanceGate`, and the status tables moving the CLI, static typing, look-ahead
protection, and indicators out of "roadmap".

**Deliberately not softened:** no document claims broker execution, live market
feeds, order execution, an autonomous loop runner, self-modifying deployment, or
real quantum dispatch as built — because none of them are.

---

## Performance

Nothing was optimised, because nothing measured slow: the full suite runs in
about one second, and the audit found no repeated parsing, redundant AST cloning,
or duplicated validation passes on a hot path.

One change goes marginally the other way and is worth stating. `run_module` now
validates before executing, which is one pass over the nodes per call.
`run_frames` validates once for the whole sequence rather than once per frame.
Against an evaluation that is nodes × bars, the check is noise — and buying a real
security boundary with it is the right trade.

---

## Remaining technical debt

Named rather than quietly carried.

**Six files exceed 500 lines**, against a house guideline of 800 max and 200–400
typical:

| File | Lines |
|---|---|
| `nano/types/checker.py` | 945 |
| `nano/compiler/parser.py` | 877 |
| `nano/indicators/compute.py` | 631 |
| `nano/ir/module.py` | 594 |
| `nano/compiler/codegen.py` | 583 |
| `nano/runtime/vm.py` | 548 |

`checker.py` is the one worth splitting: declaration handling, expression typing,
and call resolution are three separable passes sharing only the scope. Deferred
because a 945-line refactor at the end of a hardening pass trades a known-good
state for a rushed one. `compute.py`'s length is 33 independent kernels and is
fine as it stands.

**Diagnostic wording is not yet uniform.** The audit catalogued real
inconsistencies: five messages in `nano/types/checker.py` start with a lowercase
identifier where every other message leads with a capitalised construct name;
`sorted(X)` in five places leaks Python list syntax into user-facing text
(`expected one of ['BUY', 'EXECUTE', …]`); one construct is called "series
offset", "offset", and "index" in different files. Two messages in `checker.py`
raise at position `(0, 0)`, which is not a 1-based position and so violates the
invariant `nano/compiler/errors.py` states. All cosmetic except the last, and a
wholesale rewording pass is churn better done on its own.

**`nano/ir/*` errors carry no source position.** The AST position exists at
lowering time and is discarded, so a loader rejection surfaced through
`nano compile` names a node id rather than a line. Threading positions into the IR
is a real improvement and a real change, not a polish item.

**A negative `series.index` offset that somehow reached the VM would raise
`IndexError`**, surfacing as `replay failed: tuple index out of range` rather than
as a look-ahead diagnostic. It cannot silently read forward — the failure mode is a
crash, never an optimistic backtest — but the message should name the cause.

**LOOP-14 and LOOP-15 did not run.** Both require a governance ledger
(`_loopstate/governance-ledger.md`) and a benchmark suite this repository does not
have. Reporting trends from absent data would have been fabrication.

---

## Audited and found clean

No `TODO`, `FIXME`, `XXX`, `HACK`, `breakpoint()`, or `pdb` anywhere in `nano/`.
No commented-out code. No `print()` outside the `Console` class. No debug logging.
No unused dependencies — the package still installs with zero mandatory
dependencies. stdout/stderr discipline is consistent: machine-readable output to
stdout, diagnostics and progress to stderr.

One genuinely swallowed error remains, and it predates this work.
`nano/aethercode/preview.py` catches `Exception` and renders diagnostics, so a
compiler *bug* — as opposed to a source error — becomes an empty string in the
editor service rather than a report. Its stated invariant is only "never raises",
which it honours. Flagged, not fixed: narrowing it changes behaviour Aether Code
depends on, and that deserves its own change.
