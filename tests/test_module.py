"""Load-time validation of v1.0 IR — the security boundary.

`nano/ir/module.py` claims that manifest violations, tier violations, cycles,
future-reading offsets, and a broken determinism contract are *load-time
rejections, never runtime surprises*. This file is where that claim is kept
honest.

It exists because a mutation run proved it was needed: disabling the
effect-manifest check, the tier check, the forward-reference check, the
`series.index` offset check, and the fastmath refusal each left the entire suite
green. A guard nothing tests is a guard that quietly stops working, and these are
the ones standing between a hand-written or model-generated document and the VM.

Documents here are built as raw dicts on purpose. Going through the compiler could
only ever produce valid IR, which tests the compiler rather than the loader — and
the loader's whole job is to be the part that does not trust its input.
"""

import pytest

from nano.ir import NanoModule, StrategyGraph, load, load_module
from nano.ir.schema import IRValidationError, ManifestViolation, TierViolation

BASELINE = {
    "type": "Strategy",
    "nanoIrVersion": "0.1.0",
    "name": "S",
    "effects": ["intent.emit", "log.append"],
    "nodes": [
        {"type": "Schedule", "interval": "5m"},
        {"type": "Condition", "signal": "RSI", "operator": "<", "value": 30},
        {"type": "Intent", "action": "BUY"},
    ],
}


def document(**overrides):
    """A minimal valid v1.0 document, with fields overridden per test."""
    base = {
        "type": "Strategy",
        "nanoIrVersion": "1.0.0",
        "tier": "nano",
        "name": "S",
        "effects": ["log.append"],
        "nodes": [{"id": "n1", "op": "const", "inputs": [], "attrs": {"value": 1}}],
        "entries": [],
    }
    base.update(overrides)
    return base


def _nodes(*specs):
    """Build a node list from (id, op, inputs, attrs) tuples."""
    return [
        {"id": node_id, "op": op, "inputs": list(inputs), "attrs": dict(attrs)}
        for node_id, op, inputs, attrs in specs
    ]


# -- the document envelope ----------------------------------------------------


def test_a_minimal_document_loads():
    module = NanoModule.from_dict(document())
    assert module.name == "S"
    assert module.tier == "nano"


def test_round_trip_is_a_fixed_point():
    first = NanoModule.from_dict(document())
    assert NanoModule.from_dict(first.to_dict()).to_dict() == first.to_dict()


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"type": "Loop"}, "must be 'Strategy'"),
        ({"nanoIrVersion": "0.2.0"}, "unsupported by NanoModule"),
        ({"name": ""}, "non-empty 'name'"),
        ({"tier": "nano+++"}, "Unknown tier"),
        ({"effects": []}, "non-empty 'effects'"),
        ({"effects": ["intent.emit", "wire.transfer"]}, "Unknown effects declared"),
        ({"nodes": []}, "non-empty 'nodes'"),
        ({"warmup": -1}, "non-negative integer"),
        ({"entries": ["nope"]}, "not a declared node"),
    ],
)
def test_malformed_envelope_is_rejected(overrides, expected):
    with pytest.raises(IRValidationError, match=expected):
        NanoModule.from_dict(document(**overrides))


def test_baseline_documents_are_refused_with_a_pointer_to_the_right_loader():
    with pytest.raises(IRValidationError, match="nano.ir.graph.StrategyGraph"):
        NanoModule.from_dict(document(nanoIrVersion="0.1.0"))


# -- effects are a capability grant -------------------------------------------


def test_an_intent_without_the_effect_is_a_load_time_rejection():
    """The capability boundary: a graph that never declared `intent.emit` cannot
    propose an action, whatever its nodes say."""
    with pytest.raises(ManifestViolation, match=r"intent\.emit"):
        NanoModule.from_dict(
            document(
                effects=["log.append"],
                nodes=_nodes(("n1", "intent.emit", (), {"action": "BUY"})),
            )
        )


def test_declaring_the_effect_admits_the_node():
    module = NanoModule.from_dict(
        document(
            effects=["intent.emit", "log.append"],
            nodes=_nodes(("n1", "intent.emit", (), {"action": "BUY"})),
        )
    )
    assert module.of_op("intent.emit")


