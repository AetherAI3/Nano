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

# Win32 FILE_ATTRIBUTE_REPARSE_POINT (symlinks and junctions). A literal, not
# getattr(stat, ...): the determinism guards forbid string access to modules.
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

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
    attributes = getattr(info, "st_file_attributes", 0)  # Windows-only stat field
    return stat.S_ISLNK(info.st_mode) or bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


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


class DestinationChanged(OSError):
    """The destination stopped being a plain directory between check and write."""


def _write_all(stream, data: bytes) -> None:
    stream.write(data)


def _write_new_file(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively and remove it again if writing fails."""
    with open(path, "xb") as stream:
        try:
            _write_all(stream, data)
        except BaseException:
            stream.close()
            path.unlink(missing_ok=True)
            raise


def _create_new(target: Path) -> Optional[str]:
    """Write into a temporary sibling, then rename it into place.

    Returns the staging path if cleanup after a failure could not remove it.
    """
    staging = tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".init", dir=target.parent)
    try:
        for name, data in template():
            _write_new_file(Path(staging) / name, data)
        os.rename(staging, target)
    except BaseException as error:
        shutil.rmtree(staging, ignore_errors=True)
        if os.path.lexists(staging):
            error.nano_leftover = staging  # type: ignore[attr-defined]
        raise
    return None


def _still_plain_directory(target: Path) -> None:
    """Narrow the check-to-write race: refuse if the target became a link.

    Not an OS sandbox (Windows offers no no-follow directory handle): a local
    process able to rename entries in the parent could still race this.
    """
    if _is_link(target) or not target.is_dir():
        raise DestinationChanged(f"{target} changed while writing")


def _fill_empty(target: Path) -> None:
    """Create each file exclusively; remove what was created if one fails."""
    created = []
    try:
        for name, data in template():
            _still_plain_directory(target)
            path = target / name
            _write_new_file(path, data)
            created.append(path)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise


def _place(target: Path) -> Optional[str]:
    """Refusal reason, or None after writing the scaffold. Raises OSError."""
    reason = _refusal(target)
    if reason is not None:
        return reason
    if os.path.lexists(target):
        _fill_empty(target)
    else:
        _create_new(target)
    return None


def command_init(args: argparse.Namespace, console: Console) -> int:
    """Create the example package in a new or empty ``--directory``."""
    target = Path(args.directory)
    try:
        reason = _place(target)
    except OSError as error:
        detail = str(error) if isinstance(error, DestinationChanged) else (error.strerror or error)
        console.warn(f"error: cannot create {target}: {detail}")
        leftover = getattr(error, "nano_leftover", None)
        if leftover:
            console.warn(f"warning: could not remove temporary directory {leftover}")
        return EXIT_IO
    if reason is not None:
        console.warn(f"error: {target} {reason}; init only writes into a new or empty directory")
        return EXIT_USAGE
    console.warn(f"created {target} ({len(template())} files)")
    return EXIT_OK
