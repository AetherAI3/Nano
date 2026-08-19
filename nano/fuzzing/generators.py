"""Deterministic grammar/AST generators for compiler adversarial testing.

This module deliberately produces a small, typed language of programs instead
of random bytes.  Every valid seed starts as Nano's own immutable AST, then the
printer turns that tree into source.  Invalid programs are one labelled edit of
one valid production, which keeps failures minimizable and diagnostics useful.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

from ..compiler import compile_module, compile_source, compile_to_dict, parse
from ..compiler.ast import (
    ActionAst,
    AgentAst,
    Binary,
    BoolLit,
    Call,
    EscalateStmt,
    Expr,
    IfStmt,
    Index,
    InputAst,
    LetAst,
    Member,
    Name,
    NumberLit,
    ParamAst,
    RiskAst,
    RiskLimitAst,
    RuleAst,
    ScheduleAst,
    SigFieldAst,
    SignatureAst,
    Stmt,
    StrategyAst,
    StringLit,
    Unary,
)
from ..runtime.interpreter import MarketFrame, execute
from ..runtime.risk import RiskGate
from ..runtime.vm import run_module
from .rng import DeterministicRng

_P = {"line": 1, "column": 1}
_INTERVALS = ("1m", "5m", "15m", "1h")
_ASSETS = ("BTC", "ETH", "SOL", "EURUSD")
_FEEDS = ("RSI", "VOLUME", "MOMENTUM", "SPREAD", "VOLATILITY", "SENTIMENT")
_COMPARISONS = ("<", "<=", ">", ">=", "==", "!=")


def _number(value: int | float) -> NumberLit:
    return NumberLit(value=value, **_P)


def _name(value: str) -> Name:
    return Name(name=value, **_P)


def _binary(op: str, left: Expr, right: Expr) -> Binary:
    return Binary(op=op, left=left, right=right, **_P)


def _action(
    action: str, *, asset: Optional[str] = None, confidence: Optional[float] = None
) -> ActionAst:
    return ActionAst(action=action, asset=asset, confidence=confidence, **_P)


def _chain(op: str, expressions: Sequence[Expr]) -> Expr:
    result = expressions[0]
    for expression in expressions[1:]:
        result = _binary(op, result, expression)
    return result


def _format_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    return repr(value)


def _render_expr(expr: Expr) -> str:
    if isinstance(expr, NumberLit):
        return _format_number(expr.value)
    if isinstance(expr, StringLit):
        return json.dumps(expr.value)
    if isinstance(expr, BoolLit):
        return "true" if expr.value else "false"
    if isinstance(expr, Name):
        return expr.name
    if isinstance(expr, Call):
        return f"{expr.callee}({', '.join(_render_expr(arg) for arg in expr.args)})"
    if isinstance(expr, Index):
        return f"{_render_expr(expr.target)}[{_render_expr(expr.offset)}]"
    if isinstance(expr, Member):
        return f"{_render_expr(expr.target)}.{expr.field_name}"
    if isinstance(expr, Unary):
        if expr.op == "not":
            return f"not ({_render_expr(expr.operand)})"
        return f"-({_render_expr(expr.operand)})"
    if isinstance(expr, Binary):
        return f"({_render_expr(expr.left)} {expr.op} {_render_expr(expr.right)})"
    raise TypeError(f"Unsupported generated expression {type(expr).__name__}")


def _render_statement(statement: Stmt, depth: int) -> list[str]:
    prefix = "    " * depth
    if isinstance(statement, ActionAst):
        name = statement.action.lower()
        if statement.asset is None:
            return [f"{prefix}{name}()"]
        confidence = (
            ""
            if statement.confidence is None
            else f", {_format_number(statement.confidence)}"
        )
        return [f"{prefix}{name}({statement.asset}{confidence})"]
    if isinstance(statement, EscalateStmt):
        target = statement.target if statement.is_name else json.dumps(statement.target)
        return [f"{prefix}escalate {target}"]
    if isinstance(statement, IfStmt):
        lines = [f"{prefix}if {_render_expr(statement.when)} {{"]
        for nested in statement.then:
            lines.extend(_render_statement(nested, depth + 1))
        lines.append(f"{prefix}}}")
        if statement.otherwise:
            lines[-1] += " else {"
            for nested in statement.otherwise:
                lines.extend(_render_statement(nested, depth + 1))
            lines.append(f"{prefix}}}")
        return lines
    raise TypeError(f"Unsupported generated statement {type(statement).__name__}")


def render_strategy(strategy: StrategyAst) -> str:
    """Render the generated AST subset using only grammar productions."""
    lines: list[str] = []
    if strategy.tier != "nano":
        lines.append(f"tier {strategy.tier}")
    lines.append(f"strategy {strategy.name} {{")

    for param in strategy.params:
        annotation = f": {param.declared_type}" if param.declared_type else ""
        value = (
            json.dumps(param.value)
            if isinstance(param.value, str)
            else (
                str(param.value).lower()
                if isinstance(param.value, bool)
                else _format_number(param.value)
            )
        )
        lines.append(f"    param {param.name}{annotation} = {value}")
    for declared_input in strategy.inputs:
        lines.append(f"    input {declared_input.name}: {declared_input.declared_type}")
    for binding in strategy.lets:
        annotation = f": {binding.declared_type}" if binding.declared_type else ""
        lines.append(
            f"    let {binding.name}{annotation} = {_render_expr(binding.expr)}"
        )
    if strategy.risk is not None:
        lines.append("    risk {")
        for limit in strategy.risk.limits:
            lines.append(f"        {limit.name} {_format_number(limit.value)}")
        lines.append("    }")
    for signature in strategy.signatures:
        lines.append(f"    signature {signature.name} {{")
        for kind, fields in (
            ("input", signature.inputs),
            ("output", signature.outputs),
        ):
            for field in fields:
                refinement = (
                    ""
                    if field.low is None or field.high is None
                    else f" range [{_format_number(field.low)}, {_format_number(field.high)}]"
                )
                lines.append(
                    f"        {kind} {field.name}: {field.declared_type}{refinement}"
                )
        lines.append("    }")
    for agent in strategy.agents:
        role = "" if agent.role is None else f" {{ role {agent.role} }}"
        lines.append(f"    agent {agent.name}{role}")
    for schedule in strategy.schedules:
        lines.append(f"    every {schedule.interval} {{")
        for rule in schedule.rules:
            lines.append(f"        if {_render_expr(rule.when)} {{")
            for statement in rule.then:
                lines.extend(_render_statement(statement, 3))
            lines.append("        }")
            if rule.otherwise:
                lines[-1] += " else {"
                for statement in rule.otherwise:
                    lines.extend(_render_statement(statement, 3))
                lines.append("        }")
        lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class RuntimeComparison:
    reference: Tuple[Mapping[str, object], ...]
    lifted_vm: Tuple[Mapping[str, object], ...]
    vm: Tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class ValidProgram:
    id: str
    family: str
    ast: StrategyAst
    source: str
    baseline: bool

    def parse(self) -> StrategyAst:
        return parse(self.source)

    def compile(self) -> dict:
        return compile_to_dict(self.source)

    def frame(self, *, length: int = 12) -> MarketFrame:
        module = compile_module(self.source)
        names = {declared.name for declared in module.inputs}
        names.update(module.signals)
        names.update(RiskGate.for_module(module).required_measurements())
        if module.of_op("builtin.confidence"):
            names.add("confidence")

        signals: dict[str, Tuple[float, ...]] = {}
        for name in sorted(names):
            if name == "confidence":
                signals[name] = tuple(0.2 if i % 2 == 0 else 0.8 for i in range(length))
                continue
            if name.startswith("risk."):
                signals[name] = tuple(0.0 for _ in range(length))
                continue
            digest = hashlib.sha256(name.encode("utf-8")).digest()
            center = 10.0 + float(int.from_bytes(digest[:2], "big") % 80)
            signals[name] = tuple(
                (-1_000_000.0 if i % 3 == 0 else 1_000_000.0 if i % 3 == 1 else center)
                for i in range(length)
            )
        return MarketFrame(
            timestamps=tuple(index * 60 for index in range(length)), signals=signals
        )

    def compare_baseline_runtimes(self) -> RuntimeComparison:
        if not self.baseline:
            raise ValueError("Only baseline programs have a reference interpreter path")
        graph = compile_source(self.source)
        frame = self.frame()
        reference = tuple(intent.to_dict() for intent in execute(graph, frame).intents)
        lifted_vm = tuple(
            intent.to_dict() for intent in run_module(graph.to_module(), frame).intents
        )
        vm = tuple(
            intent.to_dict()
            for intent in run_module(compile_module(self.source), frame).intents
        )
        return RuntimeComparison(reference=reference, lifted_vm=lifted_vm, vm=vm)


@dataclass(frozen=True)
class InvalidProgram:
    id: str
    family: str
    source: str
    base_source: str
    mutation: str
    expected_location: Optional[Tuple[int, int]] = None

    def compile(self) -> dict:
        return compile_to_dict(self.source)


@dataclass(frozen=True)
class EquivalentPrograms:
    id: str
    family: str
    sources: Tuple[str, ...]

    def frame(self, *, length: int = 12) -> MarketFrame:
        ast = parse(self.sources[0])
        case = ValidProgram(
            id=self.id,
            family=self.family,
            ast=ast,
            source=self.sources[0],
            baseline=compile_to_dict(self.sources[0])["nanoIrVersion"] == "0.1.0",
        )
        return case.frame(length=length)


def _baseline_ast(rng: DeterministicRng, index: int) -> StrategyAst:
    count = rng.randint(1, 3)
    conditions: list[Expr] = []
    for offset, feed in enumerate(rng.sample(_FEEDS, count)):
        left: Expr = _name(feed)
        if rng.choice((False, True)):
            left = Call(callee=feed, args=(_number(rng.randint(2, 30)),), **_P)
        conditions.append(
            _binary(
                rng.choice(_COMPARISONS),
                left,
                _number(rng.randint(1 + offset, 90 + offset)),
            )
        )
    actions: list[Stmt] = [
        _action(
            rng.choice(("BUY", "SELL")),
            asset=rng.choice(_ASSETS),
            confidence=round(rng.uniform(0.1, 1.0), 2),
        )
    ]
    if rng.choice((False, True)):
        actions.append(_action(rng.choice(("OBSERVE", "PAUSE", "EXECUTE"))))
    rule = RuleAst(
        when=_chain("and", conditions),
        then=tuple(actions),
        otherwise=(),
        **_P,
    )
    agents = (
        (AgentAst(name=f"Observer{index}", role=None, **_P),)
        if rng.choice((False, True))
        else ()
    )
    return StrategyAst(
        name=f"Baseline{index}",
        agents=agents,
        schedules=(ScheduleAst(interval=rng.choice(_INTERVALS), rules=(rule,), **_P),),
    )


def _series_ast(rng: DeterministicRng, index: int) -> StrategyAst:
    window = rng.randint(2, 8)
    lag = rng.randint(1, window)
    average_name = f"average{index}"
    average = Call(
        callee=rng.choice(("SMA", "EMA", "ZSCORE")),
        args=(_name("close"), _name("window")),
        **_P,
    )
    comparison = _binary(
        rng.choice((">", ">=", "<", "<=")),
        Index(target=_name("close"), offset=_name("lag"), **_P),
        _name(average_name),
    )
    return StrategyAst(
        name=f"Series{index}",
        params=(
            ParamAst(name="window", declared_type="int", value=window, **_P),
            ParamAst(name="lag", declared_type="int", value=lag, **_P),
        ),
        inputs=(InputAst(name="close", declared_type="series<float>", **_P),),
        lets=(LetAst(name=average_name, declared_type=None, expr=average, **_P),),
        schedules=(
            ScheduleAst(
                interval=rng.choice(_INTERVALS[:3]),
                rules=(
                    RuleAst(
                        when=comparison,
                        then=(_action("BUY", asset=rng.choice(_ASSETS)),),
                        otherwise=(_action("OBSERVE"),),
                        **_P,
                    ),
                ),
                **_P,
            ),
        ),
    )


def _multi_ast(rng: DeterministicRng, index: int) -> StrategyAst:
    threshold = float(rng.randint(20, 80))
    low = _binary("<", _name("price"), _name("threshold"))
    high = _binary(">=", _name("price"), _name("threshold"))
    nested = IfStmt(
        when=_binary(">", _name("volume"), _number(0)),
        then=(_action("BUY", asset=rng.choice(_ASSETS)),),
        otherwise=(_action("OBSERVE"),),
        **_P,
    )
    rules = (
        RuleAst(when=low, then=(nested,), otherwise=(), **_P),
        RuleAst(
            when=high,
            then=(_action("SELL", asset=rng.choice(_ASSETS)),),
            otherwise=(),
            **_P,
        ),
    )
    return StrategyAst(
        name=f"Multi{index}",
        params=(
            ParamAst(name="threshold", declared_type="float", value=threshold, **_P),
        ),
        inputs=(
            InputAst(name="price", declared_type="series<float>", **_P),
            InputAst(name="volume", declared_type="series<float>", **_P),
        ),
        schedules=(
            ScheduleAst(interval=rng.choice(_INTERVALS[:3]), rules=rules, **_P),
        ),
    )


def _escalation_ast(rng: DeterministicRng, index: int) -> StrategyAst:
    agent = f"Desk{index}"
    return StrategyAst(
        name=f"Escalation{index}",
        tier="nano+",
        agents=(
            AgentAst(name=agent, role=rng.choice(("research", "validation")), **_P),
        ),
        schedules=(
            ScheduleAst(
                interval=rng.choice(_INTERVALS[:3]),
                rules=(
                    RuleAst(
                        when=_binary("<", _name("confidence"), _number(0.6)),
                        then=(EscalateStmt(target=agent, is_name=True, **_P),),
                        otherwise=(_action("OBSERVE"),),
                        **_P,
                    ),
                ),
                **_P,
            ),
        ),
    )


def _reasoning_ast(rng: DeterministicRng, index: int) -> StrategyAst:
    signature_name = f"Bias{index}"
    call_name = f"call{index}"
    signature = SignatureAst(
        name=signature_name,
        inputs=(
            SigFieldAst(name="price", declared_type="float", low=None, high=None, **_P),
        ),
        outputs=(
            SigFieldAst(name="score", declared_type="float", low=0.0, high=1.0, **_P),
        ),
        **_P,
    )
    inference = Call(callee="infer", args=(_name(signature_name), _name("close")), **_P)
    return StrategyAst(
        name=f"Reasoning{index}",
        tier="nano+",
        inputs=(InputAst(name="close", declared_type="series<float>", **_P),),
        lets=(LetAst(name=call_name, declared_type=None, expr=inference, **_P),),
        signatures=(signature,),
        schedules=(
            ScheduleAst(
                interval="1m",
                rules=(
                    RuleAst(
                        when=_binary(
                            ">",
                            Member(target=_name(call_name), field_name="score", **_P),
                            _number(round(rng.uniform(0.2, 0.8), 2)),
                        ),
                        then=(_action("BUY", asset=rng.choice(_ASSETS)),),
                        otherwise=(),
                        **_P,
                    ),
                ),
                **_P,
            ),
        ),
    )


def _risk_ast(rng: DeterministicRng, index: int) -> StrategyAst:
    limits = (
        RiskLimitAst(
            name="max_daily_loss", value=round(rng.uniform(0.01, 0.2), 3), **_P
        ),
        RiskLimitAst(name="max_orders_per_day", value=rng.randint(1, 8), **_P),
    )
    return StrategyAst(
        name=f"Risk{index}",
        risk=RiskAst(limits=limits, **_P),
        schedules=(
            ScheduleAst(
                interval=rng.choice(_INTERVALS),
                rules=(
                    RuleAst(
                        when=_binary(">", _name("DRAWDOWN"), _number(0.05)),
                        then=(_action("SELL", asset=rng.choice(_ASSETS)),),
                        otherwise=(_action("BUY", asset=rng.choice(_ASSETS)),),
                        **_P,
                    ),
                ),
                **_P,
            ),
        ),
    )


def _arithmetic_ast(rng: DeterministicRng, index: int) -> StrategyAst:
    adjusted = _binary(
        "/",
        _binary("+", _name("close"), _name("offset")),
        _name("scale"),
    )
    condition = _binary(
        "and",
        _binary(">", adjusted, _number(float(rng.randint(1, 20)))),
        Unary(op="not", operand=BoolLit(value=False, **_P), **_P),
    )
    return StrategyAst(
        name=f"Arithmetic{index}",
        params=(
            ParamAst(name="offset", declared_type="float", value=1.5, **_P),
            ParamAst(name="scale", declared_type="float", value=2.0, **_P),
        ),
        inputs=(InputAst(name="close", declared_type="series<float>", **_P),),
        schedules=(
            ScheduleAst(
                interval="1m",
                rules=(
                    RuleAst(
                        when=condition, then=(_action("EXECUTE"),), otherwise=(), **_P
                    ),
                ),
                **_P,
            ),
        ),
    )


_VALID_BUILDERS = (
    ("baseline", _baseline_ast),
    ("series", _series_ast),
    ("multi_rule", _multi_ast),
    ("escalation", _escalation_ast),
    ("reasoning", _reasoning_ast),
    ("risk", _risk_ast),
    ("arithmetic", _arithmetic_ast),
)


def generate_valid_programs(*, seed: int, count: int) -> Tuple[ValidProgram, ...]:
    rng = DeterministicRng(seed)
    cases: list[ValidProgram] = []
    for index in range(count):
        family, builder = _VALID_BUILDERS[index % len(_VALID_BUILDERS)]
        ast = builder(rng, index)
        source = render_strategy(ast)
        cases.append(
            ValidProgram(
                id=f"valid-{seed}-{index}",
                family=family,
                ast=ast,
                source=source,
                baseline=family == "baseline",
            )
        )
    return tuple(cases)


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise AssertionError(f"Mutation anchor {old!r} is not unique")
    return source.replace(old, new, 1)


def _location(source: str, marker: str) -> Tuple[int, int]:
    offset = source.index(marker)
    line = source.count("\n", 0, offset) + 1
    previous = source.rfind("\n", 0, offset)
    column = offset - previous
    return line, column


_INVALID_BASES = {
    "action": (
        "strategy S {\n"
        "    every 5m {\n"
        "        if RSI < 30 {\n"
        "            observe()\n"
        "        }\n"
        "    }\n"
        "}\n"
    ),
    "confidence": (
        "strategy S {\n"
        "    every 5m {\n"
        "        if RSI < 30 {\n"
        "            buy(BTC, 0.5)\n"
        "        }\n"
        "    }\n"
        "}\n"
    ),
    "interval": (
        "strategy S {\n"
        "    every 5m {\n"
        "        if RSI < 30 {\n"
        "            observe()\n"
        "        }\n"
        "    }\n"
        "}\n"
    ),
    "series": (
        "strategy S {\n"
        "    input close: series<float>\n"
        "    every 1m {\n"
        "        if close > close[1] {\n"
        "            buy(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n"
    ),
    "period": (
        "strategy S {\n"
        "    input close: series<float>\n"
        "    let mean = SMA(close, 2)\n"
        "    every 1m {\n"
        "        if close > mean {\n"
        "            observe()\n"
        "        }\n"
        "    }\n"
        "}\n"
    ),
    "tier": (
        "tier nano+\n"
        "strategy S {\n"
        "    agent Desk { role research }\n"
        "    every 1m {\n"
        "        if confidence < 0.5 {\n"
        "            escalate Desk\n"
        "        }\n"
        "    }\n"
        "}\n"
    ),
    "type": (
        "strategy S {\n"
        "    every 1m {\n"
        "        if (1 + 2) > 0 {\n"
        "            observe()\n"
        "        }\n"
        "    }\n"
        "}\n"
    ),
}


def _invalid_case(seed: int, index: int, family: str) -> InvalidProgram:
    case_id = f"invalid-{seed}-{index}-{family}"
    if family == "unknown_action":
        base = _INVALID_BASES["action"]
        source = _replace_once(base, "observe()", "jump()")
        return InvalidProgram(
            case_id,
            family,
            source,
            base,
            "observe -> jump",
            _location(source, "jump"),
        )
    if family == "confidence":
        base = _INVALID_BASES["confidence"]
        source = _replace_once(base, "0.5", "1.5")
        return InvalidProgram(
            case_id,
            family,
            source,
            base,
            "0.5 -> 1.5",
            _location(source, "1.5"),
        )
    if family == "interval":
        base = _INVALID_BASES["interval"]
        source = _replace_once(base, "5m", "5x")
        return InvalidProgram(
            case_id, family, source, base, "5m -> 5x", _location(source, "5x")
        )
    if family == "negative_lookahead":
        base = _INVALID_BASES["series"]
        source = _replace_once(base, "close[1]", "close[-1]")
        return InvalidProgram(
            case_id, family, source, base, "1 -> -1", _location(source, "-1")
        )
    if family == "dynamic_lookahead":
        base = _INVALID_BASES["series"]
        source = _replace_once(base, "close[1]", "close[t+1]")
        # The non-constant operation is the `+`, and the checker deliberately
        # pins the diagnostic to that operator rather than to the valid name `t`.
        return InvalidProgram(
            case_id, family, source, base, "1 -> t+1", _location(source, "+")
        )
    if family == "period":
        base = _INVALID_BASES["period"]
        source = _replace_once(base, "SMA(close, 2)", "SMA(close, 0)")
        return InvalidProgram(case_id, family, source, base, "period 2 -> 0")
    if family == "tier_capability":
        base = _INVALID_BASES["tier"]
        source = _replace_once(base, "tier nano+\n", "")
        return InvalidProgram(case_id, family, source, base, "remove tier grant")
    if family == "type_operand_order":
        base = _INVALID_BASES["type"]
        occurrence = index // len(_INVALID_FAMILIES)
        old, new = (
            ("1 + 2", '"bad" + 2') if occurrence % 2 == 0 else ("1 + 2", '1 + "bad"')
        )
        source = _replace_once(base, old, new)
        return InvalidProgram(case_id, family, source, base, f"{old} -> {new}")
    raise AssertionError(f"Unknown invalid family {family}")


_INVALID_FAMILIES = (
    "unknown_action",
    "confidence",
    "interval",
    "negative_lookahead",
    "dynamic_lookahead",
    "period",
    "tier_capability",
    "type_operand_order",
)


def generate_invalid_programs(*, seed: int, count: int) -> Tuple[InvalidProgram, ...]:
    # Rotate the fixed mutation families by seed while still guaranteeing broad
    # coverage once count reaches the family count.
    shift = seed % len(_INVALID_FAMILIES)
    families = _INVALID_FAMILIES[shift:] + _INVALID_FAMILIES[:shift]
    return tuple(
        _invalid_case(seed, index, families[index % len(families)])
        for index in range(count)
    )


def _equivalent_sources(index: int, family: str) -> Tuple[str, ...]:
    if family == "comments_whitespace":
        base = (
            "strategy Equivalent {\n"
            "    every 1m {\n"
            "        if RSI < 50 {\n"
            "            buy(BTC, 0.5)\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        return (
            base,
            "// equivalent comment\n" + base,
            base.replace("    every", "\n        every").replace(
                "        if", "            if"
            ),
        )
    if family == "parentheses":
        base = (
            "strategy Equivalent {\n"
            "    every 1m {\n"
            "        if RSI < 50 {\n"
            "            observe()\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        return (base, base.replace("if RSI < 50", "if ((RSI < 50))"))
    if family == "numeric_spelling":
        base = (
            "strategy Equivalent {\n"
            "    every 1m {\n"
            "        if RSI < 50 {\n"
            "            buy(BTC, 0.5)\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        return (base, base.replace("0.5", "0.50"))
    if family == "declaration_order":
        prefix = "strategy Equivalent {\n"
        declarations = (
            "    param threshold: float = 50.0\n"
            "    input close: series<float>\n"
            "    agent Watcher { role observer }\n"
        )
        reordered = (
            "    agent Watcher { role observer }\n"
            "    input close: series<float>\n"
            "    param threshold: float = 50.0\n"
        )
        body = (
            "    every 1m {\n"
            "        if close < threshold {\n"
            "            observe()\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        return (prefix + declarations + body, prefix + reordered + body)
    raise AssertionError(f"Unknown equivalent family {family}")


_EQUIVALENT_FAMILIES = (
    "comments_whitespace",
    "parentheses",
    "numeric_spelling",
    "declaration_order",
)


def generate_equivalent_programs(
    *, seed: int, count: int
) -> Tuple[EquivalentPrograms, ...]:
    shift = seed % len(_EQUIVALENT_FAMILIES)
    families = _EQUIVALENT_FAMILIES[shift:] + _EQUIVALENT_FAMILIES[:shift]
    return tuple(
        EquivalentPrograms(
            id=f"equivalent-{seed}-{index}",
            family=families[index % len(families)],
            sources=_equivalent_sources(index, families[index % len(families)]),
        )
        for index in range(count)
    )


__all__ = [
    "EquivalentPrograms",
    "InvalidProgram",
    "RuntimeComparison",
    "ValidProgram",
    "generate_equivalent_programs",
    "generate_invalid_programs",
    "generate_valid_programs",
    "render_strategy",
]
