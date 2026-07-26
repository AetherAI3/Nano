"""Validated Nano++ LoopGraph data model.

A LoopGraph serializes, validates, and hashes a proposed loop document. This
module does not compile or execute a loop, perform replay, or deploy changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Tuple

from .nodes import LoopNode, LoopValidationError

_NANO_IR_VERSION = "0.1.0"

# Effect vocabulary a loop graph may declare. A loop that does not declare a
# capability cannot contain a node that needs it — least privilege by default.
KNOWN_LOOP_EFFECTS = frozenset(
    {
        "quantum.submit",
        "intent.emit",
        "sign.emit",
        "mutate.propose",
        "llmre.escalate",
        "log.append",
    }
)


@dataclass(frozen=True)
class LoopGraph:
    name: str
    effects: Tuple[str, ...]
    nodes: Tuple[LoopNode, ...]

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "LoopGraph":
        if data.get("type") != "Loop":
            raise LoopValidationError("Root 'type' must be 'Loop'")
        if data.get("nanoIrVersion") != _NANO_IR_VERSION:
            raise LoopValidationError(
                f"nanoIrVersion must be {_NANO_IR_VERSION!r}"
            )
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise LoopValidationError("Loop requires a non-empty 'name'")

        effects_raw = data.get("effects")
        if not isinstance(effects_raw, list) or not effects_raw:
            raise LoopValidationError("Loop requires a non-empty 'effects' manifest")
        unknown = set(effects_raw) - KNOWN_LOOP_EFFECTS
        if unknown:
            raise LoopValidationError(f"Unknown effects declared: {sorted(unknown)}")
        effects = tuple(effects_raw)

        nodes_raw = data.get("nodes")
        if not isinstance(nodes_raw, list) or not nodes_raw:
            raise LoopValidationError("Loop requires a non-empty 'nodes' list")

        seen: set[str] = set()
        nodes: list[LoopNode] = []
        for node_data in nodes_raw:
            node = LoopNode.from_dict(node_data)
            if node.id in seen:
                raise LoopValidationError(f"Duplicate node id {node.id!r}")
            for dep in node.inputs:
                if dep not in seen:
                    raise LoopValidationError(
                        f"Node {node.id!r} references {dep!r} before it is defined"
                    )
            required = node.required_effect()
            if required is not None and required not in effects:
                raise LoopValidationError(
                    f"Node {node.id!r} ({node.stage}) needs effect {required!r} "
                    f"which the manifest does not declare"
                )
            seen.add(node.id)
            nodes.append(node)

        return LoopGraph(name=name, effects=effects, nodes=tuple(nodes))

    def content_hash(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "type": "Loop",
            "nanoIrVersion": _NANO_IR_VERSION,
            "name": self.name,
            "effects": list(self.effects),
            "nodes": [n.to_dict() for n in self.nodes],
        }
