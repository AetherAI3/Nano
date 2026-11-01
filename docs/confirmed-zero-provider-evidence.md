# Nano #58 provider-evidence review

Reviewed 2026-11-01. No provider credentials, job reads, cancellation, or submission were used; research requests fetched public documentation/source only.

## Pinned SDK/API conclusion

ATLAS `81b23df` pins `qiskit-ibm-runtime==0.46.1` in `aether-atlas/requirements-frontier-offline.txt`. In that exact SDK:

- [`BaseRuntimeJob.usage()`](https://github.com/Qiskit/qiskit-ibm-runtime/blob/0.46.1/qiskit_ibm_runtime/base_runtime_job.py#L111) reads `usage.quantum_seconds` without finality checks.
- `metrics()` forwards the job-specific metadata response unchanged.
- [`ProgramJob.metadata()`](https://github.com/Qiskit/qiskit-ibm-runtime/blob/0.46.1/qiskit_ibm_runtime/api/rest/program_job.py#L81) issues `GET /jobs/{job_id}/metrics`.
- [`RestAdapterBase`](https://github.com/Qiskit/qiskit-ibm-runtime/blob/0.46.1/qiskit_ibm_runtime/api/rest/base.py#L26) sets `IBM-API-Version: 2025-05-01`.

The [official 2025-05-01 OpenAPI](https://quantum.cloud.ibm.com/api/openapi-2025-05-01.json) metrics schema declares `usage.quantum_seconds`, `usage.seconds`, and `bss.seconds`, but does **not** declare a billing-completeness status. Therefore zero from the pinned normal SDK path cannot alone support confirmed-zero reconciliation. Terminal execution status and `timestamps.running: null` do not supply documented billing finality. In fact, `timestamps.running` is declared a string when present; null is not a documented never-executed attestation.

Reviewed public historical schema SHA256: `90fbd4e9c5153bea945d23e8334d6d369e34518348e45b9c7ee12e1c295426db`.

## Reviewed finality contract for an explicit newer read

The [official Jobs REST documentation](https://quantum.cloud.ibm.com/docs/en/api/qiskit-runtime-rest/tags/jobs#tags__jobs__operations__get_job_metrics_jid) and [current OpenAPI](https://quantum.cloud.ibm.com/api/openapi.json), reviewed at API version `2026-04-15`, declare billing status with exactly two values, `pending` and `complete`, and charged capacity seconds as `usage.qpu_charge_time_seconds`. Accept **exactly** `usage.status == "complete"`, together with finite numeric charge `0` (reject booleans). Unknown, missing, whitespace/case variants, or arbitrary non-pending status must stay held. This finality field is provider-defined, not invented.

The [REST changelog](https://quantum.cloud.ibm.com/docs/en/api/qiskit-runtime-rest/changelog) explains that API `2026-03-15` removes legacy `bss`, `usage.quantum_seconds`, and `usage.seconds`, and `2026-04-15` moves circuit execution nanoseconds to the response root. Explicitly select/review the newer version; do not silently interpret its fields as part of the older pinned contract.

Reviewed public current schema SHA256: `cd8710c7d28f07a51971731628f4d00a7160f86c54177d678d6a0e6e0af3b483`. The `?version=2025-05-01` query on `openapi.json` is ignored and must not be used to claim an old schema; the historical artifact uses the `openapi-2025-05-01.json` filename.

## Minimal adapter and evidence binding

SDK 0.46.1 has no public per-call API-version selector. A narrowly isolated compatibility adapter can issue a separate read-only GET through a new `requests.Session`, reusing the resolved SDK session's authenticated `CloudAuth`, verify setting and proxy configuration, against its already-resolved base URL, with per-call `IBM-API-Version: 2026-04-15`. Verify that the source auth CRN equals the execution context instance before reading. Fail closed if the pinned session/auth structure is unavailable. Close only the new session.

Do not invoke the original `RetrySession.get(headers={...})` to override the version: [its `request()`](https://github.com/Qiskit/qiskit-ibm-runtime/blob/0.46.1/qiskit_ibm_runtime/api/session.py#L248) writes the merged headers back to the shared session. This would persist the version override and change later calls. Sharing/copying its transport adapters also risks closing the original connection pool.

The adapter evidence must record actual selected API version, exact job ID, source method/path, instance, backend, request ID/digest, provider terminal status, retrieval time and full metrics snapshot. Build these bindings from an authenticated exact-job lookup plus the durable ledger submission and existing immutable-input verification. Never accept caller-supplied identity labels as a substitute for verifying the provider job. No aggregate account counter supplies job-specific proof.

Keep old positive settlement/recovery behavior intact. New zero closure is a separate reviewed ATLAS transition with durable history and idempotence. A positive settled charge wins a cancellation race; a DONE job retains its real result. A CANCELLED or ERROR job at confirmed zero stays a failed/cancelled execution outcome, never computational success. For any inadequate evidence, retain the full reservation, including after restart.

[IBM workload usage guidance](https://quantum.cloud.ibm.com/docs/en/guides/estimate-job-run-time) supports distinguishing job billing from execution: cancellation may consume overhead, system error can result in zero, and session-mode charging follows active elapsed session time. Do not infer charge from whether execution started. Limit this adapter to the existing single-job accounting contract; do not generalize it into session-budget release.

## Independent breaker review

Reviewed implementation work in Nano, ATLAS and Predator, with findings sent to their owners during implementation. The following concrete gaps were corrected:

- ATLAS stale concurrent receipt writes could erase an already committed zero evidence projection. `record_receipt` now protects the terminal zero state, and a deterministic stale-recovery test covers it.
- Predator's zero budget transition could apply one request's valid proof to another row or overwrite positive settled usage. It now binds request ID/digest, provider job and backend, rejects positive/conflicting settlement, and preserves consumed PUB counts.
- Predator restart initially derived the expected instance from an uninitialized live context. It now reads the established `setup5` configuration.
- Predator journal reconciliation now refuses replacing an existing positive settled charge with zero, including intermediate `received` rows.
- Nano initially accepted extra proof fields that Predator's exact schema rejected. Nano now enforces the same known evidence keys and leaves unsupported proof shapes held.
- Billing closure history now states confirmed zero billing without asserting no execution; execution timestamps/nanoseconds remain audit data rather than zero-cost proof.

Independent commands against updated implementations confirmed refusal of an unrelated positive budget row, refusal of replacing a journaled positive charge with zero, and held Nano accounting for proof carrying an unsupported extra key. No outstanding blocker identified in this review; final suite and exact-head checks remain the implementation owners' responsibility.
