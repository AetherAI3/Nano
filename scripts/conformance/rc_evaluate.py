"""Pure evaluation: execution facts -> suite rows -> capabilities -> gate.

Nothing here runs a process. Every rule that decides whether a suite passed
lives in this module so it can be tested with synthetic inputs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Iterable, List, Optional, Sequence, Tuple

from rc_common import BLOCKED, FAIL, INCOMPLETE, NOT_EXECUTED, PASS
from rc_junit import JUnitSummary, match_required

REPORTING_KINDS = ("pytest", "node")
PYTEST_STARTUP_EXITS = (3, 4)
PYTEST_NO_TESTS = 5


@dataclass(frozen=True)
class Execution:
    """What a runner observed. ``command`` is already redacted."""

    launched: bool
    exit_code: Optional[int] = None
    timed_out: bool = False
    startup_error: str = ""
    junit: Optional[JUnitSummary] = None
    junit_error: str = ""
    checks: Tuple[str, ...] = ()
    command: Tuple[str, ...] = ()
    junit_path: Optional[str] = None
    log_path: Optional[str] = None
    detail: dict = field(default_factory=dict)


def _base_row(entry: dict) -> dict:
    return {
        "id": entry["id"], "mode": entry["mode"], "kind": entry["kind"],
        "required": entry["required"], "capability": entry["capability"],
        "status": None, "reasons": [], "command": [], "exit_code": None,
        "counts": None, "skip_reasons": {}, "outcomes_sha256": None,
        "required_tests": {"declared": len(entry["required_tests"]), "missing": [], "not_passed": []},
        "junit": None, "log": None, "detail": {},
    }


def not_run_row(entry: dict, status: str, reasons: Sequence[str]) -> dict:
    if status not in (NOT_EXECUTED, BLOCKED, FAIL):
        raise ValueError(f"a suite that did not run cannot be {status}")
    row = _base_row(entry)
    row["status"] = status
    row["reasons"] = list(reasons)
    return row


def precheck(entry: dict) -> List[str]:
    """Missing test files and fixtures fail a suite before it runs."""
    reasons = [f"missing file: {item['path']}" for item in entry["files"] if item["sha256"] is None]
    paths = [item["path"] for item in entry["fixtures"]]
    for pattern in entry["fixture_patterns"]:
        if not any(fnmatchcase(path, pattern) for path in paths):
            reasons.append(f"missing fixture: {pattern}")
    if entry["kind"] == "pytest" and not entry["files"]:
        reasons.append("no test files selected")
    return reasons


def _report_reasons(entry: dict, ex: Execution, row: dict) -> List[str]:
    summary = ex.junit
    counts = summary.counts()
    row["counts"] = counts
    row["skip_reasons"] = summary.skip_reasons()
    row["outcomes_sha256"] = summary.outcomes_sha256()
    reasons = []
    if counts["collected"] == 0 or (entry["kind"] == "pytest" and ex.exit_code == PYTEST_NO_TESTS):
        reasons.append("empty collection")
    if counts["failed"] or counts["error"]:
        reasons.append(f"{counts['failed']} failed, {counts['error']} errors")
    if counts["collected"] and counts["executed"] == 0:
        reasons.append("all tests skipped")
    if ex.exit_code != 0:
        reasons.append(f"nonzero exit code {ex.exit_code}")
    missing, not_passed = match_required(summary, entry["required_tests"])
    row["required_tests"].update(missing=missing, not_passed=not_passed)
    reasons += [f"required test missing: {test_id}" for test_id in missing]
    reasons += [f"required test did not pass: {test_id}" for test_id in not_passed]
    return reasons


def _reporting_status(entry: dict, ex: Execution, row: dict) -> Tuple[str, List[str]]:
    if ex.junit is None:
        if entry["kind"] == "pytest" and ex.exit_code in PYTEST_STARTUP_EXITS:
            return NOT_EXECUTED, [f"startup failure: exit {ex.exit_code} without a report"]
        return FAIL, [ex.junit_error or "no JUnit report", f"exit code {ex.exit_code}"]
    reasons = _report_reasons(entry, ex, row)
    return (FAIL if reasons else PASS), reasons


def evaluate_suite(entry: dict, ex: Execution) -> dict:
    """Decide one suite's status from what its runner observed."""
    row = _base_row(entry)
    row.update(command=list(ex.command), exit_code=ex.exit_code, junit=ex.junit_path,
               log=ex.log_path, detail=dict(ex.detail))
    if not ex.launched:
        status, reasons = NOT_EXECUTED, [f"startup failure: {ex.startup_error or 'not launched'}"]
    elif ex.timed_out:
        status, reasons = FAIL, ["timed out"]
    elif entry["kind"] in REPORTING_KINDS:
        status, reasons = _reporting_status(entry, ex, row)
    else:
        reasons = list(ex.checks)
        if ex.exit_code != 0:
            reasons.insert(0, f"nonzero exit code {ex.exit_code}")
        status = FAIL if reasons else PASS
    row["status"] = status
    row["reasons"] = reasons
    return row


