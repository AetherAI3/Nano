"""Static and cross-process guards on Nano's determinism claims.

The package documents four properties in prose — no network, no ambient clock, no
ambient randomness, no mandatory third-party dependency — and until now all four
held by code review alone. Prose does not go red. These do.

The import scan is deliberately an AST walk rather than a text grep: a grep for
``socket`` matches a comment and misses ``from a import socket as s``.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "nano"
SOURCES = sorted(PACKAGE.rglob("*.py"))

# Reaching the network from a rule engine is out of scope by construction, not by
# policy: a strategy is a pure function of the frame it was handed.
NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "urllib",
        "http",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "webbrowser",
        "requests",
        "httpx",
        "aiohttp",
    }
)

# Entropy is injected, never sampled. `random` and friends have no legitimate use
# anywhere in this package.
ENTROPY_MODULES = frozenset({"random", "secrets", "uuid"})

# `time` and `datetime` are importable — `nano/data/frames.py` parses ISO-8601
# timestamps out of a CSV, which is reading data, not reading a clock. What is
# banned is *sampling* the ambient clock or the ambient entropy pool.
AMBIENT_CALLS = frozenset(
    {
        ("time", "time"),
        ("time", "time_ns"),
        ("time", "monotonic"),
        ("time", "monotonic_ns"),
        ("time", "perf_counter"),
        ("time", "perf_counter_ns"),
        ("datetime", "now"),
        ("datetime", "utcnow"),
        ("datetime", "today"),
        ("date", "today"),
        ("os", "urandom"),
        ("os", "getenv"),
    }
)

# The single permitted third-party import, and the single file allowed to make
# it. `nano/bridge/provenance.py` guards it behind try/except and raises
# `ProtocolCUnavailable` rather than degrading silently.
OPTIONAL_DEPENDENCIES = {"aether_protocol_c": "nano/bridge/provenance.py"}

STDLIB = set(sys.stdlib_module_names)


def _imports(tree):
    """Every absolute top-level module name imported by `tree`."""
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module.split(".")[0])
    return names


def _parsed():
    for path in SOURCES:
        yield path.relative_to(ROOT).as_posix(), ast.parse(
            path.read_text(encoding="utf-8"), filename=str(path)
        )


def test_the_scan_actually_reads_the_package():
    """A guard on the guard: an empty file list makes every check below vacuous."""
    assert len(SOURCES) >= 30, f"only found {len(SOURCES)} modules under nano/"
    assert any(name.endswith("runtime/receipt.py") for name, _ in _parsed())


def test_nothing_in_nano_can_reach_the_network():
    offenders = [
        (name, module)
        for name, tree in _parsed()
        for module in _imports(tree)
        if module in NETWORK_MODULES
    ]
    assert offenders == []


def test_nothing_in_nano_samples_ambient_randomness():
    offenders = [
        (name, module)
        for name, tree in _parsed()
        for module in _imports(tree)
        if module in ENTROPY_MODULES
    ]
    assert offenders == []


def test_nothing_in_nano_reads_an_ambient_clock_or_environment():
    offenders = []
    for name, tree in _parsed():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            base = node.value
            if isinstance(base, ast.Name) and (base.id, node.attr) in AMBIENT_CALLS:
                offenders.append((name, f"{base.id}.{node.attr}"))
    assert offenders == []


def test_the_only_third_party_import_is_the_optional_protocol_c_one():
    offenders = []
    for name, tree in _parsed():
        for module in _imports(tree):
            if module in STDLIB or module == "nano":
                continue
            if OPTIONAL_DEPENDENCIES.get(module) == name:
                continue
            offenders.append((name, module))
    assert offenders == []


# -- cross-process guards -----------------------------------------------------


def _run(script: str, **env_overrides) -> str:
    env = dict(os.environ, PYTHONPATH=str(ROOT), **env_overrides)
    done = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


_DIGEST_SCRIPT = """
from nano.compiler import compile_module
from nano.runtime.interpreter import MarketFrame
from nano.runtime.receipt import build_receipt, receipt_digest
from nano.runtime.vm import run_module

SOURCE = (
    "strategy Seeded {\\n"
    "    input close: series<float>\\n"
    "    input volume: series<float>\\n"
    "    let z = ZSCORE(close, 3)\\n"
    "    every 1m {\\n"
    "        if z > 0 {\\n"
    "            buy(BTC, 0.7)\\n"
    "        }\\n"
    "    }\\n"
    "}\\n"
)
module = compile_module(SOURCE)
frame = MarketFrame(
    timestamps=(0, 60, 120, 180, 240),
    signals={"close": (1.0, 3.0, 2.0, 8.0, 5.0), "volume": (9.0, 8.0, 7.0, 6.0, 5.0)},
)
print(receipt_digest(build_receipt(module, frame, run_module(module, frame))))
"""


@pytest.mark.parametrize("seed", ["0", "1", "12345", "4294967295"])
def test_a_receipt_digest_survives_a_permuted_hash_seed(seed):
    """String hashing is randomised per process unless PYTHONHASHSEED is pinned.

    Anything that surfaced a `set` or an unsorted `dict` iteration into the
    receipt would produce a different digest under a different seed, and a
    single-process test run cannot see that. The receipt's frame hash covers a
    two-signal mapping specifically so that ordering has something to get wrong.
    """
    assert _run(_DIGEST_SCRIPT, PYTHONHASHSEED=seed) == _run(
        _DIGEST_SCRIPT, PYTHONHASHSEED="0"
    )


_ISOLATION_SCRIPT = """
import sys
import nano.runtime.receipt  # noqa: F401

leaked = sorted(
    name
    for name in sys.modules
    if name == "aether_protocol_c" or name.startswith("nano.bridge")
)
print(",".join(leaked) or "(none)")
"""


def test_the_receipt_path_does_not_drag_in_protocol_c():
    """A base receipt must be constructible with `aether-protocol-c` absent.

    Protocol-C signing carries a real wall clock and fresh entropy by design —
    that is what makes a signature evidence of *when*. It is therefore optional
    and outside deterministic core behavior, which is only true if importing the
    receipt path never reaches it.
    """
    assert _run(_ISOLATION_SCRIPT) == "(none)"


_UNSIGNED_SCRIPT = """
from nano.bridge.provenance import PROTOCOL_C_AVAILABLE
from nano.compiler import compile_module
from nano.runtime.interpreter import MarketFrame
from nano.runtime.receipt import build_receipt, receipt_digest
from nano.runtime.vm import run_module

module = compile_module(
    "strategy Bare {\\n    every 1m {\\n        observe()\\n    }\\n}\\n"
)
frame = MarketFrame(timestamps=(0, 60), signals={})
receipt = build_receipt(module, frame, run_module(module, frame))
print(receipt_digest(receipt), PROTOCOL_C_AVAILABLE, "signature" in receipt)
"""


def test_an_unsigned_receipt_is_complete_and_claims_no_signature():
    digest, _available, has_signature = _run(_UNSIGNED_SCRIPT).split()
    assert digest.startswith("sha256:")
    # Whether Protocol-C happens to be installed must not change the artifact.
    assert has_signature == "False"
