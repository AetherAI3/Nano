"""Core-only canonical JSON and digests for quantum operation proposals.

This reproduces Predator-M ``predator_gen3/canonical.py`` without importing it:
sorted keys, compact separators, UTF-8 (``ensure_ascii=False``), no NaN or
infinity, no non-string keys. A digest is ``sha256:<64 lowercase hex>`` over
the UTF-8 bytes of the canonical text. Both sides must agree byte for byte or
no receipt seal verifies; ``tests/qsearch_fixtures/canonical_golden.json`` is
the frozen proof that they do. Enum and dataclass values (which Predator's
encoder unwraps) are deliberately unsupported here: every value Nano
canonicalises is parsed JSON or a dict Nano built itself, so they fail closed.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

_HEX = frozenset("0123456789abcdef")


class CanonicalizationError(ValueError):
    """The value cannot be represented by the canonical JSON contract."""


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("canonical mappings require string keys")
            out[key] = _json_value(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("NaN and infinity are not canonical JSON")
        return value
    raise CanonicalizationError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Sorted, compact, UTF-8-safe JSON text for ``value``."""
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def canonical_digest(value: Any) -> str:
    """``sha256:<hex>`` over the canonical UTF-8 bytes of ``value``."""
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_digest(value: Any, *, field: str = "digest") -> str:
    """Return ``value`` if it is a well-formed ``sha256:<64 lowercase hex>``."""
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError(f"{field} must use the sha256:<hex> form")
    hexadecimal = value[len("sha256:"):]
    if len(hexadecimal) != 64 or any(c not in _HEX for c in hexadecimal):
        raise ValueError(f"{field} must contain 64 lowercase hexadecimal characters")
    return value


__all__ = ["CanonicalizationError", "canonical_digest", "canonical_json", "validate_digest"]
