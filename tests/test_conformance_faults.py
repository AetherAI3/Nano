"""Release-conformance fault injection (#88) on a tiny synthetic repository.

Each control drives the real harness (git export, scrubbed child processes,
real pytest + JUnit) over synthetic suites, or the real wheel checks over
synthetic wheels, and proves the fault cannot yield a passing required gate.
A positive control and a repeat run prove the controls are not vacuous and
that canonical evidence is byte-stable.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "conformance"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

import rc_harness  # noqa: E402
from rc_catalog import CapabilitySpec, Catalog, SuiteSpec  # noqa: E402
from rc_checks import run_child_check  # noqa: E402
from rc_common import BLOCKED, FAIL, INCOMPLETE, NOT_EXECUTED, PASS, canonical_bytes  # noqa: E402
from rc_evaluate import evaluate_suite  # noqa: E402
from rc_harness import Options, prepare_manifest, run, verify_evidence  # noqa: E402
from rc_runner import Context, child_env  # noqa: E402
from rc_wheel import inspect_wheel  # noqa: E402

FILES = {
    "synth/__init__.py": "VALUE = 1\n",
    "fixtures/data.json": '{"ok": true}\n',
    "pyproject.toml": '[project.optional-dependencies]\nquantum = ["synthetic-sdk==9.9.9"]\n',
    "tests/test_ok.py": (
        "import json, pathlib\nfrom synth import VALUE\n"
        "ROOT = pathlib.Path(__file__).resolve().parents[1]\n"
        "def test_value():\n    assert VALUE == 1\n"
        "def test_reads_fixture():\n"
        "    assert json.loads((ROOT / 'fixtures' / 'data.json').read_text())['ok'] is True\n"),
    "tests/test_skip.py": ("import pytest\npytestmark = pytest.mark.skip(reason='synthetic skip')\n"
                           "def test_a():\n    pass\ndef test_b():\n    pass\n"),
    "tests/test_empty.py": '"""A module that collects nothing."""\n',
    "tests/test_partial.py": ("import pytest\n@pytest.mark.skip(reason='not implemented')\n"
                              "def test_required():\n    pass\ndef test_other():\n    pass\n"),
}
OK = SuiteSpec("synth.ok", "core", "pytest", True, "cap", files=("tests/test_ok.py",),
               required_tests=("tests/test_ok.py::test_reads_fixture",), fixtures=("fixtures/*.json",))
OPTIONAL = SuiteSpec("synth.cross_repo", "cross_repo", "pytest", False, "xrepo",
                     files=("tests/test_ok.py",), needs=("checkouts",))
CAPS = (CapabilitySpec("cap", "synthetic", True, "required"), CapabilitySpec("xrepo", "synthetic", False, "opt"),
        CapabilitySpec("native", "synthetic", True, "needs the pinned SDK"))
GOOD = Catalog((OK, OPTIONAL), CAPS[:2], origin_modules=("synth",))
BAD = Catalog((
    OK,
    SuiteSpec("synth.all_skipped", "core", "pytest", True, "cap", files=("tests/test_skip.py",)),
    SuiteSpec("synth.empty", "core", "pytest", True, "cap", files=("tests/test_empty.py",)),
    SuiteSpec("synth.required_skipped", "core", "pytest", True, "cap", files=("tests/test_partial.py",),
              required_tests=("tests/test_partial.py::test_required",)),
    SuiteSpec("synth.missing_test", "core", "pytest", True, "cap", files=("tests/test_gone.py",)),
    SuiteSpec("synth.missing_fixture", "core", "pytest", True, "cap", files=("tests/test_ok.py",),
              fixtures=("fixtures/absent/*.json",)),
    SuiteSpec("synth.pinned_sdk", "qiskit", "pytest", True, "native", files=("tests/test_ok.py",),
              needs=("qiskit_pin",)),
), CAPS, origin_modules=("synth",))


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=Conformance Test",
                           "-c", "user.email=conformance@example.invalid", "-c", "core.autocrlf=false",
                           "-c", "commit.gpgsign=false", *args],
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "synthetic")
    return root


def options(repo: Path, out: Path, catalog: Catalog = GOOD, manifest=None) -> Options:
    return Options(repo=repo, out=out, python=sys.executable, manifest=manifest, catalog=catalog,
                   timeout_s=300, inherited_env=dict(os.environ))


def statuses(doc: dict) -> dict:
    return {row["id"]: row["status"] for row in doc["canonical"]["suites"]}


def assert_refused(doc: dict, fragment: str) -> None:
    canonical = doc["canonical"]
    assert canonical["admission"]["status"] == "REFUSED"
    assert any(fragment in reason for reason in canonical["admission"]["reasons"]), canonical["admission"]
    assert set(statuses(doc).values()) == {NOT_EXECUTED}
    assert canonical["gate"]["core"] == FAIL and canonical["gate"]["release"] == FAIL
    assert verify_evidence(doc) == []


# --- positive control and determinism -----------------------------------------

def test_clean_synthetic_release_passes_and_repeats_byte_identically(repo, tmp_path):
    first = run(options(repo, tmp_path / "out-1"))
    canonical = first["canonical"]
    assert canonical["gate"]["release"] == PASS and canonical["gate"]["core"] == PASS
    assert canonical["source"]["identity"] == "exact_commit"
    assert canonical["source"]["dirty_before"] == [] and canonical["source"]["dirty_after"] == []
    assert statuses(first) == {"synth.ok": PASS, "synth.cross_repo": NOT_EXECUTED}
    assert canonical["gate"]["optional_not_executed"] == ["synth.cross_repo"]
    row = canonical["suites"][0]
    assert row["counts"]["passed"] == 2 and row["junit"] == "junit/synth.ok.xml"
    assert row["command"][0] == "{python}" and "{src}" not in "".join(row["command"][:1])
    assert verify_evidence(first) == []
    checkout = tmp_path / "out-1" / "work" / "src"
    assert git(checkout, "rev-parse", "HEAD") == canonical["source"]["tested_commit"]
    assert git(checkout, "remote") == "" and git(checkout, "status", "--porcelain") == ""
    second = run(options(repo, tmp_path / "out-2"))
    one = (tmp_path / "out-1" / "evidence.canonical.json").read_bytes()
    two = (tmp_path / "out-2" / "evidence.canonical.json").read_bytes()
    assert one == two and first["canonical_sha256"] == second["canonical_sha256"]
    assert first["telemetry"]["paths"]["out"] != second["telemetry"]["paths"]["out"]
    assert str(tmp_path) not in one.decode("ascii").replace("\\\\", "\\")


def test_real_evidence_validates_against_the_published_schema(repo, tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "docs" / "release-conformance.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for name, catalog in (("good", GOOD), ("bad", BAD)):
        doc = run(options(repo, tmp_path / name, catalog))
        assert list(validator.iter_errors(doc)) == [], name
        manifest = json.loads((tmp_path / name / "manifest.json").read_text(encoding="utf-8"))
        manifest_validator = jsonschema.Draft202012Validator({"$defs": schema["$defs"],
                                                              "$ref": "#/$defs/manifest"})
        assert list(manifest_validator.iter_errors(manifest)) == []


# --- suite-level faults: missing test, all-skips, empty, fixture, SDK pin ------

def test_suite_faults_fail_or_block_and_never_pass_the_required_gate(repo, tmp_path):
    doc = run(options(repo, tmp_path / "out", BAD))
    assert statuses(doc) == {
        "synth.ok": PASS, "synth.all_skipped": FAIL, "synth.empty": FAIL, "synth.required_skipped": FAIL,
        "synth.missing_test": FAIL, "synth.missing_fixture": FAIL, "synth.pinned_sdk": BLOCKED,
    }
    rows = {row["id"]: row for row in doc["canonical"]["suites"]}
    assert "all tests skipped" in rows["synth.all_skipped"]["reasons"]
    assert rows["synth.all_skipped"]["skip_reasons"] == {"synthetic skip": 2}
    assert "empty collection" in rows["synth.empty"]["reasons"]
    assert rows["synth.required_skipped"]["required_tests"]["not_passed"] == [
        "tests/test_partial.py::test_required"]
    assert rows["synth.missing_test"]["reasons"] == ["missing file: tests/test_gone.py"]
    assert rows["synth.missing_fixture"]["reasons"] == ["missing fixture: fixtures/absent/*.json"]
    assert "synthetic-sdk None (pinned 9.9.9)" in rows["synth.pinned_sdk"]["reasons"][0]
    caps = {cap["id"]: cap["status"] for cap in doc["canonical"]["capabilities"]}
    assert caps["cap"] == FAIL and caps["native"] == INCOMPLETE
    assert doc["canonical"]["gate"]["release"] == FAIL and doc["canonical"]["gate"]["core"] == FAIL
    assert verify_evidence(doc) == []


def test_pinned_sdk_unavailable_alone_leaves_release_incomplete_not_green(repo, tmp_path):
    catalog = Catalog((OK, BAD.suite("synth.pinned_sdk")), (CAPS[0], CAPS[2]), origin_modules=("synth",))
    gate = run(options(repo, tmp_path / "out", catalog))["canonical"]["gate"]
    assert gate["core"] == PASS and gate["release"] == INCOMPLETE
    assert gate["incomplete_required_capabilities"] == ["native"]


# --- admission faults: wrong SHA, dirty source, dependency, fixture, manifest --

@pytest.fixture
def pinned(repo, tmp_path) -> dict:
    return prepare_manifest(options(repo, tmp_path / "pin"))


def test_wrong_sha_is_refused(repo, tmp_path, pinned):
    wrong = copy.deepcopy(pinned)
    wrong["source"]["commit"] = "f" * 40
    assert_refused(run(options(repo, tmp_path / "out", manifest=wrong)), "unknown in the tested repository")
    short = copy.deepcopy(pinned)
    short["source"]["commit"] = pinned["source"]["commit"][:12]
    assert_refused(run(options(repo, tmp_path / "out-short", manifest=short)), "full 40-hex")


def test_changed_tree_is_refused_and_an_equivalent_tree_is_recorded_as_such(repo, tmp_path, pinned):
    git(repo, "commit", "-q", "--allow-empty", "-m", "same tree, new commit")
    doc = run(options(repo, tmp_path / "equivalent", manifest=pinned))
    source = doc["canonical"]["source"]
    assert source["identity"] == "equivalent_tree" and doc["canonical"]["gate"]["release"] == PASS
    assert source["pinned_commit"] == pinned["source"]["commit"] != source["tested_commit"]
    assert source["pinned_tree"] == source["tested_tree"]
    (repo / "synth" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "changed tree")
    assert_refused(run(options(repo, tmp_path / "changed", manifest=pinned)), "differs from pinned tree")


def test_dirty_source_before_the_run_is_refused(repo, tmp_path):
    (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    doc = run(options(repo, tmp_path / "out"))
    assert_refused(doc, "dirty before execution")
    assert doc["canonical"]["source"]["dirty_before"] == ["untracked.txt"]


def test_source_changed_during_the_run_fails_the_gate(repo, tmp_path, monkeypatch):
    calls = iter([[], ["synth/__init__.py"]])
    monkeypatch.setattr(rc_harness, "dirty_paths", lambda _repo: next(calls))
    doc = run(options(repo, tmp_path / "out"))
    assert statuses(doc)["synth.ok"] == PASS
    assert doc["canonical"]["gate"]["release"] == FAIL and doc["canonical"]["gate"]["core"] == FAIL
    assert doc["canonical"]["source"]["dirty_after"] == ["synth/__init__.py"]


def test_changed_dependency_is_refused(repo, tmp_path, pinned):
    drifted = copy.deepcopy(pinned)
    drifted["dependencies"]["pytest"] = "0.0.1"
    assert_refused(run(options(repo, tmp_path / "out", manifest=drifted)), "dependency drift: pytest")


def test_swapped_fixture_is_refused(repo, tmp_path, pinned):
    swapped = copy.deepcopy(pinned)
    swapped["suites"][0]["fixtures"][0]["sha256"] = "0" * 64
    assert_refused(run(options(repo, tmp_path / "out", manifest=swapped)), "suite synth.ok files, fixtures")


@pytest.mark.parametrize("mutate, fragment", [
    (lambda m: m["interpreter"].update(version="0.0.0"), "manifest mismatch: interpreter"),
    (lambda m: m["selected_suites"].append("synth.undeclared"), "selects undeclared suites"),
    (lambda m: m["suites"].pop(), "missing from the manifest"),
    (lambda m: m.update(schema="nano.release-conformance.manifest.v0"), "unsupported manifest schema"),
    (lambda m: m["source"].update(merge_tree="e" * 40), "proposed merge tree"),
])
def test_manifest_mismatch_is_refused(repo, tmp_path, pinned, mutate, fragment):
    changed = copy.deepcopy(pinned)
    mutate(changed)
    assert_refused(run(options(repo, tmp_path / "out", manifest=changed)), fragment)


def test_empty_selection_cannot_pass(repo, tmp_path, pinned):
    narrowed = copy.deepcopy(pinned)
    narrowed["selected_suites"] = []
    doc = run(options(repo, tmp_path / "out", manifest=narrowed))
    assert set(statuses(doc).values()) == {NOT_EXECUTED}
    assert doc["canonical"]["gate"]["release"] == INCOMPLETE and doc["canonical"]["gate"]["core"] == INCOMPLETE


def test_output_inside_the_repository_is_refused(repo):
    with pytest.raises(rc_harness.HarnessError):
        run(options(repo, repo / "evidence"))


# --- wheel faults: broken import, SDK at import time, leak, missing members ----

def make_wheel(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return path


def check_wheel(tmp_path: Path, members: dict) -> dict:
    wheel = make_wheel(tmp_path / "nano-0-py3-none-any.whl", members)
    out = tmp_path / "out"
    ctx = Context(tmp_path, out, sys.executable, child_env(out, os.environ), {}, 120)
    execution = run_child_check(ctx, "wheel.import", wheel, ("nano",), (), False, tmp_path)
    spec = {"id": "wheel.import", "mode": "core", "kind": "wheel_import", "required": True, "capability": None,
            "files": [], "fixtures": [], "fixture_patterns": [], "required_tests": [], "needs": []}
    return evaluate_suite(spec, execution)


def test_wheel_import_check_passes_a_sound_wheel(tmp_path):
    assert check_wheel(tmp_path, {"nano/__init__.py": "VALUE = 1\n"})["status"] == PASS


@pytest.mark.parametrize("members, fragment", [
    ({"nano/__init__.py": "raise ImportError('synthetic broken wheel')\n"}, "import failed: nano"),
    ({"nano/__init__.py": "import qiskit\n"}, "blocked by the release-conformance harness"),
    ({"other/__init__.py": ""}, "nano"),
])
def test_broken_wheel_or_optional_sdk_import_fails(tmp_path, members, fragment):
    row = check_wheel(tmp_path, members)
    assert row["status"] == FAIL and any(fragment in reason for reason in row["reasons"]), row["reasons"]


def test_wheel_missing_documented_members_fails(tmp_path):
    source = tmp_path / "src"
    for rel in ("nano/__init__.py", "nano/extra.py", "nano/quantum/fixtures/native-x.json"):
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_text("{}\n", encoding="utf-8")
    wheel = make_wheel(tmp_path / "w.whl", {"nano/__init__.py": "", "tests/test_x.py": ""})
    execution = inspect_wheel(wheel, source)
    assert "wheel is missing nano/extra.py" in execution.checks
    assert "wheel is missing nano/quantum/fixtures/native-x.json" in execution.checks
    assert "wheel ships repository tests or scripts" in execution.checks
    corrupt = tmp_path / "corrupt.whl"
    corrupt.write_bytes(b"not a zip")
    assert inspect_wheel(corrupt, source).checks[0].startswith("unreadable wheel")


def test_cli_verify_accepts_real_evidence_and_rejects_a_forged_gate(repo, tmp_path):
    doc = run(options(repo, tmp_path / "out"))
    script = HARNESS / "release_conformance.py"
    evidence = tmp_path / "out" / "evidence.json"
    ok = subprocess.run([sys.executable, str(script), "verify", str(evidence)], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    forged = copy.deepcopy(doc)
    forged["canonical"]["suites"][0]["status"] = FAIL
    forged_path = tmp_path / "forged.json"
    forged_path.write_bytes(canonical_bytes(forged))
    bad = subprocess.run([sys.executable, str(script), "verify", str(forged_path)], capture_output=True, text=True)
    assert bad.returncode == 1 and "canonical_sha256 does not match" in bad.stderr
