# Package CLI: `nano package check | build | test`

Status: **contract frozen for issue #84** (roadmap #67). Implements the CLI
orchestration RFC 0066 deferred. It adds no language semantics, registry
resolution, lock refresh or formatter. `nano package init` was added in a
separate follow-up (see its section); formatting remains deferred.

Every operation is a thin adapter over the shipped public API:
`compile_package(manifest, lock_path=lock)` builds; the existing v1 VM
(`nano.runtime.vm.run_module`) evaluates fixture frames. The CLI never edits
compiler/package internals, never searches parent directories, never reads
environment variables or credentials, never opens a network socket, and never
imports model, Qiskit or QPU code.

## Arguments

```text
nano package check --manifest PATH --lock PATH [--json] [-v]
nano package build --manifest PATH --lock PATH [-o PATH] [--module-output PATH]
nano package test  --manifest PATH --lock PATH --cases PATH [--json]
nano package init  --directory PATH
```

`--manifest` and `--lock` are **required on every build operation**; there is no
default filename and no discovery. A missing required argument or a bare
`nano package` is an argparse usage error (exit 2).

## Exit codes

Reuses the existing `nano` CLI table.

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | the package, fixture file or a case was rejected (diagnostics printed) |
| 2 | invalid invocation (argparse, or an output path that names an input) |
| 3 | an input could not be read, or an output could not be written |
| 130 | interrupted |

I/O classification: a `PackageError` whose `__cause__` or `__context__` is an
`OSError` (missing/unreadable manifest, lock or pinned source; a directory
given as a file) is reported as `IO_ERROR`/`io` and exits 3. Everything else
the API rejects exits 1 with its own code (`PACKAGE_ERROR`, `RESOURCE_LIMIT`,
or a wrapped compiler code such as `TYPE_ERROR`). An unreadable `--cases` file
exits 3; a malformed one exits 1.

## Output framing

* stdout carries only the command's result. Progress, notes and text
  diagnostics go to stderr.
* Every JSON document (check `--json`, test `--json`, build artifacts) is
  canonical: `json.dumps(value, sort_keys=True, separators=(",", ":"),
  ensure_ascii=True)` encoded as ASCII, followed by exactly one `\n` byte.
  It is written through the byte sink (`Console.emit`) or a binary file, so
  Windows never rewrites the framing byte to `\r\n`.
* Exactly one parseable document per invocation in JSON modes, success or
  failure. On failure in `build`, stdout is empty.

Text diagnostics use the existing editor form with the package-relative path
the API reports (stable across working directory and relocation):

```text
task.nano:1:8: error: module 'Missing' is not explicitly pinned
```

## `check`

Builds in memory and discards the artifact. Silent on success; `-v` prints
`ok packageHash=<sha256:...> moduleHash=<sha256:...>` to stdout.

`--json` document (`type: NanoPackageCheck`, `schemaVersion: "1.0.0"`, a
version domain independent of compiler diagnostics `1.0.0`, package `0.1.0`
and IR `1.0.0`):

```json
{"compiler":{"irCompilerVersion":"1.0.0","name":"nnc-package","packageVersion":"<installed nano>","version":"0.1.0"},
 "diagnostics":[],"moduleHash":"sha256:...","packageHash":"sha256:...",
 "schemaVersion":"1.0.0","success":true,"type":"NanoPackageCheck"}
```

On failure `success` is false, both hashes are `null`, and `diagnostics`
holds the API's first rejection:

```json
{"category":"package","code":"PACKAGE_ERROR","message":"module 'Missing' is not explicitly pinned",
 "path":"task.nano","severity":"error","span":{"end":{"column":8,"line":1},"start":{"column":8,"line":1}}}
```

`message` is the API message without its `path: ` prefix. `category` is the
error's own category, except `PACKAGE_ERROR` reports `package` (the API's
base class inherits `syntax`, which would misclassify metadata errors) and
I/O reports `io` with `span: null`. The document never contains the manifest
or lock path as supplied, absolute paths, timestamps or environment data, so it
is byte-identical across working directories and relocation.

## `build`

Writes the existing `CompiledPackage.to_dict()` envelope, canonical-encoded as
above, to `-o PATH` or stdout. Meanings are unchanged: `packageHash` commits to
the canonical envelope without `packageHash`; `module.moduleHash` identifies
executable behaviour; `module.sourceHash` is entry source bytes only;
`sourceMap` maps node ids to ordered expansion locations.

`--module-output PATH` additionally writes the validated executable
`module.to_dict()` (canonical, LF) — the same value hosts pass to the VM.

Output safety: an output path that resolves to the manifest, the lock, or any
manifest source is refused with exit 2 before any write. Files are written
atomically (temporary sibling, then `os.replace`), so a failed build leaves no
partial artifact. Nothing else is written.

## `test --cases PATH`

Builds the package, then evaluates a strict local fixture file against the
validated module with `run_module(module, frame, provider=None)`.

Admission happens **before any case runs**. The module is refused with
`UNSUPPORTED_HANDLER` (exit 1) unless its tier is `nano`, its effects are a
subset of `{"intent.emit", "log.append"}`, and it contains no inference,
escalation, route or signature node. No provider, effect handler, clock, RNG,
model or QPU can be supplied; there is no fixture field for one.

