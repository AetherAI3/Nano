"""Issue #84: `nano package check | build` over the shipped package API.

The contract is frozen in docs/package-cli.md. These tests drive the real CLI
entry point (in-process with captured streams, and as a fresh interpreter where
byte framing, working directory or import state matter) and compare it with
`compile_package` directly, so the CLI cannot drift from the API it adapts.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import pytest

import nano
from nano.cli.main import main
from nano.compiler import PackageError, compile_module, compile_package
from nano.ir.module import NanoModule

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "agent-package"
PACKAGE_HASH = "sha256:b5f5a854e33c94f48697be3b0e3f9fa2be5a3c5cd198e8556b66b9a4925da099"
MODULE_HASH = "sha256:78031edfa42c22e8d432af57bafa3a455da61b968b75ae8982994896d021200e"
DIAGNOSTIC_KEYS = {"category", "code", "message", "path", "severity", "span"}
CHECK_KEYS = {"compiler", "diagnostics", "moduleHash", "packageHash", "schemaVersion", "success", "type"}

EVIDENCE = """module Evidence {
  pure fn sufficient(count: int, verified: bool): bool { count >= 2 and verified }
}
"""
TASK = """import Evidence
module Task {
  pure fn ready(count: int, verified: bool, tokens: int): bool {
    sufficient(count, verified) and tokens >= 100
  }
}
"""
ENTRY = """import Task
strategy AgentTask {
  input evidence_count: int
  input evidence_verified: bool
  input model_tokens: int
  every 1m {
    if ready(evidence_count, evidence_verified, model_tokens) { execute() }
    else { pause() }
  }
}
"""
INLINE = ENTRY.replace("import Task\n", "").replace(
    "ready(evidence_count, evidence_verified, model_tokens)",
    "(evidence_count >= 2 and evidence_verified) and model_tokens >= 100",
)
MANIFEST = {
    "nanoPackageVersion": "0.1.0", "entry": "agent.nano",
    "modules": {"Evidence": "evidence.nano", "Task": "task.nano"},
    "effects": ["intent.emit", "log.append"],
}


# -- helpers ---------------------------------------------------------------

def canonical(value: object) -> bytes:
    """The contract's canonical encoding, written independently of the CLI."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_package(root: Path, *, evidence: str = EVIDENCE, task: str = TASK,
                  entry: str = ENTRY, manifest: Dict[str, object] = MANIFEST) -> Path:
    """Write sources and a manifest, then pin exactly the manifest's sources."""
    root.mkdir(parents=True, exist_ok=True)
    for name, source in {"evidence.nano": evidence, "task.nano": task, "agent.nano": entry}.items():
        (root / name).write_bytes(source.encode())
    (root / "nano.json").write_text(json.dumps(manifest), encoding="utf-8")
    repin(root)
    return root


def repin(root: Path) -> None:
    manifest = json.loads((root / "nano.json").read_text(encoding="utf-8"))
    paths = [manifest["entry"], *manifest["modules"].values()]
    deps = {p: sha((root / p).read_bytes()) for p in paths}
    lock = {"nanoLockVersion": "0.1.0", "dependencies": deps}
    (root / "nano.lock").write_text(json.dumps(lock), encoding="utf-8")


def copy_example(destination: Path) -> Path:
    shutil.copytree(EXAMPLE, destination)
    return destination


def paths(root: Path) -> List[str]:
    return ["--manifest", str(root / "nano.json"), "--lock", str(root / "nano.lock")]


def _run(argv: List[str], capsys) -> Tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _cli(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """A fresh interpreter; PYTHONPATH pins this checkout, not an installed copy."""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-m", "nano.cli", *args], cwd=cwd, env=env,
                          capture_output=True, timeout=300, check=False)


def api_envelope(root: Path) -> bytes:
    built = compile_package(root / "nano.json", lock_path=root / "nano.lock")
    return canonical(built.to_dict()) + b"\n"


def snapshot(root: Path) -> Dict[str, Tuple[bytes, int]]:
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def assert_one_lf_frame(stdout: bytes) -> None:
    assert stdout.endswith(b"\n") and not stdout.endswith(b"\n\n")
    assert b"\r" not in stdout
    assert stdout.count(b"\n") == 1


# -- 1. CLI == API ---------------------------------------------------------

