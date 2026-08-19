"""Small reproducible PRNG for fuzz generation without ambient entropy APIs."""

from __future__ import annotations

from typing import MutableSequence, Sequence, Tuple, TypeVar

_T = TypeVar("_T")
_MASK_64 = (1 << 64) - 1


class DeterministicRng:
    """SplitMix64 with only the sequence helpers this harness needs.

    The algorithm and integer-to-float conversion are fixed here so generated
    corpora do not inherit implementation changes from Python's ``random``
    module.  No operating-system entropy is sampled.
    """

    def __init__(self, seed: int) -> None:
        self._state = seed & _MASK_64

    def _next(self) -> int:
        self._state = (self._state + 0x9E3779B97F4A7C15) & _MASK_64
        value = self._state
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK_64
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK_64
        return value ^ (value >> 31)

    def randbelow(self, stop: int) -> int:
        if stop <= 0:
            raise ValueError("stop must be positive")
        # Rejection avoids modulo bias while preserving a fixed 64-bit stream.
        limit = (1 << 64) - ((1 << 64) % stop)
        while True:
            value = self._next()
            if value < limit:
                return value % stop

    def randint(self, start: int, stop: int) -> int:
        if stop < start:
            raise ValueError("stop must be at least start")
        return start + self.randbelow(stop - start + 1)

    def choice(self, values: Sequence[_T]) -> _T:
        if not values:
            raise IndexError("cannot choose from an empty sequence")
        return values[self.randbelow(len(values))]

    def sample(self, values: Sequence[_T], count: int) -> Tuple[_T, ...]:
        if count < 0 or count > len(values):
            raise ValueError("sample larger than population or negative")
        pool = list(values)
        for position in range(count):
            selected = position + self.randbelow(len(pool) - position)
            pool[position], pool[selected] = pool[selected], pool[position]
        return tuple(pool[:count])

    def shuffle(self, values: MutableSequence[_T]) -> None:
        for position in range(len(values) - 1, 0, -1):
            selected = self.randbelow(position + 1)
            values[position], values[selected] = values[selected], values[position]

    def uniform(self, start: float, stop: float) -> float:
        unit = self._next() / float(1 << 64)
        return start + (stop - start) * unit


__all__ = ["DeterministicRng"]
