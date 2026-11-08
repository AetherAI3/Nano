# Native prepare/sample bridge (local only)

Status: offline contract, prepared artifact, opt-in local service adapter and
caller-side verification for [#61](https://github.com/AetherAI3/Nano/issues/61).
It complements the [operations-v1 RFC](quantum-operations.md) and the
[direct local adapter](quantum-local-adapter.md). It adds no provider adapter,
hardware path, credential use or new accounting ledger.

`prepare_bits` and `sample` reach the q-search service without being disguised
as QUBO/QAOA work. The legacy `to_legacy_job` adapter and the legacy
`nano.qsearch.prepared-artifact.v1` remain `optimize_qubo` only and byte-stable.

## Capability matrix

`nano.quantum.native.capability_matrix()` is frozen by
`nano/quantum/fixtures/native-capabilities.json`.

| Operation | operations-v1 | direct local adapter | legacy job / legacy artifact | native artifact | native service | hardware |
| --- | --- | --- | --- | --- | --- | --- |
| `prepare_bits` | supported | supported | unsupported | supported | opt-in, local only | unsupported |
| `sample` | supported | supported | unsupported | supported | opt-in, local only | unsupported |
| `optimize_qubo` | supported | unsupported | supported | unsupported | unsupported | existing host gates |

The only native target is `local_ideal`. The native service is disabled by
default, and enabling it never enables a hardware target.

## Versioned records

All records are canonical JSON with exact key sets. A known schema family at an
unknown version fails with `UNSUPPORTED_VERSION`; another schema fails with
`SCHEMA`. Structural JSON Schemas live in `nano/quantum/schemas/native-*.json`;
the strict stdlib parsers are authoritative.

| Schema | Meaning |
| --- | --- |
| `nano.quantum.native-target.v1` | `local_ideal`, `local_simulator`, `qiskit.primitives.StatevectorSampler`, `num_qubits` (1..20), basis `["measure","x"]`, `qpu_seconds: 0`. |
| `nano.quantum.native-profile.v1` | Profile ID, operations `["prepare_bits","sample"]`, `target_digest`, `layout: "identity"`, `optimization_level: 0`, compiler `nano-qsearch` at the installed Nano version, Qiskit `2.3.1`, no IBM runtime. |
| `nano.quantum.native-pub-map.v1` | Digest domain for the ordered PUB map (see below). |
| `nano.quantum.native-artifact.v1` | Checked plan, profile, target, preparations, per-PUB QPY and resources (slice 2). |
| `nano.quantum.native-request.v1` | `{schema, descriptor, artifact}`; the descriptor is the signed identity. |
| `nano.quantum.native-result.v1` | The stored local result record (never a hardware receipt). |

Builders: `local_target`, `native_profile`, `prepare_native`,
`build_native_request`, `retain_identity`.

### PUB map and ordering

`pub_map(plan)` lists, in PUB order, each sample's `pub_index`, `operation_id`,
`preparation_id`, `register`, `width`, `bit_order` (`msb_first`), `shots`,
`seed`, ordered `variables` and packets with `recipient_id`, `chunk`, `slot`,
bit `offset`, payload/authentication widths, `bit_significance`, codec and
evidence reference/digest. Variable `i` is prepared on qubit `i` and measured
into classical bit `i` of the PUB's single named register. Display strings put
bit `n-1` first, exactly as in operations-v1. `pub_map_digest` covers the plan
digest and this map. A packet-bearing preparation may be sampled once: sampling
it twice would deliver one recipient/chunk twice (`RECIPIENT`).

### Resources

`native_resources(plan)` declares `preparations`, `pub_count`, `total_shots`,
`max_shots_per_pub`, `max_width`, `deadline_s` (the strictest declared
`max_wall_s`, null without PUBs) and `qpu_seconds: 0`. The request descriptor
repeats them so a host can refuse caps before parsing an artifact.

## Prepared artifact

`nano_qsearch.native_artifact.prepare_native(plan, profile, target)` admits a
checked native plan, binds the profile to the exact target digest, and builds
one circuit per sample with the pinned Qiskit: X on each `1` variable, then
`measure q[i] -> register[i]`. Circuit metadata binds the plan, profile and
target digests plus operation, preparation and PUB identity. Each PUB records
its register, width, shots, seed, identity layout, operation counts, QPY bytes
and their SHA-256 `isa_digest`. Preparations list their width and X qubits. The
artifact binds `plan_digest`, `pub_map_digest`, `profile_digest`,
`target_digest`, resources, `effect: "local_compile"` and `artifact_digest`.

`parse_native_artifact(payload, expected_digest=...)` is stdlib only. It checks
every digest, re-derives preparations, PUB map, PUB fields and resources from
the embedded plan, and refuses missing, duplicate or reordered PUBs. Passing the
caller-retained `expected_digest` refuses a substituted artifact.

`load_native_circuits` requires Qiskit `2.3.1` (`UNAVAILABLE_DEPENDENCY` when
absent, `UNSUPPORTED_SDK` for another version), loads each QPY, checks name,
metadata, registers, widths and the exact instruction sequence, and requires
the stored bytes to equal a fresh rebuild by the pinned compiler (`DRIFT`). No
IBM runtime, ATLAS or network module is imported.

A preparation-only artifact (a plan with no `sample`) is admitted. It has no
PUBs, no QPY, no samples, no verdicts and no execution claim. The service
refuses to execute it (`PREPARATION_ONLY`).

## Opt-in local service adapter

`ServiceConfig(native_policy=NativeHostPolicy(...))` enables these routes. With
the default `native_policy=None` they answer 404 `NOT_ENABLED` before
authentication and burn no nonce. `NANO_QSEARCH_NATIVE=1` requires
`NANO_QSEARCH_NATIVE_POLICY` to name a `nano.qsearch.native-host-policy.v1`
file. Existing v1/v2 routes, schemas, defaults and receipts are unchanged.

| Route | Purpose |
| --- | --- |
| `POST /qsearch/native/v1/submit` | Submit a `native-request.v1`. |
| `GET /qsearch/native/v1/requests/<request_digest>` | Read-only `nano.qsearch.job-state.v1`. |
| `POST /qsearch/native/v1/requests/<request_digest>/cancel` | Fence and cancel, empty body. |

Authentication is the existing `X-Aether-Qsearch-Auth` MAC over method, path,
body digest, timestamp and a one-use nonce. Errors use the existing lifecycle
error shape (`code`, `field`, `retryable`, `next_action`).

Admission order, all before a row is stored or a circuit is allocated:

1. Body cap (`max_body_bytes`), then authentication.
2. Descriptor header and declared resources against the host policy
   (`max_pubs`, `max_total_shots`, `max_qubits`, `max_deadline_s`):
   `RESOURCE_LIMIT`.
3. Exact host-admitted `profile_digest` and `target_digest`: 403
   `NOT_ADMITTED`. A path, profile or artifact never authorizes itself.
4. Artifact parse against the descriptor's `artifact_digest`; every descriptor
   field must equal the artifact (`IDENTITY`).
5. Optional host evidence registry: a stale evidence-bank digest is
   `STALE_EVIDENCE`.
6. Runtime preflight: the local runtime loads and checks every QPY while
   holding one of the service's concurrency slots (`SERVICE_BUSY` when none
   frees within the poll interval).