def test_build_stdout_is_the_api_envelope_with_pinned_hashes(capsys):
    code, out, err = _run(["package", "build", *paths(EXAMPLE)], capsys)
    assert (code, err) == (0, "")
    expected = api_envelope(EXAMPLE)
    assert out.encode("ascii") == expected
    document = json.loads(out)
    assert document["packageHash"] == PACKAGE_HASH
    assert document["module"]["moduleHash"] == MODULE_HASH
    api = compile_package(EXAMPLE / "nano.json", lock_path=EXAMPLE / "nano.lock").to_dict()
    assert document["sourceMap"] == json.loads(canonical(api["sourceMap"]))
    body = {k: v for k, v in document.items() if k != "packageHash"}
    assert sha(canonical(body)) == document["packageHash"]


def test_build_output_files_are_canonical_and_module_loads(tmp_path, capsys):
    envelope, module_path = tmp_path / "out" / "package.json", tmp_path / "out" / "module.json"
    envelope.parent.mkdir()
    argv = ["package", "build", *paths(EXAMPLE), "-o", str(envelope), "--module-output", str(module_path)]
    assert _run(argv, capsys) == (0, "", "")
    assert envelope.read_bytes() == api_envelope(EXAMPLE)
    api = compile_package(EXAMPLE / "nano.json", lock_path=EXAMPLE / "nano.lock")
    raw = module_path.read_bytes()
    assert raw == canonical(api.module.to_dict()) + b"\n"
    loaded = NanoModule.from_dict(json.loads(raw))
    inline = compile_module((EXAMPLE / "inline.nano").read_text(encoding="utf-8"))
    assert loaded.content_hash() == inline.content_hash() == MODULE_HASH
    assert sorted(p.name for p in envelope.parent.iterdir()) == ["module.json", "package.json"]


def test_check_is_silent_and_verbose_reports_hashes(capsys):
    assert _run(["package", "check", *paths(EXAMPLE)], capsys) == (0, "", "")
    code, out, err = _run(["package", "check", "-v", *paths(EXAMPLE)], capsys)
    assert (code, err) == (0, "")
    assert out == f"ok packageHash={PACKAGE_HASH} moduleHash={MODULE_HASH}\n"


def test_check_json_document_is_exact_and_canonical(capsys):
    code, out, err = _run(["package", "check", "--json", *paths(EXAMPLE)], capsys)
    assert (code, err) == (0, "")
    expected = {
        "compiler": {"irCompilerVersion": "1.0.0", "name": "nnc-package",
                     "packageVersion": nano.__version__, "version": "0.1.0"},
        "diagnostics": [], "moduleHash": MODULE_HASH, "packageHash": PACKAGE_HASH,
        "schemaVersion": "1.0.0", "success": True, "type": "NanoPackageCheck",
    }
    assert out.encode("ascii") == canonical(expected) + b"\n"


# -- 2. stability, relocation and framing ----------------------------------

def test_build_bytes_are_stable_across_cwd_relocation_reordering_and_repeats(tmp_path):
    expected = api_envelope(EXAMPLE)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    first = _cli(["package", "build", *paths(EXAMPLE)], elsewhere)
    second = _cli(["package", "build", *paths(EXAMPLE)], elsewhere)
    assert (first.returncode, first.stderr) == (0, b"")
    assert first.stdout == second.stdout == expected
    assert_one_lf_frame(first.stdout)

    relocated = copy_example(tmp_path / "relocated" / "deeper" / "pkg")
    local = _cli(["package", "build", "--manifest", "nano.json", "--lock", "nano.lock"], relocated)
    assert (local.returncode, local.stdout) == (0, expected)

    manifest = json.loads((relocated / "nano.json").read_text(encoding="utf-8"))
    reordered = dict(reversed(list(manifest.items())))
    reordered["modules"] = dict(reversed(list(manifest["modules"].items())))
    (relocated / "nano.json").write_text(json.dumps(reordered, indent=4), encoding="utf-8")
    rewritten = _cli(["package", "build", *paths(relocated)], elsewhere)
    assert rewritten.returncode == 0
    assert json.loads(rewritten.stdout)["packageHash"] == PACKAGE_HASH
    assert rewritten.stdout == expected


