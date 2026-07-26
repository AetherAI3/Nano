"""Symbol table for one compilation.

A symbol is a name the checker resolved, plus everything later stages need to
know about it: its type, where it came from, whether it is a compile-time
constant, and how many bars of history it consumes.

`kind` is not decoration. It decides what the name is *allowed* to do:

| kind | Introduced by | Notes |
|---|---|---|
| `param` | `param fast: int = 20` | constant — may appear as an indicator period |
| `input` | `input price: series<float>` | supplied by the host feed |
| `let` | `let ema20 = EMA(price, 20)` | derived; carries a warm-up length |
| `feed` | an undeclared name, e.g. `RSI` in `RSI < 30` | the v0.1.0 signal form |
| `builtin` | the language itself, e.g. `confidence` | read-only |
| `signature` | `signature Sentiment { … }` | callable only through `infer` |
| `agent` | `agent Research` | an escalation target |
| `route` | `route Decision { … }` | a confidence-routing entry point |

**Declaring inputs turns on strict name resolution.** With no `input`
declarations a strategy is in v0.1.0 territory, where any unknown name is a
host-supplied feed signal — that is what makes `if RSI(14) < 30` work with no
preamble, and it must keep working. The cost is that a typo becomes a signal
that never arrives. So the moment a strategy declares even one `input`, it has
told the compiler what its data is, and unknown names become errors. Strictness
is opt-in by being explicit, which is the trade a typed language should offer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Mapping, Optional, Tuple

from ..compiler.ast import Literal
from .kinds import Type

KIND_PARAM = "param"
KIND_INPUT = "input"
KIND_LET = "let"
KIND_FEED = "feed"
KIND_BUILTIN = "builtin"
KIND_SIGNATURE = "signature"
KIND_AGENT = "agent"
KIND_ROUTE = "route"

# Kinds whose value is fixed before any data arrives, so they may be used where
# the compiler needs a number at compile time (indicator periods, risk limits).
CONSTANT_KINDS = frozenset({KIND_PARAM})


@dataclass(frozen=True)
class Symbol:
    name: str
    type: Type
    kind: str
    line: int = 0
    column: int = 0
    # Set for `param` declarations; lets an indicator period resolve statically.
    const_value: Optional[Literal] = None
    # Bars of history this name consumes before it produces a value.
    lookback: int = 0
    # For `signature` symbols: declared output fields, so `.field` can be typed.
    fields: Tuple[Tuple[str, Type], ...] = ()

    @property
    def is_constant(self) -> bool:
        return self.kind in CONSTANT_KINDS and self.const_value is not None

    def describe(self) -> str:
        """`param fast: int = 20` — the hover text for this symbol."""
        head = f"{self.kind} {self.name}: {self.type}"
        if self.const_value is not None:
            return f"{head} = {self.const_value!r}"
        return head


class Scope:
    """Flat name -> Symbol table.

    Flat because Nano has no nested lexical scopes: a strategy's declarations
    are all visible to each other and to every schedule block, and nothing
    shadows anything. Redeclaration is an error rather than a shadow, so this
    stays one namespace and lookups need no chain walk.
    """

    def __init__(self, *, strict: bool = False) -> None:
        self._symbols: Dict[str, Symbol] = {}
        self.strict = strict

    # -- reads -------------------------------------------------------------

    def get(self, name: str) -> Optional[Symbol]:
        return self._symbols.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._symbols

    def names(self) -> Tuple[str, ...]:
        return tuple(self._symbols)

    def of_kind(self, kind: str) -> Tuple[Symbol, ...]:
        """Symbols of one kind, in declaration order."""
        return tuple(s for s in self._symbols.values() if s.kind == kind)

    def snapshot(self) -> Mapping[str, Symbol]:
        return dict(self._symbols)

    # -- writes ------------------------------------------------------------

    def declare(self, symbol: Symbol) -> Optional[Symbol]:
        """Bind `symbol`. Returns the existing symbol on collision, else None.

        The caller raises, because only it knows which position to blame — the
        scope has no opinion about diagnostics.
        """
        existing = self._symbols.get(symbol.name)
        if existing is not None:
            return existing
        self._symbols[symbol.name] = symbol
        return None

    def declare_feed(self, name: str, type_: Type, *, line: int, column: int) -> Symbol:
        """Record an undeclared name as a host-supplied feed signal.

        Idempotent: the same signal referenced from three conditions is one
        symbol, attributed to its first appearance.
        """
        existing = self._symbols.get(name)
        if existing is not None:
            return existing
        symbol = Symbol(name=name, type=type_, kind=KIND_FEED, line=line, column=column)
        self._symbols[name] = symbol
        return symbol

    def widen_lookback(self, name: str, lookback: int) -> None:
        """Raise a symbol's recorded history requirement to at least `lookback`.

        A feed signal read as `RSI[3]` needs three more bars than one read as
        `RSI`; warm-up is the maximum over every use, never the last one seen.
        """
        symbol = self._symbols.get(name)
        if symbol is not None and lookback > symbol.lookback:
            self._symbols[name] = replace(symbol, lookback=lookback)
