"""Core wheel: build from an exact-commit export, then inspect its members.

The build is offline (``--no-index --no-build-isolation --no-deps``), ignores
user pip configuration and pins zip timestamps to the commit time through
``SOURCE_DATE_EPOCH`` so the wheel bytes are reproducible.
"""
from __future__ import annotations

import hashlib
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

from rc_checks import NATIVE_SCHEMAS
from rc_common import canonical_sha256, sha256_file
from rc_evaluate import Execution
from rc_runner import Context, Proc, probe_interpreter, run_checked

BUILDER_NAMES = ("pip", "setuptools", "wheel")
ASSET_ROOTS = ("nano/examples", "nano/library", "nano/project", "nano/quantum", "nano/intent")
ASSET_SUFFIXES = (".nano", ".json")
CODE_ROOTS = ("nano", "nano_qsearch")


@dataclass(frozen=True)
class Builder:
    python: str
    role: str
    versions: dict

    def manifest(self) -> dict:
        return {"role": self.role, **self.versions}


def _builder_versions(probe: dict) -> dict:
    return {"python": probe["version"], **{name: probe["deps"].get(name) for name in BUILDER_NAMES}}


def select_builder(candidates: Sequence[Tuple[str, str]], cwd: Path,
                   env: Mapping[str, str]) -> Optional[Builder]:
    """First candidate ``(python, role)`` that has setuptools, else None."""
    for python, role in candidates:
        try:
            probe = probe_interpreter(python, cwd, env, BUILDER_NAMES)
        except OSError:
            continue
        if probe["deps"].get("setuptools"):
            return Builder(python, role, _builder_versions(probe))
    return None


def build_wheel(ctx: Context, suite_id: str, builder: Builder, source: Path, dest: Path,
                commit_time: int) -> Tuple[Execution, Optional[Path]]:
    dest.mkdir(parents=True, exist_ok=True)
    argv = [builder.python, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index",
            "--no-cache-dir", "--wheel-dir", str(dest), str(source)]
    extra = {"SOURCE_DATE_EPOCH": str(commit_time), "PIP_CONFIG_FILE": os.devnull}

    def judge(procs: Sequence[Proc]) -> List[str]:
        wheels = sorted(dest.glob("*.whl"))
        return [] if len(wheels) == 1 else [f"expected one wheel, found {len(wheels)}"]

    execution = run_checked(ctx, suite_id, [(argv, source)], judge, extra_env=extra)
    wheels = sorted(dest.glob("*.whl"))
    wheel = wheels[0] if execution.exit_code == 0 and len(wheels) == 1 else None
    detail = {"builder": builder.manifest()}
    if wheel is not None:
        detail.update(wheel=wheel.name, sha256=sha256_file(wheel))
    return Execution(execution.launched, execution.exit_code, execution.timed_out, execution.startup_error,
                     checks=execution.checks, command=execution.command, log_path=execution.log_path,
                     detail=detail), wheel


def expected_members(source: Path) -> List[str]:
    """Package files the exported source says an installed wheel must carry."""
    found = set()
    for root in ASSET_ROOTS:
        for path in (source / root).rglob("*"):
            if path.is_file() and path.suffix in ASSET_SUFFIXES:
                found.add(path.relative_to(source).as_posix())
    for root in CODE_ROOTS:
        for path in (source / root).rglob("*.py"):
            found.add(path.relative_to(source).as_posix())
    schemas = (source / "nano_qsearch" / "schemas").glob("*.json")
    found.update(path.relative_to(source).as_posix() for path in schemas)
    return sorted(found)


def members_digest(wheel: Path) -> Tuple[str, int, set]:
    rows = []
    with zipfile.ZipFile(wheel) as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            rows.append([info.filename, hashlib.sha256(archive.read(info)).hexdigest()])
    return canonical_sha256(rows), len(rows), {name for name, _ in rows}


def inspect_wheel(wheel: Path, source: Path) -> Execution:
    """In-process check that the wheel carries every documented asset and module."""
    try:
        digest, count, names = members_digest(wheel)
    except (OSError, zipfile.BadZipFile) as exc:
        return Execution(True, 0, checks=(f"unreadable wheel: {exc}",), command=("<zip inspection>",))
    expected = expected_members(source)
    problems = [f"wheel is missing {name}" for name in expected if name not in names]
    natives = [f"nano/quantum/schemas/{name}" for name in NATIVE_SCHEMAS]
    problems += [f"wheel is missing native bridge schema {name}" for name in natives if name not in names]
    if not any(name.startswith("nano/quantum/fixtures/native-") for name in names):
        problems.append("wheel carries no native bridge fixtures")
    if any(name.startswith(("tests/", "scripts/")) for name in names):
        problems.append("wheel ships repository tests or scripts")
    detail = {"wheel": wheel.name, "members_sha256": digest, "member_count": count,
              "expected_member_count": len(expected)}
    return Execution(True, 0, checks=tuple(problems), command=("<zip inspection>",), detail=detail)
