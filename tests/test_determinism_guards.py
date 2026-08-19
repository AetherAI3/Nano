"""Static and cross-process guards on Nano's determinism claims.

The package documents four boundaries in prose — no direct network or ambient
source in deterministic code, and no mandatory third-party dependency. These
tests are executable regression guards for statically visible imports,
references, and simple aliases. They are not a whole-program proof for dynamic
Python; the runtime and receipt tests below cover the externally promised bytes.

The scan is an AST walk rather than a text grep for three reasons a grep cannot
cover: it does not match comments or docstrings, it resolves import aliases
(``import time as t`` and ``from time import time as now`` both normalise to
``time.time``), and it flattens dotted chains, so ``datetime.datetime.now()``
and ``datetime.now()`` — different AST shapes, same ambient read — are one rule.

Everything is normalised to a dotted path before matching. `import datetime`
followed by `datetime.datetime.now` and `from datetime import datetime` followed
by `datetime.now` both resolve to ``datetime.datetime.now``, which is the entry
in ``AMBIENT_READS``.
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

# `time` and `datetime` stay importable: `nano/data/frames.py` parses ISO-8601
# timestamps out of a CSV, which is reading data, not reading a clock. What is
# banned is *sampling* an ambient source. Matched as a prefix, so `os.environ`
# also covers `os.environ.get(...)` and `os.environ[...]`.
AMBIENT_READS = frozenset(
    {
        "time.time",
        "time.time_ns",
        "time.monotonic",
        "time.monotonic_ns",
        "time.perf_counter",
        "time.perf_counter_ns",
        "time.process_time",
        "time.localtime",
        "time.gmtime",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
        "os.urandom",
        "os.getenv",
        "os.environ",
        "os.times",
        "os.cpu_count",
    }
)

# Reading the host by another door. `platform.node()` and `getpass.getuser()`
# are ambient environment reads wearing a different name, and `subprocess` is a
# door to everything above it at once.
HOST_ESCAPE_MODULES = frozenset({"subprocess", "platform", "getpass", "pwd", "grp"})

# Dynamic import is a hole straight through every module-level rule above, and
# `getattr(module, "name")` is the same evasion one level down.
DYNAMIC_IMPORTS = frozenset({"__import__", "importlib.import_module"})

# The single permitted third-party import, and the single file allowed to make
# it. `nano/bridge/provenance.py` guards it behind try/except and raises
# `ProtocolCUnavailable` rather than degrading silently.
OPTIONAL_DEPENDENCIES = {"aether_protocol_c": "nano/bridge/provenance.py"}

STDLIB = set(sys.stdlib_module_names)


def _alias_map(tree: ast.AST) -> dict:
    """Local name -> the dotted path it refers to.

    ``import time as t`` binds ``t`` to ``time``; ``from datetime import
    datetime`` binds ``datetime`` to ``datetime.datetime``; ``from os import
    urandom as rand`` binds ``rand`` to ``os.urandom``. Relative imports are
    in-package and bind nothing interesting.
    """
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for entry in node.names:
                if entry.asname:
                    aliases[entry.asname] = entry.name
                else:
                    # `import os.path` binds the top-level name `os`.
                    head = entry.name.split(".")[0]
                    aliases[head] = head
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for entry in node.names:
                aliases[entry.asname or entry.name] = f"{node.module}.{entry.name}"
    # Follow simple name/attribute aliases to a fixed point. Without this,
    # ``clock = datetime; clock.datetime.now()`` walks around the import resolver
    # while still being trivial to resolve statically. Complex dataflow
    # (arguments, containers, conditional assignment) remains outside this
    # deliberately local guard.
    assignments = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                assignments.append((target.id, node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                assignments.append((node.target.id, node.value))

    for _ in range(len(assignments) + 1):
        changed = False
        for name, value in assignments:
            resolved = _resolve(value, aliases)
            if resolved is not None and aliases.get(name) != resolved:
                aliases[name] = resolved
                changed = True
        if not changed:
            break
    return aliases


def _dotted(node: ast.AST) -> list:
    """Flatten an attribute chain into its parts, innermost first.

    ``datetime.datetime.now`` -> ``["datetime", "datetime", "now"]``. Anything
    not rooted in a bare name (``self.frame.timestamps``, a call result) returns
    empty, because it cannot be resolved statically.
    """
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return []
    parts.append(node.id)
    parts.reverse()
    return parts


def _resolve(node: ast.AST, aliases: dict):
    """The dotted path `node` refers to, or None if it is not import-rooted."""
    parts = _dotted(node)
    if not parts or parts[0] not in aliases:
        return None
    return ".".join([aliases[parts[0]], *parts[1:]])


def _banned_prefix(path: str, banned) -> bool:
    return any(path == entry or path.startswith(entry + ".") for entry in banned)


def _imports(tree: ast.AST) -> list:
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


def _references(tree: ast.AST):
    """Every statically resolvable dotted reference in `tree`, with its aliases."""
    aliases = _alias_map(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Attribute, ast.Name)):
            resolved = _resolve(node, aliases)
            if resolved is not None:
                yield resolved


def test_the_scan_actually_reads_the_package():
    """A guard on the guard: an empty file list makes every check below vacuous."""
    assert len(SOURCES) >= 30, f"only found {len(SOURCES)} modules under nano/"
    assert any(name.endswith("runtime/receipt.py") for name, _ in _parsed())


def test_the_scan_resolves_aliases_and_then_matches_them():
    """A guard on the guard: prove both halves of the scan actually work.

    A resolver that returned None for everything, or a matcher that returned
    False for everything, would make every scan below pass on an empty offender
    list. Both halves are exercised here with positive and negative controls.
    """
    tree = ast.parse(
        "import datetime\n"
        "import time as t\n"
        "from time import time as now\n"
        "from os import urandom\n"
        "a = datetime.datetime.now()\n"
        "b = t.monotonic()\n"
        "c = now()\n"
        "d = urandom(8)\n"
        "clock = datetime\n"
        "clock_type = clock.datetime\n"
        "e = clock_type.now()\n"
    )
    aliases = _alias_map(tree)
    assert aliases["clock"] == "datetime"
    assert aliases["clock_type"] == "datetime.datetime"
    resolved = set(_references(tree))
    assert {
        "datetime.datetime.now",
        "time.monotonic",
        "time.time",
        "os.urandom",
    } <= resolved

    # And the MATCHER, not just the resolver. Guarding only the resolver left
    # both ambient scans vacuous by a one-word edit: `_banned_prefix -> False`
    # turned them into `[] == []` while the whole suite stayed green.
    assert _banned_prefix("datetime.datetime.now", AMBIENT_READS)
    assert _banned_prefix("os.environ.get", AMBIENT_READS)  # prefix, not equality
    assert _banned_prefix("importlib.import_module", DYNAMIC_IMPORTS)
    # Negative controls, so "always True" is not a passing implementation either.
    assert not _banned_prefix("datetime.date", AMBIENT_READS)
    assert not _banned_prefix("json.dumps", AMBIENT_READS)


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
    offenders = [
        (name, resolved)
        for name, tree in _parsed()
        for resolved in _references(tree)
        if _banned_prefix(resolved, AMBIENT_READS)
    ]
    offenders += [
        (name, module)
        for name, tree in _parsed()
        for module in _imports(tree)
        if module in HOST_ESCAPE_MODULES
    ]
    assert offenders == []


def test_nothing_in_nano_imports_dynamically():
    """Dynamic import, or dynamic attribute access, routes around every rule above.

    Both reach a banned name as a string, which is invisible to any check that
    matches on names. Simple assignment aliases are resolved above; values that
    flow through arguments, containers, or branches still require real dataflow
    analysis. This scan is a guard, not a general Python static analyser.
    """
    offenders = []
    for name, tree in _parsed():
        aliases = _alias_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                offenders.append((name, "__import__"))
                continue
            # `getattr(time, "time")()` reaches a banned member by string, which
            # every rule above matches by name. Only flagged when the target
            # resolves to an import -- `getattr(self.out, "buffer", None)` and
            # `getattr(result, "escalations", ())` are ordinary duck typing.
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and node.args
                and _resolve(node.args[0], aliases) is not None
            ):
                offenders.append((name, f"getattr({ast.unparse(node.args[0])}, ...)"))
                continue
            resolved = _resolve(node.func, aliases)
            if resolved is not None and _banned_prefix(resolved, DYNAMIC_IMPORTS):
                offenders.append((name, resolved))
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
