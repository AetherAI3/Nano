# Nano documentation

This directory separates the **implemented contract** from **design notes and research directions**.

For questions about what Nano does today, start with the reference documents below. The source and test suite are authoritative when documentation is unclear or out of date.

## Reference documentation

| Document | Purpose |
| --- | --- |
| [Architecture](architecture.md) | The actual source -> IR -> interpreter -> host-gate flow and module boundaries. |
| [Language reference](language.md) | The locked grammar, IR shape, and execution semantics. **Known gap:** this document still describes only the v0.1.0 subset; the shipped compiler implements the v1.0 superset (declarations, expressions, offsets, `else`, computed indicators). Until it is rewritten, `nano/compiler/parser.py` carries the authoritative v1.0 grammar and `nano/indicators/registry.py` the indicator set. |
| [Watchdog profile](watchdog_profile.md) | The restricted subset for host-governed controls: signal contracts, missing-input semantics, artifacts, receipts, and replay. |
| [Status](status.md) | Implemented, experimental, optional, and unimplemented capabilities. |
| [Package CLI](package-cli.md) | `nano package check` and `nano package build`: explicit `--manifest`/`--lock` arguments, exit codes, canonical JSON framing, positioned diagnostics, and output safety for RFC 0066 packages. |
| [Immutable records](rfc-0085-immutable-records.md) | Package-only typed records, executable IR 1.1.0, strict host-input validation and fixed ceilings. |
| [Native quantum bridge](quantum-native-bridge.md) | Opt-in, local-only prepare/sample path: pinned-Qiskit prepared artifact, `/qsearch/native/v1` routes, caller-retained verification. |
| [Proposal benchmark](proposal-benchmark.md) | Frozen offline proposal-and-repair benchmark: dataset, metrics, locked split and replay command. |
| [Run log](run-log.md) | Run log v1 from `nano replay --report json`: guaranteed members, event list, byte rules, timestamp rule, versioning, and its link to receipts ([schema](run-log.schema.json)). |
| [Run receipts](receipts.md) | The versioned run artifact: canonical bytes, executable identity, and what external consumers may depend on. |
| [Q-search service](qsearch-service.md) | `nano-qsearch-serve`, the host-side `nano_qsearch` package (beside `nano/`, not part of the deterministic engine) that serves sealed q-search receipts to Predator Drive: configuration, auth, caps, recovery. |
| [Strategy corpus](../nano/library/README.md) | Conventions for paired `.nano` and IR examples. |
| [Build notes](../BUILD_ORDER.md) | Historical sequencing and the project's architectural constraints. |
| [Security policy](../SECURITY.md) | Reporting process and the boundary of Nano's guarantees. |

## Integration examples

- [`../examples/`](../examples/) contains runnable host-integration demonstrations.
- [`../nano/examples/`](../nano/examples/) is the source/IR conformance fixture corpus, not a generic application-examples directory.
- [`../nano/aethercode/`](../nano/aethercode/) contains pure editor-service helpers (diagnostics, semantic tokens, and IR preview), not a packaged editor extension.

## Design notes and research directions

[`papers/`](papers/) contains essays about the broader Nano thesis, possible LLM integration, autonomous optimization, and research directions. These documents may describe proposals that are not part of the runtime. Treat them as design material, not as a language specification or a promise of current behavior.

For the project entry point, return to the repository [README](../README.md).