def _rollup(statuses: Iterable[str]) -> str:
    statuses = list(statuses)
    if FAIL in statuses:
        return FAIL
    if statuses and all(status == PASS for status in statuses):
        return PASS
    return INCOMPLETE


def rollup_capabilities(capabilities: Sequence, rows: Sequence[dict]) -> list:
    """A capability passes only when its required suites (or, for an optional
    capability, every suite that ran) passed and none failed."""
    out = []
    for cap in capabilities:
        mine = [row for row in rows if row["capability"] == cap.id]
        considered = [row for row in mine if row["required"]] or \
            [row for row in mine if row["status"] in (PASS, FAIL)]
        status = FAIL if any(row["status"] == FAIL for row in mine) else _rollup(
            row["status"] for row in considered)
        out.append({
            "id": cap.id, "issue": cap.issue, "required": cap.required, "summary": cap.summary,
            "status": status, "suites": {row["id"]: row["status"] for row in mine},
        })
    return out


def compute_gate(rows: Sequence[dict], capabilities: Sequence[dict],
                 source_reasons: Sequence[str]) -> dict:
    required = [row for row in rows if row["required"]]
    core = _rollup(row["status"] for row in required if row["mode"] == "core")
    failing = sorted(row["id"] for row in rows if row["status"] == FAIL)
    failing_caps = sorted(cap["id"] for cap in capabilities if cap["status"] == FAIL)
    incomplete = sorted(row["id"] for row in required if row["status"] in (NOT_EXECUTED, BLOCKED))
    incomplete_caps = sorted(cap["id"] for cap in capabilities
                             if cap["required"] and cap["status"] == INCOMPLETE)
    if source_reasons:
        core = release = FAIL
    elif core == FAIL or failing or failing_caps:
        release = FAIL
    elif core != PASS or incomplete or incomplete_caps:
        release = INCOMPLETE
    else:
        release = PASS
    return {
        "core": core, "release": release, "source_reasons": list(source_reasons),
        "failing_suites": failing, "failing_capabilities": failing_caps,
        "incomplete_required_suites": incomplete, "incomplete_required_capabilities": incomplete_caps,
        "optional_not_executed": sorted(row["id"] for row in rows if not row["required"]
                                        and row["status"] in (NOT_EXECUTED, BLOCKED)),
    }


def audit_row(row: dict) -> List[str]:
    """Invariants a PASS row must satisfy; used to re-check stored evidence."""
    if row["status"] != PASS:
        return []
    problems = []
    if row["reasons"]:
        problems.append("PASS row carries failure reasons")
    if row["exit_code"] != 0:
        problems.append("PASS row has a nonzero or missing exit code")
    if row["kind"] in REPORTING_KINDS:
        counts = row["counts"] or {}
        if not counts.get("executed") or counts.get("failed") or counts.get("error"):
            problems.append("PASS row without executed, all-green tests")
        if row["required_tests"]["missing"] or row["required_tests"]["not_passed"]:
            problems.append("PASS row with unmet required tests")
    return [f"{row['id']}: {problem}" for problem in problems]
