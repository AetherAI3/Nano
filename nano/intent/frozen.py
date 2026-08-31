"""Small immutable JSON containers used by the Intent AST and IR.

``dataclass(frozen=True)`` only protects attributes; a normal ``dict`` stored in
one can still be changed by a caller.  Intent plans cross a host trust boundary,
so mappings are copied into this deliberately tiny read-only implementation and
arrays become tuples.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from typing import Any

MAX_JSON_INTEGER_DIGITS = 640
_MAX_JSON_INTEGER = (10**MAX_JSON_INTEGER_DIGITS) - 1


class FrozenMap(Mapping[str, Any]):
    """A deterministic, hashable, read-only mapping with string keys."""

    __slots__ = ("_items", "_lookup")

    def __init__(self, values: Mapping[str, Any] | None = None, /, **kwargs: Any) -> None:
        source = dict(values or {})
        source.update(kwargs)
        if any(type(key) is not str for key in source):
            raise TypeError("FrozenMap keys must be strings")
        self._items = tuple(
            (key, freeze_json(value)) for key, value in sorted(source.items())
        )
        self._lookup = dict(self._items)

    def __getitem__(self, key: str) -> Any:
        return self._lookup[key]

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        return hash(self._items)

    def __repr__(self) -> str:
        body = ", ".join(f"{key!r}: {value!r}" for key, value in self._items)
        return f"FrozenMap({{{body}}})"


def freeze_json(value: Any) -> Any:
    """Copy a JSON-shaped value into exact immutable built-in/FrozenMap types."""

    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TypeError("strings must contain valid Unicode") from exc
        return value
    if type(value) is int and not -_MAX_JSON_INTEGER <= value <= _MAX_JSON_INTEGER:
        raise TypeError(
            f"integer exceeds the {MAX_JSON_INTEGER_DIGITS}-digit canonical limit"
        )
    if type(value) is float and not math.isfinite(value):
        raise TypeError("non-finite floats are not immutable JSON values")
    if value is None or type(value) in (int, float, bool):
        return value
    if isinstance(value, Mapping):
        return FrozenMap(value)
    if type(value) in (list, tuple):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"{type(value).__name__} is not an immutable JSON value")


def thaw_json(value: Any) -> Any:
    """Return a detached ordinary dict/list tree suitable for JSON encoding."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if type(value) in (list, tuple):
        return [thaw_json(item) for item in value]
    return value


__all__ = [
    "FrozenMap",
    "MAX_JSON_INTEGER_DIGITS",
    "freeze_json",
    "thaw_json",
]
