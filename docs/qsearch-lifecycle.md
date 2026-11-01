# Q-search job state, version 1

The lifecycle surface reports an existing v2 request by its existing request
digest. It uses the same authentication key and MAC format as submission, with
the actual HTTP method and full status path in the MAC, an empty body, and a
fresh nonce for each read. The service's v2 feature gate also gates status.
Possession of the request digest alone grants no read access.

`GET /qsearch/v2/requests/<request_digest>` returns the durable snapshot in
`nano.qsearch.job-state.v1`. A status read does not construct a runtime, contact
the provider, recover a request, submit a circuit, settle usage, or update a
request row. Authentication records the nonce for replay protection. A separate
authorized host operation must refresh provider evidence; repeated reads report
the last durable evidence and recompute deadlines using the injected host clock.

The envelope contains independent dimensions:

| Field | Meaning |
| --- | --- |
| `request` | Existing request ID, request digest and target. |
| `state` | Host lifecycle state, including `QUEUE_EXPIRED`, `CANCELLATION_REQUESTED`, `ACCOUNTING_PENDING`, `ACCOUNTING_EXPIRED` and `SETTLED`. |
| `provider` | Last durable provider job ID and an allowlisted provider status, otherwise `UNKNOWN`. |
| `execution` | Execution status, outcome and terminality. `SUCCEEDED`, `FAILED`, `CANCELLED`, `NOT_RUN` and unknown outcome remain distinct. |
| `accounting` | `PENDING`, `SETTLED` or `NOT_APPLICABLE`. |
| `usage` | Reservation status, known held quantum seconds and known settled quantum seconds. A missing amount is `null`. |
| `cancellation` | Requested/confirmed booleans and their durable timestamps. A request or acknowledgement alone does not confirm cancellation. |
| `deadlines` | Absolute queue/accounting deadlines and independent expiry booleans. |
| `next_action` | One bounded proposal from the table below. |

There is no combined success flag. `SETTLED` means accounting is settled; its
execution outcome may still be cancelled or failed. A provider `DONE` with no
audited result reports `COMPLETED` with outcome `UNKNOWN`. A successful stored
receipt wins a stale cancellation snapshot, preserving a real result when
cancellation races completion.

| Next action | Host interpretation |
| --- | --- |
| `POLL_STATUS` | Read status again while within policy bounds. |
| `REQUEST_CANCEL` | Queue waiting has expired; seek cancellation through the authorized host adapter. |
| `RECONCILE` | Reconcile the existing job through the authorized host adapter. |
| `MANUAL_RECONCILIATION` | The accounting window expired; stop automatic waiting and keep the reservation held. |
| `NONE` | No lifecycle action is proposed. |

These are proposals, not grants of authority. In particular there is no automatic
replacement action. A replacement requires a new target-bound plan and a new
request identity linked to the old attempt, after reconciling the original job.
No digest can name different circuits. The first delivery provides this state
contract and read surface; authorized cancellation and deadline enforcement are
the second delivery.

The queue deadline starts at durable `queue_started_at`, when present, otherwise
the request's durable `accepted_at`. It applies until execution is running or
terminal; elapsed queue time cannot turn a running/completed job into an expired
queued job. The accounting deadline starts at durable
`accounting_pending_since`. At the exact deadline (`now >= deadline`) the window
has expired. Neither deadline substitutes for the HTTP socket timeout or the QPU
execution cap. Expiry never proves provider termination or final accounting.

Nano projects explicit `held_qpu_s` (or `held_quantum_seconds`) when supplied by
the authoritative host record. It does not infer the actual reservation from
requested circuit limits or the execution cap. Unknown usage remains held;
missing usage and zero usage do not imply a free execution. Version 1 recognizes
positive explicitly settled usage. Confirmed-terminal-zero settlement belongs
to the dedicated accounting work in issue #58 and must carry its own reviewed,
job-bound evidence.

Lifecycle errors carry a stable `code`, optional `field` path, `retryable`
boolean and bounded `next_action`. Provider error text, metrics, credentials and
local paths are excluded from status responses. Existing hardware gates and
budgets remain unchanged; no status operation authorizes live jobs or spend.
