"""Nano Intent 0.1.0 contract, policy, determinism, and compatibility guards."""

from __future__ import annotations

import ast
import json
import random
import string
import time
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from nano.intent import (
    Catalog,
    CatalogBundle,
    CatalogError,
    IntentIR,
    IntentInputError,
    MAX_INPUT_CHARS,
    NANO_INTENT_VERSION,
    canonical_bytes,
    compile_intent,
    normalize_text,
    parse_intent,
)
from nano.intent.schema import SCHEMA_PATH, load_schema, validate_document

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = (
    ROOT / "nano" / "intent" / "fixtures" / "atsv2-golden-0.1.0.json"
)

CANONICAL = (
    ("spy earnings", "noun_phrase", "open", "tile", "none", ("ui.open",), 0),
    ("show spy earnings", "local_imperative", "open", "tile", "none", ("ui.open",), 0),
    ("when is spy earnings", "temporal_question", "lookup", "compact_answer", "micro", ("data.read", "llm.call"), 1),
    (
        "describe the predictions of spy earnings over the next 3 quarters",
        "analytical_request",
        "forecast",
        "compact_answer",
        "research",
        ("data.read", "llm.call"),
        1,
    ),
    ("spy qqq volatility", "noun_phrase", "compare", "tile", "none", ("ui.open",), 0),
    ("why is spy volatility higher than qqq", "analytical_request", "compare", "compact_answer", "standard", ("data.read", "llm.call"), 1),
    (
        "open spy chart and explain today's move",
        "compound_request",
        "compound",
        "compact_answer",
        "standard",
        ("ui.open", "data.read", "llm.call"),
        1,
    ),
    ("buy spy", "prohibited_or_unsupported", "trade_request", "refusal", "none", ("trade.request",), 0),
    ("unknown short fragment", "ambiguous", "interpret", "interpretations", "none", (), 0),
)


@pytest.mark.parametrize(
    "phrase, form, operation, mode, budget, effects, llm_count", CANONICAL
)
def test_canonical_atsv2_phrase_matrix(
    phrase, form, operation, mode, budget, effects, llm_count
):
    ir = compile_intent(phrase)
    assert ir.nano_intent_version == NANO_INTENT_VERSION
    assert ir.form == form
    assert ir.operation == operation
    assert ir.response.mode == mode
    assert ir.response.budget_class == budget
    assert ir.effects == effects
    assert sum(step.kind == "llm.call" for step in ir.steps) == llm_count


def test_frozen_forecast_example_has_pinned_normalization_spans_and_capabilities():
    ir = compile_intent(
        "describe the predictions of spy earnings over the next 3 quarters"
    )
    assert ir.normalized == "describe spy earnings predictions over the next 3 quarters"
    assert [entity.to_dict() for entity in ir.entities] == [
        {"kind": "security", "raw": "spy", "canonical": "SPY", "span": [9, 12]},
        {"kind": "topic", "raw": "earnings", "canonical": "earnings", "span": [13, 21]},
        {
            "kind": "horizon",
            "raw": "next 3 quarters",
            "canonical": {"quarters": 3},
            "span": [43, 58],
        },
    ]
    assert [(step.kind, step.capability) for step in ir.steps] == [
        ("data.read", "market.earnings.history"),
        ("data.read", "market.earnings.estimates"),
        ("llm.call", "fast_nonthinking"),
    ]
    assert ir.steps[1].args["quarters"] == 3
    assert ir.confidence == 0.98


def test_noun_and_imperative_are_guaranteed_model_free():
    for phrase in ("spy earnings", "show spy earnings", "SPY   EARNINGS"):
        ir = compile_intent(phrase)
        assert "llm.call" not in ir.effects
        assert all(step.kind != "llm.call" for step in ir.steps)


def test_structural_contrast_changes_plan_not_just_keywords():
    noun = compile_intent("spy earnings")
    temporal = compile_intent("when is spy earnings")
    assert [
        (entity.kind, entity.raw, entity.canonical) for entity in noun.entities
    ] == [
        (entity.kind, entity.raw, entity.canonical) for entity in temporal.entities
    ]
    assert noun.form == "noun_phrase"
    assert temporal.form == "temporal_question"
    assert noun.response.mode == "tile"
    assert temporal.response.mode == "compact_answer"
    assert noun.effects != temporal.effects


def test_compound_order_is_preserved_and_language_work_is_coalesced():
    ir = compile_intent("open spy chart and explain today's move")
    assert [step.kind for step in ir.steps] == ["ui.open", "data.read", "llm.call"]
    assert sum(step.kind == "llm.call" for step in ir.steps) == 1
    assert any(relation["kind"] == "precedes" for relation in ir.receipt.to_dict()["relations"])