@pytest.mark.parametrize(
    "op, attrs, effect",
    [
        ("llmre.escalate", {"target": "desk"}, r"llmre\.escalate"),
        ("ai.infer", {"signature": "Bias"}, r"llm\.call"),
    ],
)
def test_reasoning_effects_are_gated_the_same_way(op, attrs, effect):
    with pytest.raises(ManifestViolation, match=effect):
        NanoModule.from_dict(
            document(
                tier="nano+", effects=["log.append"], nodes=_nodes(("n1", op, (), attrs))
            )
        )


# -- tier gates constructs ----------------------------------------------------


def test_a_nano_tier_module_cannot_contain_reasoning():
    """Reading `tier nano` should tell an auditor there is no model in the loop."""
    with pytest.raises(TierViolation, match=r"requires tier 'nano\+'"):
        NanoModule.from_dict(
            document(
                tier="nano",
                effects=["llmre.escalate", "log.append"],
                nodes=_nodes(("n1", "llmre.escalate", (), {"target": "desk"})),
            )
        )


def test_declaring_the_tier_admits_reasoning():
    module = NanoModule.from_dict(
        document(
            tier="nano+",
            effects=["llmre.escalate", "log.append"],
            nodes=_nodes(("n1", "llmre.escalate", (), {"target": "desk"})),
        )
    )
    assert module.tier == "nano+"


# -- the graph must be acyclic and ordered ------------------------------------


def test_a_forward_reference_is_rejected():
    """Backward-only references let the VM evaluate in declaration order with no
    cycle check and no way to loop forever."""
    with pytest.raises(IRValidationError, match="before it is defined"):
        NanoModule.from_dict(
            document(
                nodes=_nodes(
                    ("n1", "logic.not", ("n2",), {}),
                    ("n2", "const", (), {"value": True}),
                )
            )
        )


def test_a_self_reference_is_rejected():
    with pytest.raises(IRValidationError, match="before it is defined"):
        NanoModule.from_dict(document(nodes=_nodes(("n1", "logic.not", ("n1",), {}))))


def test_duplicate_node_ids_are_rejected():
    with pytest.raises(IRValidationError, match="Duplicate node id"):
        NanoModule.from_dict(
            document(
                nodes=_nodes(
                    ("n1", "const", (), {"value": 1}),
                    ("n1", "const", (), {"value": 2}),
                )
            )
        )


def test_unknown_opcodes_are_rejected():
    with pytest.raises(IRValidationError, match="Unknown node op"):
        NanoModule.from_dict(document(nodes=_nodes(("n1", "order.submit", (), {}))))


def test_wrong_operand_count_is_rejected():
    with pytest.raises(IRValidationError, match="takes 2 input"):
        NanoModule.from_dict(
            document(
                nodes=_nodes(
                    ("n1", "const", (), {"value": 1}),
                    ("n2", "compare.lt", ("n1",), {}),
                )
            )
        )


# -- look-ahead cannot be reintroduced by hand --------------------------------


def test_a_negative_offset_in_a_handwritten_document_is_rejected():
    """The compiler cannot emit this, so reaching it means a hand-edited or
    model-generated document is trying to read the future. The loader is the last
    line before the VM."""
    with pytest.raises(IRValidationError, match="non-negative integer 'offset'"):
        NanoModule.from_dict(
            document(
                nodes=_nodes(
                    ("n1", "feed.signal", (), {"name": "close"}),
                    ("n2", "series.index", ("n1",), {"offset": -1}),
                )
            )
        )


def test_a_non_integer_offset_is_rejected():
    with pytest.raises(IRValidationError, match="non-negative integer 'offset'"):
        NanoModule.from_dict(
            document(
                nodes=_nodes(
                    ("n1", "feed.signal", (), {"name": "close"}),
                    ("n2", "series.index", ("n1",), {"offset": 1.5}),
                )
            )
        )


def test_a_zero_offset_is_the_current_bar_and_is_fine():
    module = NanoModule.from_dict(
        document(
            nodes=_nodes(
                ("n1", "feed.signal", (), {"name": "close"}),
                ("n2", "series.index", ("n1",), {"offset": 0}),
            )
        )
    )
    assert module.node("n2").attrs["offset"] == 0


