"""Shared constants and canonical helpers for the release-conformance harness.

Stdlib only. Canonical JSON is the repository's usual form: sorted keys,
compact separators, ASCII, one trailing newline.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

MANIFEST_SCHEMA = "nano.release-conformance.manifest.v1"
EVIDENCE_SCHEMA = "nano.release-conformance.evidence.v1"
RUN_LOG_AUTHORITY = "https://github.com/AetherAI3/Nano/issues/10"

PASS = "PASS"
FAIL = "FAIL"
NOT_EXECUTED = "NOT_EXECUTED"
BLOCKED = "BLOCKED"
INCOMPLETE = "INCOMPLETE"

SUITE_STATUSES = (PASS, FAIL, NOT_EXECUTED, BLOCKED)
ROLLUP_STATUSES = (PASS, FAIL, INCOMPLETE)

MODES = ("core", "qiskit", "cross_repo")


class HarnessError(Exception):
    """A usage or environment problem that prevents the harness from starting."""


def canonical_bytes(value: Any) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return (text + "\n").encode("ascii")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def redact(text: str, mapping: Mapping[str, str]) -> str:
    """Replace absolute paths with stable placeholders, longest path first.

    Both separator spellings, and the JSON-escaped backslash spelling, are
    replaced so a Windows path printed with forward slashes or embedded in a
    JSON argument is redacted as well.
    """
    pairs = set()
    for path, token in mapping.items():
        if not path:
            continue
        native = path.replace("/", "\\")
        for spelling in (path, path.replace("\\", "/"), native, native.replace("\\", "\\\\")):
            pairs.add((spelling, token))
    for path, token in sorted(pairs, key=lambda item: (-len(item[0]), item[0])):
        text = text.replace(path, token)
    return text


def redact_argv(argv: Iterable[str], mapping: Mapping[str, str]) -> list:
    return [redact(str(arg), mapping) for arg in argv]
