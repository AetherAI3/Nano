"""The Watchdog profile: a strict subset, an artifact, a receipt, and a replay.

A Watchdog is an ordinary Nano rule held to a narrower contract: it may propose
`PAUSE` or `OBSERVE` and nothing else, it may not reach a model, and every signal
it reads must appear in its declared signal contract. The last rule is the one
that is easy to miss — with no `input` declarations an unknown name is a host
feed signal (see ``nano/types/env.py``), so a typo compiles cleanly into a rule
that can never fire. For a directional strategy that is a missed trade. For a
control it is a policy gap that reports success.

The four fixtures below are the shape a host should recognise: `normal`,
`saturated`, `missing-input`, and `recovered`. Read the missing-input case
carefully — it is what separates this module from a threshold check.
"""

import dataclasses
import hashlib
import json

import pytest

from nano.compiler import compile_module
from nano.ir.module import OPS
from nano.runtime import MarketFrame, SignalFrame
from nano.watchdog import (
    UNAVAILABLE_POLICIES,
    WATCHDOG_INTENT_ACTIONS,
    WATCHDOG_OPS,
    WatchdogArtifactV1,
    WatchdogContractError,
    WatchdogProfileError,
    WatchdogReplayMismatch,
    WatchdogSignalSpecV1,
    WatchdogState,
    compile_watchdog,
    evaluate_watchdog,
    replay_watchdog,
    validate_watchdog,
)

# --------------------------------------------------------------------------
# a sample generic rule -- deliberately not a market rule
# --------------------------------------------------------------------------

QUEUE_WATCHDOG = """
strategy QueueSaturationWatchdog {

    agent OpsDesk

    every 1m {

        if QUEUE_SATURATION_PCT >= 90 {

            observe()

        }

    }

}
"""

SATURATION = WatchdogSignalSpecV1(
    name="QUEUE_SATURATION_PCT",
    unit="percent",
    source="host queue telemetry",
    required=True,
    freshness_limit_ms=60_000,
    description="Host work-queue depth as a percentage of its configured ceiling.",
    value_domain="0..100",
    normalization="depth / ceiling * 100, clamped to 0..100",
)


def sample_artifact(**overrides) -> WatchdogArtifactV1:
    """The sample rule compiled against its one-signal contract."""
    kwargs = {
        "watchdog_id": "queue-saturation",
        "revision": 3,
        "signals": (SATURATION,),
        "risk_class": "operational-advisory",
        "unavailable_policy": "HOLD",
    }
    kwargs.update(overrides)
    return compile_watchdog(QUEUE_WATCHDOG, **kwargs)


def frame(*values, start: int = 0, step: int = 60) -> MarketFrame:
    """A frame carrying the saturation series and nothing else."""
    timestamps = tuple(start + step * i for i in range(len(values)))
    return MarketFrame(
        timestamps=timestamps, signals={"QUEUE_SATURATION_PCT": tuple(values)}
    )


# --------------------------------------------------------------------------
# the profile is an allow-list
# --------------------------------------------------------------------------


def test_the_profile_allow_list_names_real_opcodes():
    """A typo in the allow-list would silently forbid a permitted construct."""
    assert WATCHDOG_OPS <= set(OPS)


def test_the_profile_denies_every_stochastic_construct():
    """Models may author a Watchdog; they may never sit inside one."""
    for op in ("ai.infer", "ai.signature", "llmre.escalate", "route"):
        assert op in OPS
        assert op not in WATCHDOG_OPS


def test_the_profile_permits_only_pause_and_observe():
    assert WATCHDOG_INTENT_ACTIONS == frozenset({"PAUSE", "OBSERVE"})


def test_a_directional_rule_is_outside_the_profile():
    source = """
    strategy Buyer {
        every 1m {
            if SIGNAL > 1 {
                buy(BTC, 0.9)
            }
        }
    }
    """
    module = compile_module(source)
    spec = dataclasses.replace(SATURATION, name="SIGNAL")
    with pytest.raises(WatchdogProfileError) as error:
        validate_watchdog(module, (spec,))
    assert "BUY" in str(error.value)


