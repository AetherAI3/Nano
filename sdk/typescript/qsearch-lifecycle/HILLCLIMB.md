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

# v2 controls (Nano #101, PR2)

Budget: at most 3 candidate iterations against the dev set, same rules (never
weaken validation, delete a negative test, or relabel a skip as a pass).

Dev set: `test-support/canonical-devset.json`, 19 accepted vectors and 40
refusals, derived from the real Python reference
(`nano_qsearch.canonical.canonical_json(json.loads(text))`, Python 3.13.5) by
`test-support/derive-canonical-devset.py`: key order by code point (BMP above
U+FF00 against astral, escaped and raw), every control character, the short
escapes, raw U+2028/U+2029/U+FEFF/DEL, escaped surrogate pairs, an escaped
`descriptor` key, integers up to 4300 digits, `-0`, floats in both repr
notations, underflow, depth exactly 64; refusals for duplicates (also after
escape decoding), depth 65, lone surrogates, NaN/Infinity, overflow, 4301-digit
integers, RFC 8259 violations, BOM, invalid/overlong UTF-8, UTF-16, and every
wrong replacement shape. Plus the fake-transport client suite
(`test/lifecycle.test.mjs`) and the property tests in `test/canonical.test.mjs`.

Environment for every run: Node v24.18.0, npm 11.16.0, typescript 5.8.3,
Windows 10 Home 10.0.19045 (Git Bash), base `5ec301d` (contract v2 freeze).
Node v22.19.0 used as a second runtime where stated.

## Candidate 1: initial implementation

- Hypothesis: a separate lexeme-preserving strict parser that canonicalises
  each number at parse time (BigInt for integer lexemes; `toExponential()`
  shortest digits re-laid out as Python `repr` for float lexemes), a
  code-point comparator for member order, and a `ensure_ascii=False` string
  encoder reproduce Python byte for byte; the transport/timer/signing engine of
  `StatusClient` can be shared unchanged by a capability-gated
  `LifecycleClient` if hashing runs as one more phase inside the same deadline.
- Diff: new `src/canonical.ts`; `src/client.ts` extracts the engine into an
  internal `Core` (v1 framing keeps the frozen request shape, v2 framing adds
  `body`), `StatusClient` becomes a thin wrapper, new `LifecycleClient`;
  `src/errors.ts` (`transportUnavailable(method)`, `unsupportedCapability`,
  `invalidRequest`); `src/wire.ts` v2 constants; `src/webcrypto.ts`
  `createSha256`; `src/fetch.ts` sends the POST body; `src/index.ts` exports.
- Commands and results:
  - `node --test` (existing 81 tests, before any new test): 81 pass, 0 fail.
  - `node --test test/canonical.test.mjs`: dev set 2/2 pass on the first run
    (all 19 vectors byte-equal, all 40 refusals INVALID_REQUEST). The property
    test failed twice, both on the oracle side, never on the canonicaliser:
    1. int-only #3, "same value" check: the test compared `JSON.parse` of the
       canonical output with `JSON.parse` of the input using deepStrictEqual,
       which tells `-0` from `0`; canonical `-0` is `0` by contract. Fixed the
       test's comparison (normalise `-0` on both sides); no implementation
       change, no assertion removed.
    2. int-only #12: `canonical(text)` and `canonical(JSON.stringify(JSON.parse(text)))`
       differed in one key. Delta-debugging showed `JSON.parse` itself is
       wrong: on Node 24.18.0, once an object with keys `""` and an escaped
       backslash (U+005C) has been parsed, parsing `{"":0,"<escaped U+000D>":1}`
       returns the key U+005C instead of U+000D (a V8 map-transition reuse
       bug; Node 22.19.0 returns U+000D). Python's canonical bytes for the
       minimal case equal the TS output. Diagnosis: the oracle is broken, the
       canonicaliser is right.
  - `node --test test/lifecycle.test.mjs test/drift-v2.test.mjs test/fetch.test.mjs`:
    36 pass, 0 fail on the first run.
- Regression delta: v1 suite 81/81 before and after (no existing test edited).

## Candidate 2: property-test oracle hardening (test-only)

- Hypothesis: keeping the property while avoiding the Node 24 `JSON.parse`
  defect needs only a narrower key alphabet on the oracle side, plus a pinned
  test proving the canonicaliser handles the exact defect sequence.
- Diff (tests only): generated object keys never contain U+005C (it maps to
  `/`); string values still do, and the dev set still covers backslash keys.
  Added "escaped single-character keys decode exactly, whatever was parsed
  before", which runs the defect sequence through `canonicalJson` and pins
  the Python bytes.
- Command: `node --test test/canonical.test.mjs`: 8 pass, 0 fail.
- Regression delta: +1 test, 0 regressions; no implementation change.

## Golden runner self-check (no implementation change)

I generated temporary fixtures in the exact v2 interface format with the real
Python `nano_qsearch.client.LifecycleClient` (injected transport) plus
`auth._message`, from a scratch script outside the repo, covering the
canonical dev set, a signing vector per operation, three histories, two
remote errors and three binding negatives. Results with them present:
`node --test test/goldens-v2.test.mjs` 6 pass, 0 fail; the whole suite 120
pass, 0 fail, 0 skipped on Node 24.18.0 and on Node 22.19.0. I then deleted
the fixtures (not committed). Without fixtures, the 6 v2 golden tests fail
with "v2 golden fixture directory missing"; nothing is skipped.

## Current totals (`npm test`, no v2 fixtures yet)

120 tests: 114 pass, 6 fail (exactly the v2 golden tests, fixtures missing),
0 skipped, 0 cancelled. `npm run pack:check`: ok.
