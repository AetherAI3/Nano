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
| `lineage` | Valid `replaces_request_digest` and `replacement_request_digest` links, otherwise `null`. |
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
contract and read surface; the second delivery adds the explicit controls below.

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

## Explicit host controls

The v2 gate and authenticated request identity also apply to these operations:

| Method and path | Body | Effect |
| --- | --- | --- |
| `POST /qsearch/v2/requests/<request_digest>/refresh` | Empty | One bounded observation of the existing provider job, then reconciliation if terminal. |
| `POST /qsearch/v2/requests/<request_digest>/cancel` | Empty | Persist the cancellation fence and request event, then ask the authorized host/provider adapter to cancel at most once. |
| `POST /qsearch/v2/requests/<request_digest>/replace` | New `predator.qsearch.request.v2` body | Admit and submit a linked replacement only after the original execution is terminal and its accounting is settled or not applicable. |

Controls return the job-state envelope. Replacement returns the new request's
state. Each MAC binds the exact method, path and body. Use a fresh nonce for a
retry; the durable request identity remains unchanged.

The cancellation request is persisted before provider I/O and fences automatic
resume and replacement. A separate dispatch event is persisted before the cancel
RPC. Repeated cancellation and a lost acknowledgement observe the existing job;
they do not submit a new job or send a second cancel RPC. The cancellation
confirmation event requires provider `CANCELLED`. If the provider instead reports
`RUNNING`, `DONE` or `ERROR`, reconciliation preserves that actual execution and
its usage. A timed-out HTTP connection cannot establish cancellation.

Authority, request identity and current provider status are checked before the
dispatch event is consumed. A failed preflight keeps the cancellation request
fence but permits a later authorized attempt. A job already terminal needs no
cancel RPC or dispatch event. Once dispatch is claimed, an ambiguous failure
requires observation and cannot authorize another cancel RPC.

The default adapter reuses ATLAS's existing reservation and frozen request/plan
identity checks. Cancellation also verifies the host's existing signed authority
for that plan and cap. Nano creates no second spending ledger. Missing live
context, authority or identity checks fail closed, with stable error metadata.
Predator remains the owner of its campaign journal and budget admission; it can
consume the independent execution/accounting fields without treating a cancelled
computation as success or releasing unknown usage.

`NANO_QSEARCH_QUEUE_DEADLINE_S` defaults to 3600 seconds;
`NANO_QSEARCH_ACCOUNTING_WINDOW_S` defaults to 900 seconds;
`NANO_QSEARCH_POLL_INTERVAL_S` defaults to 30 seconds. All must be finite and
positive. Automatic recovery/repeated submission stops provider waiting when the
queue or accounting deadline expires or cancellation is requested. Provider poll
throttling is durable across restart. An explicit authenticated `refresh` can
reconcile later evidence; it still observes the original job and respects the
poll interval. Deadline expiry never requests cancellation by itself.

A replacement must have a different request ID, digest and backend target. The
durable parent/child link is admitted atomically and permits at most one linked
replacement. Its ordinary submission path compiles a fresh target-bound plan and
passes the existing authorization and budget gates. Replays use that same child
identity; the parent digest is never reused for the new circuits. A queued,
running, unknown or accounting-pending original cannot authorize replacement.

## Python client

`nano_qsearch.client.LifecycleClient` uses only the standard library:

```python
from nano_qsearch.client import LifecycleClient, LifecycleClientError

client = LifecycleClient("http://127.0.0.1:8798", request_key, timeout_s=30)
snapshot = client.status(request_digest)
```

`status(digest)` performs the read-only GET. `refresh(digest)`, `cancel(digest)`
and `replace(digest, new_request_body)` each perform one explicit authenticated
POST. Calling a client method does not grant host/provider authority. The client
never polls, retries, requests cancellation, or replaces a job automatically.
The optional injected transport, clock and nonce factory support offline tests.

`LifecycleClientError` exposes `code`, `field`, `retryable`, `next_action` and
`http_status`. Messages exclude free-text server/provider errors, response
bodies, credentials and URLs. The transport bounds response size and refuses
redirects so the authentication header cannot follow a redirect. Invalid JSON,
an unexpected schema, or a mismatched request identity yields `INVALID_RESPONSE`.
Socket/transport failure yields `TRANSPORT_UNAVAILABLE`: a status read proposes
`POLL_STATUS`; an ambiguous control acknowledgement proposes
`MANUAL_RECONCILIATION`. Neither means the provider job was cancelled.