def test_a_reasoning_call_is_outside_the_profile():
    source = """
    tier nano+
    strategy Thinker {
        signature Judge {
            input depth: float
            output verdict: float
        }
        every 1m {
            if QUEUE_SATURATION_PCT >= 90 {
                observe()
            }
        }
    }
    """
    module = compile_module(source)
    with pytest.raises(WatchdogProfileError) as error:
        validate_watchdog(module, (SATURATION,))
    assert "ai.signature" in str(error.value)


def test_an_escalation_is_outside_the_profile():
    source = """
    tier nano+
    strategy Escalator {
        agent Desk
        every 1m {
            if QUEUE_SATURATION_PCT >= 90 {
                escalate Desk
            }
        }
    }
    """
    module = compile_module(source)
    with pytest.raises(WatchdogProfileError) as error:
        validate_watchdog(module, (SATURATION,))
    assert "llmre.escalate" in str(error.value)


def test_a_signal_the_contract_never_declared_is_rejected():
    """The whole reason this profile exists: a typo must not become a gap.

    `QUEUE_SATURATION_PTC` compiles perfectly well — an undeclared name is a host
    feed signal — and the resulting rule can never fire. Nothing in the base
    language objects, because nothing in the base language knows what the host
    promised to publish.
    """
    typo = QUEUE_WATCHDOG.replace("QUEUE_SATURATION_PCT", "QUEUE_SATURATION_PTC")
    module = compile_module(typo)
    with pytest.raises(WatchdogProfileError) as error:
        validate_watchdog(module, (SATURATION,))
    assert "QUEUE_SATURATION_PTC" in str(error.value)


def test_a_watchdog_declares_exactly_one_cadence():
    source = """
    strategy TwoClocks {
        every 1m {
            if QUEUE_SATURATION_PCT >= 90 {
                observe()
            }
        }
        every 5m {
            if QUEUE_SATURATION_PCT >= 95 {
                pause()
            }
        }
    }
    """
    module = compile_module(source)
    with pytest.raises(WatchdogProfileError) as error:
        validate_watchdog(module, (SATURATION,))
    assert "cadence" in str(error.value)


def test_the_sample_rule_passes_the_profile():
    validate_watchdog(compile_module(QUEUE_WATCHDOG), (SATURATION,))


# --------------------------------------------------------------------------
# contracts
# --------------------------------------------------------------------------


def test_a_signal_spec_requires_a_freshness_bound():
    """Nano reads no clock, so it cannot enforce freshness.

    It can still insist the author states the bound the host is expected to
    enforce, which is the difference between an unstated assumption and a
    delegated one.
    """
    with pytest.raises(WatchdogContractError):
        dataclasses.replace(SATURATION, freshness_limit_ms=0)


def test_a_signal_spec_requires_a_name_a_source_and_a_description():
    for name, value in (("name", ""), ("source", ""), ("description", "")):
        with pytest.raises(WatchdogContractError):
            dataclasses.replace(SATURATION, **{name: value})


def test_an_unavailable_policy_outside_the_vocabulary_is_rejected():
    assert UNAVAILABLE_POLICIES == ("HOLD", "OBSERVE_ONLY")
    with pytest.raises(WatchdogContractError):
        sample_artifact(unavailable_policy="CARRY_ON")


def test_a_duplicated_signal_declaration_is_rejected():
    with pytest.raises(WatchdogContractError):
        sample_artifact(signals=(SATURATION, SATURATION))


# --------------------------------------------------------------------------
# the artifact
# --------------------------------------------------------------------------


