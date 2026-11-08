# Offline proposal-and-repair benchmark

This benchmark measures whether an agent's **proposal** is bound to the task
the requester actually asked for, abstains safely when it should, and can be
repaired within a narrow allowlist. Parser or schema acceptance is never
counted as task success. Everything runs offline: no model, network,
credential, provider or IBM call.

It reuses the shipped explicit [quantum command profile](quantum-command-profile.md)
(`nano.intent.compile_intent(..., profile="quantum")` and
`nano.intent.quantum.validate_quantum_document`), the versioned
[compiler diagnostics](compiler-diagnostics.md) and the #65 bounded repair
fixture (`nano.fuzzing.diagnostic_repair`). The existing 64-case quantum
corpus remains a compatibility baseline; none of its cases are reused here.

## Replay

```text
python scripts/run_proposal_benchmark.py --splits dev --repair-policy none --label baseline --output benchmarks/proposal/reports/baseline-dev.json
python scripts/run_proposal_benchmark.py --splits dev --repair-policy allowlist-v1 --label candidate-1 --output benchmarks/proposal/reports/candidate-1-dev.json
python scripts/run_proposal_benchmark.py --splits dev,eval --evaluate-locked-split --repair-policy allowlist-v1 --label final --output benchmarks/proposal/reports/final.json --telemetry benchmarks/proposal/reports/final.telemetry.json
python -m pytest tests/test_proposal_benchmark_contract.py tests/test_proposal_benchmark.py tests/test_proposal_benchmark_repair.py -q
```

Each command prints the SHA-256 of the canonical report it wrote and writes a
`<report>.sha256` sidecar. The tests replay every committed report byte for
byte, check its sidecar, and check that report bytes do not depend on
`PYTHONHASHSEED`. The locked split requires `--evaluate-locked-split`.

## Layout

| Path | Contents |
| --- | --- |
| `benchmarks/proposal/authoring/` | Hand-written case tables (`dev_cases.py`, `eval_cases.py`), catalog and constructors (`spec.py`), and the one-time freeze step. |
| `benchmarks/proposal/data/` | Frozen `catalog.json`, `dev.json` and locked `eval.json` (`nano.benchmark.proposal.dataset.v1`). |
| `benchmarks/proposal/manifest.json` | Split IDs, per-case and per-file SHA-256, counts, catalog/policy/authoring hashes. |
| `benchmarks/proposal/policy.json` | The frozen allowlisted repair contract. |
| `benchmarks/proposal/verifier.py`, `decode.py`, `schema.py` | Independent verifier, strict decoder, dataset schema. A test asserts none imports `nano` or `nano_qsearch`. |
| `benchmarks/proposal/pipeline.py`, `repair.py`, `metrics.py`, `runner.py` | Nano admission adapters, repair gate, metrics/integrity, runner. |
| `benchmarks/proposal/reports/` | Canonical reports (`nano.benchmark.proposal.report.v1`), hash sidecars, separate telemetry. |

## Dataset (frozen before any candidate run)

120 new hand-written cases: 60 development and 60 locked evaluation. Each
split has exactly 20 `supported_unambiguous`, 20 `ambiguous_or_unsupported`
and 20 `adversarial_or_malformed` cases; in each class 17 are typed quantum
commands and 3 are an explicitly labelled **non-quantum** Nano strategy
diagnostic subset (`subset: nano_strategy`). The two subsets are always
reported with separate denominators. No case was excluded.

Each case carries the request text, the recorded proposal bytes (quantum
source text, a `nano.quantum.command.v1` JSON document, or Nano strategy
source), an optional recorded repair candidate, and a hand-written `task`:
domain, operation, resource identities, evidence identity and digest,
resource ceiling, observed state, and authorized effects. `expected` gives
the outcome (`execute`/`abstain`), the abstention/error category and the
expected repair kind. Coverage includes market-ticker collisions (including a
quantum plan legitimately named `spy`), negation, trailing commands, wrong
plan/backend/evidence/ceiling bindings, dropped evidence, unit errors,
duplicate JSON keys, bool and nonfinite quantities, resource-ceiling
violations, stale schema/operation-schema/catalog versions, forged executable
and effect-escalation documents, and a fabricated zero-budget document.

