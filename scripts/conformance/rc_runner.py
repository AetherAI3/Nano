"""Process runners: scrubbed child environment, pytest, Node and check children.

Children never inherit the caller's environment wholesale. Only the names in
``ENV_ALLOWLIST`` pass through; HOME/USERPROFILE and TEMP point at empty
harness directories, so no user configuration or credential file is found.
Evidence records environment variable NAMES only.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from rc_common import redact_argv, sha256_bytes
from rc_evaluate import Execution
from rc_junit import JUnitError, JUnitSummary, parse_junit

ENV_ALLOWLIST = (
    "COMSPEC", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "NUMBER_OF_PROCESSORS", "PATH", "PATHEXT",
    "PROCESSOR_ARCHITECTURE", "SYSTEMDRIVE", "SYSTEMROOT", "TZ", "USER", "USERNAME", "WINDIR",
)
HARNESS_ENV = {
    "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    "PIP_NO_INPUT": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}
DEPENDENCY_NAMES = ("jsonschema", "numpy", "pytest", "qiskit", "qiskit-aer")
PROBE_TIMEOUT_S = 120

BLOCKED_MODULES = ("aether_atlas", "numpy", "predator", "qiskit", "qiskit_aer", "qiskit_ibm_runtime",
                   "rustworkx", "scipy")

BLOCKER_SOURCE = """
import sys
_BLOCKED = set(%r)
class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCKED:
            raise ModuleNotFoundError("blocked by the release-conformance harness: " + name, name=name)
        return None
sys.meta_path.insert(0, _Blocker())
""" % (BLOCKED_MODULES,)

_PROBE = """
import json, platform, sys
from importlib import metadata
deps = {}
for name in json.loads(sys.argv[1]):
    try:
        deps[name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        deps[name] = None
print(json.dumps({"implementation": platform.python_implementation(),
                  "version": platform.python_version(), "system": platform.system(),
                  "machine": platform.machine(), "platform": platform.platform(), "deps": deps,
                  "base_executable": getattr(sys, "_base_executable", sys.executable)}))
"""


@dataclass(frozen=True)
class Proc:
    launched: bool
    exit_code: Optional[int]
    timed_out: bool
    startup_error: str
    stdout: bytes
    stderr: bytes


def child_env(out: Path, inherited: Mapping[str, str], extra: Mapping[str, str] = ()) -> Dict[str, str]:
    home = out / "home"
    tmp = out / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    env = {name: inherited[name] for name in ENV_ALLOWLIST if name in inherited}
    env.update(HARNESS_ENV)
    env.update({"HOME": str(home), "USERPROFILE": str(home), "TEMP": str(tmp), "TMP": str(tmp),
                "TMPDIR": str(tmp)})
    env.update(dict(extra))
    return env


def env_names(inherited: Mapping[str, str]) -> dict:
    return {
        "allowlist": list(ENV_ALLOWLIST),
        "inherited_present": sorted(name for name in ENV_ALLOWLIST if name in inherited),
        "harness_set": sorted([*HARNESS_ENV, "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "PYTHONPATH"]),
    }


def run_proc(argv: Sequence[str], cwd: Path, env: Mapping[str, str], timeout_s: int) -> Proc:
    try:
        done = subprocess.run(list(argv), cwd=str(cwd), env=dict(env), capture_output=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        return Proc(True, None, True, "", exc.stdout or b"", exc.stderr or b"")
    except OSError as exc:
        return Proc(False, None, False, f"{type(exc).__name__}: {exc.strerror or exc}", b"", b"")
    return Proc(True, done.returncode, False, "", done.stdout, done.stderr)


def write_log(path: Path, procs: Sequence[Proc]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        for proc in procs:
            handle.write(proc.stdout)
            handle.write(b"\n--- stderr ---\n")
            handle.write(proc.stderr)
            handle.write(b"\n=== end ===\n")


def probe_interpreter(python: str, cwd: Path, env: Mapping[str, str],
                      names: Sequence[str] = DEPENDENCY_NAMES) -> dict:
    proc = run_proc([python, "-I", "-c", _PROBE, json.dumps(list(names))], cwd, env, PROBE_TIMEOUT_S)
    if not proc.launched or proc.exit_code != 0:
        detail = proc.startup_error or proc.stderr.decode(errors="replace")
        raise OSError(f"interpreter probe failed: {detail}")
    return json.loads(proc.stdout.decode("utf-8"))


def probe_node(node: str, cwd: Path, env: Mapping[str, str]) -> Optional[str]:
    proc = run_proc([node, "--version"], cwd, env, PROBE_TIMEOUT_S)
    if not proc.launched or proc.exit_code != 0:
        return None
    return proc.stdout.decode("ascii", errors="replace").strip()


@dataclass(frozen=True)
class Context:
    src: Path
    out: Path
    python: str
    env: Mapping[str, str]
    redactions: Mapping[str, str]
    timeout_s: int


def _rel(ctx: Context, path: Path) -> str:
    return path.relative_to(ctx.out).as_posix()


def _report(ctx: Context, junit: Path, root: Optional[str]) -> Tuple[Optional[JUnitSummary], str]:
    if not junit.is_file():
        return None, "no JUnit report written"
    try:
        return parse_junit(junit.read_bytes(), root=root, redactions=ctx.redactions), ""
    except JUnitError as exc:
        return None, str(exc)


def run_pytest(ctx: Context, suite_id: str, files: Sequence[str],
               extra_env: Mapping[str, str] = ()) -> Execution:
    junit = ctx.out / "junit" / f"{suite_id}.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    argv = [ctx.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit2",
            f"--junitxml={junit}", *files]
    env = {**ctx.env, "PYTHONPATH": str(ctx.src), **dict(extra_env)}
    proc = run_proc(argv, ctx.src, env, ctx.timeout_s)
    log = ctx.out / "logs" / f"{suite_id}.log"
    write_log(log, [proc])
    summary, error = _report(ctx, junit, None) if proc.launched else (None, "")
    return Execution(proc.launched, proc.exit_code, proc.timed_out, proc.startup_error, summary, error,
                     command=tuple(redact_argv(argv, ctx.redactions)),
                     junit_path=_rel(ctx, junit) if junit.is_file() else None, log_path=_rel(ctx, log))


TS_PACKAGE = ("sdk", "typescript", "qsearch-lifecycle")


def run_node(ctx: Context, suite_id: str, node: str, version: str) -> Execution:
    """The package's own ``node --test`` (default patterns) against its built ``dist/``."""
    cwd = ctx.src.joinpath(*TS_PACKAGE)
    junit = ctx.out / "junit" / f"{suite_id}.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    argv = [node, "--test", "--test-reporter=junit", f"--test-reporter-destination={junit}",
            "--test-reporter=dot", "--test-reporter-destination=stdout"]
    proc = run_proc(argv, cwd, ctx.env, ctx.timeout_s)
    log = ctx.out / "logs" / f"{suite_id}.log"
    write_log(log, [proc])
    summary, error = _report(ctx, junit, str(cwd)) if proc.launched else (None, "")
    mapping = {**ctx.redactions, node: "{node}"}
    return Execution(proc.launched, proc.exit_code, proc.timed_out, proc.startup_error, summary, error,
                     command=tuple(redact_argv(argv, mapping)),
                     junit_path=_rel(ctx, junit) if junit.is_file() else None, log_path=_rel(ctx, log),
                     detail={"node_version": version})


