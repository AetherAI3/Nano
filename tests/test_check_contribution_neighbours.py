"""Issue #26: the contribution checker verifies each `// NOT <neighbour>:` line.

Every library entry names the existing rule it is most likely to be confused
with. The catalog parser already requires the line, rejects self-reference and
duplicates; these tests prove the checker additionally rejects a neighbour that
names no library entry, end to end, on a scratch copy of the real library.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_contribution.py"
CREDENTIAL = Path("nano/library/watchdog/credential_age_alert.nano")
STALE_DATA = Path("nano/library/risk/stale_data_halt.nano")


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_contribution_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """A copy of nano/library the checker treats as the real one."""
    checker = _load_checker()
    shutil.copytree(ROOT / "nano" / "library", tmp_path / "nano" / "library")
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(checker, "LIBRARY", tmp_path / "nano" / "library")
    return checker, tmp_path


def _check(checker, root: Path, relative: Path) -> List[str]:
    problems: List[str] = []
    checker.check_entry(root / relative, False, problems)
    return problems


def _rewrite_not_line(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"fixture drift: {old!r} not in {path.name}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


def test_whole_library_passes_the_checker():
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--all"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_unmodified_entry_passes_positive_control(scratch):
    checker, root = scratch
    assert _check(checker, root, CREDENTIAL) == []


def test_neighbour_that_names_no_entry_is_rejected(scratch):
    checker, root = scratch
    _rewrite_not_line(root / CREDENTIAL, "// NOT trusted_route_guard:", "// NOT no_such_rule:")
    problems = _check(checker, root, CREDENTIAL)
    assert len(problems) == 1, problems
    [problem] = problems
    assert problem.startswith("nano/library/watchdog/credential_age_alert.nano:")
    assert "`NOT no_such_rule:`" in problem
    assert "names no library entry" in problem
    assert "nano/library/README.md" in problem


def test_renamed_neighbour_is_caught_in_the_entry_that_still_names_it(scratch):
    checker, root = scratch
    library = root / "nano" / "library" / "watchdog"
    for suffix in (".nano", "_ir.json"):
        (library / f"trusted_route_guard{suffix}").rename(library / f"route_guard{suffix}")
    problems = _check(checker, root, CREDENTIAL)
    assert any("`NOT trusted_route_guard:`" in p and "names no library entry" in p
               for p in problems), problems


def test_self_reference_is_rejected(scratch):
    checker, root = scratch
    _rewrite_not_line(root / CREDENTIAL, "// NOT trusted_route_guard:", "// NOT credential_age_alert:")
    problems = _check(checker, root, CREDENTIAL)
    assert any("points back to the same strategy" in p for p in problems), problems


def test_missing_not_line_is_rejected(scratch):
    checker, root = scratch
    path = root / CREDENTIAL
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("// NOT trusted_route_guard:"))
    kept = lines[:start] + lines[start + 3:]  # the NOT field spans three comment lines
    assert not any(line.startswith("// NOT ") for line in kept)
    path.write_text("".join(kept), encoding="utf-8", newline="\n")
    problems = _check(checker, root, CREDENTIAL)
    assert any("missing `NOT <strategy_slug>:`" in p for p in problems), problems


def test_cross_category_neighbour_is_allowed(scratch):
    # risk/stale_data_halt contrasts itself with event_volatility/event_liquidity_halt;
    # a cross-category contrast can be the most useful one, so it stays legal.
    checker, root = scratch
    assert "// NOT event_liquidity_halt:" in (root / STALE_DATA).read_text(encoding="utf-8")
    assert _check(checker, root, STALE_DATA) == []
