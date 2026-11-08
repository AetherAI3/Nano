"""Source identity: commit/tree IDs, cleanliness, and exact-byte exports.

Exports use ``git archive`` with ``core.autocrlf=false`` so the extracted
bytes are the committed blobs on every platform, independent of the
checkout's line-ending configuration.
"""
from __future__ import annotations

import io
import re
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from rc_common import HarnessError

HEX40 = re.compile(r"^[0-9a-f]{40}$")
GIT_TIMEOUT_S = 120


@dataclass(frozen=True)
class SourceIdentity:
    commit: str
    tree: str
    commit_time: int


def _git(repo: Path, *args: str, binary: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              timeout=GIT_TIMEOUT_S, text=not binary)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"git {args[0]} could not run: {exc}") from exc


def _git_text(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    if result.returncode != 0:
        raise HarnessError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def is_full_hex(value: object) -> bool:
    return isinstance(value, str) and bool(HEX40.match(value))


def head_identity(repo: Path) -> SourceIdentity:
    commit = _git_text(repo, "rev-parse", "--verify", "HEAD^{commit}")
    tree = commit_tree(repo, commit)
    when = int(_git_text(repo, "log", "-1", "--format=%ct", commit))
    return SourceIdentity(commit, tree, when)


def commit_tree(repo: Path, commit: str) -> Optional[str]:
    """Tree of ``commit``, or None when the object is unknown locally."""
    if not is_full_hex(commit):
        return None
    result = _git(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}")
    return result.stdout.strip() if result.returncode == 0 else None


def dirty_paths(repo: Path) -> List[str]:
    """Tracked modifications and untracked, non-ignored files."""
    out = _git_text(repo, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none")
    return sorted(line[3:] for line in out.splitlines() if line.strip())


def toplevel(repo: Path) -> Path:
    return Path(_git_text(repo, "rev-parse", "--show-toplevel")).resolve()


def checkout_commit(repo: Path, commit: str, dest: Path) -> None:
    """Detached checkout of ``commit`` in a new shared clone at ``dest``.

    Some tests need a git checkout (they resolve ``HEAD``), so the test tree is
    a clone rather than an archive. It shares objects read-only, keeps LF
    blob bytes (``core.autocrlf=false``) and has no remote, so nothing in it
    can fetch from or write back to the tested repository.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    clone = ["-c", "core.autocrlf=false", "clone", "-q", "--shared", "--no-checkout", str(repo), str(dest)]
    _git_text(dest.parent, *clone)
    _git_text(dest, "config", "core.autocrlf", "false")
    _git_text(dest, "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", commit)
    _git_text(dest, "remote", "remove", "origin")
    if _git_text(dest, "rev-parse", "HEAD") != commit:
        raise HarnessError("the test checkout is not at the tested commit")


def export_commit(repo: Path, commit: str, dest: Path) -> None:
    """Extract the committed bytes of ``commit`` into the new directory ``dest``."""
    result = _git(repo, "-c", "core.autocrlf=false", "-c", "core.eol=lf",
                  "archive", "--format=tar", commit, binary=True)
    if result.returncode != 0:
        raise HarnessError(f"git archive failed: {result.stderr.decode(errors='replace').strip()}")
    dest.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        if hasattr(tarfile, "data_filter"):
            archive.extractall(dest, filter="data")
        else:  # pragma: no cover - interpreters without PEP 706
            archive.extractall(dest)
