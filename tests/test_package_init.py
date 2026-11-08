"""Issue #84 follow-up: `nano package init --directory PATH`.

Contract: docs/package-cli.md, section `init --directory PATH`. A fixed,
pre-pinned template written only into a new or empty destination; it never
reads, repins or rewrites an existing project.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from nano.cli.main import main
from nano.compiler import compile_package

ROOT = Path(__file__).resolve().parent.parent
FILES = ["agent.nano", "cases.json", "nano.json", "nano.lock", "rules.nano"]


def _run(argv: List[str], capsys) -> Tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _init(target: Path, capsys) -> Tuple[int, str, str]:
    return _run(["package", "init", "--directory", str(target)], capsys)


def _tree(root: Path) -> Dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(root.iterdir())}


def _pkg_args(root: Path) -> List[str]:
    return ["--manifest", str(root / "nano.json"), "--lock", str(root / "nano.lock")]


# -- positive path ----------------------------------------------------------


def test_init_creates_a_pinned_package_that_check_build_and_test_accept(tmp_path, capsys):
    target = tmp_path / "agent"
    code, out, err = _init(target, capsys)
    assert code == 0, err
    assert out == ""
    assert err.strip() == f"created {target} (5 files)"
    tree = _tree(target)
    assert sorted(tree) == FILES
    assert all(b"\r" not in data for data in tree.values())

    lock = json.loads(tree["nano.lock"])
    manifest = json.loads(tree["nano.json"])
    pinned = [manifest["entry"], *manifest["modules"].values()]
    assert sorted(lock["dependencies"]) == sorted(pinned)
    for name, digest in lock["dependencies"].items():
        assert digest == "sha256:" + hashlib.sha256(tree[name]).hexdigest()
    assert set(manifest["effects"]) <= {"intent.emit", "log.append"}

    assert _run(["package", "check", *_pkg_args(target)], capsys)[0] == 0
    code, out, _ = _run(["package", "build", *_pkg_args(target)], capsys)
    assert code == 0
    api = compile_package(target / "nano.json", lock_path=target / "nano.lock").to_dict()
    assert json.loads(out)["packageHash"] == api["packageHash"]
    code, out, _ = _run(["package", "test", *_pkg_args(target), "--cases",
                         str(target / "cases.json"), "--json"], capsys)
    assert code == 0
    report = json.loads(out)
    assert report["success"] is True and report["total"] >= 2 and report["failed"] == 0
    statuses = {tuple(i["intent"] for i in c["observed"]["proposedIntents"]) for c in report["cases"]}
    assert len(statuses) >= 2  # a positive and a negative control, not one outcome twice


def test_init_is_byte_identical_across_runs_and_working_directories(tmp_path, capsys):
    first, second = tmp_path / "one", tmp_path / "two"
    assert _init(first, capsys)[0] == 0
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    completed = subprocess.run(
        [sys.executable, "-m", "nano.cli", "package", "init", "--directory", "../two"],
        cwd=elsewhere, env=env, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == b""
    assert _tree(first) == _tree(second)
    hashes = {compile_package(d / "nano.json", lock_path=d / "nano.lock").to_dict()["packageHash"]
              for d in (first, second)}
    assert len(hashes) == 1


def test_init_into_existing_empty_directory(tmp_path, capsys):
    target = tmp_path / "empty"
    target.mkdir()
    code, _, err = _init(target, capsys)
    assert code == 0, err
    assert sorted(_tree(target)) == FILES


# -- refusals: never overwrite, never rewrite pins --------------------------


def test_non_empty_directory_is_refused_untouched(tmp_path, capsys):
    target = tmp_path / "busy"
    target.mkdir()
    (target / "notes.txt").write_bytes(b"keep me\n")
    code, out, err = _init(target, capsys)
    assert (code, out) == (2, "")
    assert "error:" in err
    assert _tree(target) == {"notes.txt": b"keep me\n"}


def test_existing_project_pins_are_never_rewritten(tmp_path, capsys):
    target = tmp_path / "project"
    target.mkdir()
    stale = {"nano.json": b'{"stale": true}\n', "nano.lock": b'{"dependencies": {}}\n',
             "agent.nano": b"strategy Old {}\n"}
    for name, data in stale.items():
        (target / name).write_bytes(data)
    before = {p.name: p.stat().st_mtime_ns for p in target.iterdir()}
    code, _, _ = _init(target, capsys)
    assert code == 2
    assert _tree(target) == dict(sorted(stale.items()))
    assert {p.name: p.stat().st_mtime_ns for p in target.iterdir()} == before


def test_existing_file_is_refused_untouched(tmp_path, capsys):
    target = tmp_path / "file.txt"
    target.write_bytes(b"data\n")
    code, _, _ = _init(target, capsys)
    assert code == 2
    assert target.read_bytes() == b"data\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["file.txt"]


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows reparse points")
def test_junction_destination_is_refused(tmp_path, capsys):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "junction"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)],
                          capture_output=True, check=False)
    assert made.returncode == 0, made.stderr  # junctions need no privilege; never skip
    code, _, err = _init(link, capsys)
    assert code == 2
    assert "symlink or junction" in err
    assert list(real.iterdir()) == []


def test_unreadable_destination_is_io_error_not_traceback(tmp_path, capsys, monkeypatch):
    from nano.cli import package_init

    target = tmp_path / "locked"
    target.mkdir()

    def denied(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(package_init.os, "scandir", denied)
    code, out, err = _init(target, capsys)
    assert (code, out) == (3, "")
    assert "error:" in err


def test_symlink_destination_is_refused(tmp_path, capsys):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")
    code, _, _ = _init(link, capsys)
    assert code == 2
    assert list(real.iterdir()) == []


def test_missing_parent_is_io_error_and_creates_nothing(tmp_path, capsys):
    target = tmp_path / "absent" / "agent"
    code, out, err = _init(target, capsys)
    assert (code, out) == (3, "")
    assert "error:" in err
    assert list(tmp_path.iterdir()) == []


def test_missing_directory_argument_is_usage_error():
    with pytest.raises(SystemExit) as exit_info:
        main(["package", "init"])
    assert exit_info.value.code == 2


# -- no partial scaffold -----------------------------------------------------


def _fail_third_write(monkeypatch):
    from nano.cli import package_init

    real = package_init._write_new_file
    calls: List[str] = []

    def failing(path, data):
        calls.append(Path(path).name)
        if len(calls) == 3:
            raise OSError(28, "No space left on device", str(path))
        return real(path, data)

    monkeypatch.setattr(package_init, "_write_new_file", failing)
    return calls


def test_failed_init_into_new_directory_leaves_nothing(tmp_path, capsys, monkeypatch):
    calls = _fail_third_write(monkeypatch)
    target = tmp_path / "agent"
    code, out, _ = _init(target, capsys)
    assert (code, out) == (3, "")
    assert len(calls) == 3
    assert list(tmp_path.iterdir()) == []  # no destination, no temporary sibling


def _fail_after_partial_write(monkeypatch):
    """Fail the 3rd file after it was opened and partly written (review finding)."""
    from nano.cli import package_init

    real = package_init._write_all
    calls: List[int] = []

    def partial(stream, data):
        calls.append(len(data))
        if len(calls) == 3:
            stream.write(data[:10])
            stream.flush()
            raise OSError(28, "No space left on device")
        return real(stream, data)

    monkeypatch.setattr(package_init, "_write_all", partial)
    return calls


@pytest.mark.parametrize("existing", [True, False])
def test_partially_written_file_is_removed(tmp_path, capsys, monkeypatch, existing):
    calls = _fail_after_partial_write(monkeypatch)
    target = tmp_path / "agent"
    if existing:
        target.mkdir()
    code, out, _ = _init(target, capsys)
    assert (code, out) == (3, "")
    assert len(calls) == 3
    expected = ["agent"] if existing else []
    assert sorted(p.name for p in tmp_path.iterdir()) == expected
    if existing:
        assert list(target.iterdir()) == []


def test_destination_swapped_for_a_link_mid_write_is_refused(tmp_path, capsys, monkeypatch):
    """Review finding (TOCTOU): re-validate the destination before every file."""
    from nano.cli import package_init

    target = tmp_path / "empty"
    target.mkdir()
    real_is_link = package_init._is_link
    checks: List[int] = []

    def swapped_after_first_check(path):
        checks.append(1)
        return True if len(checks) > 1 else real_is_link(path)

    monkeypatch.setattr(package_init, "_is_link", swapped_after_first_check)
    code, _, err = _init(target, capsys)
    assert code == 3
    assert "changed" in err
    assert list(target.iterdir()) == []


def test_failed_init_into_empty_directory_leaves_it_empty(tmp_path, capsys, monkeypatch):
    calls = _fail_third_write(monkeypatch)
    target = tmp_path / "empty"
    target.mkdir()
    code, _, _ = _init(target, capsys)
    assert code == 3
    assert len(calls) == 3
    assert list(target.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["empty"]
