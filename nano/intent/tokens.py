"""Token and source-position types for the sibling intent frontend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Tuple

MAX_INPUT_CHARS = 4096
MAX_TOKENS = 256


class IntentInputError(ValueError):
    """Input cannot be processed safely or deterministically."""


@dataclass(frozen=True, order=True)
class SourceSpan:
    """Half-open character offsets into normalized input."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if type(self.start) is not int or type(self.end) is not int:
            raise TypeError("source span offsets must be integers")
        if self.start < 0 or self.end < self.start:
            raise ValueError("source span must be a non-negative half-open range")

    def to_list(self) -> list[int]:
        return [self.start, self.end]


@dataclass(frozen=True)
class Token:
    """One bounded lexical unit, with catalog roles kept separate from kind."""

    kind: str
    text: str
    span: SourceSpan
    roles: Tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "text": self.text,
            "span": self.span.to_list(),
        }
        if self.roles:
            result["roles"] = list(self.roles)
        return result


__all__ = [
    "IntentInputError",
    "MAX_INPUT_CHARS",
    "MAX_TOKENS",
    "SourceSpan",
    "Token",
]
