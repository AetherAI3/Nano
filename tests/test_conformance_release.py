"""Release-conformance harness (#88): JUnit parsing, suite rules, gate, catalog.

Pure, fast unit tests over synthetic reports. The process-level fault
injections and schema validation of real harness output live in
test_conformance_faults.py.
"""
from __future__ import annotations

import ast
import copy
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "conformance"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from rc_catalog import CapabilitySpec, default_catalog, resolve_suites  # noqa: E402
from rc_common import (BLOCKED, EVIDENCE_SCHEMA, FAIL, INCOMPLETE, MANIFEST_SCHEMA,  # noqa: E402
                       NOT_EXECUTED, PASS, canonical_sha256, redact)
from rc_evaluate import (Execution, compute_gate, evaluate_suite, not_run_row, precheck,  # noqa: E402
                         rollup_capabilities)
from rc_harness import verify_evidence  # noqa: E402
from rc_junit import match_required, parse_junit  # noqa: E402

SCHEMA = ROOT / "docs" / "release-conformance.schema.json"


def junit(*cases: str) -> bytes:
    body = "".join(cases)
    return (f'<?xml version="1.0"?><testsuites><testsuite name="pytest" time="1.5" '
            f'timestamp="2026-01-01T00:00:00" hostname="h">{body}</testsuite></testsuites>').encode()


def case(name: str, child: str = "", classname: str = "tests.test_x", time: str = "0.1") -> str:
    return f'<testcase classname="{classname}" name="{name}" time="{time}">{child}</testcase>'


SKIP = '<skipped type="pytest.skip" message="synthetic reason">C:\\x\\tests\\test_x.py:3: synthetic</skipped>'
XFAIL = '<skipped type="pytest.xfail" message="known gap"/>'
FAILURE = '<failure message="boom">traceback</failure>'
ERROR = '<error message="setup">traceback</error>'


def entry(kind: str = "pytest", required_tests=(), files=("tests/test_x.py",), fixtures=(),
          patterns=(), required=True, mode="core", capability="cap", needs=()) -> dict:
    return {"id": "suite", "mode": mode, "kind": kind, "required": required, "capability": capability,
            "files": [{"path": f, "sha256": "0" * 64} for f in files],
            "fixtures": [{"path": f, "sha256": "1" * 64} for f in fixtures],
            "fixture_patterns": list(patterns), "required_tests": list(required_tests), "needs": list(needs)}


def ran(data: bytes, exit_code: int = 0) -> Execution:
    return Execution(True, exit_code, junit=parse_junit(data), command=("{python}", "-m", "pytest"),
                     junit_path="junit/suite.xml", log_path="logs/suite.log")


# --- JUnit -----------------------------------------------------------------

def test_junit_counts_outcomes_and_keeps_only_declared_skip_reasons():
    summary = parse_junit(junit(case("a"), case("b", SKIP), case("c", XFAIL), case("d", FAILURE),
                                case("e", ERROR)))
    assert summary.counts() == {"passed": 1, "failed": 1, "error": 1, "skipped": 1, "xfailed": 1,
                                "collected": 5, "executed": 4}
    assert summary.skip_reasons() == {"synthetic reason": 1}


def test_outcome_digest_ignores_durations_timestamps_and_order():
    first = parse_junit(junit(case("a", time="0.1"), case("b", SKIP)))
    second = parse_junit(junit(case("b", SKIP, time="9.9"), case("a", time="3.0")))
    assert first.outcomes_sha256() == second.outcomes_sha256()
    changed = parse_junit(junit(case("a", FAILURE), case("b", SKIP)))
    assert changed.outcomes_sha256() != first.outcomes_sha256()


def test_node_file_attributes_are_relativized_and_paths_in_reasons_redacted():
    data = (b'<testsuites><testcase name="t" classname="test" file="C:\\w\\sdk\\test\\a.test.ts">'
            b'<skipped type="skipped" message="see C:\\w\\sdk"/></testcase></testsuites>')
    summary = parse_junit(data, root="C:\\w\\sdk", redactions={"C:\\w\\sdk": "{src}"})
    assert summary.cases[0].classname == "test/a.test.ts"
    assert summary.skip_reasons() == {"see {src}": 1}


