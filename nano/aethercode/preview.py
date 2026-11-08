"""IR preview renderers for `.nano` source.

Invariant: both renderers are total and deterministic — the same source always
produces the same string, and invalid source produces an "error:" rendering of
its diagnostics instead of an exception. ir_json emits the canonical IR dict
(compile_to_dict) as two-space-indented JSON, so it round-trips byte-exactly
against the hand-written example corpus; ir_preview preserves the baseline
tree format and renders v1 modules node for node in canonical graph order.
"""

from __future__ import annotations

import json

from ..compiler import compile_to_dict
from ..ir.graph import StrategyGraph
from ..ir.module import NanoModule
from ..ir.nodes import IntentNode
from ..ir.schema import NANO_IR_VERSION_BASELINE
from .diagnostics import diagnostics


def _number(value: float) -> str:
    """Render a numeric literal compactly (30 -> '30', 0.91 -> '0.91')."""
    return f"{value:g}"


def _render_intent(intent: IntentNode) -> str:
    parts = [intent.action]
    if intent.asset is not None:
        parts.append(intent.asset)
    if intent.confidence is not None:
        parts.append(f"@{_number(intent.confidence)}")
    return f"Intent: {' '.join(parts)}"


def _render_graph(graph: StrategyGraph) -> str:
    lines = [f"Strategy: {graph.name}"]
    lines.append(f"  Effects: {', '.join(graph.effects)}")
    if graph.schedule is not None:
        lines.append(f"  Schedule: every {graph.schedule.interval}")
    for condition in graph.conditions:
        lines.append(
            f"  Condition: {condition.signal} {condition.operator} "
            f"{_number(condition.value)}"
        )
    for intent in graph.intents:
        lines.append(f"  {_render_intent(intent)}")
    for agent in graph.agents:
        lines.append(f"  Agent: {agent.name}")
    return "\n".join(lines)


def _render_errors(source: str) -> str:
    return "\n".join(
        f"error: {d.line}:{d.column}: {d.message}" for d in diagnostics(source)
    )


def _render_module(module: NanoModule) -> str:
    """Render every typed DAG node in canonical order, without executing it.

    Keep declarations, operand ids, and entry points visible: otherwise a
    preview could hide a declared signature or make a shared input look like
    multiple computations. JSON escaping keeps string-valued attributes on one
    line even when the source contains escaped newlines.
    """
    lines = [f"Strategy: {module.name}", "  IR: 1.0.0", f"  Tier: {module.tier}"]
    lines.append(f"  Effects: {', '.join(module.effects)}")
    lines.append(f"  Warm-up: {module.warmup} bars")
    for param in module.params:
        value = json.dumps(param.value, ensure_ascii=True, allow_nan=False)
        lines.append(f"  Param: {param.name}: {param.type} = {value}")
    for input_ in module.inputs:
        lines.append(f"  Input: {input_.name}: {input_.type}")
    for node in module.nodes:
        type_label = f" -> {node.type}" if node.type else ""
        operands = f" ({', '.join(node.inputs)})" if node.inputs else ""
        attrs = (
            " " + json.dumps(dict(node.attrs), ensure_ascii=True, sort_keys=True)
            if node.attrs else ""
        )
        lines.append(f"  Node {node.id}: {node.op}{operands}{type_label}{attrs}")
    lines.append(f"  Entries: {', '.join(module.entries) or '(none)'}")
    return "\n".join(lines)


def ir_preview(source: str) -> str:
    """Render baseline graphs or typed modules; keep baseline text stable."""
    try:
        document = compile_to_dict(source)
        if document["nanoIrVersion"] == NANO_IR_VERSION_BASELINE:
            return _render_graph(StrategyGraph.from_dict(document))
        return _render_module(NanoModule.from_dict(document))
    except Exception:
        return _render_errors(source)


def ir_json(source: str) -> str:
    """Render the canonical IR dict as two-space-indented JSON; never raises."""
    try:
        ir = compile_to_dict(source)
    except Exception:
        return _render_errors(source)
    return json.dumps(ir, indent=2)