def test_check_json_framing_in_a_fresh_interpreter(tmp_path, capsys):
    code, out, _ = _run(["package", "check", "--json", *paths(EXAMPLE)], capsys)
    result = _cli(["package", "check", "--json", *paths(EXAMPLE)], tmp_path)
    assert (code, result.returncode, result.stderr) == (0, 0, b"")
    assert_one_lf_frame(result.stdout)
    assert result.stdout == out.encode("ascii")


def test_check_json_failure_is_identical_after_relocation(tmp_path, capsys):
    outputs = []
    for name in ("a", "b"):
        root = write_package(tmp_path / name / "pkg", task=TASK.replace("import Evidence", "import Missing"))
        code, out, _ = _run(["package", "check", "--json", *paths(root)], capsys)
        assert code == 1
        assert str(tmp_path) not in out
        outputs.append(out)
    assert outputs[0] == outputs[1]


# -- 3. positioned diagnostics ---------------------------------------------

def _drop_lock_pin(root: Path) -> None:
    lock = json.loads((root / "nano.lock").read_text(encoding="utf-8"))
    del lock["dependencies"]["evidence.nano"]
    (root / "nano.lock").write_text(json.dumps(lock), encoding="utf-8")


def _stale_pin(root: Path) -> None:
    (root / "evidence.nano").write_bytes((EVIDENCE + "// edited after pinning\n").encode())


def _duplicate_manifest_key(root: Path) -> None:
    (root / "nano.json").write_text('{"entry":"agent.nano","entry":"agent.nano"}', encoding="utf-8")


def _unknown_manifest_field(root: Path) -> None:
    (root / "nano.json").write_text(json.dumps({**MANIFEST, "registry": "https://example"}), encoding="utf-8")


def _rewrite(**sources: str) -> Callable[[Path], None]:
    def apply(root: Path) -> None:
        for name, text in sources.items():
            (root / f"{name}.nano").write_bytes(text.encode())
        repin(root)
    return apply


def _grant(effects: List[str]) -> Callable[[Path], None]:
    def apply(root: Path) -> None:
        (root / "nano.json").write_text(json.dumps({**MANIFEST, "effects": effects}), encoding="utf-8")
    return apply


MANY_FUNCTIONS = "module Evidence {\n" + "\n".join(
    f"pure fn f{i}(): bool {{ true }}" for i in range(129)) + "\n}\n"

NEGATIVE_CONTROLS = [
    ("missing-pin", _drop_lock_pin, "PACKAGE_ERROR", "package", "nano.lock", 1),
    ("unpinned-import", _rewrite(task=TASK.replace("import Evidence", "import Missing")),
     "PACKAGE_ERROR", "package", "task.nano", 1),
    ("stale-pin", _stale_pin, "PACKAGE_ERROR", "package", "task.nano", 1),
    ("duplicate-json-key", _duplicate_manifest_key, "PACKAGE_ERROR", "package", "nano.json", 1),
    ("duplicate-symbol", _rewrite(evidence=EVIDENCE.replace("}\n}", "}\n pure fn sufficient(): bool { true }\n}")),
     "PACKAGE_ERROR", "package", "evidence.nano", 3),
    ("wrong-argument-type", _rewrite(agent=ENTRY.replace("model_tokens)", "true)")),
     "TYPE_ERROR", "type", "agent.nano", 7),
    ("import-cycle", _rewrite(evidence="import Task\n" + EVIDENCE), "PACKAGE_ERROR", "package", "evidence.nano", 1),
    ("recursive-call", _rewrite(evidence=EVIDENCE.replace("count >= 2 and verified", "sufficient(count, verified)")),
     "PACKAGE_ERROR", "package", "evidence.nano", 2),
    ("ungranted-effect", _grant(["log.append"]), "PACKAGE_ERROR", "package", "agent.nano", 1),
    ("resource-limit", _rewrite(evidence=MANY_FUNCTIONS, task="module Task {}\n", agent=INLINE),
     "RESOURCE_LIMIT", "resource", "agent.nano", 1),
    ("unknown-manifest-field", _unknown_manifest_field, "PACKAGE_ERROR", "package", "nano.json", 1),
]


@pytest.mark.parametrize("name,mutate,code,category,path,line", NEGATIVE_CONTROLS,
                         ids=[case[0] for case in NEGATIVE_CONTROLS])
