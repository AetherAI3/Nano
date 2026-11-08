# Hillclimb log: TypeScript status client (Nano #86, PR2)

Budget: at most 3 candidate iterations against the dev set. Rules: never
weaken validation, raise caps, delete a negative test, or relabel a skip as a
pass; stop and report an unresolved failure instead.

Dev set: `test-support/helpers.mjs#devSet()`, nine synthetic good envelopes
(queued, running, completed, failed, cancelled, accounting-pending,
settled-positive, confirmed-zero, unknown-zero-held), plus the differential
oracle `test-support/schema-eval.mjs` (a test-only evaluator of the frozen
`job-state.v1.schema.json`) over single-member mutations and I1-I8 cross
products.

Environment for every run: Node v24.18.0, npm 11.16.0, typescript 5.8.3,
Windows 10 Home 10.0.19045 (Git Bash), contract-freeze base `26955b2`.

## Candidate 1: initial implementation

- Hypothesis: a hand-written recursive-descent parser plus a table-driven
  closed-envelope validator, checked in Python `_call` order (Python's own
  success checks first so they report `field: null` exactly like Python), is
  equivalent to the schema and to Python on everything Python accepts.
- Diff: all of `src/` (client, errors, json, validate, types, wire, webcrypto,
  fetch, index); tests; example; pack-check.
- Commands and results:
  - `node --test test/validate.test.mjs`: 10 pass, 0 fail. Dev set is accepted unchanged,
    deep-frozen, pairwise distinct; 12,000+ differential cases agree with the
    schema oracle.
  - `node --test test/json.test.mjs`: 8 pass, 1 fail. The failure was in
    the test's own normaliser: a `JSON.stringify` round trip turned `-0`
    into `0`. Fixed the test helper to convert structurally. No
    implementation change and no assertion weakened. After: 9 pass, 0 fail.
  - `node --test test/client.test.mjs`: 25 pass, 0 fail.
  - `node --test test/fetch.test.mjs test/webcrypto.test.mjs test/drift.test.mjs`: 24 pass, 0 fail.
- Regression delta: n/a (first candidate).

## Harness fixes (no implementation change)

- `node --test test/` fails on Node >= 22: positional arguments are glob
  patterns, so a bare directory matches nothing ("Cannot find module ...\test").
  Switched the `test` script to `node --test` (default patterns, which cover
  `test/**` on Node 20 and on Node 22+).
- With default patterns, `helpers.mjs` and `schema-eval.mjs` inside `test/`
  were each run as a file with zero tests, which `node --test` reports as 2
  vacuous passes. Moved them to `test-support/`, so counts contain only real
  tests (75 tests, previously 77).
- Golden runner self-check: I generated temporary fixtures in the exact
  interface format with the real Python reference (`nano_qsearch.client.LifecycleClient`
  plus `auth._message`) from a scratch script outside the repo. Result:
  `node --test test/goldens.test.mjs` 4 pass, 0 fail. I then deleted the
  fixtures (not committed). Without fixtures, the same file reports 4 fail,
  0 skip, each with "golden fixture directory missing".

With those temporary Python-derived fixtures present, the whole suite
(`node --test`) ran 75 tests: 75 pass, 0 fail, 0 skipped.

## Candidate 2: independent review follow-ups

- Hypothesis: an independent read-only review (no CRITICAL or HIGH findings)
  found that a hung signer could hang `status()` forever, a timer firing
  synchronously leaked the cancel handle it returned, the base URL accepted
  malformed IPv6 or IPv4-looking hosts, the browser fetch could send Referer
  and use the HTTP cache, and the fetch adapter refused cross-realm chunks.
  Closing these tightens behaviour without weakening any check.
- Diff:
  - `src/client.ts`: one deadline now bounds signing plus the exchange
    (`#signAndExchange`). A signer pending at the deadline is
    CLIENT_CONFIGURATION `signer` with zero transport calls. A timer that
    throws or fires synchronously is CLIENT_CONFIGURATION `set_timer`
    (previously TRANSPORT_UNAVAILABLE; still zero transport calls), and its
    cancel handle is released. The base URL is additionally parsed with
    WHATWG `URL`.
  - `src/fetch.ts`: adds `referrerPolicy: "no-referrer"` and
    `cache: "no-store"`, and accepts cross-realm chunks.
  - New `src/bytes.ts` holds the shared `isUint8Array`/`lowerHex`.
  - Tests: two timer tests now expect the more specific code (CLIENT_CONFIGURATION
    `set_timer`; both still assert zero transport calls). Added tests for a
    hung signer, a signer rejecting late, throwing response getters, the empty
    port (Python parity), the new URL refusals, the fetch init keys and
    cross-realm chunks.
- Commands and results:
  - `npm test`, no fixtures: 80 tests, 76 pass, 4 fail (golden tests only,
    fixtures missing), 0 skipped.
  - `node --test` with temporary Python-derived fixtures: 80 tests, 80 pass,
    0 fail, 0 skipped.
- Regression delta vs candidate 1: +5 tests, 0 regressions. Every
  candidate-1 assertion still holds except the two deliberate timer-setup
  code changes described above.

## Candidate 3: second independent review (lead), integrated with Python goldens

- Inputs: stacked on PR1 `d4d174c` (real Python fixtures). Before this
  candidate, `npm test` with fixtures: 80 tests, 80 pass, 0 fail, 0 skipped.
  Held-out set (frozen by the lead before implementation, never shown to the
  implementer, sha256 `213d0a35...0c0d0e`): 24/24 on candidate 2.
- Hypothesis M1: a signer result that only *looks* like a Uint8Array (spoofed
  `Symbol.toStringTag`) throws inside the `.then` callback, producing an
  unhandled rejection (crashes a default Node process) and, at the deadline,
  a misleading retryable `TRANSPORT_UNAVAILABLE`. Reproduced first: new test
  failed (RED).
- Minimal diff: `bytes.ts` adds `copyUint8Array`, which reads the internal
  `[[TypedArrayName]]` slot via the `%TypedArray%.prototype[@@toStringTag]`
  getter (unforgeable, cross-realm) and copies via the typed-array constructor
  (internal slots only). `client.ts` copies the MAC and builds the request
  inside one try that maps every failure to `CLIENT_CONFIGURATION(signer)`.
  `webcrypto.ts` uses the same copy for the key (cross-realm keys accepted;
  the copy is zeroed on every refusal path).
- Hypothesis M2: consumers on TypeScript `moduleResolution: node10` cannot
  resolve the package or its subpaths. Reproduced first: TS2307 for `.` and
  `./webcrypto` with the old package.json. Diff: top-level `main`, `types`,
  `typesVersions` for subpaths; ships `LICENSE` (MIT) instead of a path
  outside the package.
- README: cleartext warning for `http` base URLs; documents that `-0` and
  underflow behave as `JSON.parse` (a `-0` normalisation was tried and
  reverted because it broke the existing JSON.parse-parity test).
- Commands and results (Node v24.18.0, Windows 10):
  - `npm test` with fixtures: 81 tests, 81 pass, 0 fail, 0 skipped.
  - `npm run pack:check`: ok (install outside checkout, example runs).
  - `tsc --noEmit --strict --target es2022` consumer importing `.`,
    `./webcrypto`, `./fetch`: node10 exit 0, node16 exit 0, bundler exit 0
    (node10 exit 2 with the previous package.json).
- Regression delta vs candidate 2: +1 test, 0 regressions.

## Current totals (`npm test`, fixtures present)

81 tests: 81 pass, 0 fail, 0 skipped, 0 cancelled.
