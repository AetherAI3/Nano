"""Semantic analysis: types, arity, tiers, effects, warm-up, look-ahead.

The parser proves a program is *well-formed*. This pass proves it is
*meaningful* — that every name resolves, every operator has operands it accepts,
every indicator period is knowable before data arrives, and no expression can
read the future. It is also where the effect manifest is derived, because what a
program may do should follow from what it actually does, not from a list the
author maintains by hand.

Analysis stops at the first error, with an exact position. Nano's compile errors
have always been single and precise, and a half-typed tree produces cascades of
invented follow-on errors that are worse than no report at all.

Two design points worth stating plainly:

**Feed signals are series.** `RSI` in `RSI < 30` types as `series<float>`, not
`float`. The comparison therefore yields `series<bool>`, which a condition
position samples at the current bar. That reproduces v0.1.0 semantics exactly
while making `RSI[1]` mean something — history was always in the frame; the type
system just now admits it.

**Expression types are keyed by object identity.** `TypedProgram` holds the
`StrategyAst` it analysed, so every node stays alive and its `id()` stays valid
for the program's lifetime. Structural keying would collide: `RSI < 30` appearing
in two rules is two nodes with one spelling, and they can carry different
warm-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Set, Tuple, Union

from ..compiler.ast import (
    ActionAst,
    Binary,
    BoolLit,
    Call,
    DurationLit,
    EscalateStmt,
    Expr,
    IfStmt,
    Index,
    Member,
    Name,
    Number,
    NumberLit,
    RuleAst,
    SignatureAst,
    Stmt,
    StrategyAst,
    StringLit,
    Unary,
)
from ..compiler.errors import NanoTypeError
from ..indicators.registry import IndicatorSpec, lookup as lookup_indicator
from ..ir.schema import (
    ACTUATING_INTENT_ACTIONS,
    AGENT_ROLES,
    CONDITION_OPERATORS,
    EFFECT_ORDER,
    INTENT_ACTIONS,
    RISK_LIMITS,
    TIER_REQUIREMENTS,
    TIERS,
    validate_risk_limit,
)
from .env import (
    KIND_AGENT,
    KIND_BUILTIN,
    KIND_FEED,
    KIND_INPUT,
    KIND_LET,
    KIND_PARAM,
    KIND_ROUTE,
    KIND_SIGNATURE,
    Scope,
    Symbol,
)
from .kinds import (
    BOOL,
    CONFIDENCE,
    DURATION,
    FLOAT,
    INT,
    SERIES_FLOAT,
    STRING,
    Type,
    is_assignable,
    lift,
    parse_type,
    record,
    series,
    unify_comparison,
    unify_numeric,
)
from .lookahead import fold_int, is_plain_int, resolve_offset, resolve_period

_ARITHMETIC_OPS = frozenset({"+", "-", "*", "/", "%"})
# The comparison set lives in the IR schema: it is the operator vocabulary a
# `Condition` node may carry, and a checker that accepted a seventh operator the
# IR could not represent would only fail later, further from the source.
_COMPARISON_OPS = CONDITION_OPERATORS
_LOGICAL_OPS = frozenset({"and", "or"})

# `confidence` reads the confidence attached to the decision being evaluated:
# the most recent `infer` result if there is one, otherwise the confidence
# declared on the intent under consideration. It exists so
# `if confidence < 0.6 { escalate "research" }` says what it appears to say.
_BUILTINS: Tuple[Tuple[str, Type], ...] = (("confidence", CONFIDENCE),)


# ---------------------------------------------------------------------------
# resolutions — what the checker learned about each call site
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedFeed:
    """This name or call is a host-supplied signal, not something Nano computes."""

    signal: str


@dataclass(frozen=True)
class ResolvedIndicator:
    """This call is computed by ``nano/indicators``, with periods already resolved."""

    name: str
    spec: IndicatorSpec
    periods: Tuple[int, ...]
    lookback: int
    lifted: bool


@dataclass(frozen=True)
class ResolvedInfer:
    """This call hands typed inputs to a reasoning model through a signature."""

    signature: str


Resolution = Union[ResolvedFeed, ResolvedIndicator, ResolvedInfer]


@dataclass(frozen=True)
class TypedProgram:
    """A strategy that passed semantic analysis, plus everything lowering needs."""

    strategy: StrategyAst
    tier: str
    symbols: Mapping[str, Symbol]
    expr_types: Mapping[int, Type]
    resolutions: Mapping[int, Resolution]
    effects: Tuple[str, ...]
    warmup: int
    strict: bool

    def type_of(self, expr: Expr) -> Optional[Type]:
        return self.expr_types.get(id(expr))

    def resolution_of(self, expr: Expr) -> Optional[Resolution]:
        return self.resolutions.get(id(expr))

    def of_kind(self, kind: str) -> Tuple[Symbol, ...]:
        return tuple(s for s in self.symbols.values() if s.kind == kind)

    @property
    def feed_signals(self) -> Tuple[str, ...]:
        """Signal names the host must supply, in first-appearance order."""
        return tuple(s.name for s in self.of_kind(KIND_FEED))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _literal_type(value: object) -> Optional[Type]:
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, int):
        return INT
    if isinstance(value, float):
        return FLOAT
    if isinstance(value, str):
        return STRING
    return None


# ---------------------------------------------------------------------------
# the checker
# ---------------------------------------------------------------------------


class _Checker:
    def __init__(self, strategy: StrategyAst) -> None:
        self.strategy = strategy
        # Declaring an input is the author saying "this is my data" — from that
        # point an unknown bare name is a typo, not a signal. See env.Scope.
        self.scope = Scope(strict=bool(strategy.inputs))
        self.expr_types: Dict[int, Type] = {}
        self.resolutions: Dict[int, Resolution] = {}
        self.signatures: Dict[str, SignatureAst] = {}
        self.effects: Set[str] = {"log.append"}
        self.warmup = 0
        # Set by `_check_risk`, read when checking each action. See
        # `_check_statement` for what a declared floor makes illegal.
        self.min_confidence: Optional[Number] = None

    # -- entry -------------------------------------------------------------

    def run(self) -> TypedProgram:
        self._check_tier()
        for name, type_ in _BUILTINS:
            self.scope.declare(Symbol(name=name, type=type_, kind=KIND_BUILTIN))

        self._declare_signatures()
        self._declare_params()
        self._declare_inputs()
        self._declare_agents()
        self._declare_lets()
        self._check_risk()
        self._check_routes()
        self._check_schedules()

        return TypedProgram(
            strategy=self.strategy,
            tier=self.strategy.tier,
            symbols=self.scope.snapshot(),
            expr_types=dict(self.expr_types),
            resolutions=dict(self.resolutions),
            effects=tuple(e for e in EFFECT_ORDER if e in self.effects),
            warmup=self.warmup,
            strict=self.scope.strict,
        )

    # -- diagnostics -------------------------------------------------------

    @staticmethod
    def _fail(message: str, line: int, column: int) -> NanoTypeError:
        return NanoTypeError(message, line, column)

    def _declare(self, symbol: Symbol) -> None:
        clash = self.scope.declare(symbol)
        if clash is not None:
            where = f" on line {clash.line}" if clash.line else ""
            raise self._fail(
                f"{symbol.name!r} is already declared as a {clash.kind}{where}",
                symbol.line,
                symbol.column,
            )

    def _resolve_annotation(self, text: str, line: int, column: int) -> Type:
        parsed = parse_type(text)
        if parsed is None:
            raise self._fail(f"Unknown type {text!r}", line, column)
        return parsed

    # -- tier --------------------------------------------------------------

    def _check_tier(self) -> None:
        if self.strategy.tier not in TIERS:
            raise self._fail(
                f"Unknown tier {self.strategy.tier!r} "
                f"(expected one of {', '.join(TIERS)})",
                self.strategy.line,
                self.strategy.column,
            )

    def _require_tier(self, construct: str, line: int, column: int) -> None:
        """Reject a construct the declared tier does not reach."""
        required = TIER_REQUIREMENTS.get(construct)
        if required is None:
            return
        if TIERS.index(self.strategy.tier) < TIERS.index(required):
            raise self._fail(
                f"{construct!r} requires tier {required!r}, but this module "
                f"declares tier {self.strategy.tier!r} "
                f"(add `tier {required}` above the strategy)",
                line,
                column,
            )

    # -- declarations ------------------------------------------------------

    def _declare_signatures(self) -> None:
        for signature in self.strategy.signatures:
            self._require_tier("signature", signature.line, signature.column)
            seen: Set[str] = set()
            outputs: list[Tuple[str, Type]] = []
            output_names = {field.name for field in signature.outputs}

            for field in (*signature.inputs, *signature.outputs):
                if field.name in seen:
                    raise self._fail(
                        f"Signature {signature.name!r} declares "
                        f"{field.name!r} twice",
                        field.line,
                        field.column,
                    )
                seen.add(field.name)
                field_type = self._resolve_annotation(
                    field.declared_type, field.line, field.column
                )
                if (
                    field.low is not None
                    and field.high is not None
                    and field.low > field.high
                ):
                    raise self._fail(
                        f"Range [{field.low}, {field.high}] on {field.name!r} "
                        "is empty",
                        field.line,
                        field.column,
                    )
                if field.name in output_names:
                    outputs.append((field.name, field_type))

            self.signatures[signature.name] = signature
            self._declare(
                Symbol(
                    name=signature.name,
                    type=record(signature.name),
                    kind=KIND_SIGNATURE,
                    line=signature.line,
                    column=signature.column,
                    fields=tuple(outputs),
                )
            )

    def _declare_params(self) -> None:
        for param in self.strategy.params:
            inferred = _literal_type(param.value)
            if inferred is None:
                raise self._fail(
                    f"Param {param.name!r} has an unsupported literal",
                    param.line,
                    param.column,
                )
            declared = (
                self._resolve_annotation(param.declared_type, param.line, param.column)
                if param.declared_type is not None
                else inferred
            )
            if declared.is_series:
                raise self._fail(
                    f"Param {param.name!r} cannot be a series — params are "
                    "compile-time constants, and a series is runtime data",
                    param.line,
                    param.column,
                )
            if not is_assignable(inferred, declared):
                raise self._fail(
                    f"Param {param.name!r} is declared {declared} but its "
                    f"default is {inferred}",
                    param.line,
                    param.column,
                )
            self._declare(
                Symbol(
                    name=param.name,
                    type=declared,
                    kind=KIND_PARAM,
                    line=param.line,
                    column=param.column,
                    const_value=param.value,
                )
            )

    def _declare_inputs(self) -> None:
        for declared_input in self.strategy.inputs:
            input_type = self._resolve_annotation(
                declared_input.declared_type,
                declared_input.line,
                declared_input.column,
            )
            self._declare(
                Symbol(
                    name=declared_input.name,
                    type=input_type,
                    kind=KIND_INPUT,
                    line=declared_input.line,
                    column=declared_input.column,
                )
            )

    def _declare_agents(self) -> None:
        for agent in self.strategy.agents:
            if agent.role is not None and agent.role not in AGENT_ROLES:
                raise self._fail(
                    f"Unknown agent role {agent.role!r} "
                    f"(expected one of {', '.join(AGENT_ROLES)})",
                    agent.line,
                    agent.column,
                )
            self._declare(
                Symbol(
                    name=agent.name,
                    type=STRING,
                    kind=KIND_AGENT,
                    line=agent.line,
                    column=agent.column,
                    const_value=agent.role,
                )
            )

    def _declare_lets(self) -> None:
        """Bind derived values in source order — a `let` may use earlier ones."""
        for binding in self.strategy.lets:
            value_type, lookback = self._visit(binding.expr)
            if binding.declared_type is not None:
                declared = self._resolve_annotation(
                    binding.declared_type, binding.line, binding.column
                )
                if not is_assignable(value_type, declared):
                    raise self._fail(
                        f"{binding.name!r} is declared {declared} but its value "
                        f"is {value_type}",
                        binding.line,
                        binding.column,
                    )
                value_type = declared
            self._declare(
                Symbol(
                    name=binding.name,
                    type=value_type,
                    kind=KIND_LET,
                    line=binding.line,
                    column=binding.column,
                    lookback=lookback,
                )
            )
            self.warmup = max(self.warmup, lookback)

    # -- risk --------------------------------------------------------------

    def _check_risk(self) -> None:
        if self.strategy.risk is None:
            return
        seen: Set[str] = set()
        for limit in self.strategy.risk.limits:
            spec = RISK_LIMITS.get(limit.name)
            if spec is None:
                raise self._fail(
                    f"Unknown risk limit {limit.name!r} "
                    f"(expected one of {', '.join(sorted(RISK_LIMITS))})",
                    limit.line,
                    limit.column,
                )
            if limit.name in seen:
                raise self._fail(
                    f"Risk limit {limit.name!r} is set twice",
                    limit.line,
                    limit.column,
                )
            seen.add(limit.name)
            if limit.name == "min_confidence":
                self.min_confidence = limit.value

            problem = validate_risk_limit(limit.name, limit.value)
            if problem is not None:
                # Same function the IR loader calls. Two copies of these bounds
                # had already drifted once — only the loader rejected a
                # non-finite limit — and a range a compiler and a loader disagree
                # about is worse than no range at all.
                raise self._fail(
                    f"Risk limit {limit.name!r} {problem}",
                    limit.line,
                    limit.column,
                )

    # -- routes ------------------------------------------------------------

    def _check_routes(self) -> None:
        for route in self.strategy.routes:
            self._require_tier("route", route.line, route.column)
            self._declare(
                Symbol(
                    name=route.name,
                    type=BOOL,
                    kind=KIND_ROUTE,
                    line=route.line,
                    column=route.column,
                )
            )
            self._expect_condition(route.when, context=f"route {route.name!r}")
            self._check_escalate(route.otherwise)

    # -- schedules ---------------------------------------------------------

    def _check_schedules(self) -> None:
        for schedule in self.strategy.schedules:
            for rule in schedule.rules:
                self._check_rule(rule)

    def _check_rule(self, rule: RuleAst) -> None:
        self._expect_condition(rule.when, context="rule condition")
        for statement in (*rule.then, *rule.otherwise):
            self._check_statement(statement)

    def _check_statement(self, statement: Stmt) -> None:
        if isinstance(statement, ActionAst):
            if statement.action not in INTENT_ACTIONS:
                raise self._fail(
                    f"Unknown intent action {statement.action!r}",
                    statement.line,
                    statement.column,
                )
            self._check_confidence_floor(statement)
            self.effects.add("intent.emit")
            return

        if isinstance(statement, EscalateStmt):
            self._check_escalate(statement)
            return

        if isinstance(statement, IfStmt):
            self._expect_condition(statement.when, context="nested condition")
            for nested in (*statement.then, *statement.otherwise):
                self._check_statement(nested)
            return

        raise self._fail(
            f"Unsupported statement {type(statement).__name__}",
            statement.line,
            statement.column,
        )

    def _check_confidence_floor(self, statement: ActionAst) -> None:
        """Reject an actuating intent that cannot clear a declared `min_confidence`.

        The floor is checked against the intent's own declared confidence, and
        absence fails closed — so `min_confidence 0.6` beside a bare `buy(BTC)`
        proposes nothing, ever, at any input. `min_confidence 0` is the trap: the
        natural spelling of "no floor" is in range, compiles, and silently
        suppresses every intent the strategy has.

        The IR loader already refuses `min_confidence 5` on the grounds that a
        limit suppressing every intent forever is not a limit. This is the same
        rule applied to the case reachable from source. It rejects programs that
        used to compile; none of them ever proposed anything.

        `execute()` has no confidence argument in the grammar at all, so pairing
        it with any floor is rejected here rather than at run time. Exempting it
        would mean one actuating intent quietly ignoring a limit its author
        declared.
        """
        if self.min_confidence is None:
            return
        if statement.action not in ACTUATING_INTENT_ACTIONS:
            return
        if statement.confidence is not None:
            return
        surface = statement.action.lower()
        fix = (
            f"give it one, as `{surface}({statement.asset or 'ASSET'}, 0.9)`"
            if statement.asset is not None
            else "use `buy` or `sell` with a confidence"
        )
        raise self._fail(
            f"{surface}() declares no confidence, so it can never satisfy "
            f"min_confidence {self.min_confidence} and would be suppressed at "
            f"every bar — {fix}, or drop the limit",
            statement.line,
            statement.column,
        )

    def _check_escalate(self, statement: EscalateStmt) -> None:
        self._require_tier("escalate", statement.line, statement.column)
        self.effects.add("llmre.escalate")

        if not statement.is_name:
            # A quoted target is opaque: the host resolves it, so there is
            # nothing to verify here beyond it being non-empty.
            if not statement.target:
                raise self._fail(
                    "Escalation target must not be empty",
                    statement.line,
                    statement.column,
                )
            return

        symbol = self.scope.get(statement.target)
        if symbol is None or symbol.kind != KIND_AGENT:
            raise self._fail(
                f"Escalation target {statement.target!r} is not a declared agent "
                f"(declare `agent {statement.target}`, or quote the name to let "
                "the host resolve it)",
                statement.line,
                statement.column,
            )

    def _expect_condition(self, expr: Expr, *, context: str) -> None:
        """A condition must be a boolean, per-bar or otherwise."""
        condition_type, lookback = self._visit(expr)
        if condition_type.scalar != BOOL:
            raise self._fail(
                f"A {context} must be a boolean expression, got {condition_type}",
                expr.line,
                expr.column,
            )
        self.warmup = max(self.warmup, lookback)

    # -- expressions -------------------------------------------------------

    def _visit(self, expr: Expr) -> Tuple[Type, int]:
        """Type `expr` and report the bars of history it consumes."""
        result = self._visit_uncached(expr)
        self.expr_types[id(expr)] = result[0]
        return result

    def _visit_uncached(self, expr: Expr) -> Tuple[Type, int]:
        if isinstance(expr, NumberLit):
            return (INT if is_plain_int(expr.value) else FLOAT), 0
        if isinstance(expr, StringLit):
            return STRING, 0
        if isinstance(expr, BoolLit):
            return BOOL, 0
        if isinstance(expr, DurationLit):
            return DURATION, 0
        if isinstance(expr, Name):
            return self._visit_name(expr)
        if isinstance(expr, Index):
            return self._visit_index(expr)
        if isinstance(expr, Member):
            return self._visit_member(expr)
        if isinstance(expr, Call):
            return self._visit_call(expr)
        if isinstance(expr, Unary):
            return self._visit_unary(expr)
        if isinstance(expr, Binary):
            return self._visit_binary(expr)
        raise self._fail(
            f"Unsupported expression {type(expr).__name__}", expr.line, expr.column
        )

    def _visit_name(self, expr: Name) -> Tuple[Type, int]:
        symbol = self.scope.get(expr.name)
        if symbol is not None:
            if symbol.kind == KIND_SIGNATURE:
                raise self._fail(
                    f"Signature {expr.name!r} cannot be read as a value — call "
                    f"it with infer({expr.name}, ...)",
                    expr.line,
                    expr.column,
                )
            if symbol.kind in (KIND_AGENT, KIND_ROUTE):
                raise self._fail(
                    f"{symbol.kind.capitalize()} {expr.name!r} cannot be read "
                    "as a value",
                    expr.line,
                    expr.column,
                )
            # Only a `let` carries intrinsic warm-up — the cost of computing it.
            # An input or feed signal arrives raw, so reading it costs nothing.
            # Reading `symbol.lookback` for those would compound: after
            # `close[3]` widened the symbol's recorded requirement to 3, a later
            # `close[1]` would type as needing 4 bars instead of 1.
            intrinsic = symbol.lookback if symbol.kind == KIND_LET else 0
            return symbol.type, intrinsic

        if self.scope.strict:
            raise self._fail(
                f"Unknown name {expr.name!r} (this strategy declares its inputs, "
                "so undeclared names are errors — add "
                f"`input {expr.name}: series<float>` if the host supplies it)",
                expr.line,
                expr.column,
            )

        # v0.1.0 territory: an undeclared name is a host-supplied signal.
        self.scope.declare_feed(
            expr.name, SERIES_FLOAT, line=expr.line, column=expr.column
        )
        self.resolutions[id(expr)] = ResolvedFeed(expr.name)
        return SERIES_FLOAT, 0

    def _visit_index(self, expr: Index) -> Tuple[Type, int]:
        # Validate the offset before typing it. An offset like `t + 1` would
        # otherwise register `t` as a feed signal on its way to being rejected,
        # leaving a phantom signal in the manifest of a failed compile.
        offset = resolve_offset(expr, self.scope)
        self.expr_types[id(expr.offset)] = INT

        target_type, target_lookback = self._visit(expr.target)
        if not target_type.is_series:
            raise self._fail(
                f"Cannot index {target_type} — only a series has history "
                "(indexing reads N bars back)",
                expr.line,
                expr.column,
            )
        total = target_lookback + offset
        if isinstance(expr.target, Name):
            symbol = self.scope.get(expr.target.name)
            # Record how much history the *host* must supply for this name, but
            # only for data sources. Widening a `let` would feed back into its own
            # intrinsic warm-up the next time it is read.
            if symbol is not None and symbol.kind in (KIND_FEED, KIND_INPUT):
                self.scope.widen_lookback(expr.target.name, total)
        return target_type, total

    def _visit_member(self, expr: Member) -> Tuple[Type, int]:
        target_type, lookback = self._visit(expr.target)
        if target_type.name != "record" or target_type.element is None:
            raise self._fail(
                f"Cannot read field {expr.field_name!r} from {target_type} — "
                "only an infer() result has named outputs",
                expr.line,
                expr.column,
            )
        signature_name = target_type.element.name
        symbol = self.scope.get(signature_name)
        fields = dict(symbol.fields) if symbol is not None else {}
        if expr.field_name not in fields:
            available = ", ".join(sorted(fields)) or "none"
            raise self._fail(
                f"Signature {signature_name!r} declares no output "
                f"{expr.field_name!r} (outputs: {available})",
                expr.line,
                expr.column,
            )
        return fields[expr.field_name], lookback

    # -- calls -------------------------------------------------------------

    def _visit_call(self, expr: Call) -> Tuple[Type, int]:
        if expr.callee == "infer":
            return self._visit_infer(expr)

        spec = lookup_indicator(expr.callee)
        if self._is_feed_signal_form(expr, spec):
            self.scope.declare_feed(
                expr.callee, SERIES_FLOAT, line=expr.line, column=expr.column
            )
            self.resolutions[id(expr)] = ResolvedFeed(expr.callee)
            self.expr_types[id(expr.args[0])] = INT
            return SERIES_FLOAT, 0

        if spec is None:
            raise self._fail(
                f"Unknown function {expr.callee!r} (a call with a single "
                "constant integer argument is read as a host-supplied signal, "
                f"e.g. {expr.callee}(14); anything else must be a known indicator)",
                expr.line,
                expr.column,
            )
        return self._visit_indicator(expr, spec)

    def _is_feed_signal_form(self, expr: Call, spec: Optional[IndicatorSpec]) -> bool:
        """Is this the v0.1.0 `SIGNAL(period)` form the host supplies?

        One argument, a compile-time integer, and not one of the elementwise
        maths helpers whose leading parameter is a plain `float` — `ABS(3)` is a
        real call, while `RSI(14)` and `SMA_SPREAD(50)` are signal names.
        """
        if len(expr.args) != 1:
            return False
        if fold_int(expr.args[0], self.scope) is None:
            return False
        if spec is not None and spec.params and spec.params[0] == str(FLOAT):
            return False
        return True

    def _param_type(self, spec: IndicatorSpec, position: int) -> Type:
        """Resolve one parameter's declared spelling into a Type.

        The registry stores spellings rather than Type objects so it stays a leaf
        module (see its docstring); this is where they become types.
        """
        spelling = spec.params[position]
        parsed = parse_type(spelling)
        if parsed is None:  # pragma: no cover - a malformed registry entry
            raise self._fail(
                f"Indicator {spec.name} declares unknown parameter type "
                f"{spelling!r}",
                0,
                0,
            )
        return parsed

    def _return_type(self, spec: IndicatorSpec) -> Type:
        parsed = parse_type(spec.returns)
        if parsed is None:  # pragma: no cover - a malformed registry entry
            raise self._fail(
                f"Indicator {spec.name} declares unknown return type "
                f"{spec.returns!r}",
                0,
                0,
            )
        return parsed

    def _visit_indicator(self, expr: Call, spec: IndicatorSpec) -> Tuple[Type, int]:
        if len(expr.args) != spec.arity:
            raise self._fail(
                f"{spec.name} takes {spec.arity} argument(s), got "
                f"{len(expr.args)} — {spec.signature_text()}",
                expr.line,
                expr.column,
            )

        periods: list[int] = []
        lookback = 0
        lifted = False

        for index, argument in enumerate(expr.args):
            position = index + 1
            parameter = self._param_type(spec, index)
            if parameter == INT:
                periods.append(
                    resolve_period(
                        argument, self.scope, indicator=spec.name, position=position
                    )
                )
                self.expr_types[id(argument)] = INT
                continue

            argument_type, argument_lookback = self._visit(argument)
            lookback = max(lookback, argument_lookback)

            if parameter.is_series:
                if not argument_type.is_series:
                    raise self._fail(
                        f"Argument {position} of {spec.name} needs history and "
                        f"must be a {parameter}, got {argument_type} — "
                        f"{spec.signature_text()}",
                        argument.line,
                        argument.column,
                    )
                if not is_assignable(argument_type, parameter):
                    raise self._fail(
                        f"Argument {position} of {spec.name} expects "
                        f"{parameter}, got {argument_type}",
                        argument.line,
                        argument.column,
                    )
                continue

            # A plain scalar parameter: accept a scalar, or lift over a series.
            if argument_type.is_series:
                lifted = True
            if not is_assignable(argument_type.scalar, parameter):
                raise self._fail(
                    f"Argument {position} of {spec.name} expects {parameter}, "
                    f"got {argument_type} — {spec.signature_text()}",
                    argument.line,
                    argument.column,
                )

        warmup = spec.lookback(tuple(periods)) + lookback
        returns = self._return_type(spec)
        if lifted and not returns.is_series:
            returns = series(returns)

        self.resolutions[id(expr)] = ResolvedIndicator(
            name=spec.name,
            spec=spec,
            periods=tuple(periods),
            lookback=warmup,
            lifted=lifted,
        )
        return returns, warmup

    def _visit_infer(self, expr: Call) -> Tuple[Type, int]:
        self._require_tier("infer", expr.line, expr.column)
        if not expr.args:
            raise self._fail(
                "infer() needs a signature as its first argument, e.g. "
                "infer(Sentiment, headline)",
                expr.line,
                expr.column,
            )
        target = expr.args[0]
        if not isinstance(target, Name):
            raise self._fail(
                "The first argument to infer() must be a declared signature name",
                target.line,
                target.column,
            )
        signature = self.signatures.get(target.name)
        if signature is None:
            raise self._fail(
                f"{target.name!r} is not a declared signature",
                target.line,
                target.column,
            )
        self.expr_types[id(target)] = record(signature.name)

        supplied = expr.args[1:]
        if len(supplied) != len(signature.inputs):
            raise self._fail(
                f"Signature {signature.name!r} declares "
                f"{len(signature.inputs)} input(s), got {len(supplied)}",
                expr.line,
                expr.column,
            )

        lookback = 0
        for field, argument in zip(signature.inputs, supplied):
            argument_type, argument_lookback = self._visit(argument)
            lookback = max(lookback, argument_lookback)
            expected = self._resolve_annotation(
                field.declared_type, field.line, field.column
            )
            if not is_assignable(argument_type.scalar, expected.scalar):
                raise self._fail(
                    f"Input {field.name!r} of signature {signature.name!r} "
                    f"expects {expected}, got {argument_type}",
                    argument.line,
                    argument.column,
                )

        self.effects.add("llm.call")
        self.resolutions[id(expr)] = ResolvedInfer(signature.name)
        return record(signature.name), lookback

    # -- operators ---------------------------------------------------------

    def _visit_unary(self, expr: Unary) -> Tuple[Type, int]:
        operand_type, lookback = self._visit(expr.operand)
        if expr.op == "-":
            if not operand_type.scalar.is_numeric:
                raise self._fail(f"Cannot negate {operand_type}", expr.line, expr.column)
            return operand_type, lookback
        if expr.op == "not":
            if operand_type.scalar != BOOL:
                raise self._fail(
                    f"`not` needs a boolean, got {operand_type}",
                    expr.line,
                    expr.column,
                )
            return operand_type, lookback
        raise self._fail(f"Unknown unary operator {expr.op!r}", expr.line, expr.column)

    def _visit_binary(self, expr: Binary) -> Tuple[Type, int]:
        left_type, left_lookback = self._visit(expr.left)
        right_type, right_lookback = self._visit(expr.right)
        lookback = max(left_lookback, right_lookback)

        if expr.op in _ARITHMETIC_OPS:
            result = unify_numeric(left_type, right_type)
            if result is None:
                raise self._fail(
                    f"Cannot apply {expr.op!r} to {left_type} and {right_type}",
                    expr.line,
                    expr.column,
                )
            if expr.op == "/":
                # Division always produces a float. An int-preserving `/` would
                # make `fast / 2` silently truncate, and truncation that depends
                # on operand types is exactly the surprise a typed language owes
                # the author protection from.
                result = lift(FLOAT, as_series=result.is_series)
            return result, lookback

        if expr.op in _COMPARISON_OPS:
            comparison = unify_comparison(left_type, right_type)
            if comparison is None:
                raise self._fail(
                    f"Cannot compare {left_type} with {right_type}",
                    expr.line,
                    expr.column,
                )
            return comparison, lookback

        if expr.op in _LOGICAL_OPS:
            for operand_type, operand in (
                (left_type, expr.left),
                (right_type, expr.right),
            ):
                if operand_type.scalar != BOOL:
                    raise self._fail(
                        f"{expr.op!r} needs boolean operands, got {operand_type}",
                        operand.line,
                        operand.column,
                    )
            as_series = left_type.is_series or right_type.is_series
            return lift(BOOL, as_series=as_series), lookback

        raise self._fail(f"Unknown operator {expr.op!r}", expr.line, expr.column)


def check(strategy: StrategyAst) -> TypedProgram:
    """Type-check a parsed strategy. Raises ``NanoTypeError`` on the first fault."""
    return _Checker(strategy).run()