def test_module_level_skip_reports_its_reason_without_the_path():
    text = ("('C:\\\\x\\\\tests\\\\test_m.py', 9, \"Skipped: could not import 'aether_atlas': "
            "No module named 'aether_atlas'\")")
    data = junit(case("tests.test_m", f'<skipped message="collection skipped">{text}</skipped>', classname=""))
    assert parse_junit(data).skip_reasons() == {
        "collection skipped: could not import 'aether_atlas': No module named 'aether_atlas'": 1}


def test_unparseable_or_foreign_report_is_rejected():
    with pytest.raises(ValueError):
        parse_junit(b"<not-closed")
    with pytest.raises(ValueError):
        parse_junit(b"<html/>")


def test_required_tests_must_exist_and_every_parametrization_must_pass():
    summary = parse_junit(junit(case("test_a[1]"), case("test_a[2]", SKIP), case("test_b"),
                                case("test_c", classname="tests.test_y")))
    missing, not_passed = match_required(summary, ["tests/test_x.py::test_a", "tests/test_x.py::test_b",
                                                   "tests/test_x.py::test_gone", "tests/test_y.py"])
    assert missing == ["tests/test_x.py::test_gone"]
    assert not_passed == ["tests/test_x.py::test_a"]


# --- suite rules -------------------------------------------------------------

def test_a_clean_green_suite_passes_with_counts_and_skip_reasons():
    row = evaluate_suite(entry(required_tests=["tests/test_x.py::test_a"]),
                         ran(junit(case("test_a"), case("test_b", SKIP))))
    assert row["status"] == PASS and row["reasons"] == []
    assert row["counts"]["executed"] == 1 and row["skip_reasons"] == {"synthetic reason": 1}


@pytest.mark.parametrize("data, exit_code, expected", [
    (junit(), 5, "empty collection"),
    (junit(case("a", SKIP), case("b", SKIP)), 0, "all tests skipped"),
    (junit(case("a")), 1, "nonzero exit code 1"),
    (junit(case("a", FAILURE)), 1, "1 failed, 0 errors"),
    (junit(case("a", ERROR)), 2, "0 failed, 1 errors"),
])
def test_empty_all_skipped_nonzero_failed_and_errored_suites_fail(data, exit_code, expected):
    row = evaluate_suite(entry(), ran(data, exit_code))
    assert row["status"] == FAIL and expected in row["reasons"]


def test_required_test_that_is_skipped_or_absent_fails_the_suite():
    row = evaluate_suite(entry(required_tests=["tests/test_x.py::test_a", "tests/test_x.py::test_z"]),
                         ran(junit(case("test_a", SKIP), case("test_b"))))
    assert row["status"] == FAIL
    assert row["required_tests"] == {"declared": 2, "missing": ["tests/test_x.py::test_z"],
                                     "not_passed": ["tests/test_x.py::test_a"]}


def test_startup_failures_are_not_executed_and_a_missing_report_after_running_fails():
    assert evaluate_suite(entry(), Execution(False, startup_error="OSError"))["status"] == NOT_EXECUTED
    assert evaluate_suite(entry(), Execution(True, 4))["status"] == NOT_EXECUTED
    assert evaluate_suite(entry(), Execution(True, 1, junit_error="no JUnit report written"))["status"] == FAIL
    assert evaluate_suite(entry(), Execution(True, None, timed_out=True))["status"] == FAIL


def test_command_suites_fail_on_nonzero_exit_or_any_failed_expectation():
    assert evaluate_suite(entry("cli_help"), Execution(True, 0))["status"] == PASS
    assert evaluate_suite(entry("cli_help"), Execution(True, 2))["status"] == FAIL
    row = evaluate_suite(entry("wheel_import"), Execution(True, 0, checks=("import failed: nano",)))
    assert row["status"] == FAIL and row["reasons"] == ["import failed: nano"]


def test_precheck_reports_missing_tests_and_fixture_patterns_that_match_nothing():
    pinned = entry(files=("tests/test_x.py",), fixtures=("fx/a.json",), patterns=("fx/*.json", "gone/*"))
    pinned["files"][0]["sha256"] = None
    assert precheck(pinned) == ["missing file: tests/test_x.py", "missing fixture: gone/*"]
    assert precheck(entry(files=())) == ["no test files selected"]