def test_conjunction_scopes_unrelated_symbols_and_topics_to_their_own_steps():
    ir = compile_intent("open spy chart and qqq earnings")
    assert ir.form == "compound_request"
    assert ir.effects == ("ui.open",)
    assert [step.to_dict() for step in ir.steps] == [
        {
            "kind": "ui.open",
            "capability": "terminal.tile.chart",
            "args": {"symbol": "SPY", "topic": "chart"},
        },
        {
            "kind": "ui.open",
            "capability": "terminal.tile.earnings",
            "args": {"symbol": "QQQ", "topic": "earnings"},
        },
    ]
    subject_relations = [
        (relation["head"], relation["dependent"])
        for relation in ir.receipt.to_dict()["relations"]
        if relation["kind"] == "subject_topic"
    ]
    assert subject_relations == [("SPY", "chart"), ("QQQ", "earnings")]


def test_compound_analysis_uses_explicit_right_hand_subject_when_present():
    ir = compile_intent("open spy chart and explain qqq earnings")
    assert ir.steps[0].args["symbol"] == "SPY"
    assert ir.steps[1].args["symbol"] == "QQQ"
    assert ir.steps[1].capability == "market.earnings.context"
    assert sum(step.kind == "llm.call" for step in ir.steps) == 1


def test_explicit_negation_suppresses_ui_step():
    ir = compile_intent("do not open a tile, just explain spy earnings")
    assert ir.form == "analytical_request"
    assert "ui.open" not in ir.effects
    assert [step.kind for step in ir.steps] == ["data.read", "llm.call"]
    assert "UI_STEP_SUPPRESSED" in ir.receipt.reason_codes


def test_negation_contraction_and_futures_aliases_are_preserved():
    negated = compile_intent("don't open a tile, just explain spy earnings")
    assert "ui.open" not in negated.effects
    assert "UI_STEP_SUPPRESSED" in negated.receipt.reason_codes
    for phrase in ("/ES chart", "ES1! chart"):
        ir = compile_intent(phrase)
        security = next(entity for entity in ir.entities if entity.kind == "security")
        assert security.canonical == "ES"


@pytest.mark.parametrize(
    "phrase, effect",
    [
        ("focus spy chart", "ui.focus"),
        ("switch workspace", "ui.navigate"),
        ("add spy to watchlist", "ui.navigate"),
        ("remove spy from watchlist", "ui.navigate"),
    ],
)
def test_local_operator_effects_remain_model_free(phrase, effect):
    ir = compile_intent(phrase)
    assert ir.effects == (effect,)
    assert all(step.kind != "llm.call" for step in ir.steps)


def test_ambiguous_and_typo_inputs_never_become_executable():
    for phrase in ("unknown short fragment", "shwo spy earnings"):
        ir = compile_intent(phrase)
        assert ir.form == "ambiguous"
        assert ir.steps == ()
        assert ir.effects == ()
        assert ir.response.mode == "interpretations"
        assert ir.receipt.alternate_interpretations


def test_unvalidated_symbol_is_not_silently_corrected():
    ir = compile_intent("show appl earnings")
    security = next(entity for entity in ir.entities if entity.kind == "security")
    assert security.raw == "appl"
    assert security.canonical == "APPL"
    assert ir.receipt.unresolved_entities[0]["canonical"] == "APPL"


@pytest.mark.parametrize("phrase", ["send spy report", "publish spy report", "delete workspace"])
def test_external_write_always_requires_explicit_confirmation(phrase):
    ir = compile_intent(phrase)
    assert ir.form == "external_action_request"
    assert ir.effects == ("external.write",)
    assert ir.confirmation.mode == "explicit_confirmation"
    assert "llm.call" not in ir.effects


def test_trade_is_declared_but_never_hidden_behind_a_model():
    ir = compile_intent("buy spy")
    assert ir.response.mode == "refusal"
    assert ir.effects == ("trade.request",)
    assert ir.confirmation.mode == "host_decides"
    assert all(step.kind != "llm.call" for step in ir.steps)


def test_surface_invariance_preserves_the_semantic_plan():
    variants = (
        "show spy earnings",
        " SHOW   SPY   EARNINGS? ",
        "please show Spy earnings",
    )
    plans = []
    for phrase in variants:
        document = compile_intent(phrase).to_dict()
        document.pop("sourceHash")
        document["receipt"].pop("sourceHash")
        plans.append(document)
    assert plans[0] == plans[1] == plans[2]


