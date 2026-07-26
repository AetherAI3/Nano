"""StrategyGraph — the loadable, validated unit of baseline (v0.1.0) Nano IR.

Load-time validation is the security boundary: manifest violations and unknown
node types are rejected here, never discovered at runtime.

This loader is pinned to `0.1.0` on purpose. It is the *reference* shape — one
flat schedule, conditions, intents, agents — and the semantics every richer
version has to agree with. v1.0.0 documents load through ``nano/ir/module.py``,
and a graph read here lifts into that module via ``to_module()``, so both
versions share a single evaluator and cannot drift apart in behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, see to_module()
    from .module import NanoModule

from .nodes import AgentNode, ConditionNode, IntentNode, ScheduleNode
from .schema import (
    IRValidationError,
    KNOWN_EFFECTS,
    ManifestViolation,
    NANO_IR_VERSION_BASELINE,
    NODE_TYPES,
)

_NODE_PARSERS = {
    "Schedule": ScheduleNode.from_dict,
    "Condition": ConditionNode.from_dict,
    "Intent": IntentNode.from_dict,
    "Agent": AgentNode.from_dict,
}


@dataclass(frozen=True)
class StrategyGraph:
    name: str
    effects: Tuple[str, ...]
    schedule: Optional[ScheduleNode]
    conditions: Tuple[ConditionNode, ...]
    intents: Tuple[IntentNode, ...]
    agents: Tuple[AgentNode, ...]

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "StrategyGraph":
        if data.get("type") != "Strategy":
            raise IRValidationError("Root 'type' must be 'Strategy'")
        version = data.get("nanoIrVersion")
        if version != NANO_IR_VERSION_BASELINE:
            raise IRValidationError(
                f"nanoIrVersion {version!r} unsupported "
                f"(expected {NANO_IR_VERSION_BASELINE!r}; load 1.0.0 documents "
                "with nano.ir.module.NanoModule)"
            )
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise IRValidationError("Strategy requires a non-empty 'name'")

        effects_raw = data.get("effects")
        if not isinstance(effects_raw, list) or not effects_raw:
            raise IRValidationError("Strategy requires a non-empty 'effects' manifest")
        unknown_effects = set(effects_raw) - KNOWN_EFFECTS
        if unknown_effects:
            raise IRValidationError(f"Unknown effects declared: {sorted(unknown_effects)}")
        effects = tuple(effects_raw)

        nodes_raw = data.get("nodes")
        if not isinstance(nodes_raw, list) or not nodes_raw:
            raise IRValidationError("Strategy requires a non-empty 'nodes' list")

        schedules: list[ScheduleNode] = []
        conditions: list[ConditionNode] = []
        intents: list[IntentNode] = []
        agents: list[AgentNode] = []
        for node_data in nodes_raw:
            node_type = node_data.get("type")
            if node_type not in NODE_TYPES:
                raise IRValidationError(f"Unknown node type {node_type!r}")
            node = _NODE_PARSERS[node_type](node_data)
            if isinstance(node, ScheduleNode):
                schedules.append(node)
            elif isinstance(node, ConditionNode):
                conditions.append(node)
            elif isinstance(node, IntentNode):
                intents.append(node)
            else:
                agents.append(node)

        if len(schedules) > 1:
            raise IRValidationError("At most one Schedule node is allowed")
        if intents and "intent.emit" not in effects:
            raise ManifestViolation(
                "Graph declares Intent nodes but 'intent.emit' is not in the effect manifest"
            )

        return StrategyGraph(
            name=name,
            effects=effects,
            schedule=schedules[0] if schedules else None,
            conditions=tuple(conditions),
            intents=tuple(intents),
            agents=tuple(agents),
        )

    def to_dict(self) -> dict:
        nodes: list[dict] = []
        if self.schedule is not None:
            nodes.append(self.schedule.to_dict())
        nodes.extend(c.to_dict() for c in self.conditions)
        nodes.extend(i.to_dict() for i in self.intents)
        nodes.extend(a.to_dict() for a in self.agents)
        return {
            "type": "Strategy",
            "nanoIrVersion": NANO_IR_VERSION_BASELINE,
            "name": self.name,
            "effects": list(self.effects),
            "nodes": nodes,
        }

    def to_module(self) -> "NanoModule":
        """Lift this baseline graph into an equivalent v1.0 module.

        This exists so there is one evaluator rather than two. Baseline semantics
        are reproduced exactly, including the detail that a graph with no
        conditions emits nothing: the reference interpreter guards intent
        emission on `all_true and graph.conditions`, so a schedule with an empty
        body produces a schedule node and no rule.

        `tests/test_conformance.py` asserts this lift agrees with
        ``nano/runtime/interpreter.py`` bar for bar across the whole corpus. That
        test is the reason the two versions cannot drift.
        """
        # Imported here, not at module scope: `module` imports this file's schema
        # constants, and a top-level import back would be a cycle.
        from .module import COMPARISON_OPS, IRNode, NanoModule

        nodes: list[IRNode] = []
        counter = 0

        def emit(op: str, **kwargs) -> str:
            nonlocal counter
            counter += 1
            node_id = f"n{counter}"
            nodes.append(IRNode(id=node_id, op=op, **kwargs))
            return node_id

        schedule_id: Optional[str] = None
        if self.schedule is not None:
            schedule_id = emit(
                "schedule", attrs={"interval": self.schedule.interval}
            )

        signals: dict[str, str] = {}
        condition_ids: list[str] = []
        for condition in self.conditions:
            if condition.signal not in signals:
                signals[condition.signal] = emit(
                    "feed.signal",
                    attrs={"name": condition.signal},
                    type="series<float>",
                )
            value_id = emit(
                "const", attrs={"value": condition.value}, type="float"
            )
            condition_ids.append(
                emit(
                    COMPARISON_OPS[condition.operator],
                    inputs=(signals[condition.signal], value_id),
                    type="series<bool>",
                )
            )

        entries: list[str] = []
        if schedule_id is not None and condition_ids:
            guard = condition_ids[0]
            for extra in condition_ids[1:]:
                guard = emit(
                    "logic.and", inputs=(guard, extra), type="series<bool>"
                )
            intent_ids: list[str] = []
            for intent in self.intents:
                attrs: dict = {"action": intent.action}
                if intent.asset is not None:
                    attrs["asset"] = intent.asset
                if intent.confidence is not None:
                    attrs["confidence"] = intent.confidence
                intent_ids.append(emit("intent.emit", attrs=attrs))
            block_id = emit("block", inputs=tuple(intent_ids))
            entries.append(emit("rule", inputs=(schedule_id, guard, block_id)))

        for agent in self.agents:
            emit("agent", attrs={"name": agent.name})

        return NanoModule(
            name=self.name,
            tier="nano",
            effects=self.effects,
            nodes=tuple(nodes),
            entries=tuple(entries),
            inputs=(),
            signals=tuple(signals),
            warmup=0,
            provenance={"liftedFrom": NANO_IR_VERSION_BASELINE},
        )
