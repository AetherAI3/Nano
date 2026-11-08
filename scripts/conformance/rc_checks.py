"""Command suites: contribution shape, CLI help, blocked-SDK imports, wheel checks.

Every check child runs under ``python -I`` (no PYTHONPATH, no cwd on
``sys.path``), inserts exactly one import root (the exported source or the
wheel file) and refuses optional SDK imports through a meta-path blocker.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import List, Sequence, Tuple

from rc_common import redact, sha256_file
from rc_evaluate import Execution
from rc_runner import BLOCKER_SOURCE, TS_PACKAGE, Context, Proc, run_checked

CORE_MODULES = (
    "nano", "nano.bridge", "nano.cli.main", "nano.compiler", "nano.intent", "nano.project",
    "nano.quantum", "nano.quantum.native", "nano.runtime", "nano_qsearch", "nano_qsearch.native_artifact",
    "nano_qsearch.native_result", "nano_qsearch.native_service", "nano_qsearch.service",
)
HELP_ARGVS = (["--help"], ["package", "--help"])
NATIVE_SCHEMAS = (
    "native-artifact-v1.schema.json", "native-capabilities-v1.schema.json", "native-profile-v1.schema.json",
    "native-request-v1.schema.json", "native-result-v1.schema.json", "native-target-v1.schema.json",
)

CHECK_SOURCE = BLOCKER_SOURCE + r'''
import contextlib, importlib, io, json
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["path"])
result = {"origins": {}, "errors": {}, "help": [], "native": None}
for name in spec["modules"]:
    try:
        result["origins"][name] = getattr(importlib.import_module(name), "__file__", None)
    except Exception as exc:
        result["errors"][name] = type(exc).__name__ + ": " + str(exc)

def _help(argv):
    from nano.cli.main import main
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = exc.code
    return {"argv": argv, "code": code or 0, "out": buffer.getvalue()}

def _native():
    from importlib.resources import files
    from nano.quantum import QuantumValidationError
    from nano_qsearch.native_artifact import parse_native_artifact
    root = files("nano.quantum")
    artifact = json.loads(root.joinpath("fixtures/native-artifact-known-bits.json").read_text(encoding="utf-8"))
    golden = json.loads(root.joinpath("fixtures/native-goldens.json").read_text(encoding="utf-8"))
    plan = golden["plans"]["known-bits"]
    parsed = parse_native_artifact(artifact, expected_digest=plan["artifact_digest"])
    out = {"plan_digest_ok": parsed.plan_digest == plan["plan_digest"],
           "pub_map_ok": parsed.pub_map_digest == plan["pub_map_digest"],
           "schemas": sorted(p.name for p in root.joinpath("schemas").iterdir() if p.name.startswith("native-")),
           "tamper_refused": False}
    digest = plan["artifact_digest"]
    wrong = digest[:-1] + ("1" if digest[-1] == "0" else "0")
    try:
        parse_native_artifact(artifact, expected_digest=wrong)
    except QuantumValidationError:
        out["tamper_refused"] = True
    return out

try:
    for argv in spec["help"]:
        result["help"].append(_help(argv))
    if spec["native"]:
        result["native"] = _native()
except Exception as exc:
    result["errors"]["<check>"] = type(exc).__name__ + ": " + str(exc)
result["loaded_blocked"] = sorted(m for m in sys.modules if m.split(".")[0] in _BLOCKED)
sys.stdout.write(json.dumps(result))
'''

PACKAGE_CLI_SOURCE = BLOCKER_SOURCE + r'''
sys.path.insert(0, sys.argv[1])
from nano.cli.main import main
raise SystemExit(main(sys.argv[2:]))
'''


def _check_argv(python: str, path: Path, modules: Sequence[str], help_argvs: Sequence[Sequence[str]],
                native: bool) -> List[str]:
    spec = {"path": str(path), "modules": list(modules), "help": [list(a) for a in help_argvs],
            "native": native}
    return [python, "-I", "-c", CHECK_SOURCE, json.dumps(spec, sort_keys=True)]


def _under(file: object, root: Path) -> bool:
    if not isinstance(file, str):
        return False
    return os.path.normcase(os.path.abspath(file)).startswith(os.path.normcase(str(root)) + os.sep)


def _load_child(procs: Sequence[Proc]) -> Tuple[dict, List[str]]:
    try:
        return json.loads(procs[-1].stdout.decode("utf-8")), []
    except (UnicodeDecodeError, ValueError) as exc:
        return {}, [f"check child did not emit JSON: {exc}"]


def judge_child(procs: Sequence[Proc], root: Path, modules: Sequence[str], want_help: bool,
                want_native: bool) -> List[str]:
    """Shared expectations for every check child."""
    data, problems = _load_child(procs)
    if problems:
        return problems
    problems += [f"import failed: {name}: {err}" for name, err in sorted(data["errors"].items())]
    for name in modules:
        if name in data["origins"] and not _under(data["origins"][name], root):
            problems.append(f"{name} imported from outside the tested root")
    if data["loaded_blocked"]:
        problems.append(f"optional SDK modules were loaded: {data['loaded_blocked']}")
    if want_help:
        problems += _judge_help(data["help"])
    if want_native:
        problems += _judge_native(data["native"])
    return problems


def _judge_help(results: Sequence[dict]) -> List[str]:
    if len(results) != len(HELP_ARGVS):
        return ["help output missing"]
    problems = [f"{' '.join(r['argv'])} exited {r['code']}" for r in results if r["code"] != 0]
    top, package = results[0]["out"], results[1]["out"]
    if "usage:" not in top or "package" not in top:
        problems.append("nano --help does not list the package command")
    if not all(word in package for word in ("check", "build", "test")):
        problems.append("nano package --help does not list check/build/test")
    return problems


def _judge_native(native: object) -> List[str]:
    if not isinstance(native, dict):
        return ["native fixture check did not run"]
    problems = [f"native fixture: {key} is false" for key in ("plan_digest_ok", "pub_map_ok", "tamper_refused")
                if native.get(key) is not True]
    missing = sorted(set(NATIVE_SCHEMAS) - set(native.get("schemas", [])))
    problems += [f"native schema missing: {name}" for name in missing]
    return problems


def run_child_check(ctx: Context, suite_id: str, root: Path, modules: Sequence[str],
                    help_argvs: Sequence[Sequence[str]], native: bool, cwd: Path) -> Execution:
    argv = _check_argv(ctx.python, root, modules, help_argvs, native)
    return run_checked(ctx, suite_id, [(argv, cwd)],
                       lambda procs: judge_child(procs, root, modules, bool(help_argvs), native))


def run_contribution(ctx: Context, suite_id: str) -> Execution:
    argv = [ctx.python, "scripts/check_contribution.py", "--all"]
    return run_checked(ctx, suite_id, [(argv, ctx.src)], lambda procs: [],
                       extra_env={"PYTHONPATH": str(ctx.src)})


def judge_package_cli(procs: Sequence[Proc]) -> List[str]:
    problems: List[str] = []
    docs = []
    for proc in procs:
        try:
            docs.append(json.loads(proc.stdout.decode("ascii")))
        except (UnicodeDecodeError, ValueError) as exc:
            problems.append(f"package CLI emitted no JSON document: {exc}")
    if problems or len(docs) != 2:
        return problems or ["package CLI did not run both steps"]
    check, test = docs
    if check.get("type") != "NanoPackageCheck" or check.get("success") is not True:
        problems.append("nano package check did not succeed")
    if test.get("type") != "NanoPackageTestReport" or test.get("success") is not True:
        problems.append("nano package test did not succeed")
    if not test.get("total") or test.get("failed") != 0 or test.get("passed") != test.get("total"):
        problems.append("nano package test did not pass every case")
    if test.get("actionsExecuted") != 0:
        problems.append("nano package test reported executed actions")
    return problems


def run_wheel_package_cli(ctx: Context, suite_id: str, wheel: Path, work: Path) -> Execution:
    package = work / "agent-package"
    if package.exists():
        shutil.rmtree(package)
    shutil.copytree(ctx.src / "examples" / "agent-package", package)
    base = [ctx.python, "-I", "-c", PACKAGE_CLI_SOURCE, str(wheel), "package"]
    pins = ["--manifest", "nano.json", "--lock", "nano.lock"]
    steps = [([*base, "check", *pins, "--json"], package),
             ([*base, "test", *pins, "--cases", "cases.json", "--json"], package)]
    return run_checked(ctx, suite_id, steps, judge_package_cli)


def sha256sums_problems(root: Path) -> List[str]:
    """``SHA256SUMS`` must list every other file under ``root``, sorted, each matching."""
    sums = root / "SHA256SUMS"
    if not sums.is_file():
        return [f"missing {sums.name}"]
    listed = {}
    problems = []
    for line in sums.read_bytes().decode("ascii", errors="replace").splitlines():
        digest, sep, rel = line.partition("  ")
        if not sep or len(digest) != 64 or not rel:
            problems.append(f"malformed SHA256SUMS line: {line[:80]!r}")
            continue
        listed[rel] = digest
    if list(listed) != sorted(listed):
        problems.append("SHA256SUMS entries are not sorted")
    present = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                     if p.is_file() and p.name != "SHA256SUMS" and "__pycache__" not in p.parts)
    problems += [f"not listed in SHA256SUMS: {rel}" for rel in present if rel not in listed]
    for rel, digest in listed.items():
        path = root / rel
        if not path.is_file():
            problems.append(f"listed but missing: {rel}")
        elif sha256_file(path) != digest:
            problems.append(f"digest mismatch: {rel}")
    if not listed:
        problems.append("SHA256SUMS lists no files")
    return problems


def run_sha256sums(ctx: Context, sums_rel: str) -> Execution:
    root = (ctx.src / sums_rel).parent
    problems = sha256sums_problems(root)
    return Execution(True, 0, checks=tuple(problems), command=(f"<verify {sums_rel}>",),
                     detail={"verified_root": root.relative_to(ctx.src).as_posix()})


def run_regen_check(ctx: Context, suite_id: str, script_rel: str) -> Execution:
    argv = [ctx.python, "-B", script_rel, "--check"]
    return run_checked(ctx, suite_id, [(argv, ctx.src)], lambda procs: [],
                       extra_env={"PYTHONPATH": str(ctx.src)})


def _locked_typescript(package: Path) -> str:
    lock = json.loads((package / "package-lock.json").read_text(encoding="utf-8"))
    return lock["packages"]["node_modules/typescript"]["version"]


def run_ts_build(ctx: Context, suite_id: str, node: str, npm_cache: Path) -> Execution:
    """Offline lockfile install (npm cache only, no scripts) and ``tsc`` build."""
    package = ctx.src.joinpath(*TS_PACKAGE)
    npm_cli = Path(node).resolve().parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    install = [node, str(npm_cli), "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund",
               "--no-update-notifier", "--cache", str(npm_cache)]
    build = [node, str(Path("node_modules") / "typescript" / "bin" / "tsc"), "-p", "tsconfig.json"]

    def judge(procs: Sequence[Proc]) -> List[str]:
        problems = []
        installed = package / "node_modules" / "typescript" / "package.json"
        locked = _locked_typescript(package)
        found = json.loads(installed.read_text(encoding="utf-8"))["version"] if installed.is_file() else None
        if found != locked:
            problems.append(f"typescript {found} installed, lockfile pins {locked}")
        if not (package / "dist" / "index.js").is_file():
            problems.append("tsc produced no dist/index.js")
        return problems

    execution = run_checked(ctx, suite_id, [(install, package), (build, package)], judge)
    mapping = {**ctx.redactions, str(npm_cli): "{npm_cli}", node: "{node}"}
    command = tuple(redact(cmd, mapping) for cmd in execution.command)
    return Execution(execution.launched, execution.exit_code, execution.timed_out, execution.startup_error,
                     checks=execution.checks, command=command, log_path=execution.log_path,
                     detail={"typescript_locked": _locked_typescript(package)})