### Fixture schema `nanoPackageCasesVersion: "1.0.0"`

```json
{
  "nanoPackageCasesVersion": "1.0.0",
  "cases": [
    {
      "name": "sufficient evidence proposes execute",
      "frame": {"timestamps": [0], "signals": {"evidence_count": [2], "evidence_verified": [true], "model_tokens": [100]}},
      "expect": {"proposedIntents": [{"intent": "EXECUTE", "timestamp": 0}]}
    }
  ]
}
```

Strictness (each violation is `FIXTURE_ERROR`, exit 1, no case runs):

* Duplicate JSON keys, unknown or missing keys at every level, `NaN`/
  `Infinity`, or a top-level version other than `1.0.0`.
* `cases` must be a non-empty list (an empty set is a failure, never a pass)
  of at most 256 cases; `name` is a non-empty unique string.
* `frame.timestamps` is a list of 1..4096 integers, strictly increasing.
  `frame.signals` maps names to lists of the same length whose items are JSON
  numbers or booleans. `MarketFrame` construction errors are fixture errors.
* `expect` has exactly one key: `proposedIntents` (a list of intent
  dictionaries exactly as `Intent.to_dict()` emits them, compared by order and
  value) or `runtimeError` (a non-empty substring of the VM's runtime
  rejection message, e.g. a missing input signal).
* There is no skip, `only`, provider, handler or `executed` field. Because no
  case can be skipped, an all-skipped run cannot exist; any unknown key fails.
* The file is read once with a 1 MiB byte ceiling.

### Intent versus action

A fixture asserts **proposed** intents. The runner never executes an action:
`execute()` lowers to an `EXECUTE` intent value in `ModuleResult.intents`, the
same proposal the host's DecisionGate would later authorize. Reports call the
field `proposedIntents` and every report carries `"actionsExecuted": 0`, a
constant asserting the runner has no actuator.

### Results

A case passes only when its observation equals its expectation. A mismatch,
an unexpected runtime error, or a missing expected runtime error is a failure.
Exit 0 only when every case passed and at least one ran.

Text mode prints `PASS <name>` / `FAIL <name>: <reason>` lines and a final
`<passed>/<total> passed` line to stdout. `--json` emits one
`type: NanoPackageTestReport`, `schemaVersion: "1.0.0"` document:

```json
{"actionsExecuted":0,"cases":[{"name":"...","observed":{"proposedIntents":[]},"status":"pass"}],
 "diagnostics":[],"failed":0,"moduleHash":"sha256:...","packageHash":"sha256:...","passed":1,
 "schemaVersion":"1.0.0","success":true,"total":1,"type":"NanoPackageTestReport"}
```

Build or fixture rejection yields the same document shape with `success:false`,
`cases: []`, totals 0 and the diagnostic.

## `init --directory PATH`

Creates one minimal, already-pinned example package so a new user can run
`check`, `build` and `test` immediately. It is a fixed template, not a
generator: it never reads, discovers, repins or rewrites any existing project.

* `--directory` is required. The destination must **not exist**, or be an
  **existing empty directory**. Anything else (a non-empty directory, a file,
  or a symlink/junction at that path) is refused with exit 2 before any write.
  The parent must already exist; a missing parent is an I/O failure (exit 3).
  No parent search, no `-p`-style creation of intermediate directories.
* It writes exactly five files, with LF line endings and fixed bytes:
  `nano.json`, `nano.lock`, `agent.nano`, `rules.nano` and `cases.json`. The
  lock pins the two sources by SHA-256 of those exact bytes, computed from the
  template, never by reading files back from disk. Repeated runs into fresh
  destinations produce byte-identical files and the same `packageHash`.
* The scaffold is written to a temporary sibling directory and renamed into
  place when the destination does not exist. For an existing empty directory,
  each file is created exclusively (it must not appear concurrently) and any
  file already created is removed if a later one fails. A failed `init` leaves
  no partial scaffold. The destination is re-checked before every file, but
  like the package build API this is not an OS sandbox: a concurrent local
  process that can rename entries in the parent could still race the check.
* stdout stays empty; one `created <PATH> (5 files)` note goes to stderr.
  Exit 0 on success.

The scaffold's entry imports one pure helper module, declares only
`intent.emit` and `log.append`, and its `cases.json` has a passing positive
and negative case, so `nano package test` on a fresh scaffold exits 0.

## Acceptance evidence mapping

| Issue requirement | Where it is proved |
| --- | --- |
| CLI and API byte-identical envelope, hashes, source map | `tests/test_package_cli.py` |
| other cwd, relocation, metadata reordering, LF framing, repeat builds | `tests/test_package_cli.py` |
| stable positioned diagnostics and nonzero exits | `tests/test_package_cli.py` (parametrized) |
| path escapes, no network/model/QPU/env reads, read-only | `tests/test_package_cli.py` |
| fixture strictness, handlers, intent vs action, empty sets | `tests/test_package_cases.py` |
| single-source `check`/`compile`, v0.1.0 and v1 fixtures unchanged | existing `tests/test_cli.py`, `tests/test_conformance.py`, goldens |
| installed wheel outside the repository, no quantum extras | PR evidence (fresh venv, copied example) |
| `init` only into a new/empty destination, refuses overwrite, never rewrites pins | `tests/test_package_init.py` |