The request digest is the canonical digest of the descriptor, so a changed
plan, target, profile, packet map or resource bound is a new identity. The row
lives in the existing durable `requests` table; `request_id` stays unique across
v2 and native rows (`REQUEST_ID_CONFLICT`). v2 routes do not serve native rows
and native routes do not serve v2 rows.

Execution reuses the #58/#59 semantics. `claim_request_attempt` admits one
attempt, fenced by cancellation and the queue deadline. A repeated identity
returns the stored record and never executes again; a concurrent duplicate
while the first is in flight gets a retryable 409 `REQUEST_BUSY`. Startup recovery never
starts execution: an unclaimed row waits for an authenticated resubmission and
a claimed row with unknown outcome is re-attached through `runtime.recover`.
The local runtime cannot recover a lost local outcome, so it stays held
(`SUBMITTED_UNKNOWN`, accounting `PENDING`, reservation `HELD`) until explicit
reconciliation. Elapsed time and simulator output never infer zero usage.
This slice adds no automated route to close such a held local row; an operator
reconciles it explicitly.

Cancellation persists the fence before anything else. Before a claim it seals
`NOT_RUN`; during execution it answers 409 `REQUEST_BUSY` with the fence kept
and the run's real result wins; after completion it changes nothing. Status
reads never construct a runtime.

The local runtime (`LocalNativeRuntime`) samples the loaded QPY circuits with
`StatevectorSampler` in a child process killed at `deadline_s`. A deadline,
worker failure, partial or malformed result is sealed `FAILED` with no
observations. Local records use `backend: {"kind": "local_ideal", "dry_run":
true}`, so the unchanged job-state projection reports accounting
`NOT_APPLICABLE`, never settled zero.

### Result record

`nano.quantum.native-result.v1` keeps independent fields: `source_verification`
(`admitted`), `status` (`OK`, `FAILED`, `NOT_RUN`), `observations`
(`nano.quantum.fixture-result.v1` or null), `decoding` (`unverified`; the service
never decodes), `accounting` (`not_applicable`, no provider usage) and
`hardware: null`. Its seal is `hmac-sha256+native-local-v1:` over a separate
domain. It is never a hardware seal or provider-final accounting.

## Caller verification

`nano_qsearch.native_result.retain_identity(plan, request)` is computed from
the caller's own plan before submission. `verify_native_result(expected,
record, evidence_catalog, receipt_key=...)` requires the local seal key,
checks that `effect` matches the status and that no hardware, accounting or
decoding claim is present, compares every identity field with that retained
copy, never with the service's stored manifest, and decodes the observations
against the retained plan with `decode_verified`. Execution status, decoded
delivery and accounting stay separate fields; hardware qualification is
always `none`. A local success does not establish hardware qualification or
quantum advantage.

## Limits and test caps

Production defaults: native disabled; when enabled, `max_pubs` 16,
`max_total_shots` 100000, `max_qubits` 16, `max_deadline_s` 60, and the existing
1 MiB `max_body_bytes`. Hard ceilings: 4096 PUBs, 20 qubits, 600 s. The
400-recipient/1,200-PUB round trip uses explicit test caps only:
`max_pubs=1200`, `max_body_bytes=8 MiB`; the defaults are unchanged and tests
prove the defaults refuse it.

Tests that spawn the local runtime use non-golden copies of the fixture plans
built with the same builders and `max_wall_s` 60 (the default policy deadline),
because child startup counts against the declared bound. The frozen goldens keep
their 5 s bound and stay covered by the contract and artifact tests. Deadline
enforcement is proven separately and load-independently: a 1 s bound kills a
child that sleeps 60 s and the request is sealed `FAILED`.