def test_negative_controls_are_positioned_and_nonzero(tmp_path, capsys, name, mutate, code, category, path, line):
    root = write_package(tmp_path / "pkg")
    mutate(root)
    status, out, err = _run(["package", "check", "--json", *paths(root)], capsys)
    assert (status, err) == (1, "")
    assert out.encode("ascii").endswith(b"}\n")
    document = json.loads(out)
    assert set(document) == CHECK_KEYS
    assert (document["success"], document["packageHash"], document["moduleHash"]) == (False, None, None)
    [diagnostic] = document["diagnostics"]
    assert set(diagnostic) == DIAGNOSTIC_KEYS
    assert (diagnostic["code"], diagnostic["category"], diagnostic["path"]) == (code, category, path)
    assert diagnostic["severity"] == "error"
    start = diagnostic["span"]["start"]
    assert start["line"] == line and start["column"] >= 1
    assert diagnostic["span"]["end"] == start
    assert not diagnostic["message"].startswith(f"{path}: ")
    with pytest.raises(PackageError) as raised:
        compile_package(root / "nano.json", lock_path=root / "nano.lock")
    assert raised.value.message == f"{path}: {diagnostic['message']}"

    position = f"{path}:{line}:{start['column']}: error: {diagnostic['message']}\n"
    for action in (["check"], ["check", "-v"], ["build"]):
        assert _run(["package", *action, *paths(root)], capsys) == (1, "", position)


# -- 4. path escapes and no ambient discovery ------------------------------

@pytest.mark.parametrize("module_path", ["../evidence.nano", "/tmp/evidence.nano", "C:/evidence.nano", "ABSOLUTE"])
def test_escaping_manifest_paths_are_rejected(tmp_path, capsys, module_path):
    root = write_package(tmp_path / "pkg")
    shutil.copy(root / "evidence.nano", tmp_path / "evidence.nano")
    target = (tmp_path / "evidence.nano").as_posix() if module_path == "ABSOLUTE" else module_path
    manifest = {**MANIFEST, "modules": {"Evidence": target, "Task": "task.nano"}}
    (root / "nano.json").write_text(json.dumps(manifest), encoding="utf-8")
    code, out, err = _run(["package", "check", "--json", *paths(root)], capsys)
    assert code == 1
    [diagnostic] = json.loads(out)["diagnostics"]
    assert (diagnostic["code"], diagnostic["path"]) == ("PACKAGE_ERROR", "nano.json")
    assert "canonical relative" in diagnostic["message"]
    assert str(tmp_path) not in out


def test_symlink_escaping_root_is_rejected(tmp_path, capsys):
    root = write_package(tmp_path / "pkg")
    outside = tmp_path / "outside.nano"
    outside.write_bytes(EVIDENCE.encode())
    (root / "evidence.nano").unlink()
    try:
        (root / "evidence.nano").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("host lacks symlink creation privilege")
    code, out, _ = _run(["package", "check", "--json", *paths(root)], capsys)
    assert code == 1
    [diagnostic] = json.loads(out)["diagnostics"]
    assert diagnostic["code"] == "PACKAGE_ERROR"
    assert "escapes package root" in diagnostic["message"]


def test_unlisted_sibling_source_is_never_discovered(tmp_path, capsys):
    root = write_package(tmp_path / "pkg", entry="import Helper\n" + INLINE)
    (root / "helper.nano").write_bytes(b"module Helper {\n  pure fn h(): bool { true }\n}\n")
    code, out, err = _run(["package", "check", *paths(root)], capsys)
    assert (code, out) == (1, "")
    assert err == "agent.nano:1:8: error: module 'Helper' is not explicitly pinned\n"


# -- 5. I/O and usage ------------------------------------------------------

def _missing_source(root: Path) -> Tuple[List[str], str]:
    (root / "task.nano").unlink()
    return paths(root), "agent.nano"


IO_CASES = [
    ("missing-manifest", lambda r: (["--manifest", str(r / "absent.json"), "--lock", str(r / "nano.lock")], "absent.json")),
    ("missing-lock", lambda r: (["--manifest", str(r / "nano.json"), "--lock", str(r / "absent.lock")], "absent.lock")),
    ("missing-source", _missing_source),
    ("directory-manifest", lambda r: (["--manifest", str(r / "sub"), "--lock", str(r / "nano.lock")], "sub")),
]


