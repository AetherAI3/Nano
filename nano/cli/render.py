"""Renderers for `nano visualize`.

Four formats, because the audiences differ and no single one serves all of them:

* **ascii** — a terminal tree. What you want mid-edit, with no other tooling.
* **mermaid** — pastes into a Markdown file, a PR description, or Aether Code's
  IR panel and renders as a diagram.
* **dot** — Graphviz, for when the graph is large enough that layout matters.
* **json** — node and edge lists, for a host that draws its own.

All four are pure functions of a ``NanoModule`` and total: any module the loader
accepted renders, and none can fail on a valid graph. A visualiser that threw on
an unfamiliar opcode would be worse than useless, since an unfamiliar opcode is
exactly when you reach for a picture — so unknown ops render by name rather than
being special-cased.

The labels deliberately surface what a reviewer needs in order to judge a
strategy: warm-up length, the effect manifest, which signals come from the host
versus which Nano computes, and where a decision escalates instead of executing.
"""

from __future__ import annotations

import json
from typing import Dict, List, Sequence, Set

from ..ir.module import IRNode, NanoModule

FORMATS = ("ascii", "mermaid", "dot", "json")

# Human labels for the opcodes whose bare names read poorly in a diagram.
_OP_LABELS = {
    "input.ref": "input",
    "param.ref": "param",
    "feed.signal": "feed",
    "builtin.confidence": "confidence",
    "series.index": "offset",
    "record.field": "field",
    "intent.emit": "intent",
    "llmre.escalate": "escalate",
    "ai.signature": "signature",
    "ai.infer": "infer",
    "risk.limits": "risk",
    "arith.add": "+",
    "arith.sub": "-",
    "arith.mul": "*",
    "arith.div": "/",
    "arith.mod": "%",
    "arith.neg": "negate",
    "compare.lt": "<",
    "compare.le": "<=",
    "compare.gt": ">",
    "compare.ge": ">=",
    "compare.eq": "==",
    "compare.ne": "!=",
    "logic.and": "and",
    "logic.or": "or",
    "logic.not": "not",
}

_NAMED_OPS = ("input.ref", "param.ref", "feed.signal", "let", "agent")
_EFFECT_OPS = ("intent.emit", "llmre.escalate")


def node_label(node: IRNode) -> str:
    """A short, human-readable label for one node."""
    base = _OP_LABELS.get(node.op, node.op)
    attrs = node.attrs

    if node.op in _NAMED_OPS:
        role = attrs.get("role")
        return f"{base} {attrs.get('name', '?')}" + (f" [{role}]" if role else "")
    if node.op == "schedule":
        return f"every {attrs.get('interval', '?')}"
    if node.op == "const":
        return f"{attrs.get('value')!r}"
    if node.op == "series.index":
        return f"[{attrs.get('offset')}]"
    if node.op == "record.field":
        return f".{attrs.get('field')}"
    if node.op == "indicator":
        periods = attrs.get("periods") or []
        suffix = f"({', '.join(str(p) for p in periods)})" if periods else ""
        return f"{attrs.get('name')}{suffix}"
    if node.op == "intent.emit":
        parts = [str(attrs.get("action"))]
        if attrs.get("asset"):
            parts.append(str(attrs["asset"]))
        if attrs.get("confidence") is not None:
            parts.append(f"@{attrs['confidence']}")
        return " ".join(parts)
    if node.op == "llmre.escalate":
        return f"escalate {attrs.get('target')}"
    if node.op == "ai.signature":
        return f"signature {attrs.get('name')}"
    if node.op == "ai.infer":
        return f"infer {attrs.get('signature')}"
    if node.op == "route":
        return f"route {attrs.get('name')} -> {attrs.get('execute')}"
    if node.op == "risk.limits":
        limits = attrs.get("limits") or {}
        return "risk " + ", ".join(f"{k}={v}" for k, v in sorted(limits.items()))
    return base


def _header(module: NanoModule) -> List[str]:
    lines = [
        f"strategy {module.name}",
        f"  tier      {module.tier}",
        f"  effects   {', '.join(module.effects)}",
    ]
    if module.warmup:
        lines.append(f"  warmup    {module.warmup} bars")
    if module.params:
        lines.append(
            "  params    "
            + ", ".join(f"{p.name}: {p.type} = {p.value!r}" for p in module.params)
        )
    if module.inputs:
        lines.append(
            "  inputs    " + ", ".join(f"{i.name}: {i.type}" for i in module.inputs)
        )
    if module.signals:
        lines.append(f"  signals   {', '.join(module.signals)} (host-supplied)")
    return lines


def _draw(
    index: Dict[str, IRNode],
    node_id: str,
    lines: List[str],
    *,
    prefix: str,
    is_last: bool,
    seen: Set[str],
) -> None:
    node = index[node_id]
    connector = "`- " if is_last else "|- "
    label = node_label(node)

    if node_id in seen:
        # A shared node -- one `feed.signal` read by three conditions -- is drawn
        # once and referenced afterwards, so the tree shows reuse instead of
        # implying three separate data sources.
        lines.append(f"{prefix}{connector}{label} (shared, see {node_id})")
        return
    seen.add(node_id)

    lines.append(f"{prefix}{connector}{label}  [{node_id}]")
    child_prefix = prefix + ("   " if is_last else "|  ")
    children = list(node.inputs)
    for position, child in enumerate(children):
        _draw(
            index,
            child,
            lines,
            prefix=child_prefix,
            is_last=position == len(children) - 1,
            seen=seen,
        )