def test_ir_objects_and_nested_arguments_are_immutable():
    ir = compile_intent("when is spy earnings")
    with pytest.raises(FrozenInstanceError):
        ir.form = "noun_phrase"  # type: ignore[misc]
    with pytest.raises(TypeError):
        ir.steps[0].args["symbol"] = "QQQ"  # type: ignore[index]


def test_schema_python_and_canonical_bytes_round_trip():
    first = compile_intent("why is spy volatility higher than qqq")
    second = validate_document(json.loads(canonical_bytes(first)))
    assert isinstance(second, IntentIR)
    assert canonical_bytes(second) == canonical_bytes(first)
    assert first.to_dict() == second.to_dict()
    assert load_schema()["properties"]["nanoIntentVersion"]["const"] == "0.1.0"
    assert SCHEMA_PATH.is_file()


def test_published_atsv2_fixture_bundle_is_current_and_complete():
    bundle = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert bundle["nanoIntentVersion"] == "0.1.0"
    assert bundle["schema"] == "../schemas/nano-intent-0.1.0.schema.json"
    assert len(bundle["fixtures"]) == len(CANONICAL)
    for fixture in bundle["fixtures"]:
        expected = compile_intent(fixture["source"]).to_dict()
        assert fixture["ir"] == expected, fixture["id"]
        assert canonical_bytes(validate_document(fixture["ir"])) == canonical_bytes(
            expected
        )


def test_canonical_bytes_are_stable_across_repeated_compilation():
    phrase = "describe spy earnings forecasts over next three quarters"
    assert canonical_bytes(compile_intent(phrase)) == canonical_bytes(
        compile_intent(phrase)
    )


def test_normalization_is_unicode_case_and_whitespace_safe():
    assert normalize_text("  SHOW\u00a0SPY\u2014EARNINGS?!  ") == "show spy earnings"
    assert normalize_text("SHOW SPY EARNINGS") == "show spy earnings"


def test_input_and_token_work_are_bounded():
    with pytest.raises(IntentInputError, match="character limit"):
        compile_intent("x" * (MAX_INPUT_CHARS + 1))
    with pytest.raises(IntentInputError, match="character limit"):
        compile_intent("\ufb03" * MAX_INPUT_CHARS)
    with pytest.raises(IntentInputError, match="token limit"):
        compile_intent(" ".join("x" for _ in range(300)))


def test_small_seeded_hostile_corpus_never_crashes_or_emits_multiple_models():
    randomizer = random.Random(20260831)
    alphabet = string.ascii_letters + string.digits + " $/:!?'-\t\n"
    for _ in range(300):
        phrase = "".join(randomizer.choice(alphabet) for _ in range(randomizer.randrange(0, 180)))
        try:
            ir = compile_intent(phrase)
        except IntentInputError:
            continue
        assert sum(step.kind == "llm.call" for step in ir.steps) <= 1
        assert canonical_bytes(ir) == canonical_bytes(compile_intent(phrase))


def test_typical_phrase_compilation_is_single_digit_milliseconds_without_io():
    phrase = "why is spy volatility higher than qqq"
    compile_intent(phrase)
    started = time.perf_counter()
    for _ in range(200):
        compile_intent(phrase)
    average_ms = (time.perf_counter() - started) * 1000 / 200
    assert average_ms < 9.0


def test_intent_package_has_no_strategy_runtime_network_or_provider_imports():
    intent_root = ROOT / "nano" / "intent"
    banned = {
        "nano.compiler",
        "nano.runtime",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "openai",
        "anthropic",
    }
    offenders = []
    for path in intent_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                level_prefix = "nano.intent." if node.level else ""
                names = [level_prefix + (node.module or "")]
            for name in names:
                if any(name == prefix or name.startswith(prefix + ".") for prefix in banned):
                    offenders.append((path.relative_to(ROOT).as_posix(), name))
    assert offenders == []


def test_equal_precedence_alias_collision_is_rejected():
    left = Catalog("left", "0.1.0", 10, (("open", "imperative", "open"),))
    right = Catalog("right", "0.1.0", 10, (("open", "imperative", "launch"),))
    with pytest.raises(CatalogError, match="collision"):
        CatalogBundle((left, right))


def test_strategy_ir_version_is_absent_from_intent_artifacts():
    encoded = canonical_bytes(compile_intent("spy earnings"))
    assert b"nanoIntentVersion" in encoded
    assert b"nanoIrVersion" not in encoded