@pytest.mark.parametrize("name,arrange", IO_CASES, ids=[case[0] for case in IO_CASES])
def test_unreadable_inputs_exit_3_with_io_error(tmp_path, capsys, name, arrange):
    root = write_package(tmp_path / "pkg")
    (root / "sub").mkdir()
    argv, path = arrange(root)
    code, out, err = _run(["package", "check", "--json", *argv], capsys)
    assert (code, err) == (3, "")
    document = json.loads(out)
    assert (document["success"], document["packageHash"]) == (False, None)
    [diagnostic] = document["diagnostics"]
    assert set(diagnostic) == DIAGNOSTIC_KEYS
    assert (diagnostic["code"], diagnostic["category"], diagnostic["path"], diagnostic["span"]) == (
        "IO_ERROR", "io", path, None)
    assert str(tmp_path) not in out and str(root) not in out
    for action in ("check", "build"):
        code, out, err = _run(["package", action, *argv], capsys)
        assert (code, out) == (3, "")
        assert err == f"{path}: error: {diagnostic['message']}\n"


@pytest.mark.parametrize("argv", [
    ["package"],
    ["package", "check"],
    ["package", "check", "--manifest", "nano.json"],
    ["package", "build", "--lock", "nano.lock"],
    ["package", "test", "--manifest", "nano.json", "--lock", "nano.lock"],
])
def test_usage_errors_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as raised:
        main(argv)
    assert raised.value.code == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("flag", ["-o", "--module-output"])
@pytest.mark.parametrize("target", ["nano.json", "nano.lock", "agent.nano", "evidence.nano", "task.nano"])
def test_output_naming_an_input_is_refused(tmp_path, capsys, flag, target):
    root = write_package(tmp_path / "pkg")
    before = snapshot(root)
    spelled = str(root / "sub") + os.sep + ".." + os.sep + target
    code, out, err = _run(["package", "build", *paths(root), flag, spelled], capsys)
    assert (code, out) == (2, "")
    assert "error:" in err
    assert snapshot(root) == before


def test_output_and_module_output_must_differ(tmp_path, capsys):
    root = write_package(tmp_path / "pkg")
    target = tmp_path / "same.json"
    code, out, _ = _run(["package", "build", *paths(root), "-o", str(target), "--module-output", str(target)], capsys)
    assert (code, out) == (2, "")
    assert not target.exists()


def test_failed_build_writes_nothing(tmp_path, capsys):
    root = write_package(tmp_path / "pkg", task=TASK.replace("import Evidence", "import Missing"))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    argv = ["package", "build", *paths(root), "-o", str(out_dir / "p.json"), "--module-output", str(out_dir / "m.json")]
    code, out, _ = _run(argv, capsys)
    assert (code, out) == (1, "")
    assert list(out_dir.iterdir()) == []


def test_unwritable_output_exits_3_without_partial_files(tmp_path, capsys):
    target = tmp_path / "missing-dir" / "p.json"
    module_target = tmp_path / "m.json"
    argv = ["package", "build", *paths(EXAMPLE), "--module-output", str(module_target), "-o", str(target)]
    code, out, err = _run(argv, capsys)
    assert (code, out) == (3, "")
    assert "error:" in err
    assert list(tmp_path.iterdir()) == []


def test_directory_as_second_output_leaves_first_output_unwritten(tmp_path, capsys):
    # Review finding: the second os.replace failing used to leave -o in place.
    blocked = tmp_path / "md"
    blocked.mkdir()
    target = tmp_path / "out.json"
    argv = ["package", "build", *paths(EXAMPLE), "-o", str(target), "--module-output", str(blocked)]
    code, out, err = _run(argv, capsys)
    assert (code, out) == (3, "")
    assert "error:" in err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["md"]
    assert list(blocked.iterdir()) == []