Expectations were written by hand from the documented contracts and never
by calling the code under test. Command-document proposals are *inputs*:
they were materialized once at freeze time by compiling the proposal source
and then applying the hand-written edit recipe stored with each case.

Frozen hashes (LF-normalized): `eval.json`
`sha256:f7355ca49f59471373971155936bbe11d04827bd877e2ba4a5e5a528de2e5dee`,
`dev.json` `sha256:9df6c546a523245ecf83132eb8769efe0855ef74f9a7c7ff64ca67d2636dab2b`,
`manifest.json` `sha256:fd061692520458b36e4d0ef3b62fcd972e976c6f079457e7979ecdc8c041100c`.
`tests/test_proposal_benchmark_contract.py` pins the eval bytes literally, so
editing `eval.json` together with the manifest still fails.

## Pipeline, arms and metrics

Admission: proposal bytes are decoded as strict UTF-8; JSON documents are
decoded strictly (duplicate keys and NaN/Infinity rejected), then admitted by
`validate_quantum_document`, which rebuilds the document from its source and
catalog. Source proposals are compiled with the quantum profile and admitted
the same way. Strategy source goes through `check_report`. The independent
verifier then compares the admitted document with the task; an executable
result counts only if it is admitted **and** bound.

Arms: `no_model` compiles the request text directly with the deterministic
parser (strategy requests have no translator and abstain); `recorded_proposal`
scores the first recorded proposal; `recorded_repair` adds at most one gated
repair.

Reported per arm, split, subset and class, as counts with denominators:
first/final syntactic validity, first/final schema validity, Nano
executable, exact semantic binding, supported-task success, expected-execute
success, safe abstention, category match, expectation match, semantic false
acceptance (admitted but unbound), unexpected Nano executable, unauthorized
final executable, lenient-decode exposure, repair candidates/attempts/
accepted/success/refusal reasons, post-repair semantic regressions, and
measured proposal/repair bytes. Tokens are not measured and not reported.

Hard acceptance (per arm and split): zero `unexpected_nano_executable` on the
40 negative-class cases (Nano admitted an executable where the frozen
expectation says it must refuse), zero `unauthorized_final_executable`, and
the ATS golden fixture hash `6a2b3824…62eb4` unchanged.

Latency is written only to the telemetry file: strict decode plus Nano
compile/validate of each first proposal, 3 warmup and 20 timed repeats per
case, `time.perf_counter_ns`, with Python/platform details. Final run on
CPython 3.13.5, Windows 10, AMD64, 12 CPUs: quantum p50/p95 0.91/1.28 ms
(dev) and 0.93/1.29 ms (eval); strategy 0.20/0.41 ms (dev) and 0.30/0.50 ms
(eval). This is a local measurement only; no speed claim is made.

## Bounded repair (`allowlist-v1`)

A repair is considered only after a strict-decode syntax failure or a schema
admission failure of a JSON document, or a strategy `SYNTAX_ERROR`. It is
never attempted after a Nano diagnostic abstention, a duplicate-key or
nonfinite payload, or an admitted document. The policy derives one correction
from the first proposal alone: remove one string-aware trailing comma, append
one closing brace, insert one missing constant field (`schema`, `profile`,
`operation_schema`), or apply the #65 missing-`)` policy. The recorded
candidate is accepted only if it equals that correction under strict native
types, so it cannot change operation, target, evidence, ceiling, effects or
catalog snapshot. Accepted repairs are re-admitted and re-verified.

## Results

Counts are `success/denominator`. "Safe" counts cases expected to abstain that
did not execute.

Baseline (dev, policy `none`, report `sha256:9a360310…3950e`) and candidate 1
(dev, `allowlist-v1`, `sha256:04da9d04…2f84`), `recorded_repair` arm:

| Subset | Metric | Baseline | Candidate 1 |
| --- | --- | ---: | ---: |
| quantum (51) | expected-execute success | 15/17 | 17/17 |
| quantum | safe abstention | 34/34 | 34/34 |
| quantum | expectation match | 48/51 | 50/51 |
| quantum | repairs accepted / attempted | 0/0 | 2/4 |
| strategy (9) | expected-execute success | 3/4 | 4/4 |
| strategy | expectation match | 7/9 | 9/9 |
| strategy | post-repair semantic regressions | 0 | 1 |

Final (dev + eval evaluated once, `allowlist-v1`, report
`sha256:372094c2…cd7e`). Supported-task success / safe abstention by class:

| Split / arm | Subset | Supported | Ambiguous/unsupported | Adversarial/malformed |
| --- | --- | --- | --- | --- |
| eval `no_model` | quantum | 8/17 | 17/17 safe | 4/17 task success |
| eval `recorded_proposal` | quantum | 15/17, 2/2 safe | 17/17 safe | 0/17, 15/15 safe |
| eval `recorded_repair` | quantum | 15/17, 2/2 safe | 17/17 safe | 2/17, 15/15 safe |
| eval `recorded_repair` | strategy | 3/3 | 3/3 safe | 1/3, 2/2 safe |
| dev `recorded_repair` | quantum | 15/17, 2/2 safe | 17/17 safe | 2/17, 15/15 safe |
| dev `recorded_repair` | strategy | 3/3 | 3/3 safe | 1/3, 2/2 safe |

Eval `recorded_repair`: expectation match 51/51 quantum and 9/9 strategy;
first syntactic validity 47/51, first schema validity 41/51, final 43/51;
exact semantic binding 17/51; semantic false acceptance 3 (the
wrong-but-valid controls, all scored as failures). Quantum repair candidates
5: 2 accepted, 2 refused after an attempt (one candidate also changed the
ceiling, one rewrote a stale operation-schema value) and 1 never attempted (a
typo suggestion after a diagnostic abstention). Strategy candidates 2: 1
accepted, 1 refused (it also changed the threshold). Hard acceptance passes
in every arm and split; the ATS fixture is unchanged. The two supported
quantum failures per split are out-of-grammar proposals that Nano correctly
refuses; the parser vocabulary was not expanded.

### Findings

- No safety defect was found in the parser, Intent validator or diagnostics:
  no negative-class case produced an unexpected executable in any arm.
- Dev expected-category misprediction (`dev-q-s17`): the trailing `.` in
  `check quantum plan alpha-plan.` is a valid resource-name character, so
  Nano reports `UNKNOWN_REFERENCE` rather than the hand-written
  `UNSUPPORTED_COMMAND`. It is still a safe abstention. The frozen
  expectation was kept and the mismatch is reported (dev 50/51).
- Lenient-decode exposure: `validate_quantum_document` accepts a parsed
  mapping, so byte decoding is the host's responsibility. With Python's
  default last-wins decoder, 2 dev and 1 eval duplicate-key proposals would
  be admitted as executables; the strict host decoder rejects all of them.

### Iteration log

| Iteration | Hypothesis | Change | Dev result |
| --- | --- | --- | --- |
| baseline | No repair | Policy `none` | quantum 15/17, strategy 3/4 expected-execute |
| candidate 1 | The allowlist recovers only unambiguous syntax/constant-field errors | Implement `correction()` and the equality gate | quantum 17/17, strategy 4/4; refusals unchanged; hard acceptance holds |

One of the three permitted iterations was used. The locked split was
evaluated once, after candidate 1.

## Limitations

- The locked eval split is visible to implementers in the repository. It is a
  locked regression set, not a blinded experiment.
- Recorded proposals are hand-authored stand-ins for model output. This is
  not model qualification and makes no claim about open-ended language
  accuracy, GPU savings, quantum advantage or hardware results.
- Nano admission cannot know the requester's task; task binding is enforced
  by the independent verifier acting for the host.
- Benchmark base: reviewed Nano `cb6ea884` plus the #55/#56/#57 fixes from
  PRs #68/#69/#70 rebased locally; those PRs are not yet merged on main.
