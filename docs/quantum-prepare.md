# Offline quantum check and preparation

`nano quantum check` and `nano quantum prepare` consume local JSON files. They
do not construct an IBM service, obtain credentials, resolve a live backend,
run a circuit, or authorize spending. The core and `check` remain stdlib-only.
`prepare` uses the repository's existing optional **Qiskit 2.3.1** compiler pin;
it does not upgrade packages or import `qiskit_ibm_runtime`.

This is an explicit, opt-in adapter. The existing service routes and v2 receipt
semantics remain unchanged. A prepared artifact is an approval/inspection value;
the host still owns admission, permitted-target resolution, authority and limits.

```console
nano quantum check request.json --profile profile.json --target-snapshot target.json
nano quantum prepare request.json --profile profile.json --target-snapshot target.json -o prepared.json
nano quantum check prepared.json --artifact --expected-digest sha256:<retained-digest>
```

Every result is JSON, including diagnostics. Exit 0 means the local operation
succeeded, 1 means validation/preparation failed, 2 is argument misuse, and 3
means a local file could not be read/written. Duplicate JSON keys and nonfinite
numbers are rejected. Stable diagnostic codes distinguish `missing_target`,
`unavailable_dependency`, `unsupported_capability`, `invalid_plan`,
`target_drift`, and local input/output errors.

An artifact check reports `validation_scope: "structure_and_digests"` and
`isa_validation: "not_loaded"`. It validates the file contract and hashes with
the stdlib; it does not claim that QPY has been decoded or that the ISA satisfies
a current target. The pinned execution loader performs those checks before any
submission. A request check reports `isa_validation: "not_prepared"`.

## Profiles and snapshots

`nano.qsearch.compile-profile.v1` declares an ID, operation, basis-state rewrite,
layout mode (`none` or `target`), compile seed, optimization level, compiler and
SDK versions, and an optional exact local ATLAS-source digest. The seed and
optimization level must match every frozen job; the adapter never silently
overrides a job. No `NANO_QSEARCH_*` environment setting selects a profile or
changes its rewrites. Unsupported layouts fail explicitly.

The adapter enforces the shared packed-provenance wire limits during checking:
width at most 4096, shots at most `2^31-1`, at most 4096 recipients per PUB and
protocol integers (including compile/sample seeds and recipient chunk/slot)
at most `2^53-1`. A successful local check cannot approve an artifact that is
unrepresentable by that manifest contract.

The existing v2 QUBO request adapter binds the coordinated
`nano.quantum.operations.v1` operation with
`operation: {"schema": "nano.quantum.operations.v1", "op": "optimize_qubo"}`.
The operations contract is owned by #60/#61 and PR #72, inspected at
`45ea683915ad1b74b19310229df5428981ff8e03`; this adapter introduces
no additional operation grammar or natural-language frontend. `prepare_bits`,
`sample`, and unknown operations are explicitly unsupported by this initial
legacy-request adapter. Their kernels remain deferred to their owning work.

The compiler declaration is `nano-qsearch`, the installed Nano package version,
`qiskit_version: "2.3.1"`, and `qiskit_ibm_runtime_version: null`. Normal logical
construction also requires `atlas_source_digest`, obtained from
`nano_qsearch.prepare.atlas_source_digest()` for the entire local
`aether_atlas.qcompiler` Python source tree, including transitive helpers and
nested modules. `atlas_source_manifest()` exposes the versioned
`nano.qsearch.atlas-source-manifest.v1` document: sorted relative POSIX paths
and SHA-256 hashes of each file's exact bytes. Adding, removing or modifying a
helper changes the pin. Discovery reads local files without importing ATLAS
initializers or IBM dependencies; missing required compiler source, unreadable
files or an ambiguous/source-less package is `unavailable_dependency`. External
SDK/package dependencies are separate toolchain inputs, not covered by this
source-tree digest; Qiskit is pinned independently by the profile. An eligible diagonal basis-state
rewrite can compile with `atlas_source_digest: null` without importing ATLAS.

`nano.qsearch.target-snapshot.v1` contains `name`, `num_qubits`, ordered
`instructions`, `timing_properties` and one `qubit_properties` entry per physical qubit. Each native
instruction records `qargs`, error and duration; each qubit records T1, T2 and
frequency. Missing measurements/properties must be represented explicitly as
null where allowed. Qargs must be sorted, unique and in range. The snapshot
digest binds its complete contents. A separate calibration digest binds the
instruction properties and qubit properties. The existing compiler's structural
target digest remains separate from both.

