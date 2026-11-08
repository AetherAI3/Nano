# RFC 0085: bounded immutable records (records-only milestone)

Status: **records-only milestone implemented** for the opt-in package API.
Amends [RFC 0066](rfc-0066-hermetic-packages.md). Tracks
[issue #85](https://github.com/AetherAI3/Nano/issues/85); part of #67.

Enums, results, exhaustive matching, record equality, record history
(`record[1]`), records in single-source programs and records passed to
`infer` are **deferred** to separately scoped issues. No placeholder opcode,
type or permissive fallback exists for any of them.

## Problem

Scalar pure helpers force evidence to travel as parallel arguments
(`count, verified, tokens`). A reusable, explicitly typed, immutable record
lets one evidence shape be declared once, imported by several pure helpers and
validated once at the host boundary, without adding host dispatch, inference,
loops, mutation or a general object model.

The existing reasoning-signature type `record<Signature>` is **not** a general
record and is not reinterpreted: it is still produced only by `infer`, still
read through `record.field`, and still cannot be written as an annotation.

## Source subset (package source only)

Records are accepted only by `compile_package`. `parse`, `check_source`,
`compile_source`, `compile_module` and `compile_to_dict` keep their grammar and
bytes. Declarations live in package modules; the entry strategy may use
imported record types.

```text
module Evidence {
  record Evidence { count: int verified: bool source: string }
  pure fn sufficient(e: Evidence): bool { e.count >= 2 and e.verified }
  pure fn make(count: int, verified: bool): Evidence {
    Evidence(count: count, verified: verified, source: "local")
  }
}
```

```text
import Evidence
strategy Agent {
  input evidence: Evidence
  every 1m { if sufficient(evidence) { execute() } else { pause() } }
}
```

Grammar delta (package source format 0.1.0 is extended additively; sources
that use it are rejected by earlier compilers with a positioned syntax error):

```text
module     = "module" IDENT "{" (record | function)* "}"
record     = "record" IDENT "{" field+ "}"
field      = IDENT ":" fieldType
fieldType  = scalar | IDENT                       (a visible record name)
function   = "pure" "fn" IDENT "(" parameters? ")" ":" type "{" expression "}"
type       = scalar | IDENT                       (a visible record name)
primary   += IDENT "(" IDENT ":" expr ("," IDENT ":" expr)* ")"   (construction)
postfix   += "." IDENT                             (field access)
```

Rules, all checked for every declaration even when unused:

- **Names.** Records share the module export namespace with functions. A
  module exports its own records; imports never re-export. Any collision
  among records, functions, strategy symbols and builtins/indicators/scalar
  type names is rejected. Field names are unique per declaration.
- **Fields.** 1..32 fields; scalar `int`, `float`, `bool`, `string` or a record
  declared *earlier* in the same module or exported by an imported module.
  `series<...>`, `confidence`, `duration`, `record<Signature>` and unknown names
  are rejected. Self-reference and cycles are `RECORD_RECURSIVE`; nesting depth
  (a scalar-only record has depth 1) is at most 8.
- **Exact types.** Helper parameters/results, `let` annotations and
  construction fields match **nominally and exactly**: no int-to-float
  widening, no bool-as-int, no structural aliasing, no implicit series lifting.
- **Construction** names every field exactly once (any order); unknown,
  missing and duplicate fields are rejected. Field expressions may not contain
  a host, indicator or inference call (`RECORD_EFFECT`); bind such values with a
  scalar strategy `let` first. Inside a helper, construction may read only that
  helper's parameters (existing capture rule).
- **Field access** `e.f` is the only record operation. Records cannot be
  compared, used in arithmetic/logic, used as conditions, indexed, passed to
  indicators or `infer`, or emitted. There is no assignment syntax; `e.f = v`
  is a positioned `RECORD_MUTATION`.
- **Records in the entry strategy**: `input NAME: Record` (host-supplied,
  validated), and `let NAME = <record expression>` (a compile-time binding).
  Strategies cannot declare records.

## Evaluation: substitution and projection

Helper calls keep RFC 0066's **expression-substitution** contract. Record
values add one rule, **projection**: `R(f: x, g: y).f` is `x`. A constructed
record (including a record-typed `let` or a helper result) is therefore a
compile-time value; it is never materialized at run time and emits no IR node.

Consequences, specified and tested:

- An **unused** helper argument or unused construction field emits no node and
  is never evaluated (it is still parsed and type-checked).
- A **repeated** argument or field read is substituted at each occurrence,
  exactly as if written inline twice. It is bounded by the existing
  expansion/occurrence/IR budgets (8,192 each).
- Absence is per field: projecting a field whose expression is absent at a bar
  is absent at that bar, independent of other fields.

Host-supplied record inputs are the only run-time record values.

## Nominal identity and hashing

```text
recordId = "record:sha256:" + hex(SHA-256(canonical({
  "type": "NanoRecord", "identityVersion": 1,
  "module": <manifest module name>, "name": <declaration name>,
  "fields": [{"name": <field>, "type": <scalar | nested recordId>}, ...]
})))
```

`canonical` is the package encoding: sorted object keys, compact separators,
ASCII escapes. Field **order is significant** (it is the array order): a
reorder, rename, type change, field addition/removal, record rename or move to
another module is a schema change and changes the id. Declaration order of
records within a module, whitespace and comments do not change an id (they do
change the source pin and therefore `packageHash`). A nested field commits to
the nested record's id, so identity is transitive. The defining module is the
manifest module name, never a file path, so relocation is identity-neutral.

## Executable IR revision `1.1.0`

`1.1.0` is `1.0.0` plus exactly:

- top-level `"records"`: non-empty list of `{"id","module","name","fields"}`;
- input declarations whose `type` is a `recordId`;
- opcode `record.input` (0 inputs; attrs `name`, `record`): the validated host
  series for a declared record input;
- opcode `record.get` (1 input; attrs `record`, `field`): one field of a
  record-typed operand.

Loader rules (`NanoModule.from_dict`): the version must be `1.1.0` iff the
document has record declarations; every entry's id is recomputed and must
match; field count/depth limits hold; nested types refer to earlier entries;
the table equals the canonical closure of the record input types (post-order,
inputs in declaration order, fields in declaration order) with no extras;
`record.input` names a declared input of exactly that record type;
`record.get` names a declared field of its operand's record type. A
record-typed node may be consumed **only** by `record.get`; any other consumer
(arithmetic, comparison, `let`, `rule`, `ai.infer`, ...) is rejected. A `1.0.0`
document containing `records`, record opcodes or record input types is
rejected. `moduleHash` covers the table. Rejections are `RecordIRError`
(an `IRValidationError`) with code `RECORD_IR_VERSION`, `RECORD_IR_SCHEMA`,
`RECORD_IR_IDENTITY`, `RECORD_IR_LIMIT` or `RECORD_IR_FLOW`.

Compatibility policy:

| Artifact | Version | Change |
|---|---|---|
| Baseline strategy IR | 0.1.0 | none |
| Executable IR without records | 1.0.0 | none; bytes and hashes pinned |
| Executable IR with records | **1.1.0** (new) | emitted only when a record input exists |
| Package manifest / lock | 0.1.0 | none |
| Package build envelope | 0.1.0 | none; its `module` self-identifies its IR version |
| Package source format | 0.1.0 | additive record syntax |
| Intent documents | own versions | none |

`SUPPORTED_IR_VERSIONS` and `NANO_IR_VERSION_LATEST` keep their meaning (what
the single-source compiler can emit); `EXECUTABLE_IR_VERSIONS` lists what
`NanoModule` loads. Loaders released before this revision compare
`nanoIrVersion` to `"1.0.0"` exactly and reject `"1.1.0"` with an "unsupported"
error; no field or opcode is ignored. Compiler version strings are unchanged
because every pre-existing input still produces identical bytes; record
programs are distinguished by `nanoIrVersion` and the table. Migration: none
for existing sources; hosts that want records adopt a 1.1.0-aware loader and
supply envelopes below.

## Host input validation (before any rule runs)

Each bar of a record input is `None` (absent bar) or exactly:

```text
{"recordType": "<recordId>", "fields": {"<field>": <value>, ...}}
```

Nested record fields use the same envelope. The VM admits every declared
record input before evaluating any node; a failure raises
`RecordInputError` (a `RuntimeError_`) with a stable `code` and `path`
`<input>/<bar>/<field>[/<nested field>...]`, and no intent, escalation or
helper result is produced. Diagnostics never echo host values.

| Code | Rejects |
|---|---|
| `RECORD_INPUT_MISSING` | declared record input absent from the frame |
| `RECORD_INPUT_SCHEMA` | non-mapping value/fields, envelope keys other than exactly `recordType`/`fields` |
| `RECORD_INPUT_STALE_ID` | `recordType` differs from the compiled nominal id |
| `RECORD_INPUT_MISSING_FIELD` | a declared field is absent |
| `RECORD_INPUT_UNKNOWN_FIELD` | hidden/extra field (including non-string keys) |
| `RECORD_INPUT_TYPE` | numeric coercion (int for float, float for int, numeric strings), bool-as-int, int-as-bool, unencodable string |
| `RECORD_INPUT_NONFINITE` | NaN or infinity |
| `RECORD_INPUT_RANGE` | integer outside ±(2^53 − 1) |
| `RECORD_INPUT_RECURSIVE` | a value graph that revisits a mapping on its own path |
| `RECORD_INPUT_BUDGET` | materialized field values or string bytes over budget |

Accepted values are copied into immutable `RecordValue` objects; later host
mutation cannot affect a run. `int` fields are restricted to the exactly
representable range because the existing arithmetic kernels evaluate in
binary64; this is stricter than, and inside, the IR's 640-digit integer bound.

## Fixed ceilings

All are compiler/loader/VM constants, not author-adjustable, and are counted
**cumulatively before allocation**.

| Resource | Ceiling | Where |
|---|---|---|
| fields per declaration | 32 | compiler, loader |
| record nesting depth | 8 | compiler, loader, VM walk (iterative) |
| record declarations per package | 128 | compiler |
| materialized field values per admitted frame | 4,096 | VM admission: each field slot of each non-absent bar, nested slots included, counted from the schema before any value is walked or built |
| string field UTF-8 bytes per admitted frame | 262,144 | VM admission; equals the existing package source/metadata byte ceiling |
| integer field magnitude | 2^53 − 1 | VM admission |

Constructed records materialize nothing (projection), so a run result holds
no record values. Existing package ceilings (files, bytes, tokens, functions,
depth 32, expansion, IR nodes) are unchanged and still apply.

## Positioned compile diagnostics

Record compile errors are `PackageError`s (category `type`, or `resource` for
ceilings) carrying the dependency `path`, 1-based `line`/`column` of the
offending token and, when raised during helper expansion (for example an
expansion budget hit by repeated record arguments), the ordered `stack` of
call sites (outer call → inner definition). Errors found while checking a
dependency's definitions carry that dependency's path.

| Code | Meaning |
|---|---|
| `RECORD_DECLARATION` | empty record, unknown field type, forward reference, shadowed builtin/type name |
| `RECORD_DUPLICATE` | duplicate field, duplicate construction field, record name collision |
| `RECORD_RECURSIVE` | self-referential or cyclic declaration |
| `RECORD_FIELD_UNKNOWN` | construction or access of an undeclared field |
| `RECORD_FIELD_MISSING` | construction omits a field |
| `RECORD_TYPE` | non-exact field/argument/result type, unknown record name |
| `RECORD_SERIES` | series type or series value in a record position |
| `RECORD_EFFECT` | host, indicator or inference call inside construction |
| `RECORD_OPERATOR` | a record used as an operator operand |
| `RECORD_MUTATION` | assignment to a field |
| `RESOURCE_LIMIT` | field, depth or declaration ceiling |

Pre-existing pure-helper rejections keep their RFC 0066 codes: capture of host
data (`pure helper cannot capture`), host/indicator/inference calls in a helper
body (`pure helper cannot call`) and recursive helper calls are
`PACKAGE_ERROR`; non-record argument mismatches keep their wording.

## Effects

Records add no effect. Pure helpers with record parameters remain effect-free;
the strategy's inferred effects must still be a subset of the manifest grant,
and the loader still enforces effect/tier rules. No host dispatch, inference,
registry, arbitrary Python, loop or quantum call is introduced.

## Golden fixtures and evidence

- `tests/fixtures/records/records-golden.json`: an executable package (an
  `Evidence` record imported by two pure-helper modules, plus an inline entry),
  its pinned record ids, `moduleHash`, `packageHash`, and host frames with
  expected intents.
- `tests/fixtures/records/records-invalid.json`: invalid sources, IR
  documents and host values with their expected code, path and position.
- `tests/test_records*.py` execute both, pin pre-existing compile/package/
  Intent/diagnostic/receipt fixture hashes, and cover the boundaries
  (limit and limit + 1) for fields, depth, bytes and materialization.
