"""Nano — deterministic intelligence execution graphs.

IR-first: the execution graph (nano.ir) and reference runtime (nano.runtime) came
before the surface language (nano.compiler), and the IR is still the contract
every runtime speaks.

The v1.0 layout:

| Package | Role |
|---|---|
| ``nano.compiler`` | `.nano` -> IR: lexer, parser, codegen |
| ``nano.types`` | the type system, and look-ahead protection |
| ``nano.indicators`` | typed indicator signatures + deterministic kernels |
| ``nano.ir`` | both IR document versions, and the DAG runtimes execute |
| ``nano.runtime`` | the reference interpreter and the VM |
| ``nano.bridge`` | decision-gate adapter, backtester, optional provenance |
| ``nano.data`` | the one place that reads a file |
| ``nano.cli`` | check, compile, replay, visualize |
| ``nano.aethercode`` | editor language services |
| ``nano.memory`` / ``nano.loop`` | compiled-pattern cache, Nano++ loop IR |

Kept dependency-free on purpose: `pip install aether-nano` pulls in nothing, so a
compiled artifact cannot change behavior because a transitive dependency did.
"""

__version__ = "1.0.0"