Timing properties bind the target's `dt`, granularity, minimum instruction
length, pulse alignment and acquisition alignment. This adapter performs no
pulse or delay scheduling. Operation names denote the adapter's standard
Qiskit gate constructors: snapshot capture rejects custom or fixed-angle
operations whose parameter capabilities would otherwise be broadened by a
name-only snapshot.

The initial adapter supports explicit `ibm_fez`, `ibm_kingston` and
`ibm_marrakesh` snapshots and a documented set of public Qiskit native gate
constructors. Unsupported instructions or targets fail with
`unsupported_capability`. A missing target fails with `missing_target`; no
FakeFez instance substitutes for it. `TargetSnapshot.to_backend()` constructs
only a local `qiskit.transpiler.Target`. `snapshot_from_backend()` captures an
already-resolved host backend object without creating a service or resolving
another backend.

The local request/profile/target fixtures under
`tests/qsearch_fixtures/prepare_v1/` demonstrate the versioned file contract.
The fixtures contain a synthetic target and do not describe current hardware.

## Artifact identity and execution

`nano.qsearch.prepared-artifact.v1` embeds the validated frozen request, explicit
profile, target snapshot, ordered PUB compilation records, inspected metrics,
layout and measurement mapping, and normalized ISA QPY bytes. The artifact
digest covers all these fields. Separate logical, profile, snapshot, compiled
plan and ISA digests make their respective claims inspectable. Logical identity
excludes target placement and compiler choices; the full request digest retains
all original request semantics.

Profiles, snapshots and parsed artifacts are immutable values. Their
`to_payload()` methods return detached documents. Strict parsing recomputes
digests from contents and rejects unknown fields, stale versions, omitted,
duplicated or reordered PUBs, invalid widths/layouts, malformed QPY encoding and
inconsistent compilation records. An independently retained expected artifact
digest is required by the execution loader. A digest supplied inside the
received artifact alone is not independent approval.

`load_prepared_plans(artifact, current_snapshot=..., expected_digest=...)`
compares the current permitted target's full snapshot before importing Qiskit
or loading QPY. Changed coupling, instructions, errors, durations or qubit
properties require explicit re-preparation. It loads the approved ISA bytes
without recompiling, validates their target metadata, measurement register,
width, gate counts and native depth, then checks every instruction against the
current target. The host must obtain the current snapshot from its own admitted
backend rather than accepting a client's self-asserted snapshot.

The explicit host API is
`IbmRuntime.run_prepared_request(artifact, expected_artifact_digest=retained_digest,
record=record)`. It runs the existing host admission gates, resolves the permitted
backend, compares its complete snapshot, loads the approved QPY and freezes the
exact ATLAS child/request identities once. The ATLAS child ABI and fingerprints
are checked against the approved job, ISA, compiled plan, PUB order and limits
before the same frozen identity is passed to the submission adapter. A supplied
snapshot cannot override the host's resolved live backend. The returned body is
an explicitly opt-in v3 receipt for the host to seal.

A `None` result means the provider may have received the request and the result
or positive usage settlement remains held, including unverified returned
evidence. It is not permission to retry submission. The host must retain the
record and use its durable accounting/recovery workflow; this API adds no
automatic retry, service route or production activation.

At dispatch, the host freezes ATLAS children from the approved circuits, then
uses the #62 compile-manifest contract to bind the ordered PUB/job/recipient
mapping to each ATLAS child, compiled-plan digest, ISA digest, physical layout,
profile and target identities. This identity is independent of mutable usage
settlement. The new receipt remains opt-in until shared verifier review and
compatibility checks pass.

## Resources and SDK compatibility evidence

`check` reports PUB count, ordered shots/widths, declared resource limits and a
clearly labelled usage upper bound. Native gate counts require preparation and
are explicitly marked unavailable until then. `prepare` reports actual compiled
gate counts per PUB. Estimated usage is separate from `observed` and `settled`,
which are null in offline artifacts; a declared cap is not a provider usage
quote or a claim of free execution.

The supported compiler distribution remains pinned to Qiskit 2.3.1. Tests
include an actual optional pinned-compiler prepare/load round trip and deterministic
repeated compilation, plus explicit attribute-DataBin and mapping-DataBin PUB
result fixtures with different widths and display-bit order. The pure
`samples_from_pub_result` adapter preserves `get_bitstrings()` display order:
logical `x[i]` is `bits[-1-i]`. Wrong width, partial results and missing public
interfaces are rejected. These shape fixtures establish adapter contracts;
they do not claim that two different SDK distributions were installed or that
current hardware was exercised.
