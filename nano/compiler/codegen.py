"""AST -> Nano IR code generation.

Two output shapes, one decision rule: **the compiler emits the lowest IR version
that can express the program** (see ``legacy.py`` for why). Baseline output is
byte-identical to what v0.1.0 produced, so the example corpus, the strategy
library, and Aether Code's pinned snapshot are untouched by v1.0. Anything
reaching past baseline becomes a v1.0 DAG.

Invariant: output is canonical. Baseline emits the historical node ordering
(schedule, conditions, intents, agents) so compiled IR stays byte-diffable
against the hand-written corpus. v1.0 emits operands before the nodes that
consume them, giving a topologically ordered DAG with fixed key order — two
compiles of the same source produce the same bytes, and `moduleHash` is
meaningful.

Codegen validates nothing about names or types; ``nano/types/checker.py`` has
already proven the program meaningful, and the final ``from_dict`` pass re-checks
the IR contract itself.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

from ..ir.graph import StrategyGraph
from ..ir.module import (
    ARITHMETIC_OPS,
    COMPARISON_OPS,
    COMPILER_NAME,
    COMPILER_VERSION,
    DETERMINISM_CONTRACT,
    InputDecl,
    IRNode,
    NanoModule,
    ParamDecl,
)
from ..ir.schema import (
    NANO_IR_VERSION_1_0,
    NANO_IR_VERSION_BASELINE,
    SUPPORTED_IR_VERSIONS,
    IRValidationError,
)
from ..types.checker import (
    ResolvedFeed,
    ResolvedIndicator,
    ResolvedInfer,
    TypedProgram,
    check,
)
from ..types.env import KIND_FEED, KIND_INPUT, KIND_LET, KIND_PARAM
from .ast import (
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
    NumberLit,
    RuleAst,
    Stmt,
    StringLit,
    StrategyAst,
    Unary,
)
from .legacy import baseline_shape
from .parser import parse

# Every v0.1.0 strategy declares exactly this manifest, in this order. It is a
# constant rather than derived because the baseline contract fixed it: a v0.1.0
# document says `["intent.emit", "log.append"]` whether or not the strategy
# happens to emit an intent. v1.0 derives its manifest from what the program
# actually does (see TypedProgram.effects).
EFFECTS_V0_1_0 = ("intent.emit", "log.append")


class IRVersionError(IRValidationError):
    """The requested IR version cannot represent this program."""


# ---------------------------------------------------------------------------
# version inference
# ---------------------------------------------------------------------------


def required_ir_version(program: TypedProgram) -> str:
    """The lowest IR version that can express `program`."""
    return (
        NANO_IR_VERSION_BASELINE
        if baseline_shape(program) is not None
        else NANO_IR_VERSION_1_0
    )


# ---------------------------------------------------------------------------
# baseline emission
# ---------------------------------------------------------------------------


def _baseline_dict(program: TypedProgram) -> dict:
    shape = baseline_shape(program)
    if shape is None:
        raise IRVersionError(
            f"Strategy {program.strategy.name!r} uses v1.0 constructs that "
            f"{NANO_IR_VERSION_BASELINE} IR cannot represent; compile it as "
            f"{NANO_IR_VERSION_1_0}"
        )

    nodes: List[dict] = []
    if shape.interval is not None:
        nodes.append({"type": "Schedule", "interval": shape.interval})
    for condition in shape.conditions:
        nodes.append(
            {
                "type": "Condition",
                "signal": condition.signal,
                "operator": condition.operator,
                "value": condition.value,
            }
        )
    for action in shape.actions:
        intent: dict = {"type": "Intent", "action": action.action}
        if action.asset is not None:
            intent["asset"] = action.asset
        if action.confidence is not None:
            intent["confidence"] = action.confidence
        nodes.append(intent)
    for agent in shape.agents:
        nodes.append({"type": "Agent", "name": agent.name})

    return {
        "type": "Strategy",
        "nanoIrVersion": NANO_IR_VERSION_BASELINE,
        "name": program.strategy.name,
        "effects": list(EFFECTS_V0_1_0),
        "nodes": nodes,
    }


# ---------------------------------------------------------------------------
# v1.0 lowering
# ---------------------------------------------------------------------------


@dataclass
class _Lowerer:
    """Builds a topologically ordered DAG from a type-checked program."""

    program: TypedProgram

    def __post_init__(self) -> None:
        self.nodes: List[IRNode] = []
        self._counter = 0
        # Named references are shared: `RSI` read from three conditions is one
        # `feed.signal` node, so the graph shows one data source rather than
        # three, and a consumer counting inputs counts them once.
        self._named: Dict[str, str] = {}

    # -- emission ----------------------------------------------------------

    def _emit(
        self,
        op: str,
        *,
        inputs: Tuple[str, ...] = (),
        attrs: Optional[dict] = None,
        type_: Optional[str] = None,
    ) -> str:
        self._counter += 1
        node_id = f"n{self._counter}"
        self.nodes.append(
            IRNode(
                id=node_id,
                op=op,
                inputs=inputs,
                attrs=attrs or {},
                type=type_,
            )
        )
        return node_id

    def _type_of(self, expr: Expr) -> Optional[str]:
        resolved = self.program.type_of(expr)
        return str(resolved) if resolved is not None else None

    # -- entry -------------------------------------------------------------

    def run(self, *, source_hash: Optional[str]) -> NanoModule:
        strategy = self.program.strategy

        for signature in strategy.signatures:
            self._emit(
                "ai.signature",
                attrs={
                    "name": signature.name,
                    "inputs": [_field_dict(f) for f in signature.inputs],
                    "outputs": [_field_dict(f) for f in signature.outputs],
                },
            )

        for binding in strategy.lets:
            value = self._lower_expr(binding.expr)
            node_id = self._emit(
                "let",
                inputs=(value,),
                attrs={"name": binding.name},
                type_=self._type_of(binding.expr),
            )
            self._named[f"{KIND_LET}:{binding.name}"] = node_id

        if strategy.risk is not None:
            self._emit(
                "risk.limits",
                attrs={
                    "limits": {
                        limit.name: limit.value for limit in strategy.risk.limits
                    }
                },
            )

        for agent in strategy.agents:
            attrs: dict = {"name": agent.name}
            if agent.role is not None:
                attrs["role"] = agent.role
            self._emit("agent", attrs=attrs)

        entries: List[str] = []
        for schedule in strategy.schedules:
            schedule_id = self._emit(
                "schedule", attrs={"interval": schedule.interval}
            )
            for rule in schedule.rules:
                entries.append(self._lower_rule(rule, schedule_id))

        for route in strategy.routes:
            condition = self._lower_expr(route.when)
            escalation = self._lower_escalate(route.otherwise)
            entries.append(
                self._emit(
                    "route",
                    inputs=(condition, escalation),
                    attrs={"name": route.name, "execute": route.execute},
                )
            )

        provenance: dict = {
            "compiler": {"name": COMPILER_NAME, "version": COMPILER_VERSION}
        }
        if source_hash is not None:
            provenance["sourceHash"] = source_hash

        return NanoModule(
            name=strategy.name,
            tier=self.program.tier,
            effects=self.program.effects,
            nodes=tuple(self.nodes),
            entries=tuple(entries),
            params=tuple(
                ParamDecl(
                    name=symbol.name,
                    type=str(symbol.type),
                    value=symbol.const_value,
                )
                for symbol in self.program.of_kind(KIND_PARAM)
            ),
            inputs=tuple(
                InputDecl(name=symbol.name, type=str(symbol.type))
                for symbol in self.program.of_kind(KIND_INPUT)
            ),
            signals=self.program.feed_signals,
            warmup=self.program.warmup,
            determinism=dict(DETERMINISM_CONTRACT),
            provenance=provenance,
        )

    # -- statements --------------------------------------------------------

    def _lower_rule(self, rule: RuleAst, schedule_id: str) -> str:
        condition = self._lower_expr(rule.when)
        then_block = self._lower_block(rule.then, schedule_id)
        inputs = [schedule_id, condition, then_block]
        if rule.otherwise:
            inputs.append(self._lower_block(rule.otherwise, schedule_id))
        return self._emit("rule", inputs=tuple(inputs))

    def _lower_block(self, statements: Tuple[Stmt, ...], schedule_id: str) -> str:
        return self._emit(
            "block",
            inputs=tuple(self._lower_statement(s, schedule_id) for s in statements),
        )

    def _lower_statement(self, statement: Stmt, schedule_id: str) -> str:
        if isinstance(statement, ActionAst):
            attrs: dict = {"action": statement.action}
            if statement.asset is not None:
                attrs["asset"] = statement.asset
            if statement.confidence is not None:
                attrs["confidence"] = statement.confidence
            return self._emit("intent.emit", attrs=attrs)

        if isinstance(statement, EscalateStmt):
            return self._lower_escalate(statement)

        if isinstance(statement, IfStmt):
            # A nested `if` is just a rule under the same schedule. Reusing the
            # opcode keeps one evaluation path for guarded work at any depth.
            condition = self._lower_expr(statement.when)
            then_block = self._lower_block(statement.then, schedule_id)
            inputs = [schedule_id, condition, then_block]
            if statement.otherwise:
                inputs.append(self._lower_block(statement.otherwise, schedule_id))
            return self._emit("rule", inputs=tuple(inputs))

        raise IRValidationError(
            f"Cannot lower statement {type(statement).__name__}"
        )

    def _lower_escalate(self, statement: EscalateStmt) -> str:
        return self._emit(
            "llmre.escalate",
            attrs={"target": statement.target, "isAgent": statement.is_name},
        )

    # -- expressions -------------------------------------------------------

    def _lower_expr(self, expr: Expr) -> str:
        if isinstance(expr, NumberLit):
            return self._emit(
                "const", attrs={"value": expr.value}, type_=self._type_of(expr)
            )
        if isinstance(expr, StringLit):
            return self._emit(
                "const", attrs={"value": expr.value}, type_=self._type_of(expr)
            )
        if isinstance(expr, BoolLit):
            return self._emit(
                "const", attrs={"value": expr.value}, type_=self._type_of(expr)
            )
        if isinstance(expr, DurationLit):
            return self._emit(
                "const", attrs={"value": expr.text}, type_=self._type_of(expr)
            )
        if isinstance(expr, Name):
            return self._lower_name(expr)
        if isinstance(expr, Index):
            return self._lower_index(expr)
        if isinstance(expr, Member):
            return self._emit(
                "record.field",
                inputs=(self._lower_expr(expr.target),),
                attrs={"field": expr.field_name},
                type_=self._type_of(expr),
            )
        if isinstance(expr, Call):
            return self._lower_call(expr)
        if isinstance(expr, Unary):
            op = "logic.not" if expr.op == "not" else "arith.neg"
            return self._emit(
                op,
                inputs=(self._lower_expr(expr.operand),),
                type_=self._type_of(expr),
            )
        if isinstance(expr, Binary):
            return self._lower_binary(expr)
        raise IRValidationError(f"Cannot lower expression {type(expr).__name__}")

    def _lower_name(self, expr: Name) -> str:
        symbol = self.program.symbols.get(expr.name)
        if symbol is None:  # pragma: no cover - the checker declares every name
            raise IRValidationError(f"Unresolved name {expr.name!r}")

        if symbol.kind == KIND_LET:
            return self._named[f"{KIND_LET}:{expr.name}"]

        op = {
            KIND_PARAM: "param.ref",
            KIND_INPUT: "input.ref",
            KIND_FEED: "feed.signal",
        }.get(symbol.kind, "builtin.confidence")

        key = f"{symbol.kind}:{expr.name}"
        existing = self._named.get(key)
        if existing is not None:
            return existing

        attrs = {} if op == "builtin.confidence" else {"name": expr.name}
        node_id = self._emit(op, attrs=attrs, type_=str(symbol.type))
        self._named[key] = node_id
        return node_id

    def _lower_index(self, expr: Index) -> str:
        # The checker already proved the offset folds to a non-negative integer,
        # so it becomes a compile-time attribute rather than a live operand —
        # there is nothing left to evaluate, and nothing a runtime could subvert.
        offset = _fold_offset(expr, self.program)
        return self._emit(
            "series.index",
            inputs=(self._lower_expr(expr.target),),
            attrs={"offset": offset},
            type_=self._type_of(expr),
        )

    def _lower_call(self, expr: Call) -> str:
        resolution = self.program.resolution_of(expr)

        if isinstance(resolution, ResolvedFeed):
            key = f"{KIND_FEED}:{resolution.signal}"
            existing = self._named.get(key)
            if existing is not None:
                return existing
            node_id = self._emit(
                "feed.signal",
                attrs={"name": resolution.signal},
                type_=self._type_of(expr),
            )
            self._named[key] = node_id
            return node_id

        if isinstance(resolution, ResolvedInfer):
            arguments = tuple(self._lower_expr(a) for a in expr.args[1:])
            return self._emit(
                "ai.infer",
                inputs=arguments,
                attrs={"signature": resolution.signature},
                type_=self._type_of(expr),
            )

        if isinstance(resolution, ResolvedIndicator):
            # Period arguments are compile-time constants and live in `periods`;
            # only the value operands become graph inputs, so the DAG shows real
            # data flow instead of literals masquerading as dependencies.
            period_positions = set(resolution.spec.period_indices)
            operands = tuple(
                self._lower_expr(argument)
                for index, argument in enumerate(expr.args)
                if index not in period_positions
            )
            return self._emit(
                "indicator",
                inputs=operands,
                attrs={
                    "name": resolution.name,
                    "periods": list(resolution.periods),
                    "lookback": resolution.lookback,
                    "lifted": resolution.lifted,
                },
                type_=self._type_of(expr),
            )

        raise IRValidationError(  # pragma: no cover - checker resolves every call
            f"Unresolved call {expr.callee!r}"
        )

    def _lower_binary(self, expr: Binary) -> str:
        if expr.op in COMPARISON_OPS:
            op = COMPARISON_OPS[expr.op]
        elif expr.op in ARITHMETIC_OPS:
            op = ARITHMETIC_OPS[expr.op]
        else:
            op = f"logic.{expr.op}"
        return self._emit(
            op,
            inputs=(self._lower_expr(expr.left), self._lower_expr(expr.right)),
            type_=self._type_of(expr),
        )


def _field_dict(field) -> dict:
    """Serialise one signature field, omitting an absent range."""
    out: dict = {"name": field.name, "type": field.declared_type}
    if field.low is not None and field.high is not None:
        out["range"] = [field.low, field.high]
    return out


def _fold_offset(expr: Index, program: TypedProgram) -> int:
    """Re-fold a validated series offset.

    Imports locally to keep the module-level dependency graph one-directional:
    `types` already imports `compiler.ast`, and a top-level import back into
    `types.lookahead` from here would make the two packages mutually dependent at
    import time.
    """
    from ..types.lookahead import fold_int

    folded = fold_int(expr.offset, _scope_of(program))
    if folded is None:  # pragma: no cover - resolve_offset already proved this
        raise IRValidationError("Series offset did not fold to a constant")
    return folded


def _scope_of(program: TypedProgram):
    """A Scope view over the checked program, for constant re-folding."""
    from ..types.env import Scope

    scope = Scope()
    for symbol in program.symbols.values():
        scope.declare(symbol)
    return scope


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def source_hash(source: str) -> str:
    """Content address of `.nano` source text.

    Separate from `moduleHash` on purpose: this changes when a comment changes,
    that one does not, and being able to tell those apart is the difference
    between "the file was edited" and "the behavior was edited".
    """
    return "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


def check_source(source: str) -> TypedProgram:
    """Parse and type-check `.nano` source. Raises on the first fault."""
    return check(parse(source))


def ast_to_dict(strategy: StrategyAst) -> dict:
    """Lower a parsed strategy to a canonical baseline IR dict.

    Retained for callers that specifically want baseline output. Raises
    ``IRVersionError`` if the strategy needs v1.0.
    """
    return _baseline_dict(check(strategy))


def compile_module(source: str) -> NanoModule:
    """Compile `.nano` source to a validated v1.0 module.

    Always succeeds for any program the checker accepts, including baseline ones
    — the module is the executable form, so this is what runtimes and the CLI
    use.
    """
    program = check_source(source)
    module = _Lowerer(program).run(source_hash=source_hash(source))
    # Round-trip through the loader so the emitted document is held to exactly
    # the contract an external one would be. A compiler that trusts its own
    # output is a compiler whose invariants drift.
    return NanoModule.from_dict(module.to_dict())


def compile_source(source: str) -> StrategyGraph:
    """Compile `.nano` source to a validated baseline StrategyGraph.

    Raises ``IRVersionError`` when the program uses v1.0 constructs; use
    ``compile_module`` for those, or ``compile_to_dict`` to get whichever
    document shape fits.
    """
    return StrategyGraph.from_dict(_baseline_dict(check_source(source)))


def compile_to_dict(source: str, *, ir_version: Optional[str] = None) -> dict:
    """Compile `.nano` source to the canonical Nano IR dict.

    With no `ir_version`, emits the lowest version that can express the program:
    baseline output stays byte-identical to v0.1.0 for programs that fit, and
    everything else becomes `1.0.0`. Pass `ir_version` to force one shape.
    """
    if ir_version is not None and ir_version not in SUPPORTED_IR_VERSIONS:
        raise IRVersionError(
            f"Unsupported IR version {ir_version!r} "
            f"(expected one of {', '.join(SUPPORTED_IR_VERSIONS)})"
        )

    program = check_source(source)
    target = ir_version or required_ir_version(program)

    if target == NANO_IR_VERSION_BASELINE:
        return _baseline_dict(program)
    return _Lowerer(program).run(source_hash=source_hash(source)).to_dict()


CompiledIR = Union[StrategyGraph, NanoModule]
