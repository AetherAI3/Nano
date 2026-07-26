# Nano documentation

This directory separates the **implemented contract** from **design notes and research directions**.

For questions about what Nano does today, start with the reference documents below. The source and test suite are authoritative when documentation is unclear or out of date.

## Reference documentation

| Document | Purpose |
| --- | --- |
| [Architecture](architecture.md) | The actual source -> IR -> interpreter -> host-gate flow and module boundaries. |
| [Language reference](language.md) | The locked v0.1.0 grammar, IR shape, and execution semantics. |
| [Status](status.md) | Implemented, experimental, optional, and unimplemented capabilities. |
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
