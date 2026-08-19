"""NanoModule — the v1.0.0 IR document, and the canonical executable form.

Where baseline IR is a flat bag of typed records, v1.0 is a real **DAG**: every
node has an id, an opcode, typed operand references, and a compile-time
attribute dictionary. That shape is what makes the rest of the platform possible
— a graph can be rendered, partitioned, indexed, diffed, and content-addressed;
a bag of records can only be replayed.

Three properties are enforced at load time, not discovered at run time:

**References point backwards.** A node may only name ids already defined above
it. Cycles and forward references are rejected here, so no evaluator needs a
cycle check and none can loop forever.

**Effects are a capability grant.** An `intent.emit` node in a module that never
declared `intent.emit` is a load-time rejection. Same for `llmre.escalate` and
`ai.infer`. A runtime bug above this layer cannot manufacture a capability the
module did not ask for.

**Tier gates constructs.** A `tier nano` module containing a reasoning call is
rejected, so the entry language stays small and an auditor reading `tier nano`
knows there is no model in the loop.

`moduleHash` is a content address over the canonical form with the hash field
itself removed — a document cannot commit to its own digest. `sourceHash` is
separate and covers the `.nano` text, so "same source" and "same compiled graph"
stay independently checkable: a comment-only edit changes one and not the other.

Baseline documents are not a second dialect to maintain. ``StrategyGraph`` lifts
into a NanoModule (see ``nano/ir/graph.py``), the VM evaluates modules only, and
a conformance test asserts the two paths agree bar for bar. One evaluator, two
front doors.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Set, Tuple

from .schema import (
    AGENT_ROLES,
    INTEGER_RISK_LIMITS,
    INTENT_ACTIONS,
    IRValidationError,
    KNOWN_EFFECTS,
    ManifestViolation,
    NANO_IR_VERSION_1_0,
    RISK_LIMITS,
    TIER_REQUIREMENTS,
    TIERS,
    TierViolation,
)

COMPILER_NAME = "nnc"
COMPILER_VERSION = "1.0.0"

# The determinism contract every module this compiler emits must satisfy. A
# runtime that cannot honour it is expected to refuse the module rather than
# execute it approximately.
DETERMINISM_CONTRACT: Mapping[str, Any] = {
    "clock": "injected",
    "entropy": "injected",
    "fastmath": False,
}


@dataclass(frozen=True)
class OpSpec:
    """What one opcode requires: operand count, capability, and tier."""

    min_inputs: int = 0
    max_inputs: Optional[int] = 0
    effect: Optional[str] = None
    construct: Optional[str] = None


# `max_inputs=None` means variadic. `construct` keys into TIER_REQUIREMENTS.
OPS: Mapping[str, OpSpec] = {
    # data sources
    "input.ref": OpSpec(),
    "param.ref": OpSpec(),
    "feed.signal": OpSpec(),
    "const": OpSpec(),
    "builtin.confidence": OpSpec(),
    # derivation
    "let": OpSpec(min_inputs=1, max_inputs=1),
    "series.index": OpSpec(min_inputs=1, max_inputs=1),
    "record.field": OpSpec(min_inputs=1, max_inputs=1),
    "indicator": OpSpec(min_inputs=0, max_inputs=None),
    # arithmetic and logic
    "arith.add": OpSpec(min_inputs=2, max_inputs=2),
    "arith.sub": OpSpec(min_inputs=2, max_inputs=2),
    "arith.mul": OpSpec(min_inputs=2, max_inputs=2),
    "arith.div": OpSpec(min_inputs=2, max_inputs=2),
    "arith.mod": OpSpec(min_inputs=2, max_inputs=2),
    "arith.neg": OpSpec(min_inputs=1, max_inputs=1),
    "compare.lt": OpSpec(min_inputs=2, max_inputs=2),
    "compare.le": OpSpec(min_inputs=2, max_inputs=2),
    "compare.gt": OpSpec(min_inputs=2, max_inputs=2),
    "compare.ge": OpSpec(min_inputs=2, max_inputs=2),
    "compare.eq": OpSpec(min_inputs=2, max_inputs=2),
    "compare.ne": OpSpec(min_inputs=2, max_inputs=2),
    "logic.and": OpSpec(min_inputs=2, max_inputs=2),
    "logic.or": OpSpec(min_inputs=2, max_inputs=2),
    "logic.not": OpSpec(min_inputs=1, max_inputs=1),
    # control flow
    "schedule": OpSpec(),
    "block": OpSpec(min_inputs=0, max_inputs=None),
    # rule inputs are [schedule, condition, thenBlock] plus an optional
    # elseBlock. Branches are real operands rather than ids buried in attrs, so
    # the graph stays walkable by anything that understands `inputs`.
    "rule": OpSpec(min_inputs=3, max_inputs=4),
    # effects and declarations
    "intent.emit": OpSpec(effect="intent.emit"),
    "llmre.escalate": OpSpec(effect="llmre.escalate", construct="escalate"),
    "ai.signature": OpSpec(construct="signature"),
    "ai.infer": OpSpec(
        min_inputs=0, max_inputs=None, effect="llm.call", construct="infer"
    ),
    "route": OpSpec(min_inputs=2, max_inputs=2, construct="route"),
    "risk.limits": OpSpec(),
    "agent": OpSpec(),
}

# Operator spellings shared with codegen, so the two cannot disagree about what
# `<` or `+` lowers to.
COMPARISON_OPS: Mapping[str, str] = {
    "<": "compare.lt",
    "<=": "compare.le",
    ">": "compare.gt",
    ">=": "compare.ge",
    "==": "compare.eq",
    "!=": "compare.ne",
}

ARITHMETIC_OPS: Mapping[str, str] = {
    "+": "arith.add",
    "-": "arith.sub",
    "*": "arith.mul",
    "/": "arith.div",
    "%": "arith.mod",
}


@dataclass(frozen=True)
class IRNode:
    """One node of the graph. Immutable, like everything downstream of the parser."""

    id: str
    op: str
    inputs: Tuple[str, ...] = ()
    attrs: Mapping[str, Any] = field(default_factory=dict)
    type: Optional[str] = None

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "IRNode":
        node_id = data.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise IRValidationError("Every node requires a non-empty 'id'")
        op = data.get("op")
        if op not in OPS:
            raise IRValidationError(f"Unknown node op {op!r} in node {node_id!r}")

        inputs = data.get("inputs", [])
        if not isinstance(inputs, list) or not all(isinstance(i, str) for i in inputs):
            raise IRValidationError(
                f"Node {node_id!r} 'inputs' must be a list of node ids"
            )
        attrs = data.get("attrs", {})
        if not isinstance(attrs, dict):
            raise IRValidationError(f"Node {node_id!r} 'attrs' must be an object")

        node_type = data.get("type")
        if node_type is not None and not isinstance(node_type, str):
            raise IRValidationError(f"Node {node_id!r} 'type' must be a string")

        spec = OPS[op]
        count = len(inputs)
        if count < spec.min_inputs or (
            spec.max_inputs is not None and count > spec.max_inputs
        ):
            if spec.max_inputs is None:
                expected = f"at least {spec.min_inputs}"
            elif spec.min_inputs == spec.max_inputs:
                expected = str(spec.min_inputs)
            else:
                expected = f"{spec.min_inputs}..{spec.max_inputs}"
            raise IRValidationError(
                f"Node {node_id!r} ({op}) takes {expected} input(s), got {count}"
            )

        return IRNode(
            id=node_id, op=op, inputs=tuple(inputs), attrs=dict(attrs), type=node_type
        )

    def to_dict(self) -> dict:
        out: dict = {"id": self.id, "op": self.op, "inputs": list(self.inputs)}
        if self.attrs:
            out["attrs"] = dict(self.attrs)
        if self.type is not None:
            out["type"] = self.type
        return out


@dataclass(frozen=True)
class ParamDecl:
    name: str
    type: str
    value: Any

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type, "value": self.value}


@dataclass(frozen=True)
class InputDecl:
    name: str
    type: str

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type}


# ---------------------------------------------------------------------------
# small validation helpers
# ---------------------------------------------------------------------------


def _require_string_list(value: Any, what: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise IRValidationError(f"{what} must be a list of strings")
    return tuple(value)


def _require_object_list(value: Any, what: str) -> Sequence[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(v, Mapping) for v in value):
        raise IRValidationError(f"{what} must be a list of objects")
    return value


def _require_text(data: Mapping[str, Any], key: str, what: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise IRValidationError(f"{what} requires a non-empty {key!r}")
    return value


def _require_non_negative_int(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise IRValidationError(f"{what} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class NanoModule:
    """A validated v1.0.0 Nano IR document."""

    name: str
    tier: str
    effects: Tuple[str, ...]
    nodes: Tuple[IRNode, ...]
    entries: Tuple[str, ...]
    params: Tuple[ParamDecl, ...] = ()
    inputs: Tuple[InputDecl, ...] = ()
    signals: Tuple[str, ...] = ()
    warmup: int = 0
    determinism: Mapping[str, Any] = field(
        default_factory=lambda: dict(DETERMINISM_CONTRACT)
    )
    provenance: Mapping[str, Any] = field(default_factory=dict)

    # -- lookup ------------------------------------------------------------

    def index(self) -> Dict[str, IRNode]:
        """Every node by id. Build once and reuse when walking the graph."""
        return {node.id: node for node in self.nodes}

    def node(self, node_id: str) -> IRNode:
        for candidate in self.nodes:
            if candidate.id == node_id:
                return candidate
        raise KeyError(node_id)

    def of_op(self, op: str) -> Tuple[IRNode, ...]:
        return tuple(node for node in self.nodes if node.op == op)

    @property
    def source_hash(self) -> Optional[str]:
        value = self.provenance.get("sourceHash")
        return value if isinstance(value, str) else None

    # -- load --------------------------------------------------------------

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "NanoModule":
        if data.get("type") != "Strategy":
            raise IRValidationError("Root 'type' must be 'Strategy'")
        version = data.get("nanoIrVersion")
        if version != NANO_IR_VERSION_1_0:
            raise IRValidationError(
                f"nanoIrVersion {version!r} unsupported by NanoModule "
                f"(expected {NANO_IR_VERSION_1_0!r}; load 0.1.0 documents with "
                "nano.ir.graph.StrategyGraph)"
            )
        name = _require_text(data, "name", "Strategy")

        tier = data.get("tier", "nano")
        if tier not in TIERS:
            raise IRValidationError(
                f"Unknown tier {tier!r} (expected one of {', '.join(TIERS)})"
            )

        effects_raw = data.get("effects")
        if not isinstance(effects_raw, list) or not effects_raw:
            raise IRValidationError("Strategy requires a non-empty 'effects' manifest")
        unknown = set(effects_raw) - KNOWN_EFFECTS
        if unknown:
            raise IRValidationError(f"Unknown effects declared: {sorted(unknown)}")
        if len(set(effects_raw)) != len(effects_raw):
            # A manifest is a set of granted capabilities. Allowing a repeat would
            # let two byte-different documents grant exactly the same thing, which
            # makes `moduleHash` depend on manifest spelling rather than meaning.
            raise IRValidationError(
                f"Duplicate effects declared: {sorted(effects_raw)}"
            )
        effects = tuple(effects_raw)

        determinism = data.get("determinism", dict(DETERMINISM_CONTRACT))
        if not isinstance(determinism, dict):
            raise IRValidationError("'determinism' must be an object")
        if determinism.get("fastmath"):
            # Result-changing float re-association makes replay a lottery. There
            # is no flag to accept it, because a module whose numbers drift
            # between runs cannot honour any of Nano's other guarantees.
            raise IRValidationError(
                "fastmath is not permitted: it breaks bit-identical replay"
            )
        for key in ("clock", "entropy"):
            if determinism.get(key, "injected") != "injected":
                raise IRValidationError(
                    f"determinism.{key} must be 'injected' — Nano reads no ambient "
                    "clock or entropy"
                )

        nodes_raw = data.get("nodes")
        if not isinstance(nodes_raw, list) or not nodes_raw:
            raise IRValidationError("Strategy requires a non-empty 'nodes' list")

        seen: Set[str] = set()
        nodes: list[IRNode] = []
        for node_data in nodes_raw:
            if not isinstance(node_data, Mapping):
                raise IRValidationError("Each node must be an object")
            node = IRNode.from_dict(node_data)
            if node.id in seen:
                raise IRValidationError(f"Duplicate node id {node.id!r}")
            for reference in node.inputs:
                if reference not in seen:
                    raise IRValidationError(
                        f"Node {node.id!r} references {reference!r} before it is "
                        "defined (the graph must be acyclic and topologically "
                        "ordered)"
                    )
            spec = OPS[node.op]
            if spec.effect is not None and spec.effect not in effects:
                raise ManifestViolation(
                    f"Node {node.id!r} ({node.op}) needs effect {spec.effect!r}, "
                    "which the manifest does not declare"
                )
            if spec.construct is not None:
                required = TIER_REQUIREMENTS.get(spec.construct)
                if required is not None and TIERS.index(tier) < TIERS.index(required):
                    raise TierViolation(
                        f"Node {node.id!r} ({node.op}) requires tier {required!r}, "
                        f"but the module declares {tier!r}"
                    )
            _validate_attrs(node)
            seen.add(node.id)
            nodes.append(node)

        entries = _require_string_list(data.get("entries", []), "'entries'")
        for entry in entries:
            if entry not in seen:
                raise IRValidationError(f"Entry {entry!r} is not a declared node")

        params = tuple(
            ParamDecl(
                name=_require_text(p, "name", "Param"),
                type=_require_text(p, "type", "Param"),
                value=p.get("value"),
            )
            for p in _require_object_list(data.get("params", []), "'params'")
        )
        inputs = tuple(
            InputDecl(
                name=_require_text(i, "name", "Input"),
                type=_require_text(i, "type", "Input"),
            )
            for i in _require_object_list(data.get("inputs", []), "'inputs'")
        )

        provenance = data.get("provenance", {})
        if not isinstance(provenance, dict):
            raise IRValidationError("'provenance' must be an object")

        return NanoModule(
            name=name,
            tier=tier,
            effects=effects,
            nodes=tuple(nodes),
            entries=entries,
            params=params,
            inputs=inputs,
            signals=_require_string_list(data.get("signals", []), "'signals'"),
            warmup=_require_non_negative_int(data.get("warmup", 0), "'warmup'"),
            determinism=dict(determinism),
            provenance=dict(provenance),
        )

    def validate(self) -> "NanoModule":
        """Re-run load-time validation, returning the validated module.

        ``from_dict`` is the trust boundary, but ``NanoModule`` is a frozen
        dataclass, so in-process code can construct one directly and skip it —
        and a module built that way could carry an `intent.emit` node its manifest
        never granted. That is not a hypothetical: an audit built exactly such a
        module and ran it.

        So the boundary is enforced where it matters rather than merely
        documented: the VM calls this before executing. Cost is one pass over the
        nodes, against an evaluation that is nodes × bars — the check disappears
        into the noise of the work it protects.
        """
        return NanoModule.from_dict(self.to_dict(include_hash=False))

    # -- serialise ---------------------------------------------------------

    def to_dict(self, *, include_hash: bool = True) -> dict:
        """The canonical document. Key order is fixed so output stays diffable."""
        out: dict = {
            "type": "Strategy",
            "nanoIrVersion": NANO_IR_VERSION_1_0,
            "tier": self.tier,
            "name": self.name,
            "effects": list(self.effects),
            "determinism": dict(self.determinism),
        }
        if self.params:
            out["params"] = [p.to_dict() for p in self.params]
        if self.inputs:
            out["inputs"] = [i.to_dict() for i in self.inputs]
        if self.signals:
            out["signals"] = list(self.signals)
        if self.warmup:
            out["warmup"] = self.warmup
        out["nodes"] = [n.to_dict() for n in self.nodes]
        out["entries"] = list(self.entries)
        if self.provenance:
            out["provenance"] = dict(self.provenance)
        if include_hash:
            out["moduleHash"] = self.content_hash()
        return out

    def content_hash(self) -> str:
        """Content address of the executable graph.

        Two modules share a hash exactly when they would execute identically. That
        property fixes what the digest may cover: the graph and its declarations,
        and nothing else.

        Two exclusions follow from it. The hash field itself is out, because a
        document cannot commit to its own digest. `provenance` is out too — it
        records *where a module came from*, not what it does, and `sourceHash`
        lives there. Including it would make a comment-only edit change the
        module hash, collapsing the distinction between "the file was edited" and
        "the behavior was edited" that having two hashes exists to draw.
        """
        document = self.to_dict(include_hash=False)
        document.pop("provenance", None)
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# attribute validation
# ---------------------------------------------------------------------------

# Opcodes whose behaviour is keyed by an `attrs["name"]`. Shared with the CLI
# renderer, which labels them the same way -- two copies of this set would drift
# the moment a named opcode is added.
NAMED_OPS = frozenset({"input.ref", "param.ref", "feed.signal", "let", "agent"})

# Position of each risk limit in the declared vocabulary. Validation walks a
# document's limits in this order, so which rejection an auditor is shown first
# does not depend on the order the document happened to serialise its keys in.
_RISK_LIMIT_ORDER = {name: index for index, name in enumerate(RISK_LIMITS)}


def _validate_attrs(node: IRNode) -> None:
    """Check the attributes each opcode's behavior depends on.

    Only opcodes driven by an attribute are checked. An evaluator should never
    have to ask whether an intent has an action.
    """
    attrs = node.attrs

    if node.op == "schedule":
        _require_text(attrs, "interval", f"Node {node.id!r} (schedule)")
        return

    if node.op in NAMED_OPS:
        _require_text(attrs, "name", f"Node {node.id!r} ({node.op})")
        if node.op == "agent":
            role = attrs.get("role")
            if role is not None and role not in AGENT_ROLES:
                raise IRValidationError(
                    f"Node {node.id!r} declares unknown agent role {role!r}"
                )
        return

    if node.op == "const":
        if "value" not in attrs:
            raise IRValidationError(f"Node {node.id!r} (const) requires a 'value'")
        return

    if node.op == "series.index":
        offset = attrs.get("offset")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            # The compiler cannot emit a negative offset, so reaching this means a
            # hand-written or generated document is trying to read the future.
            raise IRValidationError(
                f"Node {node.id!r} (series.index) requires a non-negative integer "
                "'offset' — offsets count backwards from the current bar"
            )
        return

    if node.op == "record.field":
        _require_text(attrs, "field", f"Node {node.id!r} (record.field)")
        return

    if node.op == "indicator":
        _require_text(attrs, "name", f"Node {node.id!r} (indicator)")
        periods = attrs.get("periods", [])
        if not isinstance(periods, list) or not all(
            isinstance(p, int) and not isinstance(p, bool) and p >= 1 for p in periods
        ):
            raise IRValidationError(
                f"Node {node.id!r} (indicator) 'periods' must be positive integers"
            )
        _require_non_negative_int(
            attrs.get("lookback", 0), f"Node {node.id!r} (indicator) 'lookback'"
        )
        return

    if node.op == "intent.emit":
        action = attrs.get("action")
        if action not in INTENT_ACTIONS:
            raise IRValidationError(
                f"Node {node.id!r} declares intent action {action!r}, expected one "
                f"of {sorted(INTENT_ACTIONS)}"
            )
        asset = attrs.get("asset")
        if asset is not None and (not isinstance(asset, str) or not asset):
            raise IRValidationError(
                f"Node {node.id!r} 'asset' must be a non-empty string"
            )
        confidence = attrs.get("confidence")
        if confidence is not None:
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                raise IRValidationError(
                    f"Node {node.id!r} 'confidence' must be numeric"
                )
            if not 0.0 <= float(confidence) <= 1.0:
                raise IRValidationError(
                    f"Node {node.id!r} 'confidence' must be within [0, 1]"
                )
        return

    if node.op == "llmre.escalate":
        _require_text(attrs, "target", f"Node {node.id!r} (llmre.escalate)")
        return

    if node.op == "ai.signature":
        _require_text(attrs, "name", f"Node {node.id!r} (ai.signature)")
        if not attrs.get("outputs"):
            raise IRValidationError(
                f"Node {node.id!r} (ai.signature) requires at least one output"
            )
        return

    if node.op == "ai.infer":
        _require_text(attrs, "signature", f"Node {node.id!r} (ai.infer)")
        return

    if node.op == "route":
        _require_text(attrs, "name", f"Node {node.id!r} (route)")
        _require_text(attrs, "execute", f"Node {node.id!r} (route)")
        return

    if node.op == "risk.limits":
        limits = attrs.get("limits")
        if not isinstance(limits, dict) or not limits:
            raise IRValidationError(
                f"Node {node.id!r} (risk.limits) requires a non-empty 'limits' object"
            )
        for key in limits:
            if key not in RISK_LIMITS:
                raise IRValidationError(
                    f"Node {node.id!r} declares unknown risk limit {key!r}"
                )
        # Iterated in schema order rather than document order. The runtime reads
        # these limits and logs what they reject, so the order a document happens
        # to spell them in must not change which rejection an auditor sees first.
        for key in sorted(limits, key=_RISK_LIMIT_ORDER.__getitem__):
            value = limits[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise IRValidationError(
                    f"Node {node.id!r} risk limit {key!r} must be numeric"
                )
            if not math.isfinite(float(value)):
                # NaN sits below every threshold and infinity sits above every
                # one. A limit that cannot be compared is not a limit.
                raise IRValidationError(
                    f"Node {node.id!r} risk limit {key!r} must be a finite number"
                )
            unit, low, high = RISK_LIMITS[key]
            if key in INTEGER_RISK_LIMITS and float(value) != int(value):
                raise IRValidationError(
                    f"Node {node.id!r} risk limit {key!r} is measured in {unit} "
                    f"and must be a whole number, got {value}"
                )
            if value < low or (high is not None and value > high):
                # The type checker range-checks the `risk { ... }` block, but raw
                # IR never passed through it. Without this a hand-built document
                # could declare `max_drawdown -1` — a limit nothing can satisfy —
                # or `min_confidence 5`, which suppresses every intent forever.
                bound = f"[{low}, {high}]" if high is not None else f">= {low}"
                raise IRValidationError(
                    f"Node {node.id!r} risk limit {key!r} is measured in {unit} "
                    f"and must be {bound}, got {value}"
                )
        return

