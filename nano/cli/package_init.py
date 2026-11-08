"""`nano package init --directory PATH`: one fixed, pre-pinned example package.

Contract: ``docs/package-cli.md`` (section ``init --directory PATH``). This is a
template writer, not a generator or a pin manager. It writes only into a
destination that does not exist or is an empty directory, refuses anything
else before writing, and never reads, discovers, repins or rewrites an
existing project. Lock pins are computed from the template bytes in memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from .commands import EXIT_IO, EXIT_OK, EXIT_USAGE, Console

RULES_SOURCE = b"""module Rules {
  pure fn ready(count: int, verified: bool): bool { count >= 2 and verified }
}
"""

AGENT_SOURCE = b"""import Rules
strategy Agent {
  input evidence_count: int
  input evidence_verified: bool
  every 1m {
    if ready(evidence_count, evidence_verified) { execute() }
    else { pause() }
  }
}
"""

MANIFEST = {
    "nanoPackageVersion": "0.1.0",
    "entry": "agent.nano",
    "modules": {"Rules": "rules.nano"},
    "effects": ["intent.emit", "log.append"],
}

CASES = {
    "nanoPackageCasesVersion": "1.0.0",
    "cases": [
        {
            "name": "verified evidence proposes execute",
            "frame": {"timestamps": [0], "signals": {"evidence_count": [2], "evidence_verified": [True]}},
            "expect": {"proposedIntents": [{"intent": "EXECUTE", "timestamp": 0}]},
        },
        {
            "name": "unverified evidence proposes pause",
            "frame": {"timestamps": [0], "signals": {"evidence_count": [2], "evidence_verified": [False]}},
            "expect": {"proposedIntents": [{"intent": "PAUSE", "timestamp": 0}]},
        },
    ],
}


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _pin(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def template() -> Tuple[Tuple[str, bytes], ...]:
    """The five scaffold files in write order, with pins from these exact bytes."""
    lock = {
        "nanoLockVersion": "0.1.0",
        "dependencies": {"agent.nano": _pin(AGENT_SOURCE), "rules.nano": _pin(RULES_SOURCE)},
    }
    return (
        ("rules.nano", RULES_SOURCE),
        ("agent.nano", AGENT_SOURCE),
        ("nano.json", _json_bytes(MANIFEST)),
        ("nano.lock", _json_bytes(lock)),
        ("cases.json", _json_bytes(CASES)),
    )


def _is_link(path: Path) -> bool:
    """A symlink, or (on Windows) any reparse point such as a junction."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse)


def _refusal(target: Path) -> Optional[str]:
    """Why ``target`` may not receive a scaffold, or None when it may."""
    if _is_link(target):
        return "is a symlink or junction"
    if not os.path.lexists(target):
        return None
    if not target.is_dir():
        return "exists and is not a directory"
    with os.scandir(target) as entries:
        if any(True for _ in entries):
            return "is not empty"
    return None


def _write_new_file(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively; an existing file is never overwritten."""
    with open(path, "xb") as stream:
        stream.write(data)


def _create_new(target: Path) -> None:
    """Write into a temporary sibling, then rename it into place."""
    staging = tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".init", dir=target.parent)
    try:
        for name, data in template():
            _write_new_file(Path(staging) / name, data)
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _fill_empty(target: Path) -> None:
    """Create each file exclusively; remove what was created if one fails."""
    created = []
    try:
        for name, data in template():
            path = target / name
            _write_new_file(path, data)
            created.append(path)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise


def command_init(args: argparse.Namespace, console: Console) -> int:
    """Create the example package in a new or empty ``--directory``."""
    target = Path(args.directory)
    reason = _refusal(target)
    if reason is not None:
        console.warn(f"error: {target} {reason}; init only writes into a new or empty directory")
        return EXIT_USAGE
    try:
        if os.path.lexists(target):
            _fill_empty(target)
        else:
            _create_new(target)
    except OSError as error:
        console.warn(f"error: cannot create {target}: {error.strerror or error}")
        return EXIT_IO
    console.warn(f"created {target} ({len(template())} files)")
    return EXIT_OK
