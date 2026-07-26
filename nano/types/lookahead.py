"""Look-ahead protection.

A backtest that reads one bar into the future looks brilliant and is worthless.
The bug is invisible in results — equity curves get *better*, not noisier — so
Nano refuses to represent it rather than trying to detect it after the fact.

Three properties combine to make future reads unreachable:

1. **Offsets only count backwards.** `price[k]` means "k bars ago". There is no
   forward-indexing syntax, so `close[t+1]` cannot mean next bar's close — it
   can only mean `t+1` bars *ago*.
2. **Offsets must fold to a non-negative integer at compile time.** `price[-1]`
   folds to `-1` and is rejected. `close[t+1]`, where `t` is not a compile-time
   constant, does not fold at all and is also rejected: an offset the compiler
   cannot bound is an offset that could point anywhere.
3. **Warm-up is never filled in.** A bar with insufficient history yields no
   value, and the VM will not emit an intent from it (see
   ``nano/indicators/compute.py``). Fabricating a warm-up value is look-ahead
   wearing a different hat — both invent data the strategy could not have had.

Rejecting non-constant offsets is stricter than it has to be. A dynamic offset
provably in `[0, n]` would be sound, but proving it needs range analysis, and
the failure mode of getting that wrong is a silently optimistic backtest. Until
there is a real use case, the strict rule costs a rewrite and buys a guarantee.
"""

from __future__ import annotations

from typing import Optional

from ..compiler.ast import Binary, Expr, Index, Name, NumberLit, Unary
from ..compiler.errors import LookaheadError, NanoTypeError
from .env import Scope

# Arithmetic the folder understands. Enough to write `slow - 1` or `period * 2`
# as an offset or period; deliberately not a general evaluator.
_INTEGER_OPS = frozenset({"+", "-", "*", "%"})


def _is_plain_int(value: object) -> bool:
    """True for a real integer. `bool` is an int in Python and is not one here."""
    return isinstance(value, int) and not isinstance(value, bool)


def fold_int(expr: Expr, scope: Scope) -> Optional[int]:
    """Evaluate `expr` to an integer at compile time, or None if it cannot be.

    Folds integer literals, `param` references with integer values, unary minus,
    and integer arithmetic over those. Anything touching runtime data — a feed
    signal, an input, a let-binding, a float — does not fold, by design.
    """
    if isinstance(expr, NumberLit):
        return expr.value if _is_plain_int(expr.value) else None

    if isinstance(expr, Name):
        symbol = scope.get(expr.name)
        if (
            symbol is not None
            and symbol.is_constant
            and _is_plain_int(symbol.const_value)
        ):
            return int(symbol.const_value)
        return None

    if isinstance(expr, Unary):
        inner = fold_int(expr.operand, scope)
        if inner is None:
            return None
        return -inner if expr.op == "-" else None

    if isinstance(expr, Binary) and expr.op in _INTEGER_OPS:
        left = fold_int(expr.left, scope)
        right = fold_int(expr.right, scope)
        if left is None or right is None:
            return None
        if expr.op == "+":
            return left + right
        if expr.op == "-":
            return left - right
        if expr.op == "*":
            return left * right
        return left % right if right != 0 else None

    return None


def resolve_offset(node: Index, scope: Scope) -> int:
    """Validate a series offset and return it.

    Raises ``LookaheadError`` when the offset is negative or not resolvable at
    compile time — the two ways a program could end up reading the future.
    """
    offset = fold_int(node.offset, scope)

    if offset is None:
        raise LookaheadError(
            "Series offset must be a compile-time non-negative integer "
            "(offsets count backwards, so an offset the compiler cannot bound "
            "could reach into the future)",
            node.offset.line,
            node.offset.column,
        )
    if offset < 0:
        raise LookaheadError(
            f"Series offset {offset} reads into the future "
            "(offsets count backwards from the current bar; the minimum is 0)",
            node.offset.line,
            node.offset.column,
        )
    return offset


def resolve_period(expr: Expr, scope: Scope, *, indicator: str, position: int) -> int:
    """Validate an indicator period and return it.

    Periods must be positive compile-time constants: warm-up length has to be
    known before any data arrives, and a zero or negative window has no
    meaning. Raises ``NanoTypeError`` — a bad period is a typing mistake, not an
    attempt to read the future.
    """
    period = fold_int(expr, scope)

    if period is None:
        raise NanoTypeError(
            f"Argument {position} of {indicator} is a period and must be a "
            "compile-time integer constant (a literal or a `param`), so warm-up "
            "length is known before any data arrives",
            expr.line,
            expr.column,
        )
    if period < 1:
        raise NanoTypeError(
            f"Argument {position} of {indicator} is a period and must be at "
            f"least 1, got {period}",
            expr.line,
            expr.column,
        )
    return period
