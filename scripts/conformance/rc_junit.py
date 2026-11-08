"""Parse JUnit XML (pytest ``--junitxml`` and Node ``--test-reporter=junit``).

Only stable facts are kept: a test identity, its outcome and, for skips and
expected failures, the declared reason. Durations, timestamps, hostnames and
failure tracebacks are dropped, so the summary can enter the canonical hash.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Mapping, Optional, Sequence, Tuple

from rc_common import canonical_sha256, redact

OUTCOMES = ("passed", "failed", "error", "skipped", "xfailed")
COLLECTION_SKIP = re.compile(r"Skipped: (.*?)['\"]\)\s*$", re.DOTALL)


@dataclass(frozen=True)
class Case:
    classname: str
    name: str
    outcome: str
    reason: str = ""

    @property
    def ident(self) -> str:
        return f"{self.classname}::{self.name}" if self.classname else self.name


@dataclass(frozen=True)
class JUnitSummary:
    cases: Tuple[Case, ...]

    def counts(self) -> dict:
        tally = Counter(case.outcome for case in self.cases)
        counts = {outcome: tally.get(outcome, 0) for outcome in OUTCOMES}
        counts["collected"] = len(self.cases)
        counts["executed"] = len(self.cases) - counts["skipped"]
        return counts

    def skip_reasons(self) -> dict:
        tally = Counter(case.reason for case in self.cases if case.outcome == "skipped")
        return dict(sorted(tally.items()))

    def outcomes_sha256(self) -> str:
        rows = sorted(f"{case.ident}\t{case.outcome}\t{case.reason}" for case in self.cases)
        return canonical_sha256(rows)


class JUnitError(ValueError):
    """The report is missing or is not a JUnit document."""


def _pure(path: str) -> PurePath:
    """Parse a path by its own spelling, not the host OS, so reports and tests
    produced on Windows relativize identically on Linux and vice versa."""
    if "\\" in path or (len(path) > 1 and path[1] == ":"):
        return PureWindowsPath(path)
    return PurePosixPath(path)


def _classname(element: ET.Element, root: Optional[str]) -> str:
    file_attr = element.get("file")
    if file_attr and root is not None:
        try:
            return _pure(file_attr).relative_to(_pure(root)).as_posix()
        except ValueError:
            return _pure(file_attr).name
    return element.get("classname", "")


def _outcome(element: ET.Element) -> Tuple[str, str]:
    children = {child.tag: child for child in element}
    if "error" in children:
        return "error", ""
    if "failure" in children:
        return "failed", ""
    skipped = children.get("skipped")
    if skipped is not None:
        kind = "xfailed" if skipped.get("type") == "pytest.xfail" else "skipped"
        return kind, _skip_reason(skipped)
    return "passed", ""


def _skip_reason(skipped: ET.Element) -> str:
    """The declared reason; a module-level skip carries it only in the text."""
    message = skipped.get("message", "")
    if message == "collection skipped":
        found = COLLECTION_SKIP.search(skipped.text or "")
        if found:
            return f"collection skipped: {found.group(1)}"
    return message


def parse_junit(data: bytes, root: Optional[str] = None,
                redactions: Mapping[str, str] = ()) -> JUnitSummary:
    """Return the stable summary of one JUnit document.

    ``root`` relativizes Node's absolute ``file`` attributes; ``redactions``
    maps absolute paths inside skip reasons to placeholders.
    """
    try:
        tree = ET.fromstring(data)
    except ET.ParseError as exc:
        raise JUnitError(f"unparseable JUnit report: {exc}") from exc
    if tree.tag not in ("testsuites", "testsuite"):
        raise JUnitError(f"not a JUnit report (root element {tree.tag!r})")
    mapping = dict(redactions)
    cases = []
    for element in tree.iter("testcase"):
        outcome, reason = _outcome(element)
        cases.append(Case(_classname(element, root), element.get("name", ""),
                          outcome, redact(reason, mapping)))
    return JUnitSummary(tuple(cases))


def _module_and_function(test_id: str) -> Tuple[str, str]:
    path, _, function = test_id.partition("::")
    module = path[:-3] if path.endswith(".py") else path
    return module.replace("/", "."), function


def _matches(case: Case, module: str, function: str) -> bool:
    if case.classname != module and not case.classname.startswith(module + "."):
        return False
    if not function:  # a file-level requirement: every test in the module
        return True
    return case.name == function or case.name.startswith(function + "[")


def match_required(summary: JUnitSummary, required: Sequence[str]) -> Tuple[list, list]:
    """Return (missing, not_passed) for required ``path::function`` or ``path`` IDs.

    Every parametrization of a required function must pass; a required test
    that was skipped, failed, errored or xfailed is ``not_passed``.
    """
    missing, not_passed = [], []
    for test_id in required:
        module, function = _module_and_function(test_id)
        hits = [case for case in summary.cases if _matches(case, module, function)]
        if not hits:
            missing.append(test_id)
        elif any(case.outcome != "passed" for case in hits):
            not_passed.append(test_id)
    return missing, not_passed