Judge = Callable[[Sequence[Proc]], Sequence[str]]


def display_argv(argv: Sequence[str], redactions: Mapping[str, str]) -> List[str]:
    """Redacted argv; an inline ``-c`` program is shown by its digest only."""
    shown = []
    for arg in redact_argv(argv, redactions):
        if "\n" in arg:
            arg = f"<inline-source sha256:{sha256_bytes(arg.encode('utf-8'))[:16]}>"
        shown.append(arg)
    return shown


def run_checked(ctx: Context, suite_id: str, steps: Sequence[Tuple[List[str], Path]], judge: Judge,
                extra_env: Mapping[str, str] = ()) -> Execution:
    """Run command steps in order; ``judge(procs)`` returns failed expectations."""
    env = {**ctx.env, **dict(extra_env)}
    procs: List[Proc] = []
    commands: List[str] = []
    for argv, cwd in steps:
        proc = run_proc(argv, cwd, env, ctx.timeout_s)
        procs.append(proc)
        commands.append(" ".join(display_argv(argv, ctx.redactions)))
        if not proc.launched or proc.timed_out or proc.exit_code != 0:
            break
    log = ctx.out / "logs" / f"{suite_id}.log"
    write_log(log, procs)
    last = procs[-1]
    ok = last.launched and not last.timed_out and last.exit_code == 0
    checks = tuple(judge(procs)) if ok else ()
    return Execution(last.launched, last.exit_code, last.timed_out, last.startup_error, checks=checks,
                     command=tuple(commands), log_path=_rel(ctx, log))
