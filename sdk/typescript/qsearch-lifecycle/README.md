# @nano/qsearch-lifecycle (TypeScript, read-only)

A small, **read-only** status client for the Nano q-search lifecycle wire
contract `nano.qsearch.lifecycle-wire.v1`
([`protocol/qsearch-lifecycle/v1/CONTRACT.md`](../../../protocol/qsearch-lifecycle/v1/CONTRACT.md)).
It reads job state; it cannot cancel, refresh, replace or submit anything.
The Python service and `nano_qsearch.client.LifecycleClient` stay
authoritative. This package is `"private": true` and is not published.

## Runtime target

- Node.js >= 20 or a modern browser. ESM only; compiled to ES2022.
- Zero runtime dependencies. Uses only `TextEncoder`/`TextDecoder`,
  `AbortController`, `setTimeout` (replaceable) and, in the optional
  adapters, `crypto.subtle`/`crypto.getRandomValues` and a fetch you pass in.
- Dev dependency: `typescript` pinned to `5.8.3` (exact) with a committed
  `package-lock.json`.

## Package layout

| Entry point | Contents |
| --- | --- |
| `@nano/qsearch-lifecycle` | `StatusClient`, `LifecycleClientError`, `parseStrictJson`, `validateJobState`, `JOB_STATE_SPEC`, wire constants, types (`JobStateV1`, `Transport`, `Signer`, ...) |
| `@nano/qsearch-lifecycle/webcrypto` | `createHmacSha256Signer(key)`, `createRandomNonce()` |
| `@nano/qsearch-lifecycle/fetch` | `createFetchTransport({ fetch })` |

```
src/        TypeScript sources (client, errors, json, validate, types, wire, webcrypto, fetch)
dist/       tsc output (.js + .d.ts), gitignored, built by `npm run build`
test/       node:test suites on plain .mjs importing ../dist/
examples/   offline-status.mjs (fake transport, synthetic key, no network)
scripts/    pack-check.mjs (out-of-checkout install proof)
```

## Usage

```js
import { StatusClient } from "@nano/qsearch-lifecycle";
import { createHmacSha256Signer, createRandomNonce } from "@nano/qsearch-lifecycle/webcrypto";
import { createFetchTransport } from "@nano/qsearch-lifecycle/fetch";

const client = new StatusClient({
  baseUrl: "https://qsearch.example",           // origin only: no path, query, fragment, user info
  transport: createFetchTransport({ fetch: globalThis.fetch }),
  signer: createHmacSha256Signer(requestKeyBytes), // bytes from YOUR secret store, >= 16 bytes
  clock: () => Date.now() / 1000,                 // Unix seconds
  nonce: createRandomNonce,                       // 32 lowercase hex per call
  // timeoutMs: 30000, setTimer: (cb, ms) => cancelFn
});

const state = await client.status("sha256:<64 lowercase hex>"); // deep-frozen JobStateV1
```

Every capability is caller-injected and required (`setTimer` defaults to
`globalThis.setTimeout`/`clearTimeout`). `examples/offline-status.mjs` is a
complete runnable example with a fake transport.

## Wire summary (normative text: CONTRACT.md)

- One `status(digest)` = exactly one `GET <origin>/qsearch/v2/requests/<digest>`
  with exactly two headers: `X-Aether-Qsearch-Auth: v1 ts=<ts> nonce=<nonce> mac=<mac>`
  and `Accept: application/json`. A malformed digest is refused before any I/O
  (`IDENTITY`).
- `mac = hex(HMAC-SHA256(key, "GET\n" PATH "\n" EMPTY_BODY_SHA256 "\n" TS "\n" NONCE))`;
  `TS = floor(clock())`.
- Response handling, in the order of the Python client: status must be an
  integer 100..599; 3xx is `UNEXPECTED_REDIRECT`; body over 1,048,576 bytes,
  bad UTF-8 (including a BOM), or non-strict JSON is `INVALID_RESPONSE`;
  non-2xx bodies are mapped through the `wire.v1.json` allowlists; 2xx bodies
  must pass the full closed-envelope validation (`job-state.v1.schema.json`,
  invariants I1-I8) and bind to the requested digest.
- One deadline (`timeoutMs`, default 30 s, via the injected `setTimer`) bounds
  signing plus the exchange: a signer still pending at the deadline is
  `CLIENT_CONFIGURATION` (field `signer`) with no transport call; a transport
  still pending is `TRANSPORT_UNAVAILABLE` (retryable, `POLL_STATUS`) and its
  `signal` is aborted. Late results are ignored.
- Errors expose only `code`, `field`, `retryable`, `nextAction`, `httpStatus`;
  the message is always `q-search lifecycle failed (<code>)`.
- `supports(op)` is true only for `"status"`; `requireCapability(op)` throws
  `UNSUPPORTED_CAPABILITY` (field `"operation"`) for cancel/refresh/replace and
  native #61 operations. No write method exists.

## Number and byte handling

- Bodies are bytes (`Uint8Array`). The client re-checks the 1 MiB cap even if
  the transport did not, decodes with a fatal UTF-8 decoder that keeps a BOM
  (which the parser then refuses), and parses with a hand-written RFC 8259
  parser: duplicate member names refused at any depth, numbers must be finite
  with magnitude <= 2^53-1 (no silent rounding of large integers), nesting
  depth <= 64 (the top-level value is depth 1), null-prototype objects so a
  `__proto__` member cannot pollute anything.
- Times and quantum seconds are plain JS numbers (`3600.0` arrives as `3600`).
  Booleans are never numbers and numbers never booleans.
- `null` usage means **unknown** and is returned as `null`, never `0`.
  `settled_quantum_seconds: 0` is passed through only as server-reported data;
  the client does not derive, check or upgrade zero finality, and has no
  budget or accounting logic.

## Crypto adapter boundary

The core never touches keys. It hands the canonical message bytes to the
injected `signer(message) => Promise<Uint8Array>` and requires exactly 32
bytes back (otherwise `CLIENT_CONFIGURATION`, field `signer`). Use the
`webcrypto` adapter, or plug in an HSM/KMS/OS keystore signer with the same
shape. `createHmacSha256Signer` copies the key into a non-extractable
`CryptoKey` and zeroes its private copy once imported.

## What the client never does

- read environment variables, files, cookies or browser storage;
- discover services, follow redirects or contact any origin but the base URL
  (the fetch adapter also sends no cookies or Referer and bypasses HTTP caches);
- retry, poll, cancel, refresh, replace or resubmit (errors, timeouts and
  `next_action` are information for the caller, not actions);
- recompute lifecycle deadlines from its own clock (the request timeout is a
  client-side bound on one exchange, independent of envelope deadlines);
- log, emit telemetry, or put URLs, keys, headers, nonces, bodies or
  underlying exceptions into errors (there is no `cause`).

## Development

```sh
npm ci                 # installs typescript only
npm test               # tsc build, then node --test test/
npm run pack:check     # npm pack -> install into a temp dir outside the repo -> run the example -> clean up
```

`test/goldens.test.mjs` replays Python-derived vectors from
`protocol/qsearch-lifecycle/v1/fixtures/`; it **fails** (never skips) when
that directory is missing. `test/drift.test.mjs` fails if the embedded
constants or the validator drift from the frozen contract files.
