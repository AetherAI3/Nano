# Q-search lifecycle wire contract, version 1

Contract identity: `nano.qsearch.lifecycle-wire.v1`. This document is the
language-neutral description of the existing **read-only status** surface of the
Nano q-search service (`nano_qsearch/service.py`, `nano_qsearch/lifecycle.py`,
`nano_qsearch/auth.py`) so another host language can read job state without
recreating scheduling, cancellation, replacement or accounting.

It adds no endpoint, no lifecycle authority and no new schema identity for the
job state. The Python service and `nano_qsearch.client.LifecycleClient` remain
authoritative; where this document and the Python implementation disagree, the
disagreement is recorded as a failing compatibility test, never silently
resolved here (see "Known Python discrepancies").

Machine-readable parts, next to this file:

| File | Content |
| --- | --- |
| `job-state.v1.schema.json` | JSON Schema (2020-12) of a successful status body, `nano.qsearch.job-state.v1`, closed. |
| `lifecycle-error.v1.schema.json` | JSON Schema of a non-2xx lifecycle error body. |
| `wire.v1.json` | Constants: paths, header name, caps, enums, remote code/field allowlists, client error codes. |
| `fixtures/` | Golden vectors produced by the actual Python implementation. |
| `SHA256SUMS` | Hashes of every file above; frozen per contract revision. |

Scope of v1: `status` only. Cancellation, refresh and replacement are write
operations with separate semantics; they are **not** part of this contract and a
v1 client must refuse them explicitly (`UNSUPPORTED_CAPABILITY`). Native quantum
operation IDs coordinated in issue #61 are likewise unsupported by a v1 client.

## 1. Request

One status invocation is exactly one HTTP request:

```
GET <origin>/qsearch/v2/requests/<request_digest>
X-Aether-Qsearch-Auth: v1 ts=<ts> nonce=<nonce> mac=<mac>
Accept: application/json
```

* No request body, no `Content-Type`, no other headers carrying data, no query,
  no fragment. The service rejects a non-empty body with `EMPTY_BODY_REQUIRED`.
* `request_digest` is `sha256:` followed by exactly 64 lowercase hex characters.
  It is inserted into the path verbatim (the `:` is not percent-encoded). Anything
  else is refused by the client before any I/O with `IDENTITY`.
* Possession of a digest grants nothing; every read is authenticated.

### 1.1 Origin

The caller supplies a base URL. It must be `http` or `https`, have a non-empty
host, no user info, a path of `""` or `"/"`, no query and no fragment, and not
port 0. Otherwise the client refuses construction with `CLIENT_CONFIGURATION`
(`field: "base_url"`). The request URL is the base origin with any trailing `/`
removed, followed by the fixed path. The client never contacts any other origin
and **never follows a redirect** (a 3xx is `UNEXPECTED_REDIRECT`), so the
authentication header cannot be forwarded elsewhere. Use `https` for anything
other than loopback; the client does not discover services or read environment
configuration.

### 1.2 Authentication bytes (canonical framing)

```
message = UTF-8( METHOD "\n" PATH "\n" BODY_SHA256_HEX "\n" TS "\n" NONCE )
mac     = lowercase_hex( HMAC-SHA256(request_key, message) )
header  = "v1 ts=" TS " nonce=" NONCE " mac=" mac
```

* `METHOD` is `GET`. `PATH` is the exact request path beginning
  `/qsearch/v2/requests/` (no origin). For status the body is empty, so
  `BODY_SHA256_HEX` is always
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
* `TS` is the decimal integer `floor(clock_seconds)`, with no sign, leading zeros
  or exponent. The clock is caller-injected and must return finite, non-negative
  Unix seconds no larger than 2^53-1. The server accepts `|server_now - TS| <= 120`.
* `NONCE` is 16..64 lowercase hex characters, fresh per request. The server
  records it for replay protection; a reused nonce is `UNAUTHORIZED`.
* `request_key` is at least 16 bytes. It is the request key, distinct from the
  receipt-seal key. A portable client never needs the key: it hands `message` to
  a caller-injected signing capability that returns the 32 MAC bytes. Keys are
  never read from the environment, browser storage or files by the client.

`fixtures/signing.v1.json` pins these bytes for synthetic keys.

### 1.3 Timeouts versus lifecycle deadlines

The request timeout is a client-side bound on one HTTP exchange (default 30 s in
Python). It is independent of the server's queue deadline
(`NANO_QSEARCH_QUEUE_DEADLINE_S`), accounting window
(`NANO_QSEARCH_ACCOUNTING_WINDOW_S`) and the QPU execution cap. A client timeout
is `TRANSPORT_UNAVAILABLE`, `retryable: true`, `next_action: POLL_STATUS`; it
never produces, implies or fabricates a job state. Lifecycle deadlines are
server data in the envelope (`deadlines.*`); a client never recomputes them from
its own clock.

## 2. Response

### 2.1 Bytes

* Status code: an integer 100..599. 3xx → `UNEXPECTED_REDIRECT`. Anything not an
  integer in range → `INVALID_RESPONSE`.
* Body: at most **1,048,576 bytes**. A body of exactly the cap is accepted; one
  byte more is `INVALID_RESPONSE`. A client must stop reading at cap + 1.
