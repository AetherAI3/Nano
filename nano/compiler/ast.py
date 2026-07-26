"""The `.nano` abstract syntax tree (v1.0).

Every node is immutable and carries the 1-based line/column of its first token,
because the type checker reports positions the parser never sees again — an AST
node without a position is a diagnostic that has to guess, and Nano does not
guess (see ``nano/compiler/errors.py``).

The tree is a strict superset of the v0.1.0 shape. `StrategyAst.schedules` and
`ScheduleAst.rules` are tuples now that a strategy may declare several of each,
with `.schedule` / `.rule` kept as first-element accessors so v0.1-era callers
read unchanged. `ConditionAst` survives as the *legacy lowering view* — a
flattened `SIGNAL op NUMBER` triple recovered from an expression tree by
``nano/compiler/legacy.py`` — not as something the parser produces directly.

Base classes contribute `line`/`column` as their first fields, so every
construction site passes operands by keyword. That is deliberate: it keeps
argument order out of the correctness budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple, Union

Number = Union[int, float]
Literal = Union[int, float, str, bool]


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Expr:
    """Base for every expression. Subclasses add operands, never mutability."""

    line: int
    column: int


@dataclass(frozen=True)
class NumberLit(Expr):
    value: Number


@dataclass(frozen=True)
class StringLit(Expr):
    value: str


@dataclass(frozen=True)
class BoolLit(Expr):
    value: bool


@dataclass(frozen=True)
class DurationLit(Expr):
    """An interval literal used as a value, e.g. `5m`."""

    text: str


@dataclass(frozen=True)
class Name(Expr):
    """A reference to a param, input, let-binding, feed signal, or builtin."""

    name: str


@dataclass(frozen=True)
class Index(Expr):
    """`target[offset]` — read `target` as it stood `offset` bars ago.

    Offsets count *backwards*. There is no syntax for a forward offset, and a
    provably negative one is a compile error, which is how look-ahead becomes
    unrepresentable rather than merely discouraged.
    """

    target: Expr
    offset: Expr


@dataclass(frozen=True)
class Member(Expr):
    """`target.field` — reads one declared output of a signature result."""

    target: Expr
    field_name: str


@dataclass(frozen=True)
class Call(Expr):
    callee: str
    args: Tuple[Expr, ...]


@dataclass(frozen=True)
class Unary(Expr):
    op: str  # "-" | "not"
    operand: Expr


@dataclass(frozen=True)
class Binary(Expr):
    op: str  # + - * / % < <= > >= == != and or
    left: Expr
    right: Expr


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stmt:
    line: int
    column: int


@dataclass(frozen=True)
class ActionAst(Stmt):
    """A proposal: `buy(BTC, 0.9)`, `pause()`.

    `action` is already the IR spelling (BUY/SELL/EXECUTE/PAUSE/OBSERVE); the
    parser maps surface names so no downstream stage repeats that table.
    """

    action: str
    asset: Optional[str] = None
    confidence: Optional[Number] = None


@dataclass(frozen=True)
class EscalateStmt(Stmt):
    """`escalate "research-agent"` — hand this decision back to a reasoning model.

    Requires `llmre.escalate` in the effect manifest. Escalating is an effect,
    not a control-flow keyword: it is recorded, rate-limitable, and gated
    exactly like proposing an intent.

    `is_name` records which spelling was used, and it changes what gets checked.
    `escalate Research` names a declared `agent`, so a misspelling is a compile
    error. `escalate "research-agent"` is an opaque host-resolved target the
    compiler cannot verify. Both are useful; collapsing them would mean either
    losing the check or banning the string form.
    """

    target: str
    is_name: bool = False


@dataclass(frozen=True)
class IfStmt(Stmt):
    when: Expr
    then: Tuple[Stmt, ...]
    otherwise: Tuple[Stmt, ...] = ()


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamAst:
    """A compile-time-tunable constant — the surface an optimizer sweeps."""

    name: str
    declared_type: Optional[str]
    value: Literal
    line: int
    column: int


@dataclass(frozen=True)
class InputAst:
    """A declared feed input. The host supplies it; Nano never fetches it."""

    name: str
    declared_type: str
    line: int
    column: int


@dataclass(frozen=True)
class LetAst:
    """A derived binding, typically a computed series: `let ema20 = EMA(price, 20)`."""

    name: str
    declared_type: Optional[str]
    expr: Expr
    line: int
    column: int


@dataclass(frozen=True)
class RiskLimitAst:
    name: str
    value: Number
    line: int
    column: int


@dataclass(frozen=True)
class RiskAst:
    limits: Tuple[RiskLimitAst, ...]
    line: int
    column: int


@dataclass(frozen=True)
class SigFieldAst:
    """One field of a signature, with an optional `range [lo, hi]` refinement."""

    name: str
    declared_type: str
    low: Optional[Number]
    high: Optional[Number]
    line: int
    column: int


@dataclass(frozen=True)
class SignatureAst:
    """A typed reasoning-call contract. Raw prompt strings do not exist in Nano."""

    name: str
    inputs: Tuple[SigFieldAst, ...]
    outputs: Tuple[SigFieldAst, ...]
    line: int
    column: int


@dataclass(frozen=True)
class RouteAst:
    """Confidence routing: run the compiled path, or escalate when unsure."""

    name: str
    execute: str
    when: Expr
    otherwise: EscalateStmt
    line: int
    column: int


@dataclass(frozen=True)
class AgentAst:
    """A named behavior block. `role` classifies it for escalation routing."""

    name: str
    role: Optional[str] = None
    line: int = 0
    column: int = 0


@dataclass(frozen=True)
class RuleAst:
    when: Expr
    then: Tuple[Stmt, ...]
    otherwise: Tuple[Stmt, ...]
    line: int
    column: int


@dataclass(frozen=True)
class ScheduleAst:
    interval: str
    rules: Tuple[RuleAst, ...] = ()
    line: int = 0
    column: int = 0

    @property
    def rule(self) -> Optional[RuleAst]:
        """The first rule, or None — the v0.1.0 single-rule accessor."""
        return self.rules[0] if self.rules else None


@dataclass(frozen=True)
class StrategyAst:
    name: str
    tier: str = "nano"
    params: Tuple[ParamAst, ...] = ()
    inputs: Tuple[InputAst, ...] = ()
    lets: Tuple[LetAst, ...] = ()
    risk: Optional[RiskAst] = None
    signatures: Tuple[SignatureAst, ...] = ()
    routes: Tuple[RouteAst, ...] = ()
    agents: Tuple[AgentAst, ...] = ()
    schedules: Tuple[ScheduleAst, ...] = ()
    line: int = 1
    column: int = 1

    @property
    def schedule(self) -> Optional[ScheduleAst]:
        """The first schedule block, or None — the v0.1.0 single-schedule accessor."""
        return self.schedules[0] if self.schedules else None


# ---------------------------------------------------------------------------
# Legacy lowering view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConditionAst:
    """A `SIGNAL op NUMBER` comparison against a host-supplied feed signal.

    Recovered from an expression tree, not parsed. This is the only condition
    shape v0.1.0 IR can represent, so `nano/compiler/legacy.py` tries to
    flatten every rule into these before the compiler picks an IR version.
    """

    signal: str
    operator: str
    value: Number


__all__ = [
    "ActionAst",
    "AgentAst",
    "Binary",
    "BoolLit",
    "Call",
    "ConditionAst",
    "DurationLit",
    "EscalateStmt",
    "Expr",
    "IfStmt",
    "Index",
    "InputAst",
    "LetAst",
    "Literal",
    "Member",
    "Name",
    "Number",
    "NumberLit",
    "ParamAst",
    "RiskAst",
    "RiskLimitAst",
    "RouteAst",
    "RuleAst",
    "ScheduleAst",
    "SigFieldAst",
    "SignatureAst",
    "Stmt",
    "StrategyAst",
    "StringLit",
    "Unary",
]
