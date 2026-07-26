"""Nano's type vocabulary.

Types are immutable value objects with a canonical textual form: `int`,
`float`, `series<float>`. That rendering is the single spelling used in
diagnostics, IR (`inputs[].type`), editor hovers, and `.nano` source
annotations, so a type never reads differently depending on who printed it.

Two rules carry most of the weight:

* **Integer literals widen, nothing else does.** `RSI < 30` must typecheck with
  `RSI: float` and `30: int` — a language that rejected it would be unusable.
  Nothing else coerces: `float` never narrows to `int`, `bool` is not a number,
  and `string` converts to nothing.
* **Series lift pointwise.** Comparing `series<float>` to `float` yields
  `series<bool>` — the comparison happens at every bar, not once. A condition
  position accepts `bool` or `series<bool>` and samples the latter at the
  current bar, which is what makes `if ema20 > price { ... }` mean what a
  strategy author expects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Type:
    """A Nano type. `element` is set only for `series<T>`."""

    name: str
    element: Optional["Type"] = None

    def __str__(self) -> str:
        if self.element is not None:
            return f"{self.name}<{self.element}>"
        return self.name

    # -- shape predicates --------------------------------------------------

    @property
    def is_series(self) -> bool:
        return self.name == "series"

    @property
    def is_numeric(self) -> bool:
        return self.name in _NUMERIC_NAMES

    @property
    def scalar(self) -> "Type":
        """This type with any series wrapper removed (`series<float>` -> `float`)."""
        return self.element if self.element is not None else self


INT = Type("int")
FLOAT = Type("float")
BOOL = Type("bool")
STRING = Type("string")
DURATION = Type("duration")
# A float in [0, 1] carrying provenance. Distinct from `float` so a raw
# measurement can never be passed where a calibrated confidence is required.
CONFIDENCE = Type("confidence")
VOID = Type("void")

_NUMERIC_NAMES = frozenset({"int", "float", "confidence"})

SCALAR_TYPES = {t.name: t for t in (INT, FLOAT, BOOL, STRING, DURATION, CONFIDENCE)}


def series(element: Type) -> Type:
    """`series<element>` — a time-indexed, look-ahead-safe sequence."""
    if element.is_series:
        raise ValueError("series<series<T>> is not a Nano type")
    return Type("series", element)


SERIES_FLOAT = series(FLOAT)
SERIES_BOOL = series(BOOL)


def record(signature_name: str) -> Type:
    """`record<Sentiment>` — the result of one `infer` call against a signature.

    The signature name rides in `element` so the type prints readably and the
    checker can look the declaration back up to type a `.field` access. A record
    is deliberately not a struct literal: the only way to make one is to call a
    model through a declared signature, so an untyped bag of model output has no
    way into the language.
    """
    return Type("record", Type(signature_name))


def lift(element: Type, *, as_series: bool) -> Type:
    """Wrap `element` in a series when the surrounding expression is one."""
    return series(element) if as_series else element


def is_assignable(source: Type, target: Type) -> bool:
    """Can a value of `source` be used where `target` is required?

    Identity, integer widening, and confidence-as-float. Series are assignable
    only to series whose elements are assignable — a bare `float` never
    satisfies a `series<float>` parameter, because the callee needs history.
    """
    if source == target:
        return True
    if source.is_series or target.is_series:
        if not (source.is_series and target.is_series):
            return False
        return is_assignable(source.element, target.element)
    if source == INT and target in (FLOAT, CONFIDENCE):
        return True
    if source == CONFIDENCE and target == FLOAT:
        return True
    return False


def unify_numeric(left: Type, right: Type) -> Optional[Type]:
    """Result type of an arithmetic operation, or None if the operands don't fit.

    `int op int` stays `int` so integer params keep exact arithmetic; any float
    or confidence operand widens the result to `float`. Series-ness propagates:
    if either side is a series, so is the result.
    """
    as_series = left.is_series or right.is_series
    lhs, rhs = left.scalar, right.scalar
    if not (lhs.is_numeric and rhs.is_numeric):
        return None
    if lhs == INT and rhs == INT:
        return lift(INT, as_series=as_series)
    return lift(FLOAT, as_series=as_series)


def unify_comparison(left: Type, right: Type) -> Optional[Type]:
    """Result type of a comparison, or None if the operands aren't comparable.

    Numerics compare with numerics; otherwise the scalar types must match
    exactly (`string == string`, `bool != bool`). Series-ness propagates, so a
    comparison against history is itself history.
    """
    as_series = left.is_series or right.is_series
    lhs, rhs = left.scalar, right.scalar
    comparable = (lhs.is_numeric and rhs.is_numeric) or lhs == rhs
    return lift(BOOL, as_series=as_series) if comparable else None


def parse_type(text: str) -> Optional[Type]:
    """Parse a canonical type spelling. Returns None for anything unknown."""
    text = text.strip()
    if text.startswith("series<") and text.endswith(">"):
        element = parse_type(text[len("series<") : -1])
        if element is None or element.is_series:
            return None
        return series(element)
    return SCALAR_TYPES.get(text)


def type_names() -> Tuple[str, ...]:
    """Every spelling a type annotation may use, for diagnostics and completions."""
    return tuple(sorted(SCALAR_TYPES)) + (
        "series<bool>",
        "series<float>",
        "series<int>",
    )