* Encoding: UTF-8. The Python service emits
  `json.dumps(payload, sort_keys=True, allow_nan=False)` (ASCII-escaped,
  `", "`/`": "` separators). Clients must not depend on the serialisation form,
  only on the parsed value. Response bytes are **not** canonical JSON and are not
  signed; integrity comes from the transport (use https) and from binding below.
* Strict JSON (RFC 8259): one top-level **object**; no duplicate member names at
  any depth; no `NaN`, `Infinity`, `-Infinity`; every number finite with
  magnitude at most 2^53-1 (9007199254740991) so that it is exactly representable
  in IEEE-754 double; nesting depth at most 64. Violations → `INVALID_RESPONSE`.
  A portable client must use a parser that detects duplicates and unsafe numbers
  (a platform parser that keeps the last duplicate or rounds integers is
  insufficient).

### 2.2 Success (2xx)

The body must validate against `job-state.v1.schema.json` in full, including the
cross-field invariants listed there, and:

* `schema` must equal `nano.qsearch.job-state.v1` exactly;
* `request.request_digest` must equal the requested digest exactly.

Any failure → `INVALID_RESPONSE`, `next_action: MANUAL_RECONCILIATION`. Nothing
partial is returned.

**Version policy.** The v1 envelope is closed: every object has exactly the
listed members, every enum is closed, and `null` appears only where listed. Any
additive change (new member, new enum value, new state) requires a new schema
identity; a v1 client rejects it as `INVALID_RESPONSE` rather than guessing. There
is no optional-extension slot in v1.

Envelope dimensions (see `docs/qsearch-lifecycle.md` for meaning):
`request`, `lineage`, `state`, `provider`, `execution`, `accounting`, `usage`,
`cancellation`, `deadlines`, `next_action`. Notes that bind every client:

* There is no combined success flag. `state: SETTLED` means accounting is
  settled; the execution outcome may still be `CANCELLED` or `FAILED`.
* Unknown usage is `null` and the reservation stays `HELD`/`UNKNOWN`. A client
  must expose `null` as unknown and never coerce it to `0`.
* `settled_quantum_seconds: 0` appears only when the server itself projected an
  authenticated confirmed-zero receipt. A client accepts it as reported data and
  never re-derives, validates or upgrades zero finality; it never modifies a
  budget.
* `cancellation.requested: true` is not terminal. Only
  `execution.status: CANCELLED` (with `cancellation.confirmed: true`) is.
* Numbers: times and quantum seconds are JSON numbers, non-negative, possibly
  written with a fraction (`3600.0`). Booleans are never numbers and numbers never
  booleans.

### 2.3 Errors (non-2xx, non-3xx)

The body must still be a strict-JSON object (else `INVALID_RESPONSE`). It is
mapped, never trusted:

* `code`: kept if it is a string in `wire.v1.json#remote_codes`, else
  `REMOTE_ERROR`.
* `field`: kept if it is a string in `wire.v1.json#remote_fields`, else `null`.
* `retryable`: `true` only if it is the JSON literal `true`.
* `next_action`: kept if it is a string in `next_actions`, else
  `MANUAL_RECONCILIATION`.
* `http_status`: the status code.

Free text (`error`) and all other members are discarded. Unknown codes are not a
parse failure.

### 2.4 Client errors

A client error exposes only `code`, `field`, `retryable`, `next_action`,
`http_status`. Its message is the constant `q-search lifecycle failed (<code>)`.
It never contains the URL, key, auth header, nonce, raw exception text or
response body.

| Code | When | retryable | next_action |
| --- | --- | --- | --- |
| `CLIENT_CONFIGURATION` | invalid base URL, capability, clock or nonce | false | NONE |
| `IDENTITY` | malformed requested digest (before I/O) | false | NONE |
| `UNSUPPORTED_CAPABILITY` | an operation other than `status` | false | NONE |
| `TRANSPORT_UNAVAILABLE` | transport failure or timeout | true | POLL_STATUS |
| `UNEXPECTED_REDIRECT` | 3xx | false | NONE |
| `INVALID_RESPONSE` | bad status code, cap, encoding, JSON, schema, digest | false | MANUAL_RECONCILIATION |
| remote code / `REMOTE_ERROR` | non-2xx error body | mapped | mapped |

## 3. Side-effect rules

* A status invocation performs **exactly one** transport call, or zero if refused
  before I/O. No retry, poll, refresh, cancel, replace or resubmission follows
  from an error, timeout, deadline or `next_action`.
* `next_action` is information for caller policy, not authorization.
* The server-side status read constructs no runtime, contacts no provider and
  writes nothing except the replay nonce.

## 4. Known Python discrepancies

Recorded as failing compatibility tests in `tests/test_qsearch_lifecycle_wire_contract.py`
(`xfail(strict=True)`), so fixing one turns the suite red until the marker is
removed deliberately:

* `LifecycleClient` checks only `schema`, `request.request_digest` and
  `next_action` on success; it does not validate the closed envelope (missing or
  extra members, member types, enums, or invariants I1-I8).
* `LifecycleClient` accepts integers above 2^53-1, and maps a non-2xx body that
  exceeds the 2^53-1 or depth-64 rules as a remote error rather than
  `INVALID_RESPONSE`.

Resolved: duplicate member names are refused by the Python client in success and
error bodies (separate parity patch; previously `json.loads` kept the last one).

Python integers are unbounded, but the service never emits a number above 2^53-1;
the cap is a portability rule for every client.
