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
from pathlib import Path

import pytest

import nano
from nano.compiler import compile_module
from nano.ir.module import OPS
from nano.runtime import MarketFrame, SignalFrame
from nano.watchdog import (
    BOOLEAN_DOMAIN,
    UNAVAILABLE_HOLD,
    UNAVAILABLE_OBSERVE_ONLY,
    UNAVAILABLE_POLICIES,
    WATCHDOG_INTENT_ACTIONS,
    WATCHDOG_OPS,
    WatchdogArtifactV1,
    WatchdogContractError,
    WatchdogProfileError,
    WatchdogReplayMismatch,
    WatchdogSignalSpecV1,
    WatchdogState,
    canonical_json,
    compile_watchdog,
    evaluate_watchdog,
    referenced_signals,
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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_current_input_fails_closed_as_canonical_absence(value):
    """NaN comparisons are false, which used to look like a calm ``OK`` run.

    JSON has no non-finite number spelling. The Watchdog boundary normalizes all
    three values to absence before hashing or execution, producing the same
    replayable unavailable receipt as a missing observation.
    """
    artifact = sample_artifact()
    receipt = evaluate_watchdog(artifact, frame(41.0, value))
    document = receipt.to_dict()

    assert receipt.input_state == WatchdogState.INPUT_UNAVAILABLE
    assert receipt.missing_inputs == ("QUEUE_SATURATION_PCT",)
    assert receipt.proposed_intents == ()
    assert document["input_frame"]["signals"]["QUEUE_SATURATION_PCT"][-1] is None
    encoded = canonical_json(document)
    assert "NaN" not in encoded and "Infinity" not in encoded
    assert replay_watchdog(artifact, receipt).to_dict() == document


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
    assert document["nano_version"] == nano.__version__

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


def test_replay_compares_canonical_bytes_not_python_equality():
    """Python treats ``True`` and ``1`` as equal; their JSON bytes are not."""
    artifact = sample_artifact()
    original = evaluate_watchdog(artifact, frame(96.0, start=1))
    doctored = dataclasses.replace(original, frame_timestamp=True)
    assert doctored.to_dict() == original.to_dict()
    with pytest.raises(WatchdogReplayMismatch, match="/frame_timestamp"):
        replay_watchdog(artifact, doctored)


def test_replay_rejects_nested_container_subclasses_without_iterating_them():
    class SwitchingList(list):
        calls = 0

        def __iter__(self):
            self.calls += 1
            return super().__iter__()

    artifact = sample_artifact()
    original = evaluate_watchdog(artifact, frame(96.0))
    hostile = SwitchingList([96.0])
    input_frame = dict(original.input_frame)
    input_frame["signals"] = {"QUEUE_SATURATION_PCT": hostile}
    doctored = dataclasses.replace(original, input_frame=input_frame)

    with pytest.raises(WatchdogReplayMismatch, match="built-in lists"):
        replay_watchdog(artifact, doctored)
    assert hostile.calls == 0


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


def test_evaluation_uses_one_snapshot_when_the_caller_mutates_its_frame(monkeypatch):
    """The bytes and the VM must see the same immutable observation."""
    from nano.runtime.vm import run_module as real_run_module
    from nano.watchdog import evaluate as evaluate_module

    live = MarketFrame(
        timestamps=(0,), signals={"QUEUE_SATURATION_PCT": [96.0]}
    )

    def mutate_then_run(module, snapshot, **kwargs):
        live.signals["QUEUE_SATURATION_PCT"][0] = 41.0
        return real_run_module(module, snapshot, **kwargs)

    monkeypatch.setattr(evaluate_module, "run_module", mutate_then_run)
    receipt = evaluate_watchdog(sample_artifact(), live)
    assert receipt.input_frame["signals"]["QUEUE_SATURATION_PCT"] == [96.0]
    assert [intent.action for intent in receipt.proposed_intents] == ["OBSERVE"]


def test_watchdog_rejects_container_subclasses_at_the_snapshot_boundary():
    class SwitchingDict(dict):
        calls = 0

        def items(self):
            self.calls += 1
            return super().items()

    signals = SwitchingDict(QUEUE_SATURATION_PCT=(96.0,))
    hostile = MarketFrame(timestamps=(0,), signals=signals)
    signals.calls = 0  # MarketFrame validates on construction; Watchdog starts here.
    with pytest.raises(WatchdogContractError, match="built-in dict"):
        evaluate_watchdog(sample_artifact(), hostile)
    assert signals.calls == 0


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


# --------------------------------------------------------------------------
# the release-note publication family
# --------------------------------------------------------------------------
#
# `nano/library/watchdog/aether_release_notes_*` is a deployment rather than six
# unrelated rules: one host feed, one cadence, one gate. The tests below run the
# whole set the way a host would, because the property that matters is a
# property of the set - no single receipt says "these release notes are safe to
# publish", and the one rule that would be easiest to leave out is the one that
# makes the others mean anything.
#
# Nothing here reaches a forge. The host gathers the facts, Nano evaluates a
# frame, the host acts; `publication_cleared` below is a model of the host's
# half, and it lives in the test file precisely because it must never live in
# `nano/`.

RELEASE_NOTES_DIR = (
    Path(__file__).resolve().parent.parent / "nano" / "library" / "watchdog"
)

COVERAGE = "aether_release_notes_coverage"
BATCH = "aether_release_notes_batch_unaccounted"

RELEASE_NOTE_STEMS = (
    "aether_release_notes_uncovered_merge",
    "aether_release_notes_candidate_unattached",
    "aether_release_notes_proof_missing",
    "aether_release_notes_copy_unvalidated",
    BATCH,
    COVERAGE,
)


def _spec(name, unit, description, *, domain=BOOLEAN_DOMAIN, required=True, note=""):
    return WatchdogSignalSpecV1(
        name=name,
        unit=unit,
        source="host release-notes scanner",
        required=required,
        freshness_limit_ms=300_000,
        description=description,
        value_domain=domain,
        normalization=note,
    )


# The host feed, in full. Every name the rules read, plus four the host publishes
# and no rule reads - see `test_the_family_contract_names_what_no_rule_reads`.
#
# `MERGE_COVERAGE_STATE` is not here, and its absence is the design decision this
# family turns on. A Watchdog signal is a numeric series; there is no string or
# enum type, and the profile is explicit that an unordered vocabulary must not be
# flattened onto one number, because `unavailable=0, empty=1, available=2` invents
# an ordering and every `>=` written against it inherits the invention. So the
# host's five-value coverage vocabulary is published as five independent 0/1
# facts, exactly one of which is 1 on a bar.
RELEASE_NOTE_SIGNALS = {
    spec.name: spec
    for spec in (
        _spec("MERGED", "boolean", "The pull request under examination has merged."),
        _spec(
            "CANONICAL_REPOSITORY",
            "boolean",
            "The merge landed in the repository this programme publishes notes for.",
        ),
        _spec(
            "CANONICAL_BASE",
            "boolean",
            "The merge landed on that repository's release branch.",
        ),
        _spec(
            "IS_RELEASE_NOTES_PR",
            "boolean",
            "The pull request is itself the release note, so it needs no coverage.",
        ),
        _spec(
            "HAS_RELEASE_CANDIDATE",
            "boolean",
            "Some draft release note already covers this merge.",
        ),
        _spec(
            "HAS_SOURCE_ATTACHMENT",
            "boolean",
            "The draft carries the merge, commit or artifact it describes.",
        ),
        _spec(
            "PENDING_AGE_SECONDS",
            "seconds",
            "How long the candidate has held its current state.",
            domain="NONNEGATIVE",
            note="host resets the age when the candidate is revised",
        ),
        _spec(
            "PUBLIC_CANDIDATE",
            "boolean",
            "The candidate is bound for a surface outside the organisation.",
        ),
        _spec(
            "REQUIRED_PROOF_AVAILABLE",
            "boolean",
            "Every claim the host requires evidence for can actually be fetched.",
        ),
        _spec(
            "COPY_VALIDATED",
            "boolean",
            "The text passed whatever validation the host requires of it.",
        ),
        _spec(
            "UNACCOUNTED_MERGE_COUNT",
            "count",
            "Merges on the canonical base that no release candidate covers.",
            domain="NONNEGATIVE",
            note=(
                "published only from a completed scan; absent when there is no "
                "scan, because absence is the honest answer and a zero is not"
            ),
        ),
        _spec(
            "MERGE_COVERAGE_AVAILABLE",
            "boolean",
            "The forge answered and the scan enumerated every merge.",
        ),
        _spec(
            "MERGE_COVERAGE_EMPTY",
            "boolean",
            "The forge answered and there was nothing to enumerate.",
        ),
        _spec(
            "MERGE_COVERAGE_UNAVAILABLE",
            "boolean",
            "The forge did not answer.",
            required=False,
        ),
        _spec(
            "MERGE_COVERAGE_ERROR",
            "boolean",
            "The forge answered with an error.",
            required=False,
        ),
        _spec(
            "MERGE_COVERAGE_STALE",
            "boolean",
            "The last answer predates the batch it would be accounting for.",
            required=False,
        ),
        _spec(
            "BATCH_ACCOUNTING_COMPLETE",
            "boolean",
            "The host considers this batch's accounting finished.",
            required=False,
        ),
        _spec(
            "WAITING_PROOF_COUNT",
            "count",
            "Candidates in this batch still waiting on their proof.",
            domain="NONNEGATIVE",
            required=False,
        ),
        _spec(
            "ROLLING_PR_EXISTS",
            "boolean",
            "A rolling release-notes pull request is open.",
            required=False,
        ),
        _spec(
            "ROLLING_PR_CI_GREEN",
            "boolean",
            "That pull request's checks are passing.",
            required=False,
        ),
    )
}

# A frame on which the whole family is silent: nothing merged this tick, the
# candidate is written and attached, the public release is proven, the batch is
# accounted for, and the merge scan completed. Every test below is a departure
# from this one frame, which is what keeps the departures readable.
CLEARED_TO_PUBLISH = {
    "MERGED": 0.0,
    "CANONICAL_REPOSITORY": 1.0,
    "CANONICAL_BASE": 1.0,
    "IS_RELEASE_NOTES_PR": 0.0,
    "HAS_RELEASE_CANDIDATE": 1.0,
    "HAS_SOURCE_ATTACHMENT": 1.0,
    "PENDING_AGE_SECONDS": 0.0,
    "PUBLIC_CANDIDATE": 1.0,
    "REQUIRED_PROOF_AVAILABLE": 1.0,
    "COPY_VALIDATED": 1.0,
    "UNACCOUNTED_MERGE_COUNT": 0.0,
    "MERGE_COVERAGE_AVAILABLE": 1.0,
    "MERGE_COVERAGE_EMPTY": 0.0,
    "MERGE_COVERAGE_UNAVAILABLE": 0.0,
    "MERGE_COVERAGE_ERROR": 0.0,
    "MERGE_COVERAGE_STALE": 0.0,
    "BATCH_ACCOUNTING_COMPLETE": 1.0,
    "WAITING_PROOF_COUNT": 0.0,
    "ROLLING_PR_EXISTS": 1.0,
    "ROLLING_PR_CI_GREEN": 1.0,
}

# The forge stopped answering. Note what is *not* said here: nothing claims the
# count is zero, and nothing claims it is anything else.
FORGE_UNAVAILABLE = {
    "MERGE_COVERAGE_AVAILABLE": 0.0,
    "MERGE_COVERAGE_EMPTY": 0.0,
    "MERGE_COVERAGE_UNAVAILABLE": 1.0,
}


def release_frame(*, omit=(), **overrides) -> SignalFrame:
    """One observation of the release-notes feed.

    `omit` drops a signal from the frame entirely, which is how a host says it
    has no value. It is not the same as passing zero, and no test here is allowed
    to pretend it is.
    """
    values = dict(CLEARED_TO_PUBLISH)
    values.update(overrides)
    for name in omit:
        values.pop(name)
    return SignalFrame(
        timestamps=(0,), signals={name: (value,) for name, value in values.items()}
    )


def release_artifact(stem: str, **overrides) -> WatchdogArtifactV1:
    """One library entry admitted against the subset of the feed it reads.

    The subset matters. A watchdog gated on every signal in the family would be
    unable to answer whenever any of them was missing, and the coverage guard has
    to answer during exactly the outage that makes the count missing.
    """
    source = (RELEASE_NOTES_DIR / f"{stem}.nano").read_text(encoding="utf-8")
    specs = tuple(
        RELEASE_NOTE_SIGNALS[name] for name in referenced_signals(compile_module(source))
    )
    kwargs = {
        "watchdog_id": stem,
        "revision": 1,
        "signals": specs,
        "risk_class": "release-publication",
        "unavailable_policy": UNAVAILABLE_HOLD,
    }
    kwargs.update(overrides)
    return compile_watchdog(source, **kwargs)


def deployment(**per_watchdog):
    """The six artifacts a host runs together, with optional per-entry overrides."""
    return [release_artifact(stem, **per_watchdog.get(stem, {})) for stem in RELEASE_NOTE_STEMS]


def evaluate_all(artifacts, frame):
    return [evaluate_watchdog(a, frame, created_at=1767225600) for a in artifacts]


def publication_cleared(receipts) -> bool:
    """The host's half of the boundary, modelled: may these notes go out?

    Publication needs every watchdog to have been evaluated and none of them to
    have proposed a hold. An `INPUT_UNAVAILABLE` receipt is not a quiet pass: the
    artifact's declared `unavailable_policy` decides whether the gap holds the
    batch or is merely recorded, which is the whole reason the field is stamped
    on every receipt.

    This function is deliberately not in `nano/`. Nano proposes; the host
    disposes, and a decision gate inside the evaluation plane would be the one
    boundary this design exists to hold.
    """
    for receipt in receipts:
        if receipt.input_state != WatchdogState.OK:
            if receipt.unavailable_policy == UNAVAILABLE_HOLD:
                return False
            continue
        if any(intent.action == "PAUSE" for intent in receipt.proposed_intents):
            return False
    return True


def held_by(receipts):
    return [
        receipt.watchdog_id
        for receipt in receipts
        if any(intent.action == "PAUSE" for intent in receipt.proposed_intents)
    ]


# For each rule: what to change on the cleared frame to arm it, what it proposes,
# and the single signal to put back to make the near-miss frame.
RELEASE_NOTE_TRIGGERS = {
    "aether_release_notes_uncovered_merge": (
        {"MERGED": 1.0, "HAS_RELEASE_CANDIDATE": 0.0},
        "OBSERVE",
        {"HAS_RELEASE_CANDIDATE": 1.0},
    ),
    "aether_release_notes_candidate_unattached": (
        {"HAS_SOURCE_ATTACHMENT": 0.0, "PENDING_AGE_SECONDS": 3600.0},
        "OBSERVE",
        {"PENDING_AGE_SECONDS": 3599.0},
    ),
    "aether_release_notes_proof_missing": (
        {"REQUIRED_PROOF_AVAILABLE": 0.0},
        "PAUSE",
        {"REQUIRED_PROOF_AVAILABLE": 1.0},
    ),
    "aether_release_notes_copy_unvalidated": (
        {"COPY_VALIDATED": 0.0},
        "PAUSE",
        {"COPY_VALIDATED": 1.0},
    ),
    BATCH: (
        {"UNACCOUNTED_MERGE_COUNT": 1.0},
        "PAUSE",
        {"UNACCOUNTED_MERGE_COUNT": 0.0},
    ),
    COVERAGE: (
        dict(FORGE_UNAVAILABLE),
        "PAUSE",
        {"MERGE_COVERAGE_AVAILABLE": 1.0},
    ),
}


def test_every_release_note_entry_is_admitted_against_the_family_contract():
    for stem in RELEASE_NOTE_STEMS:
        artifact = release_artifact(stem)
        assert artifact.cadence == "5m"
        assert set(artifact.allowed_intents) <= WATCHDOG_INTENT_ACTIONS
        assert artifact.unavailable_policy in UNAVAILABLE_POLICIES


def test_the_family_is_silent_on_a_frame_that_clears_publication():
    """The positive control for every departure below.

    Without it, a family that could never fire would pass every no-fire
    assertion in this file.
    """
    receipts = evaluate_all(deployment(), release_frame())
    assert [r.input_state for r in receipts] == [WatchdogState.OK] * len(receipts)
    assert [r.proposed_intents for r in receipts] == [()] * len(receipts)
    assert publication_cleared(receipts)


@pytest.mark.parametrize("stem", RELEASE_NOTE_STEMS)
def test_each_release_note_rule_fires_on_its_trigger_and_not_on_a_near_miss(stem):
    """One signal is the difference between a proposal and silence.

    The near-miss frame changes exactly one value from the trigger frame, so a
    rule that fires on both is a rule where that signal was never load-bearing.
    """
    armed, action, near_miss = RELEASE_NOTE_TRIGGERS[stem]
    artifact = release_artifact(stem)

    fired = evaluate_watchdog(artifact, release_frame(**armed))
    assert fired.input_state == WatchdogState.OK
    assert [i.action for i in fired.proposed_intents] == [action]

    quiet = evaluate_watchdog(artifact, release_frame(**{**armed, **near_miss}))
    assert quiet.input_state == WatchdogState.OK
    assert quiet.proposed_intents == (), (
        f"{stem} still proposes {action} with {sorted(near_miss)} put back"
    )


@pytest.mark.parametrize("stem", RELEASE_NOTE_STEMS)
def test_a_release_note_receipt_is_identical_across_two_runs_and_a_replay(stem):
    """Same artifact, same frame, byte-identical receipt - twice, then from the record.

    Two evaluations prove the evaluator is a function. The replay proves the
    stronger thing an audit needs: the receipt alone reproduces the decision,
    because `replay_watchdog` rebuilds the frame out of `input_frame` rather than
    reusing the object this test still has a reference to.
    """
    armed, _, _ = RELEASE_NOTE_TRIGGERS[stem]
    artifact = release_artifact(stem)
    frame = release_frame(**armed)

    first = evaluate_watchdog(artifact, frame, created_at=1767225600)
    second = evaluate_watchdog(artifact, frame, created_at=1767225600)
    replayed = replay_watchdog(artifact, first)

    def canonical(receipt):
        return json.dumps(receipt.to_dict(), sort_keys=True)

    assert canonical(first) == canonical(second)
    assert canonical(replayed) == canonical(first)
    assert first.evaluation_id == second.evaluation_id == replayed.evaluation_id


def test_an_absent_unaccounted_count_is_not_a_zero():
    """The two frames propose the same thing and mean opposite things.

    A zero says the host counted and found nothing. An absent count says the host
    has no number. `proposed_intents` is empty either way, which is why a gate
    reading only the proposals is a gate that publishes during an outage;
    `input_state` is where the difference lives.
    """
    artifact = release_artifact(BATCH)
    counted = evaluate_watchdog(artifact, release_frame(UNACCOUNTED_MERGE_COUNT=0.0))
    absent = evaluate_watchdog(
        artifact, release_frame(omit=("UNACCOUNTED_MERGE_COUNT",))
    )

    assert counted.input_state == WatchdogState.OK
    assert absent.input_state == WatchdogState.INPUT_UNAVAILABLE
    assert absent.missing_inputs == ("UNACCOUNTED_MERGE_COUNT",)
    assert counted.proposed_intents == absent.proposed_intents == ()
    assert publication_cleared([counted])
    assert not publication_cleared([absent])


def test_a_blank_count_at_the_evaluated_bar_is_absent_too():
    """A published `None` is the same claim as a missing key, and gets the same answer."""
    artifact = release_artifact(BATCH)
    blank = SignalFrame(
        timestamps=(0,),
        signals={
            name: (None if name == "UNACCOUNTED_MERGE_COUNT" else value,)
            for name, value in CLEARED_TO_PUBLISH.items()
        },
    )
    receipt = evaluate_watchdog(artifact, blank)
    assert receipt.input_state == WatchdogState.INPUT_UNAVAILABLE
    assert receipt.missing_inputs == ("UNACCOUNTED_MERGE_COUNT",)


# --------------------------------------------------------------------------
# the mutation: what the coverage guard is actually holding up
# --------------------------------------------------------------------------

# The guard rewritten as a deny-list. It still mentions coverage, still compiles,
# still passes the profile, and still pauses on the one state its author happened
# to think of - which is what makes it the interesting mutant rather than a
# deletion. It is wrong about `unavailable`, and it would be wrong about every
# coverage state added after it was written.
COVERAGE_GUARD_AS_A_DENY_LIST = """
strategy AetherReleaseNotesCoverage {

    agent ReleaseDesk

    every 5m {

        if MERGE_COVERAGE_ERROR >= 1 {

            pause()

        }

    }

}
"""


def weakened_coverage_guard() -> WatchdogArtifactV1:
    return compile_watchdog(
        COVERAGE_GUARD_AS_A_DENY_LIST,
        watchdog_id=COVERAGE,
        revision=1,
        signals=(RELEASE_NOTE_SIGNALS["MERGE_COVERAGE_ERROR"],),
        risk_class="release-publication",
        unavailable_policy=UNAVAILABLE_HOLD,
    )


def test_a_forge_outage_is_never_read_as_zero_unaccounted_merges():
    """The failure this whole family exists to prevent, stated as a test.

    The realistic shape of the bug is not a missing number, it is a *zero*: a
    host whose error path falls through to an empty result publishes
    `UNACCOUNTED_MERGE_COUNT = 0` while the forge is down, and every rule that
    reads the count agrees the batch is fully accounted for. Nothing in the count
    can detect that, because the count is the thing that lied.

    So the guard is not on the count. It is on the scan's own state, and it names
    the two states that permit publication rather than the states that do not.

    The two mutants below are the ways someone removes it without noticing:
    deleting the entry, and rewriting it as a deny-list over the failure states.
    Both publish this frame. That is what makes the assertion above non-vacuous -
    and if the live guard is deleted or weakened, the first assertion in this
    test is the one that goes red.
    """
    frame = release_frame(**FORGE_UNAVAILABLE, UNACCOUNTED_MERGE_COUNT=0.0)

    live = deployment()
    receipts = evaluate_all(live, frame)
    assert not publication_cleared(receipts), (
        "the forge was down and the family cleared publication anyway"
    )
    assert held_by(receipts) == [COVERAGE]
    assert [r.input_state for r in receipts] == [WatchdogState.OK] * len(receipts)

    # Mutant 1: the guard deleted.
    without_guard = [a for a in live if a.watchdog_id != COVERAGE]
    assert publication_cleared(evaluate_all(without_guard, frame))

    # Mutant 2: the guard rewritten as a deny-list over the failure states. It
    # pauses on `error` and waves `unavailable` through.
    deny_list = without_guard + [weakened_coverage_guard()]
    assert publication_cleared(evaluate_all(deny_list, frame))
    error_state = release_frame(
        MERGE_COVERAGE_AVAILABLE=0.0, MERGE_COVERAGE_EMPTY=0.0, MERGE_COVERAGE_ERROR=1.0
    )
    assert not publication_cleared(evaluate_all(deny_list, error_state)), (
        "the deny-list mutant should still catch the one state it names, or this "
        "test is measuring a rule that never fires rather than a weakened one"
    )


def test_a_forge_outage_with_no_count_published_at_all_is_still_held():
    """The other shape: the host publishes nothing rather than a wrong zero.

    Here the batch rule cannot answer - it reads the count, and there is no
    count - so it returns `INPUT_UNAVAILABLE`. Under `HOLD` that is already
    enough to stop publication, but `OBSERVE_ONLY` is a policy a host is entitled
    to choose, and it means "record the gap and carry on". Under that policy the
    coverage guard is the only thing standing between a forge outage and a
    published set of release notes claiming to be complete.
    """
    frame = release_frame(**FORGE_UNAVAILABLE, omit=("UNACCOUNTED_MERGE_COUNT",))
    live = deployment(**{BATCH: {"unavailable_policy": UNAVAILABLE_OBSERVE_ONLY}})

    receipts = evaluate_all(live, frame)
    batch = next(r for r in receipts if r.watchdog_id == BATCH)
    assert batch.input_state == WatchdogState.INPUT_UNAVAILABLE
    assert batch.unavailable_policy == UNAVAILABLE_OBSERVE_ONLY

    coverage = next(r for r in receipts if r.watchdog_id == COVERAGE)
    assert coverage.input_state == WatchdogState.OK, (
        "the coverage guard must be answerable during the outage that silenced "
        "the count, which is why it does not read the count"
    )
    assert not publication_cleared(receipts)

    without_guard = [a for a in live if a.watchdog_id != COVERAGE]
    assert publication_cleared(evaluate_all(without_guard, frame))


def test_the_coverage_guard_holds_every_state_that_is_not_a_completed_scan():
    """`available` and `empty` clear; everything else holds, including the unnamed.

    `empty` is the state that has to be told apart from the failures: the forge
    answered and there was nothing to count. `unavailable` and `error` mean it
    did not answer, and a host that lets one become the other publishes a zero
    that reads exactly like a clean scan.
    """
    artifact = release_artifact(COVERAGE)
    vocabulary = {
        "available": {"MERGE_COVERAGE_AVAILABLE": 1.0},
        "empty": {"MERGE_COVERAGE_AVAILABLE": 0.0, "MERGE_COVERAGE_EMPTY": 1.0},
        "unavailable": FORGE_UNAVAILABLE,
        "error": {"MERGE_COVERAGE_AVAILABLE": 0.0, "MERGE_COVERAGE_ERROR": 1.0},
        "stale": {"MERGE_COVERAGE_AVAILABLE": 0.0, "MERGE_COVERAGE_STALE": 1.0},
        # A state that does not exist yet. An allow-list holds it; a deny-list
        # over today's failure states would not, and nobody would find out.
        "partial": {"MERGE_COVERAGE_AVAILABLE": 0.0},
    }
    for state, overrides in vocabulary.items():
        receipt = evaluate_watchdog(artifact, release_frame(**overrides))
        held = [i.action for i in receipt.proposed_intents]
        assert receipt.input_state == WatchdogState.OK
        if state in ("available", "empty"):
            assert held == [], f"coverage state {state!r} should clear publication"
        else:
            assert held == ["PAUSE"], f"coverage state {state!r} was not held"


def test_the_coverage_guard_cannot_answer_when_the_host_publishes_no_state():
    """Host silence about coverage is not `available` either.

    The guard reads two signals, so absence of either one is `INPUT_UNAVAILABLE`
    and the declared `HOLD` policy keeps the batch where it is. There is no
    spelling of "assume the scan was fine".
    """
    artifact = release_artifact(COVERAGE)
    receipt = evaluate_watchdog(
        artifact, release_frame(omit=("MERGE_COVERAGE_AVAILABLE",))
    )
    assert receipt.input_state == WatchdogState.INPUT_UNAVAILABLE
    assert receipt.missing_inputs == ("MERGE_COVERAGE_AVAILABLE",)
    assert receipt.unavailable_policy == UNAVAILABLE_HOLD
    assert not publication_cleared([receipt])


def test_the_coverage_guard_reads_the_permitting_states_and_only_those():
    """An allow-list, pinned. Adding a failure state here would invert the rule.

    A guard reading `MERGE_COVERAGE_UNAVAILABLE` would be enumerating the ways
    the scan can fail, and a vocabulary of failures is never finished. Reading
    the count would be worse: it would make the guard unanswerable during exactly
    the outage it exists for.
    """
    module = compile_module(
        (RELEASE_NOTES_DIR / f"{COVERAGE}.nano").read_text(encoding="utf-8")
    )
    assert referenced_signals(module) == (
        "MERGE_COVERAGE_AVAILABLE",
        "MERGE_COVERAGE_EMPTY",
    )


def test_the_family_contract_names_what_no_rule_reads():
    """Four host signals are carried and deliberately unread.

    `required=False` describes the host contract, not a rule's dependency, and
    the profile is explicit that it cannot be used to weaken the availability
    gate for a signal a rule does read. These four are read by nothing: the
    closed set of reason codes this family carries does not include one they
    would drive, and adding a condition to make the contract look complete can
    only weaken the rule it is added to.
    """
    carried_not_read = {
        "BATCH_ACCOUNTING_COMPLETE",
        "WAITING_PROOF_COUNT",
        "ROLLING_PR_EXISTS",
        "ROLLING_PR_CI_GREEN",
    }
    read = set()
    for stem in RELEASE_NOTE_STEMS:
        source = (RELEASE_NOTES_DIR / f"{stem}.nano").read_text(encoding="utf-8")
        read |= set(referenced_signals(compile_module(source)))

    assert read <= set(RELEASE_NOTE_SIGNALS)
    assert read & carried_not_read == set()
    assert carried_not_read <= set(RELEASE_NOTE_SIGNALS)
    for name in carried_not_read:
        assert RELEASE_NOTE_SIGNALS[name].required is False


def test_every_boolean_in_the_family_declares_the_zero_one_domain():
    """0 and 1, never -1 for unknown. Unknown is an absent value.

    The rules compare against 0 and 1 literally, so a host that published a 2 for
    "degraded" would land on whichever side of the comparison it happened to fall
    - silently, and differently for `>= 1` than for `<= 0`.
    """
    for name, spec in RELEASE_NOTE_SIGNALS.items():
        if spec.unit == "boolean":
            assert spec.value_domain == BOOLEAN_DOMAIN, name
