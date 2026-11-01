# Packed quantum compile provenance (opt-in contract)

Issue #62 introduces `nano.qsearch.compile-manifest.v1` and
`predator.qsearch.receipt.v3`. These are local issuer/consumer APIs and shared
fixtures. The production `/qsearch/v2` route continues to emit v2; this change
does not activate v3 submission, billing or live hardware. Activation requires
an explicit adapter, compatible Predator verification and review. The host
continues to own admission and authorization.

V2 verifies matching ATLAS envelope/PUB child identities while constructing the
receipt, but does not carry those identities or the compiled plan in its sealed
output. Existing v2 readers and seal domains remain unchanged. A v2 receipt is
never interpreted as a v3 claim, even if a caller knows a compiled plan later.

## Compile identity and evidence

Before dispatch, an adapter freezes and retains a manifest containing:

- The complete source request descriptor digest.
- The target name and content digest of its pinned snapshot, plus the calibration
  digest when a calibration snapshot influenced compilation.
- A versioned compile-profile identifier and digest.
- Compiler name/version, pinned Qiskit version and optional IBM Runtime version.
- An ordered list mapping each PUB index to its requested job, ordered recipient
  agent/chunk/slot identities, width, shots and seed, ATLAS child digest, compiled
  plan digest, ISA artifact digest and logical-to-physical layout.

`build_compile_manifest()` requires every digest and layout explicitly. Missing
ATLAS or compiler evidence is an error; adapters must obtain it from the real
freeze/compile operation. Fixture digests are deliberately synthetic and must
never stand in for execution evidence. The manifest contains no QPY bytes,
source payloads, credentials, keys or samples.

`compile_identity()` is `canonical_digest(manifest)` using Nano/Predator canonical
JSON: UTF-8, sorted keys, compact separators and finite JSON numbers. It excludes
provider usage and settlement. Altering a child, compiled plan, ISA artifact,
layout, profile, compiler, target, calibration, routing or PUB order changes this
identity. Validation rejects malformed or stale manifest versions, reordered
indices, repeated jobs or deliveries, invalid digest strings, bools as integers
and unknown fields. Manifest v1 caps PUBs, logical width and recipients per PUB
at 4096, shots at `2**31 - 1`, and integer fields at `2**53 - 1` for cross-language
JSON interoperability. Layouts have one unique physical index per logical bit.

ATLAS's child wire hashes are bare lowercase SHA-256 hex. The manifest builder
and receipt constructor normalize that representation to `sha256:<hex>`;
manifests themselves admit only the prefixed canonical form. The raw ATLAS
document is preserved under `atlas_receipt_sha256`. This boundary changes
neither ATLAS's ASCII-escaped fingerprint algorithm nor any v2 behavior.

## Issuing and verifying

The issuer calls `request_receipt_v3(request, atlas_receipt,
expected_manifest=approved_manifest, purpose=request.purpose)`, then
`seal_receipt_v3(body, key)`. Construction compares the expected manifest with
the frozen source request and ATLAS envelope children, then applies the existing
sample/energy audit. Partial, duplicate or reordered successful PUB results
refuse the whole request. Mixed widths remain per-PUB. Provider-terminal failures
carry the expected compile identity, submitted job identity and no verified
result blocks.

The standalone consumer calls:

```python
verify_receipt_v3(receipt, key, expected_manifest=retained_approved_manifest)
```

The expected manifest must come from the caller's own approval state. Extracting
it from the received receipt defeats the expectation check. Verification checks
the complete expected manifest identity, ordered block routing, count widths and
shot totals, result digests, hardware shape and accounting history. There is no
default argument that permits verification without this independent expectation.
When the consumer also retains the parsed source request, it should pass
`expected_request=request`. This additionally compares declared purpose,
execution-time limit and each classical floor against the source descriptor.
The independent Predator consumer requires that source-request check. A manifest
alone binds the request's digest and compile mapping; it does not reveal the
descriptor's individual fields for those additional comparisons.

The HMAC domain is the UTF-8 bytes
`predator.qsearch.hardware-receipt.v3\n`, followed by canonical JSON of the body
without `seal`. The seal prefix is `hmac-sha256+hardware-v3:` followed by 64 lower
hexadecimal characters. This differs from every v1/v2 domain. It authenticates
the issuing host's claim; it is not a provider signature, proof of trustworthy
hardware or an independent measurement of billing.

## Accounting revisions

The initial receipt has `accounting_revision: 0` and `accounting_history: []`.
`atlas_receipt_sha256` binds the currently represented ATLAS evidence document.
That digest may change when provider accounting becomes available; the compile
manifest and compile identity do not change.

`reseal_usage_v3()` first verifies the receipt against the independent expected
manifest. It accepts only positive finite usage with `usage_pending: false` and
refuses a decrease in previously observed positive seconds. It appends a snapshot
containing the previous usage block, ATLAS evidence digest, previous seal and
digest of the previous complete sealed receipt. It then replaces only usage,
the optional current ATLAS evidence digest and accounting revision/history.

Every old sealed body can be reconstructed from the immutable current fields,
that snapshot's usage and ATLAS digest, its history index as revision and the
preceding history prefix. Consumers verify each reconstructed HMAC and receipt
digest. Thus historical usage cannot be edited and merely re-sealed as a new
revision. History is bounded at 64 revisions; revisions beyond that fail closed.

## Portable fixtures and schemas

The declarative schemas live in `nano_qsearch/schemas/`. Schema validation alone
does not perform expected-manifest, cryptographic, ordering or accounting checks;
the API enforces those semantic requirements.

`tests/qsearch_fixtures/packed_provenance_v3.json` is the shared Nano/Predator
golden fixture. It includes a public test key, source request, synthetic ATLAS
receipt, expected manifest, its exact canonical JSON and digest, mixed-width
pending receipt and positive settled receipt with historical evidence. The key
is test-only. Independent consumers must read identical fixture bytes and verify
both seals plus expected-manifest and historical-accounting semantics before
activation. No network, IBM service, credentials or optional quantum dependency
is required to run this contract suite.

The explicit prepare/profile protocol in issue #63 supplies source/profile/target
and compiled artifact identities. Its execution adapter must bind actual frozen
ATLAS children before using this receipt contract; it must never invent missing
identity evidence or silently recompile under another approval.

ATLAS's actual frozen-child wire digest uses 64 bare lowercase hex characters.
The boundary accepts that form and the prefixed form used by early local
fixtures, normalizing both into the manifest's required `sha256:<64 lower hex>`
form. This normalization never rewrites the ATLAS evidence document:
`atlas_receipt_sha256` continues to digest the original raw document. Uppercase,
wrong-length or other algorithm spellings fail closed. A received manifest must
already use the canonical prefixed form.