# -- the determinism contract -------------------------------------------------


def test_fastmath_is_refused_outright():
    """There is no flag to accept it. A module whose numbers drift between runs
    cannot honour any of Nano's other guarantees."""
    with pytest.raises(IRValidationError, match="breaks bit-identical replay"):
        NanoModule.from_dict(
            document(
                determinism={
                    "clock": "injected",
                    "entropy": "injected",
                    "fastmath": True,
                }
            )
        )


@pytest.mark.parametrize("key", ["clock", "entropy"])
def test_ambient_clock_or_entropy_is_refused(key):
    contract = {"clock": "injected", "entropy": "injected", "fastmath": False}
    contract[key] = "ambient"
    with pytest.raises(IRValidationError, match=f"determinism.{key}"):
        NanoModule.from_dict(document(determinism=contract))


# -- attribute validation per opcode ------------------------------------------


@pytest.mark.parametrize(
    "op, attrs, expected",
    [
        ("intent.emit", {"action": "HODL"}, "expected one of"),
        ("intent.emit", {"action": "BUY", "confidence": 1.5}, r"within \[0, 1\]"),
        ("intent.emit", {"action": "BUY", "asset": ""}, "non-empty string"),
        ("schedule", {}, "non-empty 'interval'"),
        ("feed.signal", {}, "non-empty 'name'"),
        ("agent", {"name": "A", "role": "overlord"}, "unknown agent role"),
        ("indicator", {"name": "EMA", "periods": [0]}, "positive integers"),
        ("indicator", {"name": "EMA", "lookback": -1}, "non-negative integer"),
        ("risk.limits", {"limits": {}}, "non-empty 'limits'"),
        ("risk.limits", {"limits": {"max_yolo": 1}}, "unknown risk limit"),
        ("risk.limits", {"limits": {"max_daily_loss": "big"}}, "must be numeric"),
        ("risk.limits", {"limits": {"max_daily_loss": True}}, "must be numeric"),
        # The type checker range-checks a `risk { ... }` block, but raw IR never
        # went through it. These four are the shapes that make the *runtime* gate
        # misbehave rather than merely look odd: a negative ceiling nothing can
        # satisfy, a confidence floor above 1 that suppresses every intent
        # forever, a limit no comparison can order, and a fractional count.
        ("risk.limits", {"limits": {"max_drawdown": -0.1}}, "fraction of equity"),
        ("risk.limits", {"limits": {"min_confidence": 5}}, r"\[0.0, 1.0\]"),
        ("risk.limits", {"limits": {"max_drawdown": float("nan")}}, "finite number"),
        ("risk.limits", {"limits": {"max_open_positions": 2.5}}, "whole number"),
        ("const", {}, "requires a 'value'"),
    ],
)
def test_opcode_attributes_are_validated(op, attrs, expected):
    effects = ["intent.emit", "log.append"] if op == "intent.emit" else ["log.append"]
    with pytest.raises(IRValidationError, match=expected):
        NanoModule.from_dict(
            document(effects=effects, nodes=_nodes(("n1", op, (), attrs)))
        )


@pytest.mark.parametrize(
    "limits",
    [
        {"min_confidence": 5, "max_drawdown": -1},
        {"max_drawdown": -1, "min_confidence": 5},
    ],
    ids=["confidence-first", "drawdown-first"],
)
def test_a_document_with_two_bad_limits_names_the_same_one_first(limits):
    """Which rejection an auditor is shown must not depend on key order.

    A risk block reaches the loader as a JSON object, and object key order is a
    serialisation accident — the same module round-tripped through two hosts can
    arrive with its limits spelled in either order. Validation walks them in the
    schema's order so the diagnostic is a property of the document rather than of
    whoever last serialised it.
    """
    with pytest.raises(IRValidationError, match="max_drawdown"):
        NanoModule.from_dict(
            document(nodes=_nodes(("n1", "risk.limits", (), {"limits": limits})))
        )