def test_a_suite_that_did_not_run_cannot_be_recorded_as_passing():
    with pytest.raises(ValueError):
        not_run_row(entry(), PASS, [])


# --- capabilities and gate ---------------------------------------------------

def _row(sid, status, required=True, mode="core", capability="cap"):
    row = not_run_row(dict(entry(required=required, mode=mode, capability=capability), id=sid),
                      FAIL, ["x"])
    row.update(status=status, reasons=[] if status == PASS else ["x"])
    return row


CAPS = (CapabilitySpec("cap", "u", True, "required cap"), CapabilitySpec("opt", "u", False, "optional"))


def test_blocked_required_capability_keeps_the_release_gate_incomplete_never_pass():
    rows = [_row("core", PASS), _row("native", BLOCKED, mode="qiskit")]
    caps = rollup_capabilities(CAPS, rows)
    assert {c["id"]: c["status"] for c in caps} == {"cap": INCOMPLETE, "opt": INCOMPLETE}
    gate = compute_gate(rows, caps, [])
    assert gate["core"] == PASS and gate["release"] == INCOMPLETE
    assert gate["incomplete_required_suites"] == ["native"]


def test_optional_not_executed_is_listed_and_optional_failure_fails_the_release():
    rows = [_row("core", PASS), _row("x", NOT_EXECUTED, required=False, capability="opt")]
    gate = compute_gate(rows, rollup_capabilities(CAPS[:1], rows), [])
    assert gate["release"] == PASS and gate["optional_not_executed"] == ["x"]
    rows[1]["status"] = FAIL
    gate = compute_gate(rows, rollup_capabilities(CAPS, rows), [])
    assert gate["release"] == FAIL and gate["failing_suites"] == ["x"]


def test_source_reasons_fail_both_gates_even_when_every_suite_passed():
    rows = [_row("core", PASS)]
    gate = compute_gate(rows, rollup_capabilities(CAPS[:1], rows), ["source checkout is dirty"])
    assert gate["core"] == FAIL and gate["release"] == FAIL


def test_capability_without_any_executed_suite_is_incomplete():
    caps = rollup_capabilities(CAPS, [_row("core", PASS)])
    assert caps[1]["status"] == INCOMPLETE


# --- stored evidence ---------------------------------------------------------

def _evidence(rows):
    caps = rollup_capabilities(CAPS[:1], rows)
    canonical = {"schema": EVIDENCE_SCHEMA, "manifest": {"m": 1},
                 "manifest_sha256": canonical_sha256({"m": 1}), "admission": {"status": "ADMITTED"},
                 "suites": rows, "capabilities": caps, "gate": compute_gate(rows, caps, [])}
    return {"schema": EVIDENCE_SCHEMA, "canonical": canonical,
            "canonical_sha256": canonical_sha256(canonical)}


def test_verify_detects_a_hand_edited_gate_status_or_inconsistent_pass_row():
    good = evaluate_suite(entry(), ran(junit(case("test_a"))))
    doc = _evidence([good])
    assert verify_evidence(doc) == []
    tampered = copy.deepcopy(doc)
    tampered["canonical"]["suites"][0]["counts"]["failed"] = 3
    tampered["canonical_sha256"] = canonical_sha256(tampered["canonical"])
    assert any("PASS row" in p for p in verify_evidence(tampered))
    bad = evaluate_suite(entry(), ran(junit(case("test_a", FAILURE)), 1))
    forged = _evidence([bad])
    forged["canonical"]["gate"]["release"] = PASS
    forged["canonical_sha256"] = canonical_sha256(forged["canonical"])
    assert "gate does not follow from the suite rows" in verify_evidence(forged)
    forged["canonical_sha256"] = "0" * 64
    assert "canonical_sha256 does not match the canonical evidence" in verify_evidence(forged)


def test_redaction_replaces_both_separator_spellings_longest_first():
    mapping = {"C:\\out": "{out}", "C:\\out\\work\\src": "{src}"}
    assert redact("C:/out/work/src/x and C:\\out\\y", mapping) == "{src}/x and {out}\\y"
    assert redact(json.dumps({"path": "C:\\out\\work\\src"}), mapping) == '{"path": "{src}"}'


