# Changelog

All notable changes to `aether-nano` are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `nano_qsearch`, the q-search service Predator Drive calls
  (`POST /qsearch/v1/submit`, `nano-qsearch-serve`): signed and replay-protected
  requests, HMAC-sealed `predator.qsearch.receipt.v1` receipts, a durable job
  store with restart recovery, statevector and MPS engines on Qiskit Aer behind
  the new `[quantum]` extra, and an IBM runtime that refuses by default. It
  lives beside the deterministic `nano` package. See `docs/qsearch-service.md`.

## [1.1.0] — 2026-09-18

### Added

- A deterministic Intent frontend that turns bounded natural-language requests
  into typed, host-governed plans with canonical receipts.
- Project intelligence for parsing and searching PR, commit, and memory records,
  including evidence-bearing tags, grouping, title suggestions, and compact
  context capsules.
- A faster README on-ramp with a direct PyPI install, live package/star badges,
  and a reproducible CLI demo.
- A manual, OIDC-backed PyPI release workflow with package and test gates.

### Changed

- The package description and project links now expose Nano's rule-engine,
  replay, audit, issue, and changelog surfaces directly in package indexes.

## [1.0.12] — 2026-08-20

### Added

- The curated G3 strategy batch and its byte-stable conformance artifacts.

## [0.1.0] — 2026-07-22

### Added

- First public compiler preview with Nano IR, the reference interpreter,
  host-governed intents, pattern memory, and the initial strategy library.

[Unreleased]: https://github.com/AetherAI3/Nano/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/AetherAI3/Nano/compare/7410e58...v1.1.0
[1.0.12]: https://github.com/AetherAI3/Nano/commit/7410e58
[0.1.0]: https://github.com/AetherAI3/Nano/releases/tag/v0.1.0