@pytest.mark.parametrize(
    "limits",
    [
        {"max_yolo": 1, "max_fomo": 1},
        {"max_fomo": 1, "max_yolo": 1},
    ],
    ids=["yolo-first", "fomo-first"],
)
def test_a_document_with_two_unknown_limits_names_the_same_one_first(limits):
    """The sibling of the test above, for the loop that runs before it.

    Known limits are walked in the schema's order; unknown ones have no place in
    that order, so they are walked by name. Both loops exist to keep the
    diagnostic a property of the document rather than of whichever host
    serialised it last, and leaving one of them on document order would have kept
    exactly half the defect.
    """
    with pytest.raises(IRValidationError, match="max_fomo"):
        NanoModule.from_dict(
            document(nodes=_nodes(("n1", "risk.limits", (), {"limits": limits})))
        )


# -- version dispatch ---------------------------------------------------------


def test_load_dispatches_on_version():
    assert isinstance(load(BASELINE), StrategyGraph)
    assert isinstance(load(document()), NanoModule)


def test_load_module_lifts_baseline_so_runtimes_need_one_path():
    module = load_module(BASELINE)
    assert isinstance(module, NanoModule)
    assert module.provenance["liftedFrom"] == "0.1.0"
    assert module.entries  # the rule survived the lift


def test_an_unknown_version_is_rejected_rather_than_guessed():
    with pytest.raises(IRValidationError, match="unsupported"):
        load({"type": "Strategy", "nanoIrVersion": "9.9.9", "nodes": []})


# -- the boundary holds against direct construction ---------------------------


def _handbuilt_module(**overrides):
    """A module assembled by hand, skipping ``from_dict`` entirely."""
    from nano.ir.module import IRNode

    fields = {
        "name": "Sneak",
        "tier": "nano",
        "effects": ("log.append",),
        "nodes": (
            IRNode(id="n1", op="schedule", attrs={"interval": "1m"}),
            IRNode(id="n2", op="const", attrs={"value": True}),
            IRNode(id="n3", op="intent.emit", attrs={"action": "BUY"}),
            IRNode(id="n4", op="block", inputs=("n3",)),
            IRNode(id="n5", op="rule", inputs=("n1", "n2", "n4")),
        ),
        "entries": ("n5",),
    }
    fields.update(overrides)
    return NanoModule(**fields)


def test_validate_catches_a_module_that_never_went_through_the_loader():
    """``NanoModule`` is a frozen dataclass, so in-process code can build one and
    skip ``from_dict``. An audit did exactly that and ran an ungranted
    `intent.emit`. ``validate()`` is what makes the boundary real."""
    with pytest.raises(ManifestViolation, match=r"intent\.emit"):
        _handbuilt_module().validate()


def test_the_vm_refuses_a_hand_built_module_with_an_ungranted_effect():
    from nano.runtime.interpreter import MarketFrame
    from nano.runtime.vm import run_module

    frame = MarketFrame(timestamps=(0,), signals={})
    with pytest.raises(ManifestViolation, match=r"intent\.emit"):
        run_module(_handbuilt_module(), frame)


def test_a_correctly_declared_hand_built_module_still_runs():
    # The check rejects an ungranted capability, not hand construction itself --
    # `to_module()` builds modules directly and must keep working.
    from nano.runtime.interpreter import MarketFrame
    from nano.runtime.vm import run_module

    module = _handbuilt_module(effects=("intent.emit", "log.append"))
    result = run_module(module, MarketFrame(timestamps=(0,), signals={}))
    assert [i.action for i in result.intents] == ["BUY"]


def test_run_frames_validates_once_and_still_rejects():
    from nano.runtime.interpreter import MarketFrame
    from nano.runtime.vm import run_frames

    frames = [MarketFrame(timestamps=(0,), signals={})]
    with pytest.raises(ManifestViolation):
        run_frames(_handbuilt_module(), frames)


def test_duplicate_effects_are_rejected():
    # Two byte-different documents granting the same capability would make
    # moduleHash depend on manifest spelling rather than meaning.
    with pytest.raises(IRValidationError, match="Duplicate effects"):
        NanoModule.from_dict(document(effects=["log.append", "log.append"]))
