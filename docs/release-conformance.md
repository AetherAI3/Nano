# Offline release conformance

Status: first milestone of [#88](https://github.com/AetherAI3/Nano/issues/88)
(roadmap [#67](https://github.com/AetherAI3/Nano/issues/67)): a local harness
and a versioned evidence schema. There is no workflow integration yet. CI
workflows are unchanged.

`scripts/conformance/release_conformance.py` tests one exact commit: its
source export and its core wheel. It writes machine-readable evidence that
cannot report green unless every required suite actually ran and passed. The
harness is stdlib-only. It makes no network, provider, model or IBM calls and
reads no credentials.

```text
python scripts/conformance/release_conformance.py manifest --out manifest.json
python scripts/conformance/release_conformance.py run --out-dir OUT [--manifest manifest.json]
python scripts/conformance/release_conformance.py verify OUT/evidence.json
```

`OUT` must be a new or empty directory outside the repository. Useful options:

* `--python` sets the test interpreter (default: the running one).
* `--wheel-python` sets an interpreter that has setuptools.
* `--node` (repeatable, up to two runtimes) and `--no-node` control the Node runtimes.
* `--suite ID` (repeatable) selects suites.
* `--merge-tree TREE` names a proposed merge tree that must equal the tested tree.
* `--atlas-checkout` and `--predator-checkout` supply the mode 3 directories.
* `--npm-cache` names an npm cache that already holds the TypeScript client's locked devDependencies, so `npm ci --offline` can install them. Without it the optional TypeScript rows are `NOT_EXECUTED`.
* `--timeout` is the timeout per suite.
* `--gate core|release` chooses which gate decides the exit code.

The `run` exit code is 0 only when the chosen gate is `PASS`. Other codes:

* 1: the gate is `FAIL` or `INCOMPLETE`, or `verify` found problems.
* 2: usage error.
* 3: the harness could not start, for example because the output directory is inside the repository.

## What a run does

1. Records `HEAD` (full 40-hex commit and tree) and `git status --porcelain`
   (tracked changes and untracked, non-ignored files).
2. Checks out the exact commit into `OUT/work/src`, which is where tests run.
   This is a detached checkout in a shared clone with `core.autocrlf=false`
   and no remote, because some tests resolve `HEAD` and need a real git
   checkout. Separately, it exports the committed bytes with
   `git -c core.autocrlf=false archive` into `OUT/work/wheel-src` for the
   wheel build. Tests and wheels therefore never see the working tree, its
   line-ending conversion, or its build outputs.
3. Observes the test interpreter, its dependency versions (`jsonschema`,
   `numpy`, `pytest`, `qiskit`, `qiskit-aer`), the optional-SDK pins declared
   by the `quantum` extra in `pyproject.toml`, Node versions, the wheel
   builder, and any caller-supplied checkouts.
4. Builds the manifest and admits the run. If a manifest was supplied, every
   pinned field must equal what was observed (see below).
5. Confirms that `nano` and `nano_qsearch` import from the export, not from an
   editable install or the current directory.
6. Runs the selected suites, re-checks cleanliness, and evaluates the results.

Children get a scrubbed environment. Only the names in `ENV_ALLOWLIST` pass
through, such as `PATH`, `SYSTEMROOT` and `USERNAME`. `HOME`, `USERPROFILE`,
`TEMP` and `TMP` point at empty directories under `OUT`, so user
configuration and credential files are not found. `PYTHONHASHSEED=0`,
`PYTHONNOUSERSITE=1` and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` are set. The
evidence lists environment variable **names** only, never values.

## Input manifest (`nano.release-conformance.manifest.v1`)

| Field | Pins |
| --- | --- |
| `source` | `commit` and `tree` (full 40-hex), optional proposed `merge_tree` |
| `interpreter` | implementation, version, system, machine, platform string |
| `dependencies` | exact versions in the test interpreter (`null` when absent) |
| `sdk_pins` | `quantum` extra pins from `pyproject.toml` (`qiskit`, `qiskit-aer`) |
| `node` | version for each runtime slot |
| `wheel_builder` | role (`explicit`, `test-interpreter`, `base-interpreter`) plus Python, pip, setuptools and wheel versions |
| `checkouts` | mode 3 roles with commit, tree and a dirty flag (paths are never pinned) |
| `selected_suites` | suite IDs that run; every other declared suite is recorded `NOT_EXECUTED` |
| `suites` | every declared suite: mode, kind, required flag, capability, test files and fixtures with SHA-256, fixture patterns, required test IDs |

### Admission refusals

A refusal means no suite runs. Every suite is recorded as `NOT_EXECUTED` and
both gates are `FAIL`. Admission is refused when:

* the commit or tree is not full hex;
* the pinned commit is unknown locally, or its tree differs from the pinned tree;
* the tested tree differs from the pinned tree;
* any interpreter, dependency, pin, Node, builder, checkout or selection field differs (a dependency difference is reported as `dependency drift`);
* any suite's files, fixtures or requirements differ, including a swapped fixture SHA-256;
* the selection names a suite the source does not declare;
* the proposed merge tree is not the tested tree;
* the checkout is dirty;
* an import resolves outside the export.

### Exact commit versus equivalent tree

`source.identity` is `exact_commit` when the tested commit is the pinned one.
It is `equivalent_tree` when the commits differ but the pinned commit exists
locally with the same tree as the tested commit. The evidence records both
commits and both trees.

## Modes, suites and capabilities

| Suite | Mode | Required | What runs |
| --- | --- | --- | --- |
| `core.contribution` | core | yes | `scripts/check_contribution.py --all` |
| `core.cli_help` | core | yes | `nano --help`, `nano package --help` under `python -I` with optional SDKs blocked |
| `core.import_without_optional_sdks` | core | yes | imports core `nano`/`nano_qsearch` modules under `python -I`; `qiskit`, `qiskit_aer`, `qiskit_ibm_runtime`, `numpy`, `scipy`, `rustworkx`, ATLAS and Predator imports raise |
| `core.pytest` | core | yes | every `tests/test_*.py` not claimed by another suite (the list is pinned in the manifest) |
| `wheel.build` | core | yes | `pip wheel --no-deps --no-build-isolation --no-index --no-cache-dir` from the export, `SOURCE_DATE_EPOCH` = commit time, user pip config ignored |
| `wheel.contents` | core | yes | every `.py` under `nano/` and `nano_qsearch/`, every `.nano`/`.json` under `nano/{examples,library,project,quantum,intent}`, `nano_qsearch/schemas/*.json`, the six native-bridge schemas and fixtures; no `tests/` or `scripts/` |
| `wheel.import_without_optional_sdks`, `wheel.cli_help` | core | yes | as the core checks, with only the wheel file on `sys.path`, cwd outside the repository |
| `wheel.native_fixture` | core | yes | from the wheel: parse the frozen known-bits native artifact against its golden digests, refuse a wrong retained digest, find all native schemas |
| `wheel.package_cli` | core | yes | from the wheel, in a copied `examples/agent-package`: `nano package check --json` and `nano package test --json` must succeed with every case passing and `actionsExecuted: 0` |
| `cap.package_cli.pytest` | core | yes | `tests/test_package_cli.py`, `test_package_cases.py`, `test_package_test_cli.py` |
| `cap.records.pytest` | core | yes | `tests/test_records*.py` |
| `cap.lifecycle_wire.sha256sums` | core | yes | every file under `protocol/qsearch-lifecycle/v1/` is listed in its `SHA256SUMS`, sorted, and matches; nothing listed is missing |
| `cap.lifecycle_wire.regen_check` | core | yes | `python -B protocol/qsearch-lifecycle/v1/fixtures/regen.py --check` exits 0 (fixtures regenerate byte for byte) |
| `cap.lifecycle_wire.pytest` | core | yes | `tests/test_qsearch_lifecycle_wire_contract.py`, `tests/test_qsearch_lifecycle_client.py`; regen, SHA256SUMS, signing-vector, history, error-mapping and negative-expectation tests are required IDs; the 26 strict xfails (contract refusals the Python client still accepts) count as executed, and an XPASS fails the suite |
| `cap.ts_client.build` | core | no | in `sdk/typescript/qsearch-lifecycle`: `npm ci --offline --ignore-scripts` from `--npm-cache` (the installed TypeScript must equal the lockfile pin), then `tsc -p tsconfig.json` |
| `cap.ts_client.node.primary` / `.secondary` | core | no | the package's own `node --test` (default patterns) against the built `dist/`, with the JUnit reporter, one row per runtime |
| `cap.proposal_benchmark.pytest` | core | yes | `tests/test_proposal_benchmark*.py`, which replay the committed reports byte for byte |
| `qiskit.local_compile` | qiskit | yes | QUBO prepared-artifact, known-bit adapter and provenance tests; `test_quantum_contract.py` and `test_quantum_adapter.py` must pass in full |
| `qiskit.native_bridge` | qiskit | yes | the native contract, artifact, round-trip and service tests, with 17 required test IDs (see below) |
| `cross_repo.fake_provider` | cross_repo | no | ATLAS/Predator fake-provider tests, only with caller-supplied checkout directories |

A capability's status comes from its suites:

| Capability | Issue | Required | Suites |
| --- | --- | --- | --- |
| `package_cli` | #84 | yes | `cap.package_cli.pytest`, `wheel.package_cli` |
| `records` | #85 | yes | `cap.records.pytest` |
| `lifecycle_wire` | #86 | yes | `cap.lifecycle_wire.sha256sums`, `.regen_check`, `.pytest` |
| `ts_client` | #86 | no | `cap.ts_client.build` and the Node rows |
| `proposal_benchmark` | #87 | yes | `cap.proposal_benchmark.pytest` |
| `qubo_artifact` | #88 | yes | `qiskit.local_compile` |
| `native_bridge` | #61 | yes | `qiskit.native_bridge`, `wheel.native_fixture` |
| `cross_repo_lifecycle` | #88 | no | `cross_repo.fake_provider` |

The native bridge's required IDs cover these behaviours:

* phrase or builder → plan → prepared artifact → local service → verified typed result;
* 400 recipients mapped onto 1,200 PUBs;
* refusal of substituted, reordered, missing or duplicate outputs;
* refusal of hardware and accounting claims;
* stale evidence digests;
* default-off routes;
* restart and lost-acknowledgement holds;
* the actual local runtime over HTTP;
* the dependency-free caller check.

If any of these tests is absent, skipped or failing, the native bridge cannot
pass.

Mode 2 runs only when the test interpreter's `qiskit` and `qiskit-aer` equal
the `quantum` extra's pins. Otherwise its suites are `BLOCKED`, the required
capabilities `qubo_artifact` and `native_bridge` are `INCOMPLETE`, and the
release gate is `INCOMPLETE`. The core gate can still pass.

Mode 3 never discovers checkouts and never reads credentials. The caller passes
directories with `--atlas-checkout` and `--predator-checkout`. They reach the
tests only through the existing `NANO_QSEARCH_ATLAS_PATH` and
`NANO_QSEARCH_PREDATOR_PATH` names. A checkout must be a clean git checkout
whose commit and tree are pinned. Without one, the mode is `NOT_EXECUTED` with
a reason and listed in `gate.optional_not_executed`, never silently green.

## Suite rules

A pytest or Node suite **fails** on any of the following:

* an empty collection (pytest exit 5, or no test cases);
* any failure or error;
* every collected test skipped;
* a nonzero exit code;
* a missing or unparseable JUnit report after the process ran;
* a required test ID that is absent, or any parametrization of it that did not pass;
* a declared test file that is missing, or a fixture pattern that matches nothing (both are checked before running);
* a timeout.

A child that could not be launched, or a pytest startup or usage error (exit
3 or 4) without a report, is `NOT_EXECUTED`. Command suites fail on a nonzero
exit or any unmet expectation. `BLOCKED` means a pinned prerequisite is absent.

Each capability is then classified:

* `FAIL` if any of its suites failed.
* `PASS` when every required suite passed. An optional capability passes when every suite that ran passed and at least one ran.
* `INCOMPLETE` otherwise.

The two gates follow from those statuses:

* **core**: all required core-mode suites.
* **release**: also requires every required capability and no failing suite, including optional suites that ran.

Source problems fail both gates. These are an admission refusal, a dirty
checkout, a change during the run, or import leakage.

## Evidence (`docs/release-conformance.schema.json`)

`OUT/evidence.json` is `nano.release-conformance.evidence.v1`. It has three parts:

* `canonical`: the input manifest and its SHA-256, the admission, the source
  identity (`tested_*`, `pinned_*`, `identity`, `merge_tree_match`,
  `dirty_before`, `dirty_after`), environment names, artifacts (wheel SHA-256,
  member digest, builder), suite rows, capabilities and the gate.
* `canonical_sha256`: the SHA-256 of the canonical JSON (sorted keys,
  compact, ASCII, trailing LF). `OUT/evidence.canonical.json` holds exactly
  those bytes.
* `telemetry`: everything that legitimately varies between identical runs.
  That is timestamps, per-suite durations, absolute paths, and the raw
  SHA-256 of every JUnit file, log and artifact under `OUT`.

Each suite row records the following:

* the status and reasons;
* the command, with absolute paths replaced by `{python}`, `{src}`, `{out}`, `{node}`, `{npm_cli}`, `{npm_cache}` and `{wheel_python}`, and inline check programs shown by digest;
* the exit code;
* counts (`collected`, `executed`, `passed`, `failed`, `error`, `skipped`, `xfailed`);
* skip reasons with counts;
* `outcomes_sha256` over the sorted `(test id, outcome, reason)` rows;
* required-test results;
* JUnit and log paths relative to `OUT`.

Timings, tracebacks and hostnames never enter the canonical hash.

`verify` checks a stored document for internal consistency:

* it recomputes `canonical_sha256` and the manifest hash;
* it checks that every `PASS` row has a zero exit code, executed tests, no failures and met requirements;
* it re-derives the capabilities and the gate from the rows.

A hand-edited status therefore cannot verify.

Runtime run-log serialization is not defined here. Byte stability of the
runtime log remains [#10](https://github.com/AetherAI3/Nano/issues/10).

## Reproducibility and platform matrix

Two fresh runs on the same commit and environment must produce byte-identical
`evidence.canonical.json`. The wheel is built with `SOURCE_DATE_EPOCH` set to
the commit time, so its SHA-256 is part of that canonical identity.
`tests/test_conformance_faults.py` checks repeatability on a synthetic
repository on every test run.

| Platform / interpreter | Status |
| --- | --- |
| Windows 10, CPython 3.13.5, qiskit 2.3.1 / qiskit-aer 0.17.2, Node v24.18.0 and v22.19.0, TypeScript 5.8.3 | local reference platform for this milestone. The acceptance check is two fresh full runs with byte-identical `evidence.canonical.json`; the run evidence records the result. |
| Linux, CPython 3.10–3.13 | **UNVERIFIED**: not run for this milestone |

## Fault-injection controls

`tests/test_conformance_faults.py` drives the real harness over a tiny
synthetic git repository and synthetic wheels. None of the following can
produce a passing required gate:

* a wrong or short SHA;
* a changed tree;
* a dirty source before the run, or a change during it;
* dependency drift;
* a swapped fixture hash;
* manifest mismatches (interpreter, schema, undeclared selection, removed suite, wrong merge tree);
* a missing test file or fixture;
* all-skipped and empty suites;
* a required test that is skipped;
* a pinned SDK that is unavailable;
* broken wheel imports, wheels that import an optional SDK, wheels without the package, and wheels missing documented members.

A positive control proves that a clean synthetic release passes and repeats
byte for byte. `tests/test_conformance_release.py` covers the evaluation
rules, JUnit parsing, `verify`, the stdlib-only constraint, and that every
declared suite file and required test ID exists in this source.

## Not in this milestone

* Workflow integration. A later PR may run this harness on an already authorized self-hosted route, with serialized execution and artifact upload. Runner, credential and security settings are out of scope.
* Actual CI runner, job and run identities (`environment.runner.ci_run` stays `null` locally).
* Pytest itself has no installed-wheel hook for the package CLI. The harness checks the wheel directly in `wheel.package_cli`.
