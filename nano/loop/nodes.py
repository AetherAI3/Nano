"""Experimental Nano++ loop-document vocabulary.

These values describe and validate a separate loop data model. They do not
execute stages, invoke reasoning models, or provide autonomous coordination.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Tuple

# The universal lifecycle: observe -> propose -> compile -> execute ->
# measure -> verify -> admit -> repeat. Every stage is deterministic and
# recorded. Domains supply different node payloads; the stages never change.
LOOP_STAGES = frozenset(
    {
        "Observe",     # collect evidence from the environment
        "Infer",       # reasoning-model call (the only stochastic stage; logged)
        "Evaluate",    # classical scoring of proposals
        "Optimize",    # propose candidates (quantum-seeded or classical)
        "Mutate",      # produce a candidate change to the loop itself
        "Verify",      # replay / benchmark / regression / static / capability
        "Gate",        # policy + risk + provenance admission
        "Execute",     # act via a runtime (intents only; a gate disposed)
        "Pause",       # halt without teardown
        "Replay",      # re-run a recorded segment deterministically
        "Checkpoint",  # snapshot state for rollback
        "Rollback",    # restore a checkpoint
        "Escalate",    # hand control back to a reasoning model
        "Sign",        # bind a signed, timestamped provenance record
        "Benchmark",   # measure outcome quality
    }
)

# Stages whose payload requires a specific effect-manifest capability.
# Enforced at load time: the graph cannot use a stage it did not declare.
_STAGE_EFFECT_REQUIREMENTS = {
    "Execute": "intent.emit",
    "Sign": "sign.emit",
    "Escalate": "llmre.escalate",
}


class LoopValidationError(ValueError):
    """The document is not a valid Nano++ loop graph."""


@dataclass(frozen=True)
class LoopNode:
    id: str
    stage: str
    inputs: Tuple[str, ...] = ()
    attrs: Mapping[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "LoopNode":
        node_id = data.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise LoopValidationError("Loop node requires a non-empty 'id'")
        stage = data.get("stage")
        if stage not in LOOP_STAGES:
            raise LoopValidationError(f"Unknown loop stage {stage!r}")
        inputs = data.get("inputs", [])
        if not isinstance(inputs, list) or not all(isinstance(i, str) for i in inputs):
            raise LoopValidationError(f"Node {node_id!r} 'inputs' must be a list of ids")
        attrs = data.get("attrs", {})
        if not isinstance(attrs, dict):
            raise LoopValidationError(f"Node {node_id!r} 'attrs' must be an object")
        return LoopNode(
            id=node_id, stage=stage, inputs=tuple(inputs), attrs=dict(attrs)
        )

    def required_effect(self) -> str | None:
        """The manifest capability this node's stage/payload demands, if any."""
        if self.stage == "Optimize" and self.attrs.get("kind") == "quantum_seed":
            return "quantum.submit"
        return _STAGE_EFFECT_REQUIREMENTS.get(self.stage)

    def to_dict(self) -> dict:
        out: dict = {"id": self.id, "stage": self.stage, "inputs": list(self.inputs)}
        if self.attrs:
            out["attrs"] = dict(self.attrs)
        return out
