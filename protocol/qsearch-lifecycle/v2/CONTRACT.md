# Q-search lifecycle wire contract, version 2

Contract identity: `nano.qsearch.lifecycle-wire.v2`. Version 2 is **additive**:
it keeps every rule of `../v1/CONTRACT.md` for `status` unchanged and adds the
three existing explicit host controls of `nano_qsearch/service.py`
(`control_request`) and `nano_qsearch.client.LifecycleClient`:

| Operation | Request | Success body |
| --- | --- | --- |
| `refresh` | `POST /qsearch/v2/requests/<digest>/refresh`, empty body | `nano.qsearch.job-state.v1` of `<digest>` |
| `cancel` | `POST /qsearch/v2/requests/<digest>/cancel`, empty body | `nano.qsearch.job-state.v1` of `<digest>` |
| `replace` | `POST /qsearch/v2/requests/<digest>/replace`, canonical replacement body | `nano.qsearch.job-state.v1` of the **new** request |

No endpoint, schema identity, receipt, provider, ledger, authority or budget rule
changes. The job-state schema is still `../v1/job-state.v1.schema.json`
(unchanged identity). The Python service and client stay authoritative; a
disagreement is recorded as a failing compatibility test, never resolved here.
v1 files are frozen; nothing in `../v1/` changes for v2.

## 1. Capabilities

A v2 client is constructed with an explicit, caller-chosen capability set drawn
from `status`, `refresh`, `cancel`, `replace`. The default set is `{status}`. An
operation outside the set fails with `UNSUPPORTED_CAPABILITY` (field
`operation`) before any I/O, as does any other operation name (including the
#61 native operations). Granting a capability to a client grants no host,
provider, ATLAS, reservation or budget authority: every authority check stays
server-side.

## 2. Request bytes

Each control is exactly one HTTP request:

```
POST <origin>/qsearch/v2/requests/<request_digest>/<action>
X-Aether-Qsearch-Auth: v1 ts=<ts> nonce=<nonce> mac=<mac>
Accept: application/json
Content-Type: application/json
```

* Origin, digest, timestamp, nonce, key and redirect rules are exactly v1
  sections 1.1 and 1.2, with `METHOD = POST` and `PATH` ending in `/<action>`.
* `refresh` and `cancel` send an **empty** body; `BODY_SHA256_HEX` is the
  empty-body constant. The service refuses a non-empty body
  (`EMPTY_BODY_REQUIRED`).
* `replace` sends `canonical_json(replacement)` encoded as UTF-8, and
  `BODY_SHA256_HEX` is the SHA-256 of exactly those bytes. A portable client
  needs a caller-injected SHA-256 capability for this; it never reads keys.

### 2.1 Canonical replacement bytes

`canonical_json` is `nano_qsearch/canonical.py`: Python
`json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
allow_nan=False)` over the **parsed** replacement. A non-Python client receives
the replacement as JSON text and must reproduce those bytes exactly:

* Members sorted by Unicode **code point** of the key (Python `str` order), not
  UTF-16 code unit order. Arrays keep their order. No whitespace.
* Strings: raw UTF-8 (no `\uXXXX` for non-ASCII); escaped are `"` `\\`, the
  short forms `\b \f \n \r \t`, and every other U+0000–U+001F as `\u00xx`
  (lowercase hex). A lone surrogate is refused.
* A number's **kind follows its lexeme**, as Python's `json` module parses it:
  a lexeme without `.`, `e` or `E` is an integer and is written as its exact
  decimal value (any magnitude; `-0` → `0`). Any other lexeme is a float, parsed
  to the nearest IEEE-754 double and written as Python `repr(float)`: the
  shortest round-trip digits; positional notation when the decimal exponent
  `e` satisfies `-4 <= e < 16`, always with a fractional part (`1.0`, `-0.0`,
  `100.0`); otherwise scientific notation `d[.ddd]e±XX` with at least two
  exponent digits (`1e+16`, `1.5e-05`, `5e-324`). A float that overflows to
  infinity is refused.
* Duplicate member names are refused (Python's `json.loads` would silently keep
  the last; a portable client must not guess). Nesting depth at most 64.

The expected response digest of a replacement is
`"sha256:" + hex(SHA-256(canonical_json(replacement["descriptor"])))`. The
replacement must be a JSON object whose `descriptor` member is an object; any
canonicalisation failure is refused locally as `INVALID_REQUEST`
(field `replacement`) with zero transport calls. Validating the replacement
against `predator.qsearch.request.v2` and refusing a reused identity or target
are **server** decisions (`INVALID_REQUEST`, `NEW_IDENTITY_REQUIRED`,
`NEW_TARGET_REQUIRED`, `ORIGINAL_ATTEMPT_UNRECONCILED`); a client does not
pre-empt them, because doing so would need an extra status read.

`fixtures/canonical.v2.json` pins canonical bytes and digests produced by Python.

## 3. Responses

Bytes, status-code, redirect, cap, strict-JSON and error-mapping rules are v1
section 2 unchanged. On 2xx the body must validate against
`../v1/job-state.v1.schema.json` in full and be bound:

* `refresh`/`cancel`: `request.request_digest` equals the requested digest.
* `replace`: `request.request_digest` equals the replacement descriptor digest
  **and** `lineage.replaces_request_digest` equals the original digest.

Any failure is `INVALID_RESPONSE` with `MANUAL_RECONCILIATION`.

## 4. Side effects and meaning

* One control invocation is **exactly one** transport call, or zero when refused
  locally. No status read, retry, cancel, refresh, replace or resubmission ever
  follows automatically from a response, error, deadline or `next_action`.
* A transport failure or client timeout on a control is `TRANSPORT_UNAVAILABLE`,
  `retryable: true`, `next_action: MANUAL_RECONCILIATION`: the acknowledgement
  is unknown and the caller must observe (status) before deciding anything. It
  never implies the control took effect or was cancelled.
* A cancel response is the server's state **after** the request. It is not
  cancellation: only `execution.status: CANCELLED` (with
  `cancellation.confirmed: true`) is. `CANCELLATION_REQUESTED`, a `RUNNING`/`DONE`
  provider status, or a lost acknowledgement are reported as they are.
* `refresh` may return an unchanged state (the server throttles provider polls).
* A retry of a control by the caller uses a fresh nonce; the durable request
  identity is unchanged.

## 5. Client errors

v1 section 2.4 applies. Additional local refusals:

| Code | When | retryable | next_action |
| --- | --- | --- | --- |
| `UNSUPPORTED_CAPABILITY` | operation not granted (field `operation`) | false | NONE |
| `INVALID_REQUEST` | replacement cannot be canonicalised (field `replacement`) | false | NONE |

## 6. Known Python discrepancies

Recorded as strict-xfail compatibility tests in
`tests/test_qsearch_lifecycle_wire_v2.py`. Expected at freeze:

* The Python client's `replace` takes an already-parsed `Mapping`, so duplicate
  members in replacement text are resolved by the caller's parser (last wins),
  not refused. Text-level rules apply to text-accepting clients.
* The Python client has no capability set: every operation is always available.