def to_ascii(module: NanoModule) -> str:
    """A terminal tree rooted at each entry point.

    Data flows bottom-up in the DAG, so each entry is drawn downward through its
    operands — the direction a reader traces when asking "what decided this?".
    """
    index = module.index()
    lines = _header(module)

    if not module.entries:
        lines.append("  (no rules — nothing is scheduled to run)")

    for entry in module.entries:
        lines.append("")
        _draw(index, entry, lines, prefix="  ", is_last=True, seen=set())
    return "\n".join(lines)


def _mermaid_shape(node: IRNode, label: str) -> str:
    """Pick a node shape that signals what kind of thing this is."""
    safe = label.replace('"', "'")
    if node.op in _EFFECT_OPS:
        return f'{node.id}{{{{"{safe}"}}}}'  # hexagon: an effect leaves the graph
    if node.op in ("rule", "route", "schedule"):
        return f'{node.id}[["{safe}"]]'  # subroutine: control flow
    if node.op in ("input.ref", "feed.signal", "param.ref", "const"):
        return f'{node.id}(["{safe}"])'  # stadium: a data source
    return f'{node.id}["{safe}"]'


def to_mermaid(module: NanoModule) -> str:
    """A Mermaid flowchart. Renders in Markdown, a PR, or Aether Code."""
    lines = [
        f"%% {module.name} - tier {module.tier}, warmup {module.warmup} bars",
        "flowchart BT",
    ]
    for node in module.nodes:
        lines.append(f"    {_mermaid_shape(node, node_label(node))}")
    for node in module.nodes:
        for child in node.inputs:
            lines.append(f"    {child} --> {node.id}")
    for entry in module.entries:
        # Highlight entry points: execution starts there, and in a bottom-up graph
        # they are otherwise indistinguishable from any other sink.
        lines.append(f"    style {entry} stroke-width:3px")
    return "\n".join(lines)


def to_dot(module: NanoModule) -> str:
    """Graphviz DOT, for graphs large enough that layout matters."""
    lines = [
        f'digraph "{module.name}" {{',
        "  rankdir=BT;",
        '  node [shape=box, fontname="monospace"];',
        f'  label="{module.name} - tier {module.tier}, warmup {module.warmup} bars";',
        "  labelloc=t;",
    ]
    for node in module.nodes:
        label = node_label(node).replace('"', '\\"')
        shape = "hexagon" if node.op in _EFFECT_OPS else "box"
        peripheries = 2 if node.id in module.entries else 1
        lines.append(
            f'  "{node.id}" [label="{label}", shape={shape}, '
            f"peripheries={peripheries}];"
        )
    for node in module.nodes:
        for child in node.inputs:
            lines.append(f'  "{child}" -> "{node.id}";')
    lines.append("}")
    return "\n".join(lines)


def graph_document(module: NanoModule) -> dict:
    """Node and edge lists, for a host that draws its own diagram.

    This is what Aether Code's IR visualiser consumes: labels are pre-computed so
    the front end need not re-implement opcode formatting, while raw `op` and
    `attrs` travel along so it can style or filter as it likes.
    """
    nodes = [
        {
            "id": node.id,
            "op": node.op,
            "label": node_label(node),
            "type": node.type,
            "attrs": dict(node.attrs),
            "isEntry": node.id in module.entries,
        }
        for node in module.nodes
    ]
    edges = [
        {"from": child, "to": node.id, "port": position}
        for node in module.nodes
        for position, child in enumerate(node.inputs)
    ]
    return {
        "name": module.name,
        "tier": module.tier,
        "effects": list(module.effects),
        "warmup": module.warmup,
        "signals": list(module.signals),
        "params": [p.to_dict() for p in module.params],
        "inputs": [i.to_dict() for i in module.inputs],
        "moduleHash": module.content_hash(),
        "nodes": nodes,
        "edges": edges,
        "entries": list(module.entries),
    }


def to_graph_json(module: NanoModule) -> str:
    return json.dumps(graph_document(module), indent=2)


_RENDERERS = {
    "ascii": to_ascii,
    "mermaid": to_mermaid,
    "dot": to_dot,
    "json": to_graph_json,
}


def render(module: NanoModule, fmt: str) -> str:
    """Render `module` in one of ``FORMATS``."""
    renderer = _RENDERERS.get(fmt)
    if renderer is None:
        raise ValueError(
            f"Unknown format {fmt!r} (expected one of {', '.join(FORMATS)})"
        )
    return renderer(module)


def summarise_run(
    intents: Sequence[object], escalations: Sequence[object], skipped: int
) -> str:
    """One-line outcome summary, shared by `replay`'s text report."""
    return (
        f"{len(intents)} intent(s), {len(escalations)} escalation(s), "
        f"{skipped} unwarmed bar(s) skipped"
    )