# --- catalog against this source ----------------------------------------------

def test_every_declared_suite_file_exists_and_required_tests_name_real_functions():
    for item in resolve_suites(default_catalog(), ROOT):
        assert all(f["sha256"] for f in item["files"]), item["id"]
        assert precheck(item) == [], item["id"]
        for test_id in item["required_tests"]:
            path, _, function = test_id.partition("::")
            text = (ROOT / path).read_text(encoding="utf-8")
            assert not function or re.search(rf"^def {re.escape(function)}\(", text, re.MULTILINE), test_id


def test_core_partition_excludes_every_capability_qiskit_and_cross_repo_file():
    entries = {item["id"]: item for item in resolve_suites(default_catalog(), ROOT)}
    core = {f["path"] for f in entries["core.pytest"]["files"]}
    claimed = {f["path"] for sid, item in entries.items() if sid != "core.pytest"
               and item["kind"] == "pytest" for f in item["files"]}
    assert core and not core & claimed
    every = {p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("test_*.py")}
    assert core | claimed == every


def test_native_bridge_is_required_and_needs_the_pinned_sdk():
    catalog = default_catalog()
    native = catalog.suite("qiskit.native_bridge")
    assert native.required and native.needs == ("qiskit_pin",) and native.capability == "native_bridge"
    assert {c.id: c.required for c in catalog.capabilities}["native_bridge"] is True
    assert catalog.suite("cross_repo.fake_provider").required is False


def test_harness_sources_import_only_the_standard_library_and_each_other():
    stdlib = set(sys.stdlib_module_names)
    for path in sorted(HARNESS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                assert root in stdlib or root.startswith("rc_") or root == "__future__", (path.name, name)


def test_harness_files_stay_small():
    for path in HARNESS.glob("*.py"):
        assert len(path.read_text(encoding="utf-8").splitlines()) < 800, path.name


def test_sha256sums_check_refuses_tampered_unlisted_missing_and_unsorted_files(tmp_path):
    from rc_checks import sha256_file, sha256sums_problems
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "a.json").write_bytes(b"{}\n")
    (tmp_path / "fixtures" / "b.json").write_bytes(b"[]\n")

    def write_sums(*rels):
        lines = [f"{sha256_file(tmp_path / r) if (tmp_path / r).exists() else '0' * 64}  {r}" for r in rels]
        (tmp_path / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")

    write_sums("a.json", "fixtures/b.json")
    assert sha256sums_problems(tmp_path) == []
    (tmp_path / "fixtures" / "b.json").write_bytes(b"[1]\n")
    assert sha256sums_problems(tmp_path) == ["digest mismatch: fixtures/b.json"]
    write_sums("fixtures/b.json", "a.json")
    assert "SHA256SUMS entries are not sorted" in sha256sums_problems(tmp_path)
    write_sums("a.json")
    assert sha256sums_problems(tmp_path) == ["not listed in SHA256SUMS: fixtures/b.json"]
    write_sums("a.json", "fixtures/b.json", "gone.json")
    assert sha256sums_problems(tmp_path) == ["listed but missing: gone.json"]
    (tmp_path / "SHA256SUMS").unlink()
    assert sha256sums_problems(tmp_path) == ["missing SHA256SUMS"]


def test_cli_refuses_an_output_dir_too_long_for_windows_max_path(monkeypatch, tmp_path, capsys):
    import types

    import release_conformance as cli
    monkeypatch.setattr(cli, "os", types.SimpleNamespace(name="nt"))
    long_out = tmp_path / ("x" * (cli.MAX_WINDOWS_OUT_DIR + 1))
    assert cli.main(["run", "--out-dir", str(long_out), "--no-node"]) == cli.EXIT_HARNESS
    assert "MAX_PATH" in capsys.readouterr().err and not long_out.exists()


def test_this_source_lifecycle_sums_verify():
    from rc_checks import sha256sums_problems
    assert sha256sums_problems(ROOT / "protocol" / "qsearch-lifecycle" / "v1") == []


def test_schema_document_names_both_versioned_record_types():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == EVIDENCE_SCHEMA
    assert schema["$defs"]["manifest"]["properties"]["schema"]["const"] == MANIFEST_SCHEMA
