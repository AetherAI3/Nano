"""Nano - deterministic, host-governed rule execution.

The core package provides an IR-first strategy DSL with static typing, a
reference interpreter and VM, and host-gate integration primitives. It does not
include an LLM runtime, live-action connector, or general agent executor: a
reasoning provider is a protocol the host implements, and market data arrives as
frames the host supplies.

Package map:

| Package | Role |
|---|---|
| ``nano.compiler`` | `.nano` -> IR: lexer, parser, codegen |
| ``nano.types`` | the type system, and look-ahead protection |
| ``nano.indicators`` | typed indicator signatures + deterministic kernels |
| ``nano.ir`` | both IR document versions, and the DAG runtimes execute |
| ``nano.runtime`` | the reference interpreter and the VM |
| ``nano.bridge`` | decision-gate adapter, backtester, optional provenance |
| ``nano.watchdog`` | the restricted profile for host-governed controls |
| ``nano.data`` | the one place that reads a file |
| ``nano.cli`` | check, compile, replay, visualize |
| ``nano.aethercode`` | editor language services |
| ``nano.memory`` / ``nano.loop`` | compiled-pattern cache, loop IR |

Kept dependency-free on purpose: installing pulls in nothing, so a compiled
artifact cannot change behavior because a transitive dependency did.
"""

__version__ = "1.0.1"
