"""Strict proposal-byte decoding used ahead of Nano admission. Imports nothing from Nano.

Nano's public quantum admission (`validate_quantum_document`) accepts an already
parsed mapping, so byte-level decoding is the host's job. The benchmark host
rejects duplicate keys and nonfinite numbers instead of silently choosing one.
"""
from __future__ import annotations

import json
from typing import Any


class DecodeError(ValueError):
    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(f"{category}: {message}")


def decode_text(payload: bytes) -> str:
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DecodeError("encoding", "payload is not valid UTF-8") from exc


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DecodeError("json_duplicate_key", f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _nonfinite(token: str) -> Any:
    raise DecodeError("json_nonfinite", f"nonfinite JSON number {token}")


def strict_json(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_unique, parse_constant=_nonfinite)
    except DecodeError:
        raise
    except json.JSONDecodeError as exc:
        raise DecodeError("json_syntax", f"{exc.msg} at offset {exc.pos}") from exc


def lenient_json(text: str) -> Any:
    """Python's default decoder: last duplicate wins, NaN/Infinity accepted."""
    return json.loads(text)