def test_the_artifact_is_content_addressed_over_its_canonical_bytes():
    artifact = sample_artifact()
    assert artifact.name == "QueueSaturationWatchdog"
    assert artifact.cadence == "1m"
    assert artifact.required_signals == ("QUEUE_SATURATION_PCT",)
    assert artifact.allowed_intents == ("OBSERVE",)
    assert artifact.canonical_ir_hash == artifact.module.content_hash()
    assert artifact.source_hash.startswith("sha256:")

    canonical = json.dumps(
        artifact.to_dict(include_hash=False), sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert artifact.artifact_hash() == f"sha256:{digest}"


def test_the_artifact_hash_moves_when_the_revision_moves():
    assert (
        sample_artifact().artifact_hash() != sample_artifact(revision=4).artifact_hash()
    )


def test_compiling_the_same_source_twice_gives_the_same_artifact():
    assert sample_artifact().to_dict() == sample_artifact().to_dict()


def test_compile_watchdog_rejects_a_rule_the_profile_denies():
    with pytest.raises(WatchdogProfileError):
        compile_watchdog(
            QUEUE_WATCHDOG.replace("observe()", "buy(BTC, 0.5)"),
            watchdog_id="bad",
            revision=1,
            signals=(SATURATION,),
            risk_class="operational-advisory",
        )


# --------------------------------------------------------------------------
# the four fixtures
# --------------------------------------------------------------------------


def test_normal_a_valid_frame_below_the_threshold_proposes_nothing():
    receipt = evaluate_watchdog(sample_artifact(), frame(41.0, 44.0, 39.0))
    assert receipt.input_state == WatchdogState.OK
    assert receipt.proposed_intents == ()
    assert receipt.missing_inputs == ()


def test_saturated_a_frame_over_the_threshold_proposes_observe():
    receipt = evaluate_watchdog(sample_artifact(), frame(41.0, 44.0, 96.0))
    assert receipt.input_state == WatchdogState.OK
    assert [i.action for i in receipt.proposed_intents] == ["OBSERVE"]
    assert receipt.proposed_intents[0].timestamp == 120


def test_missing_input_an_absent_required_signal_is_not_a_quiet_zero():
    """`missing != 0`. Evaluation does not proceed as if the value were known."""
    unrelated = MarketFrame(timestamps=(0, 60), signals={"UNRELATED": (1.0, 1.0)})
    receipt = evaluate_watchdog(sample_artifact(), unrelated)
    assert receipt.input_state == WatchdogState.INPUT_UNAVAILABLE
    assert receipt.missing_inputs == ("QUEUE_SATURATION_PCT",)
    assert receipt.proposed_intents == ()
    assert receipt.unavailable_policy == "HOLD"


def test_missing_input_a_blank_cell_at_the_current_bar_is_missing_not_calm():
    """A gap in the series at the bar being judged is absence, not health."""
    receipt = evaluate_watchdog(sample_artifact(), frame(41.0, 44.0, None))
    assert receipt.input_state == WatchdogState.INPUT_UNAVAILABLE
    assert receipt.missing_inputs == ("QUEUE_SATURATION_PCT",)
    assert receipt.proposed_intents == ()


def test_missing_input_a_frame_with_no_observation_at_all_is_unavailable():
    empty = MarketFrame(timestamps=(), signals={})
    receipt = evaluate_watchdog(sample_artifact(), empty)
    assert receipt.input_state == WatchdogState.INPUT_UNAVAILABLE
    assert receipt.missing_inputs == ("QUEUE_SATURATION_PCT",)
    assert receipt.frame_timestamp is None


def test_recovered_the_gap_is_in_the_past_and_the_current_bar_is_readable():
    """The frame observed after the signal came back.

    Absence at the *current* bar means the rule cannot judge now, so the frame is
    `INPUT_UNAVAILABLE`. Absence three bars ago does not: the VM already refuses
    to fire on a bar whose inputs did not exist, and it says so in the log. The
    gap is recorded rather than erased, which is the difference between a replay
    that can explain a quiet window and one that cannot.
    """
    receipt = evaluate_watchdog(sample_artifact(), frame(None, 44.0, 39.0))
    assert receipt.input_state == WatchdogState.OK
    assert receipt.proposed_intents == ()
    assert receipt.missing_inputs == ()
    assert any(e.event == "condition.unwarmed" for e in receipt.execution_log)


def test_no_proposal_is_never_a_health_claim():
    """The `normal` and `missing-input` fixtures both propose nothing.

    They are not the same answer, and a host reading only ``proposed_intents``
    cannot tell them apart. ``input_state`` is the field that carries the
    difference, which is the reason it exists.
    """
    calm = evaluate_watchdog(sample_artifact(), frame(41.0))
    blind = evaluate_watchdog(sample_artifact(), frame(None))
    assert calm.proposed_intents == blind.proposed_intents == ()
    assert calm.input_state != blind.input_state


# --------------------------------------------------------------------------
# the receipt
# --------------------------------------------------------------------------


def test_the_receipt_answers_the_four_questions():
    """What was observed, which rule judged it, what matched, what was proposed."""
    artifact = sample_artifact()
    receipt = evaluate_watchdog(artifact, frame(41.0, 96.0), created_at=1767225600)
    document = receipt.to_dict()

    # what was observed
    assert document["input_frame"]["signals"]["QUEUE_SATURATION_PCT"] == [41.0, 96.0]
    assert document["input_frame_hash"].startswith("sha256:")
    assert document["frame_timestamp"] == 60

    # which exact rule
    assert document["watchdog_id"] == "queue-saturation"
    assert document["watchdog_revision"] == 3
    assert document["ir_hash"] == artifact.canonical_ir_hash
    assert document["source_hash"] == artifact.source_hash
    assert document["nano_version"] == "1.0.0"

    # what matched
    assert any(e["event"] == "condition.evaluated" for e in document["execution_log"])

    # what was proposed
    assert document["proposed_intents"] == [{"intent": "OBSERVE", "timestamp": 60}]
    assert document["created_at"] == 1767225600


def test_the_receipt_serialises_to_json():
    receipt = evaluate_watchdog(sample_artifact(), frame(96.0))
    assert json.loads(json.dumps(receipt.to_dict())) == receipt.to_dict()


def test_the_evaluation_id_is_a_content_address_not_a_nonce():
    """A random id would make byte-identical replay impossible."""
    artifact = sample_artifact()
    first = evaluate_watchdog(artifact, frame(96.0))
    again = evaluate_watchdog(artifact, frame(96.0))
    other = evaluate_watchdog(artifact, frame(41.0))
    assert first.evaluation_id == again.evaluation_id
    assert first.evaluation_id != other.evaluation_id


def test_a_receipt_carries_no_gate_decision():
    """Disposal is the other half of the boundary and is not recorded here."""
    document = evaluate_watchdog(sample_artifact(), frame(96.0)).to_dict()
    assert "decision" not in document
    assert "approved" not in document


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


def test_replay_reproduces_the_receipt_byte_for_byte():
    artifact = sample_artifact()
    original = evaluate_watchdog(artifact, frame(41.0, 96.0), created_at=1767225600)
    replayed = replay_watchdog(artifact, original)
    assert replayed.to_dict() == original.to_dict()
    assert json.dumps(replayed.to_dict(), sort_keys=True) == json.dumps(
        original.to_dict(), sort_keys=True
    )


def test_replay_reads_the_frame_back_out_of_the_receipt():
    """Replay rebuilds the frame from the receipt rather than reusing the object.

    Reusing the caller's frame would prove the evaluator is deterministic and say
    nothing about whether the receipt on its own is sufficient.
    """
    artifact = sample_artifact()
    original = evaluate_watchdog(artifact, frame(41.0, None))
    assert original.input_state == WatchdogState.INPUT_UNAVAILABLE
    assert replay_watchdog(artifact, original).to_dict() == original.to_dict()


def test_replay_rejects_a_receipt_from_a_different_revision():
    original = evaluate_watchdog(sample_artifact(), frame(96.0))
    with pytest.raises(WatchdogReplayMismatch):
        replay_watchdog(sample_artifact(revision=4), original)


def test_replay_detects_a_doctored_frame():
    artifact = sample_artifact()
    original = evaluate_watchdog(artifact, frame(96.0))
    doctored = dataclasses.replace(
        original,
        input_frame={"timestamps": [0], "signals": {"QUEUE_SATURATION_PCT": [41.0]}},
    )
    with pytest.raises(WatchdogReplayMismatch):
        replay_watchdog(artifact, doctored)


def test_replay_detects_a_doctored_proposal():
    artifact = sample_artifact()
    original = evaluate_watchdog(artifact, frame(41.0))
    fired = evaluate_watchdog(artifact, frame(96.0))
    doctored = dataclasses.replace(original, proposed_intents=fired.proposed_intents)
    with pytest.raises(WatchdogReplayMismatch):
        replay_watchdog(artifact, doctored)


# --------------------------------------------------------------------------
# isolation
# --------------------------------------------------------------------------


def test_an_artifact_whose_ir_no_longer_matches_its_hash_is_invalid():
    tampered = dataclasses.replace(
        sample_artifact(), canonical_ir_hash="sha256:" + "0" * 64
    )
    receipt = evaluate_watchdog(tampered, frame(96.0))
    assert receipt.input_state == WatchdogState.WATCHDOG_INVALID
    assert receipt.proposed_intents == ()


def test_an_artifact_carrying_a_denied_construct_is_invalid_not_executed():
    smuggled = dataclasses.replace(
        sample_artifact(),
        module=compile_module(QUEUE_WATCHDOG.replace("observe()", "buy(BTC, 0.5)")),
    )
    receipt = evaluate_watchdog(smuggled, frame(96.0))
    assert receipt.input_state == WatchdogState.WATCHDOG_INVALID
    assert receipt.proposed_intents == ()


def test_a_broken_watchdog_does_not_stop_the_ones_beside_it():
    healthy = sample_artifact()
    broken = dataclasses.replace(healthy, canonical_ir_hash="sha256:" + "1" * 64)
    states = [
        evaluate_watchdog(artifact, frame(96.0)).input_state
        for artifact in (healthy, broken, healthy)
    ]
    assert states == [
        WatchdogState.OK,
        WatchdogState.WATCHDOG_INVALID,
        WatchdogState.OK,
    ]


def test_declaring_a_signal_optional_does_not_weaken_the_gate():
    """`required=False` describes the host contract, not the rule's dependency.

    A signal the rule actually reads is gated whether or not the contract calls
    it required — otherwise `required=False` would be a way to talk the evaluator
    into proceeding without a value it is about to compare against.
    """
    optional = dataclasses.replace(SATURATION, required=False)
    artifact = sample_artifact(signals=(optional,))
    assert artifact.required_signals == ()
    elsewhere = MarketFrame(timestamps=(0,), signals={"OTHER": (1.0,)})
    receipt = evaluate_watchdog(artifact, elsewhere)
    assert receipt.input_state == WatchdogState.INPUT_UNAVAILABLE
    assert receipt.missing_inputs == ("QUEUE_SATURATION_PCT",)
    assert receipt.proposed_intents == ()


def test_a_runtime_fault_becomes_a_receipt_rather_than_an_exception(monkeypatch):
    """Defence in depth behind the availability gate.

    The gate is meant to catch every reachable cause of a runtime fault, so this
    path should not be hit in practice. "Should not" is not a guarantee, and an
    exception escaping here would take down every watchdog sharing the loop
    rather than the one that broke.
    """
    from nano.runtime.interpreter import RuntimeError_
    from nano.watchdog import evaluate as evaluate_module

    def explode(*args, **kwargs):
        raise RuntimeError_("the series went away mid-run")

    monkeypatch.setattr(evaluate_module, "run_module", explode)
    receipt = evaluate_watchdog(sample_artifact(), frame(96.0))
    assert receipt.input_state == WatchdogState.WATCHDOG_EVALUATION_FAILED
    assert receipt.proposed_intents == ()
    assert "went away" in receipt.execution_log[-1].detail


# --------------------------------------------------------------------------
# frame terminology
# --------------------------------------------------------------------------


def test_signal_frame_is_the_market_frame_type_not_a_second_one():
    """One frame type, two names. A parallel type would fork the evaluator."""
    assert SignalFrame is MarketFrame
    assert isinstance(frame(1.0), SignalFrame)


def test_a_watchdog_evaluates_a_frame_built_under_either_name():
    built = SignalFrame(timestamps=(0,), signals={"QUEUE_SATURATION_PCT": (96.0,)})
    receipt = evaluate_watchdog(sample_artifact(), built)
    assert [i.action for i in receipt.proposed_intents] == ["OBSERVE"]
