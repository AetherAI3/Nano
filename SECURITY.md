# Security policy

Nano is an alpha reference implementation for deterministic, host-governed decision rules. This document explains how to report a vulnerability and what the current code does and does not protect.

## Reporting a vulnerability

Please do not open a public issue for security-sensitive reports. Use [GitHub private vulnerability reporting](https://github.com/DBarr3/Nano/security/advisories/new) and include a minimal reproduction where possible. The latest `main` branch is the supported security-fix target.

## Security properties of the current core

| Concern | Current protection |
| --- | --- |
| Malformed strategy IR | `StrategyGraph.from_dict()` validates the supported version, nodes, effects, and intent-manifest requirement. |
| A strategy directly calling an external system | The v0.1 grammar and reference runtime expose only intent emission; there is no exchange, network, subprocess, or action primitive. |
| Reference-runtime reproducibility | The interpreter consumes caller-provided frames and does not read an ambient clock, RNG, or network. Identical graph/frame inputs produce the same reference result. |
| Gate separation | `NanoBridge` forwards intents to a host-provided `DecisionGate` and records its returned decisions. It does not execute them. |
| Optional receipt verification | The optional Protocol-C adapter can sign and verify host decision receipts when its dependency is installed. |

## Boundaries and non-goals

Nano is **not** an OS sandbox, a complete authorization system, or a durable audit platform.

- **The host process is trusted.** Any Python code in the same process, including a data feed, gate, or dependency, can perform side effects outside Nano's strategy boundary.
- **The host owns the gate.** Nano cannot guarantee that a host policy is correct, deterministic, or safe. `Backtester.verify_replay()` can detect divergent gate results; it cannot prevent a permissive gate from approving a bad action.
- **Inputs are caller-provided.** Deterministic replay means identical inputs reproduce the reference result. It does not authenticate market data, prove its provenance, or prevent poisoned inputs.
- **The base log is in memory.** It is an ordered result value, not durable append-only storage or tamper-proof evidence. Durable storage and signing are integration responsibilities.
- **Strategy IR is not content-addressed.** The separate experimental `LoopGraph` has a content hash; `StrategyGraph` does not.
- **No LLM/runtime escalation exists in the core.** Model safety, prompt injection, live actuation, and autonomous deployment are outside the current implementation.

## In scope for reports

- strategy-IR validation bypasses or manifest violations that lead to invalid reference execution;
- an intent/action path that bypasses the `DecisionGate` boundary in the shipped core;
- nondeterministic behavior from identical graph/frame inputs in the reference runtime;
- defects in optional provenance receipt verification, when the optional dependency is installed; and
- security-relevant crashes or data corruption in the public Python API.

## Out of scope

- vulnerabilities in a host's gate, data feed, execution connector, or Python environment;
- market losses or strategy quality;
- unsupported research proposals described only in design notes; and
- third-party supply-chain defects without a Nano-specific integration issue.

For the implementation map, see [docs/architecture.md](docs/architecture.md). For current maturity, see [docs/status.md](docs/status.md).