def test_failed_second_commit_restores_the_first_output(tmp_path, capsys, monkeypatch):
    from nano.cli import package as package_cli

    first, second = tmp_path / "out.json", tmp_path / "module.json"
    first.write_bytes(b"previous\n")
    real_replace = package_cli.os.replace
    calls: List[str] = []

    def fail_second(src, dst):
        calls.append(str(dst))
        if Path(dst) == second:
            raise PermissionError(13, "simulated", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(package_cli.os, "replace", fail_second)
    argv = ["package", "build", *paths(EXAMPLE), "-o", str(first), "--module-output", str(second)]
    code, out, _ = _run(argv, capsys)
    assert (code, out) == (3, "")
    assert first.read_bytes() == b"previous\n"
    assert not second.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.json"]


def test_atomic_write_cleans_its_temporary_on_failure(tmp_path, monkeypatch):
    from nano.cli import package as package_cli

    target = tmp_path / "artifact.json"
    target.write_bytes(b"previous\n")

    def refuse(src: str, dst: str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(package_cli.os, "replace", refuse)
    with pytest.raises(OSError):
        package_cli.write_bytes_atomic(target, b"new\n")
    assert target.read_bytes() == b"previous\n"
    assert [p.name for p in tmp_path.iterdir()] == ["artifact.json"]


def test_public_helpers(tmp_path):
    from nano.cli import package as package_cli

    assert package_cli.canonical_bytes({"b": 1, "a": "\u00e9"}) == b'{"a":"\\u00e9","b":1}\n'
    assert package_cli.compiler_identity() == {
        "irCompilerVersion": "1.0.0", "name": "nnc-package",
        "packageVersion": nano.__version__, "version": "0.1.0"}
    root = write_package(tmp_path / "pkg")
    built = package_cli.build_package(root / "nano.json", root / "nano.lock")
    assert canonical(built.to_dict()) + b"\n" == package_cli.canonical_bytes(built.to_dict())
    rejected = package_cli.build_package(root / "absent.json", root / "nano.lock")
    assert rejected.exit_code == 3
    assert rejected.diagnostic["code"] == "IO_ERROR"
    with pytest.raises(PackageError) as raised:
        compile_package(root / "nano.json", lock_path=root / "absent.lock")
    diagnostic, exit_code = package_cli.diagnostic_dict(raised.value)
    assert (exit_code, diagnostic["code"], diagnostic["span"]) == (3, "IO_ERROR", None)


# -- 6. read-only ----------------------------------------------------------

def test_package_directory_is_never_written(tmp_path, capsys):
    root = copy_example(tmp_path / "pkg")
    broken = write_package(tmp_path / "broken", task=TASK.replace("import Evidence", "import Missing"))
    before, broken_before = snapshot(root), snapshot(broken)
    outside = tmp_path / "artifacts"
    outside.mkdir()
    runs = [
        (["package", "check", *paths(root)], 0),
        (["package", "check", "--json", *paths(root)], 0),
        (["package", "check", "-v", *paths(root)], 0),
        (["package", "check", "--json", *paths(broken)], 1),
        (["package", "build", *paths(broken), "-o", str(outside / "x.json")], 1),
        (["package", "build", *paths(root), "-o", str(outside / "p.json"),
          "--module-output", str(outside / "m.json")], 0),
    ]
    for argv, expected in runs:
        assert _run(argv, capsys)[0] == expected
    assert snapshot(root) == before
    assert snapshot(broken) == broken_before
    assert sorted(p.name for p in outside.iterdir()) == ["m.json", "p.json"]


# -- 7. hermetic -----------------------------------------------------------

HERMETIC = r"""
import json, socket, sys

def deny(*args, **kwargs):
    raise AssertionError("network access attempted")

socket.socket = deny
socket.create_connection = deny
socket.getaddrinfo = deny

import nano
from nano.cli.main import main

manifest, lock, report = sys.argv[1:4]
codes = [
    main(["package", "check", "--json", "--manifest", manifest, "--lock", lock]),
    main(["package", "build", "--manifest", manifest, "--lock", lock]),
]
loaded = sorted(m for m in ("qiskit", "nano_qsearch", "numpy") if m in sys.modules)
with open(report, "w", encoding="utf-8") as stream:
    json.dump({"codes": codes, "loaded": loaded, "nano": nano.__file__}, stream)
"""


def test_check_and_build_are_hermetic_in_a_fresh_interpreter(tmp_path):
    root = copy_example(tmp_path / "pkg")
    report = tmp_path / "report.json"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", HERMETIC, str(root / "nano.json"), str(root / "nano.lock"), str(report)],
        cwd=tmp_path, env=env, capture_output=True, timeout=300, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    outcome = json.loads(report.read_text(encoding="utf-8"))
    assert outcome["codes"] == [0, 0]
    assert outcome["loaded"] == []
    assert Path(outcome["nano"]).resolve().is_relative_to(ROOT)
    assert result.stdout.endswith(api_envelope(root))
