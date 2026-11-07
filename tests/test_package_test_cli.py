"""Issue #84: `nano package test --cases PATH` through the real CLI entry point.

The fixture library (`nano/cli/package_cases.py`) is covered by
tests/test_package_cases.py. These tests prove the command layer: framing,
exit codes, the report document, and that admission and fixture validation
happen before any case is evaluated.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from nano.cli.main import main
from nano.runtime import vm

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "agent-package"
PACKAGE_HASH = "sha256:b5f5a854e33c94f48697be3b0e3f9fa2be5a3c5cd198e8556b66b9a4925da099"
MODULE_HASH = "sha256:78031edfa42c22e8d432af57bafa3a455da61b968b75ae8982994896d021200e"
REPORT_KEYS = {
    "actionsExecuted", "cases", "diagnostics", "failed", "moduleHash",
    "packageHash", "passed", "schemaVersion", "success", "total", "type",
}
FRAME = {"timestamps": [0], "signals": {
    "evidence_count": [2], "evidence_verified": [True], "model_tokens": [100]}}


def _run(argv: List[str], capsys) -> Tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _args(root: Path, cases: Path, *extra: str) -> List[str]:
    return ["package", "test", "--manifest", str(root / "nano.json"),
            "--lock", str(root / "nano.lock"), "--cases", str(cases), *extra]


def _copy_example(tmp_path: Path) -> Path:
    target = tmp_path / "pkg"
    shutil.copytree(EXAMPLE, target)
    return target


def _write_cases(path: Path, cases: list) -> Path:
    path.write_text(json.dumps({"nanoPackageCasesVersion": "1.0.0", "cases": cases}), encoding="utf-8")
    return path


def _report(out: str) -> Dict:
    document = json.loads(out)
    assert set(document) == REPORT_KEYS
    assert document["type"] == "NanoPackageTestReport"
    assert document["schemaVersion"] == "1.0.0"
    assert document["actionsExecuted"] == 0
    return document


def _snapshot(root: Path) -> Dict[str, Tuple[bytes, int]]:
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(root.iterdir())}


@pytest.fixture
def run_calls(monkeypatch):
    calls: List[object] = []
    real = vm.run_module

    def counting(module, frame, **kwargs):
        calls.append(kwargs.get("provider", "absent"))
        return real(module, frame, **kwargs)

    monkeypatch.setattr(vm, "run_module", counting)
    return calls


# -- the frozen development fixture -----------------------------------------


def test_example_cases_pass_with_json_report(capsys, run_calls):
    code, out, err = _run(_args(EXAMPLE, EXAMPLE / "cases.json", "--json"), capsys)
    assert code == 0, err
    report = _report(out)
    assert report["success"] is True
    assert (report["passed"], report["failed"], report["total"]) == (5, 0, 5)
    assert report["packageHash"] == PACKAGE_HASH and report["moduleHash"] == MODULE_HASH
    assert report["diagnostics"] == []
    assert [c["status"] for c in report["cases"]] == ["pass"] * 5
    assert report["cases"][0]["observed"] == {"proposedIntents": [{"intent": "EXECUTE", "timestamp": 0}]}
    assert "runtimeError" in report["cases"][4]["observed"]
    assert run_calls == [None] * 5  # every case ran with no provider


def test_example_cases_text_mode(capsys):
    code, out, err = _run(_args(EXAMPLE, EXAMPLE / "cases.json"), capsys)
    assert code == 0, err
    lines = out.splitlines()
    assert lines[:5] == [f"PASS {c['name']}" for c in json.loads(
        (EXAMPLE / "cases.json").read_text(encoding="utf-8"))["cases"]]
    assert lines[-1] == "5/5 passed"
    assert err == ""


def test_subprocess_report_is_one_canonical_lf_document(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    completed = subprocess.run(
        [sys.executable, "-m", "nano.cli", *_args(EXAMPLE, EXAMPLE / "cases.json", "--json")],
        cwd=tmp_path, env=env, capture_output=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    raw = completed.stdout
    assert raw.endswith(b"}\n") and raw.count(b"\n") == 1 and b"\r" not in raw
    value = json.loads(raw)
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    assert raw == canonical


def test_repeated_runs_are_byte_identical(capsys):
    first = _run(_args(EXAMPLE, EXAMPLE / "cases.json", "--json"), capsys)
    second = _run(_args(EXAMPLE, EXAMPLE / "cases.json", "--json"), capsys)
    assert first == second


# -- mismatches fail explicitly, every case is still reported ---------------


def test_mismatched_expectation_fails_and_all_cases_report(tmp_path, capsys):
    cases = _write_cases(tmp_path / "cases.json", [
        {"name": "right", "frame": FRAME, "expect": {"proposedIntents": [{"intent": "EXECUTE", "timestamp": 0}]}},
        {"name": "wrong", "frame": FRAME, "expect": {"proposedIntents": [{"intent": "PAUSE", "timestamp": 0}]}},
    ])
    code, out, _ = _run(_args(EXAMPLE, cases, "--json"), capsys)
    assert code == 1
    report = _report(out)
    assert report["success"] is False
    assert (report["passed"], report["failed"], report["total"]) == (1, 1, 2)
    assert [c["status"] for c in report["cases"]] == ["pass", "fail"]
    assert "reason" in report["cases"][1]


def test_mismatch_text_mode_names_the_failure(tmp_path, capsys):
    cases = _write_cases(tmp_path / "cases.json", [
        {"name": "wrong", "frame": FRAME, "expect": {"runtimeError": "never happens"}},
    ])
    code, out, _ = _run(_args(EXAMPLE, cases), capsys)
    assert code == 1
    assert out.splitlines()[0].startswith("FAIL wrong: ")
    assert out.splitlines()[-1] == "0/1 passed"


# -- fixture rejection happens before any case runs -------------------------


@pytest.mark.parametrize("body,pointer", [
    ({"nanoPackageCasesVersion": "1.0.0", "cases": []}, "/cases"),
    ({"nanoPackageCasesVersion": "2.0.0", "cases": []}, "/nanoPackageCasesVersion"),
    ({"nanoPackageCasesVersion": "1.0.0", "cases": [
        {"name": "x", "frame": FRAME, "expect": {"proposedIntents": []}, "skip": True}]}, "/cases/0/skip"),
    ({"nanoPackageCasesVersion": "1.0.0", "cases": [
        {"name": "x", "frame": FRAME, "expect": {"proposedIntents": []}, "provider": "model"}]}, "/cases/0/provider"),
    ({"nanoPackageCasesVersion": "1.0.0", "cases": [
        {"name": "x", "frame": FRAME, "expect": {"executed": []}}]}, "/cases/0/expect/executed"),
])
def test_malformed_fixture_is_rejected_before_running(tmp_path, capsys, run_calls, body, pointer):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(body), encoding="utf-8")
    code, out, _ = _run(_args(EXAMPLE, cases, "--json"), capsys)
    assert code == 1
    report = _report(out)
    assert report["success"] is False and report["cases"] == []
    assert (report["passed"], report["failed"], report["total"]) == (0, 0, 0)
    [diagnostic] = report["diagnostics"]
    assert diagnostic["code"] == "FIXTURE_ERROR" and diagnostic["category"] == "fixture"
    assert diagnostic["pointer"] == pointer
    assert diagnostic["path"] == "cases.json" and diagnostic["span"] is None
    assert report["packageHash"] == PACKAGE_HASH
    assert run_calls == []


def test_duplicate_key_fixture_text_mode(tmp_path, capsys, run_calls):
    cases = tmp_path / "dup.json"
    cases.write_text('{"nanoPackageCasesVersion":"1.0.0","nanoPackageCasesVersion":"1.0.0","cases":[]}',
                     encoding="utf-8")
    code, out, err = _run(_args(EXAMPLE, cases), capsys)
    assert code == 1
    assert out == ""
    assert err.startswith("dup.json: error: /nanoPackageCasesVersion: duplicate key")
    assert run_calls == []


def test_missing_cases_file_is_io_error(tmp_path, capsys, run_calls):
    code, out, _ = _run(_args(EXAMPLE, tmp_path / "absent.json", "--json"), capsys)
    assert code == 3
    [diagnostic] = _report(out)["diagnostics"]
    assert diagnostic["code"] == "IO_ERROR" and diagnostic["category"] == "io"
    assert diagnostic["path"] == "absent.json" and diagnostic["span"] is None
    assert str(tmp_path) not in out
    assert run_calls == []


def test_directory_as_cases_is_io_error(tmp_path, capsys):
    code, _, err = _run(_args(EXAMPLE, tmp_path), capsys)
    assert code == 3
    assert "error:" in err


def test_oversized_cases_file_is_rejected(tmp_path, capsys, run_calls):
    cases = tmp_path / "big.json"
    cases.write_bytes(b" " * (1048576 + 1))
    code, out, _ = _run(_args(EXAMPLE, cases, "--json"), capsys)
    assert code == 1
    assert _report(out)["diagnostics"][0]["code"] == "FIXTURE_ERROR"
    assert run_calls == []


def test_missing_cases_argument_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["package", "test", "--manifest", str(EXAMPLE / "nano.json"),
              "--lock", str(EXAMPLE / "nano.lock")])
    assert exit_info.value.code == 2


# -- package rejection and unsupported handlers -----------------------------


def test_stale_pin_reports_package_error_and_runs_nothing(tmp_path, capsys, run_calls):
    root = _copy_example(tmp_path)
    (root / "evidence.nano").write_bytes((root / "evidence.nano").read_bytes() + b"// edit\n")
    code, out, _ = _run(_args(root, root / "cases.json", "--json"), capsys)
    assert code == 1
    report = _report(out)
    assert report["packageHash"] is None and report["moduleHash"] is None
    assert report["cases"] == [] and report["total"] == 0
    assert report["diagnostics"][0]["code"] == "PACKAGE_ERROR"
    assert run_calls == []


def _reasoning_package(root: Path) -> Path:
    root.mkdir()
    entry = (
        "tier nano+\n"
        "strategy Agent {\n"
        "  input value: series<float>\n"
        "  signature Review { input value: float output confidence: float }\n"
        "  let review = infer(Review, value)\n"
        "  every 1m { if review.confidence > 0.5 { execute() } }\n"
        "}\n"
    )
    (root / "agent.nano").write_bytes(entry.encode())
    manifest = {"nanoPackageVersion": "0.1.0", "entry": "agent.nano", "modules": {},
                "effects": ["intent.emit", "llm.call", "log.append"]}
    (root / "nano.json").write_text(json.dumps(manifest), encoding="utf-8")
    pin = "sha256:" + hashlib.sha256(entry.encode()).hexdigest()
    (root / "nano.lock").write_text(json.dumps(
        {"nanoLockVersion": "0.1.0", "dependencies": {"agent.nano": pin}}), encoding="utf-8")
    return root


def test_inference_package_is_refused_before_execution(tmp_path, capsys, run_calls):
    root = _reasoning_package(tmp_path / "pkg")
    cases = _write_cases(tmp_path / "cases.json", [
        {"name": "x", "frame": {"timestamps": [0], "signals": {"value": [1.0]}},
         "expect": {"proposedIntents": []}}])
    code, out, _ = _run(_args(root, cases, "--json"), capsys)
    assert code == 1
    report = _report(out)
    [diagnostic] = report["diagnostics"]
    assert diagnostic["code"] == "UNSUPPORTED_HANDLER" and diagnostic["category"] == "capability"
    assert diagnostic["path"] == "agent.nano" and diagnostic["span"] is None
    assert report["cases"] == [] and report["total"] == 0
    assert report["packageHash"] is not None
    assert run_calls == []


# -- read-only --------------------------------------------------------------


def test_test_command_writes_nothing(tmp_path, capsys):
    root = _copy_example(tmp_path)
    before = _snapshot(root)
    for extra in ((), ("--json",)):
        code, _, _ = _run(_args(root, root / "cases.json", *extra), capsys)
        assert code == 0
    _run(_args(root, root / "nano.json"), capsys)  # a manifest is not a fixture
    assert _snapshot(root) == before
