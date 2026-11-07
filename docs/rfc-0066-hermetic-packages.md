# RFC 0066: hermetic packages and pure typed helpers

Status: **first executable subset implemented**, broader data types proposed.
Tracks [issue #66](https://github.com/AetherAI3/Nano/issues/66).

## Problem and boundary

Agent programs need to reuse evidence qualification and resource calculations.
The ordinary compiler accepts one strategy followed by EOF; its input remains
a string, with no file reads. An opt-in `compile_package(manifest_path,
lock_path=...)` build API accepts an explicit closed set of local files.
It never downloads packages, searches parent directories, refreshes pins,
reads a clock/RNG, or invokes a model, QPU or external action.

## Shipped syntax: the smallest useful subset

Imports precede either a strategy or a pure module. Import names resolve only
through the manifest. Only a module's own functions are exported; imports do
not re-export transitively. Names are unqualified in this first subset;
collisions are errors instead of shadowing or precedence rules.

```text
import Evidence
module Task {
  pure fn ready(count: int, verified: bool, tokens: int): bool {
    sufficient(count, verified) and tokens >= 100
  }
}
```

```text
module Evidence {
  pure fn sufficient(count: int, verified: bool): bool {
    count >= 2 and verified
  }
}
```

```text
import Task
strategy AgentTask {
  input evidence_count: int
  input evidence_verified: bool
  input model_tokens: int
  every 1m {
    if ready(evidence_count, evidence_verified, model_tokens) { execute() }
    else { pause() }
  }
}
```

Minimal grammar (package source format 0.1.0):

```text
unit       = import* (strategy | module)
import     = "import" IDENT
module     = "module" IDENT "{" function* "}"
function   = "pure" "fn" IDENT "(" parameters? ")" ":" scalar "{" expression "}"
parameters = IDENT ":" scalar ("," IDENT ":" scalar)*
scalar     = "int" | "float" | "bool" | "string"
```

Every parameter and result is explicitly typed, with **exact** scalar type
matching at calls and returns, without implicit widening or series lifting.
Expressions support literals, parameter references, arithmetic, comparisons,
Boolean operators and calls to visible pure helpers. No loops, recursion,
mutable locals, record construction, host calls, indicators, historical reads
or captured strategy inputs are allowed inside definitions. Every definition
is checked even when unused. Forward function references are supported;
recursive strongly connected call paths are rejected at the call token.

Expansion substitutes scalar argument expressions in lexical scope. This is
an explicit expression-substitution contract, equivalent to writing that
expanded expression inline; it does not introduce call-by-value evaluation or
memoization. Unused arguments need not evaluate. Direct host/indicator calls
inside helper arguments are rejected, preventing dropped or duplicated host
effects. Bind such results with the existing strategy `let` before passing a
scalar value or field. Arguments and return values cannot carry implicit series.

## Manifest, pins and admission

The example is in `examples/agent-package/`, with a checked-in inline control.
Both metadata filenames are explicit API inputs; there is no default search.

```json
{
  "nanoPackageVersion": "0.1.0",
  "entry": "agent.nano",
  "modules": {"Evidence": "evidence.nano", "Task": "task.nano"},
  "effects": ["intent.emit", "log.append"]
}
```

```json
{
  "nanoLockVersion": "0.1.0",
  "dependencies": {
    "agent.nano": "sha256:<64 lowercase hex digits>",
    "evidence.nano": "sha256:<64 lowercase hex digits>",
    "task.nano": "sha256:<64 lowercase hex digits>"
  }
}
```

The compiler verifies SHA-256 over exact UTF-8 file bytes, including entry and
unused manifest members. The lock must contain exactly the manifest sources.
Changes require an intentional edit to the pin; compilation never repairs it.
Duplicate JSON keys and unknown metadata fields are errors. Paths must be
canonical relative POSIX `.nano` paths within the manifest directory: no
absolute paths, URLs, `..`, drive names, backslashes or case-folded duplicates.
Resolved symlinks/junctions escaping the root are rejected. The build API is
not an OS sandbox: the trusted host must hold inputs stable during a build.
Each file is read once, bounded, then parsed from that verified byte snapshot.

Compiler-owned ceilings are fixed: 32 manifest sources, 262,144 total source
bytes, 262,144 bytes per metadata file, 16,384 tokens per source, 128 function
definitions, depth 32 for imports/calls/all source and expanded expression trees/syntax delimiters,
8,192 visited expansion objects, 8,192 expanded tree occurrences and 8,192 emitted IR nodes. A separate internal
expansion traversal depth ceiling is 128. These limits cannot be increased by
source or manifest. The IR emitter checks *during* emission, including repeated
parameter trees, before an exponential expansion can allocate unbounded nodes.
Occurrence counts are also computed before type-checking the expanded tree,
so shared parameter subtrees cannot cause exponential checker traversal.
Compiler stack exhaustion also becomes a positioned depth-budget diagnostic.
Limits are admission ceilings, not execution cost estimates.

Stale imported pins point at the importing module-name token. Duplicate symbols,
bad arity/types, import cycles and recursive calls identify the relevant source
path, 1-based line and column. Metadata errors without a concrete token use the
document start; JSON syntax errors retain the JSON decoder's position.
`PackageResourceError` is a `PackageError`/`NanoCompileError` with code
`RESOURCE_LIMIT`, category `resource`, path, line and column; other package
diagnostics carry `PACKAGE_ERROR` unless wrapping a structured compiler error.
Wrapped parser/type errors preserve their original code, category, expected/
actual/hint and resource/limit fields while adding the dependency path.
The package adapter supplies the bounded parser's default limits when available,
and retains compatibility with the original parser constructor. The common
parser/error implementation is unchanged.

## IR, canonical identity and source maps

There is **no new executable opcode or executable IR version**. Typed helpers
are checked before expansion, expanded hygienically, then checked again and
lowered to existing Nano IR 1.0.0. Its existing loader validates the result.
The emitted executable has exactly the same module hash and behavior as the
equivalent inline strategy. The baseline v0.1.0 compiler remains byte-stable.

`CompiledPackage.to_dict()` is a distinct build artifact:

```text
type: NanoPackage
nanoPackageVersion: 0.1.0
compiler: nnc-package/0.1.0 + executable compiler version
manifest: explicit source/effect contract
lock: independently versioned dependency byte hashes
module: independently versioned executable NanoModule
sourceMap: node id -> ordered [outer call, inner call, definition] locations
packageHash: SHA-256 of canonical artifact without packageHash
```

Canonical encoding sorts object keys with compact JSON separators and ASCII
escapes; no absolute paths or timestamps enter the artifact. Relocation and
metadata whitespace/key order do not change identity. Source byte edits,
pins, manifest effect grants, compiler versions, executable and source-map
changes do. Unused dependencies are deliberately included. `moduleHash`
continues to identify executable behavior, excluding provenance, while
`packageHash` commits to the complete build inputs and build evidence.
`sourceHash` retains its existing meaning: entry source bytes only. It must not
be mistaken for the package identity. All executable nodes get a location;
expression nodes originating in dependencies retain the ordered expansion
stack. Substituted parameter roots also retain their parameter-read and actual
argument location. Structural nodes without an expression use the entry strategy header.
The package envelope is build evidence, not an alternate executable loader:
hosts pass its validated `module` to the existing VM.

## Effects and host governance

Pure helper effects are the empty set. Their composition is set union, still
empty. The strategy's effects are inferred by the existing type checker and
must be a subset of the explicit package `effects` grant. The existing v1
loader separately checks the executable's declared effects and tier.
Imports cannot introduce inference, evidence reads, QPU work, an escalation,
an intent or an undeclared capability. Evidence is supplied by the host as
typed scalar fields in this milestone. `execute()` proposes an EXECUTE intent;
it does not actuate anything. The host still owns DecisionGate authorization,
model execution, evidence authenticity, reservations and device execution.

## Proposed next data-type subset (not shipped)

After scalar helpers prove useful, add reusable immutable records first:

```text
record Evidence { count: int verified: bool }
pure fn sufficient(e: Evidence): bool { e.count >= 2 and e.verified }
enum Status { qualified rejected }
pure fn qualify(e: Evidence): result<Status, string> { ... }
```

Records would introduce explicit named field schemas and host-input validation,
with field access lowered to typed `record.field` only after validation. New
record construction, enum tags, result constructors and exhaustive matching
need a separately reviewed executable IR revision with loader/VM semantics;
they are not currently accepted. Nominal identities should commit to the
defining module, name and canonical schema, avoiding accidental structural
aliasing. Enums should use closed tags and results a closed `ok`/`err` sum,
with bounded fields and exhaustive matching. Do not silently reinterpret the
existing reasoning-signature `record<Signature>` as a general record.

Effectful functions are also future work: any future signature must carry an
explicit effect row, composition must union all call-path effects, and admission
must compare the result to the host grant before execution. This RFC provides
no new permission to call inference or quantum services.

## Compatibility and migration

Existing `parse`, `check_source`, `compile_source`, `compile_module` and
`compile_to_dict` entry points retain their single-source behavior. They do not
accept imports, read manifests or emit package envelopes. No existing source
or fixture migration is required. Package callers explicitly choose the new
API, add pins and consume `CompiledPackage.module` as existing v1 IR.

The separate Intent frontend retains its own schema and version, and cannot
load package source or package envelopes. Project/query records, Intent
documents, baseline strategy IR, executable v1 modules and package build
evidence remain independently versioned artifacts. A future executable IR
revision must name its new version and loader migration; it cannot be smuggled
into this package version.

## Resource dimensions and deferred work

Resource helpers must preserve units. Shots are a count; QPU seconds are device
execution duration; queue seconds are waiting duration; model tokens are token
counts; measured joules are host/device energy telemetry. They are separate
quantities, never interchangeable or implicitly converted. Compiler QUBO
objective energy is a mathematical score, **not measured joules**. The example's
`model_tokens` threshold is a caller-supplied token count, not an energy or
hardware measurement. This subset has no dimension type system; callers must
name and validate units explicitly. A future record schema can carry them.

Deferred: `nano init/check/test/fmt`, registry/network resolution, portable host
clients, records/enums/results execution, and task-graph/fanout syntax. Future
fanout must compute per-child and shared-pool limits before host execution and
reuse Predator qualification/reservation rules. This is not an unbounded agent
runtime. No paid inference, simulator dependency or hardware is needed to test
this milestone.

## Evidence

`tests/test_package.py` proves two local modules share an evidence helper;
imported and inline programs have identical graph hashes and VM results, with
positive and negative controls for evidence count, verification and token budget.
It covers deterministic repeat builds, relocation, transitive changes, positioned
diagnostics, missing pins, ambient capture, effect injection, recursion, cycles,
path escapes and bounded expansion. Run the focused file and full suite plus
the contribution checker before merging. Repository CI supplies the exact-head
integration evidence; a local passing run alone is not a merged issue closure.
