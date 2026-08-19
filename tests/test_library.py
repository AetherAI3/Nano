"""Conformance + execution suite for the Nano strategy library.

Mirrors tests/test_conformance.py but globs nano/library/** — every published
library strategy is a `.nano`/`_ir.json` pair that must compile bit-identically
and round-trip through StrategyGraph. Representative strategies also get
execution tests against crafted MarketFrames asserting exact fire/no-fire
behavior.
"""

import json
import re
from pathlib import Path

import pytest

from nano.compiler import compile_source, compile_to_dict
from nano.ir.graph import StrategyGraph
from nano.runtime.interpreter import MarketFrame, RuntimeError_, execute

LIBRARY = Path(__file__).resolve().parent.parent / "nano" / "library"

NANO_SOURCES = sorted(LIBRARY.glob("**/*.nano"))

EXPECTED_CATEGORIES = {
    "momentum",
    "mean_reversion",
    "trend",
    "volatility",
    "volume",
    "risk",
    "event_volatility",
    "watchdog",
}


def _ir_path(nano_path: Path) -> Path:
    return nano_path.with_name(f"{nano_path.stem}_ir.json")


def _id(path: Path) -> str:
    return f"{path.parent.name}/{path.stem}"


def test_library_is_nonempty_and_paired():
    assert len(NANO_SOURCES) >= 12, "library must ship at least 12 strategies"
    for nano_path in NANO_SOURCES:
        assert _ir_path(nano_path).exists(), f"{nano_path.name} has no IR partner"
    categories = {p.parent.name for p in NANO_SOURCES}
    assert categories == EXPECTED_CATEGORIES


def test_the_advertised_library_size_is_not_stale():
    """The library README quotes a strategy count, and counts rot silently.

    It said 26 while the directory held 32 — six entries a reader browsing the
    prose would not know were there. The number is the first thing a would-be
    contributor reads, so it gets a guard rather than a good intention.
    """
    readme = (LIBRARY / "README.md").read_text(encoding="utf-8")
    claimed = {int(n) for n in re.findall(r"contains (\d+) strateg", readme)}
    assert claimed, "the library README no longer states how many strategies ship"
    assert claimed == {len(NANO_SOURCES)}, (
        f"the README claims {sorted(claimed)} strategies; the directory holds "
        f"{len(NANO_SOURCES)}. Update the count and the category table together."
    )


def test_the_root_readme_category_counts_match_the_directories():
    """The root README repeats the per-category counts, a third place to go stale.

    It is the first table a visitor sees, so it is the one worth pinning: parse
    the row rather than trusting it, and fail on the category that drifted.
    """
    readme = (LIBRARY.parent.parent / "README.md").read_text(encoding="utf-8")
    header = next(
        (line for line in readme.splitlines() if line.startswith("| momentum |")),
        None,
    )
    assert header, "the root README no longer carries the library category table"
    lines = readme.splitlines()
    counts_row = lines[lines.index(header) + 2]

    categories = [cell.strip() for cell in header.strip("|").split("|")]
    counts = [cell.strip() for cell in counts_row.strip("|").split("|")]
    assert set(categories) == EXPECTED_CATEGORIES

    for category, claim in zip(categories, counts):
        actual = len(list((LIBRARY / category).glob("*.nano")))
        assert claim == f"{actual} rules", (
            f"the root README says `{claim}` for {category}/, which holds {actual}"
        )


def test_every_category_is_listed_in_the_readme():
    """A category the README does not name is a category nobody contributes to."""
    readme = (LIBRARY / "README.md").read_text(encoding="utf-8")
    for category in sorted(EXPECTED_CATEGORIES):
        assert f"`{category}/`" in readme, (
            f"nano/library/{category}/ ships but the README category table does "
            "not list it"
        )


def test_no_orphan_ir_files():
    for ir_path in LIBRARY.glob("**/*_ir.json"):
        partner = ir_path.with_name(ir_path.name[: -len("_ir.json")] + ".nano")
        assert partner.exists(), f"{ir_path.name} has no .nano partner"


@pytest.mark.parametrize("nano_path", NANO_SOURCES, ids=_id)
def test_compiled_ir_matches_handwritten_ir(nano_path: Path):
    compiled = compile_to_dict(nano_path.read_text())
    handwritten = json.loads(_ir_path(nano_path).read_text())
    assert compiled == handwritten


@pytest.mark.parametrize("nano_path", NANO_SOURCES, ids=_id)
def test_ir_round_trips(nano_path: Path):
    data = json.loads(_ir_path(nano_path).read_text())
    assert StrategyGraph.from_dict(data).to_dict() == data
    assert data["effects"] == ["intent.emit", "log.append"]


@pytest.mark.parametrize("nano_path", NANO_SOURCES, ids=_id)
def test_compiled_graph_replays_identically(nano_path: Path):
    graph = compile_source(nano_path.read_text())
    signals = {c.signal: (0.0, 1e9) for c in graph.conditions}
    frame = MarketFrame(timestamps=(0, 86400), signals=signals)
    first = execute(graph, frame).to_dict()
    second = execute(graph, frame).to_dict()
    assert first == second


# --------------------------------------------------------------------------
# Execution tests: crafted MarketFrames with exact fire / no-fire assertions.
# --------------------------------------------------------------------------


def _load(rel: str) -> StrategyGraph:
    return compile_source((LIBRARY / rel).read_text())


def test_rsi_oversold_reversal_fires_only_when_oversold():
    graph = _load("momentum/rsi_oversold_reversal.nano")
    # 15m schedule over three 15-minute ticks: only the middle one is oversold.
    frame = MarketFrame(
        timestamps=(0, 900, 1800),
        signals={"RSI": (55.0, 25.0, 30.0)},  # 30 is NOT < 30
    )
    result = execute(graph, frame)
    assert len(result.intents) == 1
    intent = result.intents[0]
    assert intent.action == "BUY"
    assert intent.asset == "BTCUSD"
    assert intent.confidence == 0.85
    assert intent.timestamp == 900


def test_volume_spike_requires_both_conditions():
    graph = _load("volume/volume_spike_confirmation.nano")
    frame = MarketFrame(
        timestamps=(0, 900, 1800, 2700),
        signals={
            # spike w/o oversold | oversold w/o spike | both | neither
            "VOL_RATIO": (4.0, 1.0, 3.5, 2.0),
            "RSI": (50.0, 25.0, 22.0, 45.0),
        },
    )
    result = execute(graph, frame)
    assert [i.timestamp for i in result.intents] == [1800]
    assert result.intents[0].action == "BUY"


def test_max_drawdown_breaker_pauses_and_names_agent():
    graph = _load("risk/max_drawdown_breaker.nano")
    assert [a.name for a in graph.agents] == ["RiskDesk"]
    frame = MarketFrame(
        timestamps=(0, 60, 120),
        signals={"DRAWDOWN": (1.0, 5.0, 7.5)},  # >= 5 fires at 60 and 120
    )
    result = execute(graph, frame)
    assert [(i.action, i.timestamp) for i in result.intents] == [
        ("PAUSE", 60),
        ("PAUSE", 120),
    ]
    assert result.intents[0].asset is None


def test_golden_cross_ignores_negative_spread():
    graph = _load("trend/golden_cross.nano")
    frame = MarketFrame(
        timestamps=(0, 86400, 172800),
        signals={"SMA_SPREAD": (-12.5, 0.0, 8.0)},  # 0 is NOT > 0
    )
    result = execute(graph, frame)
    assert len(result.intents) == 1
    assert result.intents[0].action == "BUY"
    assert result.intents[0].asset == "SPY"
    assert result.intents[0].timestamp == 172800


def _supertrend_frame(**overrides):
    """A qualifying MNQ 15m tape, with named fields overridden per case.

    Every field starts from a value that clears its floor, so each test below
    changes exactly one thing and the change in outcome is attributable to it.
    """
    signals = {
        "SUPERTREND_FLIP_BULL": (0.0, 1.0),
        "ATR_PCT": (0.09, 0.09),
        "STOP_DISTANCE_ATR": (1.0, 1.0),
        "TARGET_R": (2.4, 2.4),
    }
    signals.update(overrides)
    return MarketFrame(timestamps=(0, 900), signals=signals)


def test_supertrend_flip_long_fires_only_on_the_flip_bar():
    # The flip is a one-bar event. The host publishes the *change*, not the trend
    # state, which is what keeps this from firing on every bar of the leg that
    # follows -- the failure mode golden_cross exists to demonstrate.
    graph = _load("trend/supertrend_flip_long.nano")
    result = execute(graph, _supertrend_frame())
    assert [(i.action, i.timestamp) for i in result.intents] == [("BUY", 900)]
    assert result.intents[0].asset == "MNQ"
    assert result.intents[0].confidence == 0.7

    # No flip on the tape at all, everything else qualifying: silent.
    quiet = _supertrend_frame(SUPERTREND_FLIP_BULL=(0.0, 0.0))
    assert execute(graph, quiet).intents == ()


def test_supertrend_flip_long_refuses_a_flip_in_a_dead_tape():
    # A real flip, but ATR is 0.04 percent of price. A band that flips in a tape
    # this quiet is flipping on noise, and the stop under it has nothing to clear.
    graph = _load("trend/supertrend_flip_long.nano")
    assert execute(graph, _supertrend_frame(ATR_PCT=(0.04, 0.04))).intents == ()

    # Positive control: the identical flip sitting exactly on the volatility
    # floor. Without it the silence above reads the same as a rule that could
    # never fire on any tape.
    on_the_floor = _supertrend_frame(ATR_PCT=(0.05, 0.05))
    assert [i.action for i in execute(graph, on_the_floor).intents] == ["BUY"]


def test_supertrend_flip_long_refuses_a_stop_packed_inside_the_noise():
    # The risk parameter. A stop 0.4 ATR under entry sits inside the range the
    # instrument travels routinely, so it is taken out by the tape breathing
    # rather than by the idea being wrong.
    graph = _load("trend/supertrend_flip_long.nano")
    assert execute(graph, _supertrend_frame(STOP_DISTANCE_ATR=(0.4, 0.4))).intents == ()

    on_the_floor = _supertrend_frame(STOP_DISTANCE_ATR=(0.5, 0.5))
    assert [i.action for i in execute(graph, on_the_floor).intents] == ["BUY"]


def test_supertrend_flip_long_refuses_a_target_that_does_not_pay_for_the_stop():
    # The target parameter, and the only floor measured against the risk rather
    # than against price. 2.32 R is not a bad trade; it is not this rule's trade,
    # which is the distinction a named parameter is supposed to make arguable.
    graph = _load("trend/supertrend_flip_long.nano")
    assert execute(graph, _supertrend_frame(TARGET_R=(2.32, 2.32))).intents == ()

    on_the_floor = _supertrend_frame(TARGET_R=(2.33, 2.33))
    assert [i.action for i in execute(graph, on_the_floor).intents] == ["BUY"]


def test_supertrend_flip_long_reports_a_missing_signal_rather_than_assuming_one():
    # A host that has not published TARGET_R has not decided the target is
    # unacceptable -- it has not decided anything. Defaulting an absent series to
    # zero would read as a refusal and defaulting it high would read as approval;
    # either way the runtime would be inventing the risk decision it exists to
    # relay. Three of this rule's four signals are that decision.
    graph = _load("trend/supertrend_flip_long.nano")
    partial = MarketFrame(
        timestamps=(0, 900),
        signals={
            "SUPERTREND_FLIP_BULL": (0.0, 1.0),
            "ATR_PCT": (0.09, 0.09),
            "STOP_DISTANCE_ATR": (1.0, 1.0),
        },
    )
    with pytest.raises(RuntimeError_, match="TARGET_R"):
        execute(graph, partial)

    # Positive control: the same tape with the field present fires, so the raise
    # above is the absent signal rather than a rule that cannot qualify.
    assert [i.action for i in execute(graph, _supertrend_frame()).intents] == ["BUY"]


def test_cpi_twin_arms_are_mutually_exclusive():
    # The host publishes CPI_COOL_SCORE and CPI_HOT_SCORE separately so the
    # bull and bear branches can never both qualify on one print. Feed one
    # tape where every shared gate passes: only the branch whose release
    # score is high may fire.
    long_graph = _load("event_volatility/cpi_impulse_pullback_long.nano")
    short_graph = _load("event_volatility/cpi_impulse_pullback_short.nano")
    shared = {
        "EVENT_READY": (1.0, 1.0),
        "ENTRY_WINDOW_OPEN": (1.0, 1.0),
        "RELEASE_CONFIRMED": (1.0, 1.0),
        "RETRACE_HOLD_SCORE": (0.8, 0.8),
        "LIQUIDITY_OK": (1.0, 1.0),
    }
    cool_print = MarketFrame(
        timestamps=(0, 5),
        signals={
            **shared,
            "CPI_COOL_SCORE": (0.86, 0.86),
            "CPI_HOT_SCORE": (0.0, 0.0),
            "UPSIDE_IMPULSE_ATR": (1.4, 1.4),
            "DOWNSIDE_IMPULSE_ATR": (0.1, 0.1),
            "BULL_CROSS_CONFIRM": (0.7, 0.7),
            "BEAR_CROSS_CONFIRM": (0.1, 0.1),
        },
    )
    long_result = execute(long_graph, cool_print)
    short_result = execute(short_graph, cool_print)
    assert [i.action for i in long_result.intents] == ["BUY", "BUY"]
    assert short_result.intents == ()

    # The mirror print. Without it the short arm is only ever asserted silent,
    # so a short rule that could never fire would pass this test unchanged —
    # and the mutual-exclusion claim would rest on one arm alone.
    hot_print = MarketFrame(
        timestamps=(0, 5),
        signals={
            **shared,
            "CPI_COOL_SCORE": (0.0, 0.0),
            "CPI_HOT_SCORE": (0.86, 0.86),
            "UPSIDE_IMPULSE_ATR": (0.1, 0.1),
            "DOWNSIDE_IMPULSE_ATR": (1.4, 1.4),
            "BULL_CROSS_CONFIRM": (0.1, 0.1),
            "BEAR_CROSS_CONFIRM": (0.7, 0.7),
        },
    )
    assert [i.action for i in execute(short_graph, hot_print).intents] == ["SELL", "SELL"]
    assert execute(long_graph, hot_print).intents == ()


def test_event_entry_fires_only_inside_entry_window():
    # ENTRY_WINDOW_OPEN is the host's T+5s..T+180s gate: identical impulse
    # tape, but the window closes on the last tick and the rule must abstain.
    graph = _load("event_volatility/event_impulse_pullback_long.nano")
    frame = MarketFrame(
        timestamps=(0, 5, 10),
        signals={
            "EVENT_READY": (1.0, 1.0, 1.0),
            "ENTRY_WINDOW_OPEN": (0.0, 1.0, 0.0),
            "UPSIDE_IMPULSE_ATR": (1.2, 1.2, 1.2),
            "RETRACE_HOLD_SCORE": (0.75, 0.75, 0.75),
            "BULL_CROSS_CONFIRM": (0.7, 0.7, 0.7),
            "LIQUIDITY_OK": (1.0, 1.0, 1.0),
        },
    )
    result = execute(graph, frame)
    assert [(i.action, i.timestamp) for i in result.intents] == [("BUY", 5)]
    assert result.intents[0].asset == "MES"
    assert result.intents[0].confidence == 0.78


def test_event_release_integrity_halt_pauses_on_conflict():
    graph = _load("event_volatility/event_release_integrity_halt.nano")
    frame = MarketFrame(
        timestamps=(0, 1, 2),
        signals={"RELEASE_CONFLICT": (0.0, 1.0, 1.0)},
    )
    result = execute(graph, frame)
    assert [(i.action, i.timestamp) for i in result.intents] == [
        ("PAUSE", 1),
        ("PAUSE", 2),
    ]


def test_daily_loss_limit_fires_at_the_boundary_not_below_it():
    # 2.0 is >= 2, so the boundary tick fires. This is where a control differs
    # from an entry: a limit that only trips *past* its number lets the book sit
    # exactly on the limit indefinitely.
    graph = _load("risk/daily_loss_limit.nano")
    frame = MarketFrame(
        timestamps=(0, 60, 120),
        signals={"DAY_LOSS_PCT": (0.4, 2.0, 3.1)},
    )
    result = execute(graph, frame)
    assert [(i.action, i.timestamp) for i in result.intents] == [
        ("PAUSE", 60),
        ("PAUSE", 120),
    ]
    assert [a.name for a in graph.agents] == ["RiskDesk"]


def test_stale_data_halt_emits_pause_then_observe_in_order():
    # The ordered run log is the product here: a host reading it needs to know
    # the halt came first and the review flag second, not merely that both fired.
    graph = _load("risk/stale_data_halt.nano")
    frame = MarketFrame(
        timestamps=(0, 60),
        signals={"FEED_AGE_SEC": (2.0, 45.0)},
    )
    result = execute(graph, frame)
    assert [(i.action, i.timestamp) for i in result.intents] == [
        ("PAUSE", 60),
        ("OBSERVE", 60),
    ]


def test_concentration_and_leverage_are_independent_controls():
    # The distinction the two rules exist to make: a book can be perfectly
    # diversified and over-levered, or unlevered and entirely in one name.
    # Neither rule catches the other's failure, which is why both ship.
    concentration = _load("risk/position_concentration_cap.nano")
    leverage = _load("risk/leverage_ceiling.nano")

    # 1x gross, everything in one position -> concentration only.
    lopsided = MarketFrame(
        timestamps=(0, 300),
        signals={"MAX_POSITION_PCT": (80.0, 80.0), "GROSS_LEVERAGE": (1.0, 1.0)},
    )
    assert [i.action for i in execute(concentration, lopsided).intents] == ["PAUSE", "PAUSE"]
    assert execute(leverage, lopsided).intents == ()

    # 5x gross spread thinly -> leverage only.
    spread_thin = MarketFrame(
        timestamps=(0, 300),
        signals={"MAX_POSITION_PCT": (4.0, 4.0), "GROSS_LEVERAGE": (5.0, 5.0)},
    )
    assert execute(concentration, spread_thin).intents == ()
    assert [i.action for i in execute(leverage, spread_thin).intents] == ["PAUSE", "PAUSE"]


def test_consecutive_loss_circuit_ignores_a_streak_that_resets():
    # Three losses, a win (host resets the count), then three more. The streak
    # never reaches four, so a rule counting losses rather than tracking runs
    # would fire here and this one must not.
    graph = _load("risk/consecutive_loss_circuit.nano")
    reset = MarketFrame(
        timestamps=(0, 300, 600, 900, 1200, 1500, 1800),
        signals={"CONSECUTIVE_LOSSES": (1.0, 2.0, 3.0, 0.0, 1.0, 2.0, 3.0)},
    )
    assert execute(graph, reset).intents == ()

    # Positive control: the same graph on an unbroken run. Without it the
    # no-fire assertion above passes just as well against a rule that can never
    # fire at all.
    unbroken = MarketFrame(
        timestamps=(0, 300, 600, 900),
        signals={"CONSECUTIVE_LOSSES": (1.0, 2.0, 3.0, 4.0)},
    )
    assert [(i.action, i.timestamp) for i in execute(graph, unbroken).intents] == [
        ("PAUSE", 900)
    ]


def test_correlation_cluster_guard_ignores_a_diversified_book():
    graph = _load("risk/correlation_cluster_guard.nano")
    diversified = MarketFrame(
        timestamps=(0, 900, 1800),
        signals={"CLUSTER_EXPOSURE_PCT": (12.0, 28.0, 39.9)},  # never >= 40
    )
    assert execute(graph, diversified).intents == ()

    # Positive control on the same graph — proves the silence above is the rule
    # declining to fire, not the rule being incapable of firing.
    concentrated = MarketFrame(
        timestamps=(0, 900),
        signals={"CLUSTER_EXPOSURE_PCT": (12.0, 40.0)},
    )
    assert [i.action for i in execute(graph, concentrated).intents] == [
        "PAUSE",
        "OBSERVE",
    ]


def test_atr_halt_emits_no_intent_in_calm_regime():
    graph = _load("volatility/atr_volatility_halt.nano")
    calm = MarketFrame(
        timestamps=(0, 300, 600),
        signals={"ATR_PCT": (1.2, 3.0, 5.0)},  # never > 5
    )
    assert execute(graph, calm).intents == ()

    # Positive control on the same graph. A no-fire assertion on its own cannot
    # tell a rule that declined to fire from a rule that never could — which is
    # exactly how a vacuous test stays green.
    violent = MarketFrame(
        timestamps=(0, 300),
        signals={"ATR_PCT": (1.2, 5.1)},
    )
    assert [(i.action, i.timestamp) for i in execute(graph, violent).intents] == [
        ("PAUSE", 300)
    ]


def test_trusted_route_guard_pauses_only_while_the_route_is_down():
    graph = _load("watchdog/trusted_route_guard.nano")
    assert [a.name for a in graph.agents] == ["SecurityDesk"]
    frame = MarketFrame(
        timestamps=(0, 60, 120),
        # verified | down | recovered. The rule does not latch: it stops
        # proposing a pause the moment the host stops reporting the outage,
        # because deciding when a pause ends is the host's job, not the rule's.
        signals={"TRUSTED_ROUTE_DOWN": (0.0, 1.0, 0.0)},
    )
    result = execute(graph, frame)
    assert [(i.action, i.timestamp) for i in result.intents] == [("PAUSE", 60)]
    assert result.intents[0].asset is None


def test_credential_age_alert_observes_rather_than_pausing():
    # The positive control matters more than the no-fire here: a watchdog that
    # silently upgraded an advisory into a PAUSE would halt a system over a key
    # that is merely getting old.
    graph = _load("watchdog/credential_age_alert.nano")
    frame = MarketFrame(
        timestamps=(0, 86400, 172800),
        signals={"CREDENTIAL_AGE_DAYS": (79.0, 80.0, 91.0)},  # 79 is NOT >= 80
    )
    result = execute(graph, frame)
    assert [(i.action, i.timestamp) for i in result.intents] == [
        ("OBSERVE", 86400),
        ("OBSERVE", 172800),
    ]


def test_watchdog_rules_never_propose_a_direction():
    """Every watchdog is a control, so none of them may emit a trade.

    The category exists to express policy conditions. A BUY or SELL appearing
    here would mean a security rule had quietly become a trading rule.
    """
    for nano_path in sorted((LIBRARY / "watchdog").glob("*.nano")):
        graph = compile_source(nano_path.read_text())
        actions = {intent.action for intent in graph.intents}
        assert actions <= {"PAUSE", "OBSERVE"}, (
            f"{nano_path.name} proposes {sorted(actions - {'PAUSE', 'OBSERVE'})}; "
            "watchdog rules emit PAUSE and OBSERVE only"
        )


def test_every_watchdog_entry_is_admissible_under_the_watchdog_profile():
    """The corpus and the runtime profile must not drift apart.

    `nano/watchdog/` admits a rule under a narrower contract than the language
    allows; `nano/library/watchdog/` is the corpus of rules written for it. They
    were added by separate changes, so nothing tied them together — this does.
    The signal specs are synthesised from what each rule actually reads, so the
    assertion is about the rule's admissibility (opcodes, tier, effects, intents,
    cadence), not about any one host's feed.
    """
    from nano.watchdog import WatchdogSignalSpecV1, compile_watchdog
    from nano.watchdog.profile import referenced_signals

    from nano.compiler import compile_module

    entries = sorted((LIBRARY / "watchdog").glob("*.nano"))
    assert entries, "the watchdog category is empty"

    for nano_path in entries:
        source = nano_path.read_text(encoding="utf-8")
        specs = [
            WatchdogSignalSpecV1(
                name=name,
                unit="unit",
                source="host",
                required=True,
                freshness_limit_ms=300_000,
                description="host-published control input",
                value_domain="NONNEGATIVE",
            )
            for name in referenced_signals(compile_module(source))
        ]
        artifact = compile_watchdog(
            source,
            watchdog_id=nano_path.stem,
            revision=1,
            signals=specs,
            risk_class="LOW",
        )
        assert artifact.watchdog_id == nano_path.stem


# --------------------------------------------------------------------------
# the release-note publication family
# --------------------------------------------------------------------------
#
# Six watchdog entries gate the publication of release notes for a repository
# programme. They are tested together because they are deployed together: the
# reason codes they carry are a closed set, and two of them are the two halves
# of a single disjunction that baseline IR cannot express in one rule.

RELEASE_NOTE_ENTRIES = sorted(
    (LIBRARY / "watchdog").glob("aether_release_notes_*.nano")
)

# The reason code each entry carries. It is the entry's name rather than a field
# on the intent, because an `Intent` has an action, an asset and a confidence and
# nothing else - there is no reason slot to write into, and inventing one would
# mean changing the language to carry a label. One rule per reason code is the
# shape the runtime already has: `WatchdogReceiptV1` records `watchdog_id`, so a
# host reads the reason off the receipt without parsing anything.
RELEASE_NOTE_REASONS = {
    "aether_release_notes_uncovered_merge": "MERGE_WITHOUT_RELEASE_CANDIDATE",
    "aether_release_notes_candidate_unattached": "RELEASE_CANDIDATE_NOT_ATTACHED",
    "aether_release_notes_proof_missing": "PUBLIC_RELEASE_NOT_PROVEN",
    "aether_release_notes_copy_unvalidated": "PUBLIC_RELEASE_NOT_PROVEN",
    "aether_release_notes_batch_unaccounted": "RELEASE_BATCH_ACCOUNTING_INCOMPLETE",
    "aether_release_notes_coverage": "MERGE_COVERAGE_UNKNOWN",
}


def _satisfies(condition) -> float:
    """A value that satisfies one baseline condition, and only just."""
    if condition.operator in (">=", "<="):
        return float(condition.value)
    raise AssertionError(
        "the release-note family reads >= and <= only; got "
        f"{condition.operator!r} on {condition.signal}"
    )


def _fails(condition) -> float:
    """A value that fails one baseline condition, and only just."""
    if condition.operator == ">=":
        return float(condition.value) - 1.0
    return float(condition.value) + 1.0


def _one_bar(values: dict) -> MarketFrame:
    return MarketFrame(
        timestamps=(0,), signals={name: (value,) for name, value in values.items()}
    )


def test_the_release_note_family_is_the_expected_six_entries():
    """The reason codes are a closed set; an entry outside it has no host mapping."""
    assert {path.stem for path in RELEASE_NOTE_ENTRIES} == set(RELEASE_NOTE_REASONS)


@pytest.mark.parametrize("nano_path", RELEASE_NOTE_ENTRIES, ids=lambda p: p.stem)
def test_every_release_note_condition_is_load_bearing(nano_path: Path):
    """Fire on the trigger frame; go quiet with any single signal flipped.

    A no-fire assertion on its own cannot tell a rule that declined to fire from
    a rule that never could, so the positive control runs first. Flipping one
    condition at a time then proves every term is doing work: a condition that
    can be falsified without changing the answer is a condition that was never
    part of the rule, and it would sit there looking like a guard.
    """
    graph = compile_source(nano_path.read_text(encoding="utf-8"))
    signals = [condition.signal for condition in graph.conditions]
    assert len(set(signals)) == len(signals), "a signal is read twice in one rule"

    trigger = {c.signal: _satisfies(c) for c in graph.conditions}
    fired = execute(graph, _one_bar(trigger)).intents
    assert len(fired) == 1, f"{nano_path.stem} did not fire on its own trigger frame"
    assert fired[0].action in {"PAUSE", "OBSERVE"}
    assert fired[0].asset is None

    for condition in graph.conditions:
        near_miss = dict(trigger)
        near_miss[condition.signal] = _fails(condition)
        assert execute(graph, _one_bar(near_miss)).intents == (), (
            f"{nano_path.stem} still fires with {condition.signal} flipped, so "
            f"{condition.signal} {condition.operator} {condition.value} is not "
            "carrying any weight"
        )


def test_the_coverage_guard_holds_on_every_state_that_is_not_a_complete_scan():
    """The point of the whole family: an outage must never read as a clean scan.

    The host's coverage vocabulary is `available / empty / unavailable / error /
    stale`, published as five independent booleans. Only the first two mean the
    forge answered, so those two are the ones the rule names - an allow-list.
    The sixth row is a state nobody has invented yet, and it is held for the same
    reason a new opcode is denied by `WATCHDOG_OPS`: a rule that listed the bad
    states instead would wave every future one through.
    """
    graph = _load("watchdog/aether_release_notes_coverage.nano")
    assert [a.name for a in graph.agents] == ["ReleaseDesk"]

    states = {
        "available": (1.0, 0.0),
        "empty": (0.0, 1.0),
        "unavailable": (0.0, 0.0),
        "error": (0.0, 0.0),
        "stale": (0.0, 0.0),
        "a state added next year": (0.0, 0.0),
    }
    cleared = {"available", "empty"}
    for state, (available, empty) in states.items():
        frame = _one_bar(
            {"MERGE_COVERAGE_AVAILABLE": available, "MERGE_COVERAGE_EMPTY": empty}
        )
        actions = [intent.action for intent in execute(graph, frame).intents]
        if state in cleared:
            assert actions == [], f"coverage state {state!r} should permit publication"
        else:
            assert actions == ["PAUSE"], f"coverage state {state!r} was not held"


def test_the_coverage_guard_never_reads_the_unaccounted_count():
    """The two rules are separate so an outage cannot arrive as a zero.

    `aether_release_notes_batch_unaccounted` reads the count and therefore cannot
    answer at all when the count is absent. The coverage guard reads only the two
    facts about the scan itself, so it answers during the outage that made the
    count absent. Merging them into one rule would put the count back in the
    coverage guard's dependency set and re-open exactly the hole.
    """
    graph = _load("watchdog/aether_release_notes_coverage.nano")
    assert {c.signal for c in graph.conditions} == {
        "MERGE_COVERAGE_AVAILABLE",
        "MERGE_COVERAGE_EMPTY",
    }


def test_the_proof_and_copy_rules_reconstruct_one_disjunction():
    """`not proof or not copy` is two entries because baseline IR has no `or`.

    The split is only safe if the pair covers the disjunction exactly: a hold for
    every combination except the one where both halves are satisfied. Deleting
    either entry leaves a public release that can be published with half its
    evidence, which is the failure this walks the whole truth table to exclude.
    """
    proof = _load("watchdog/aether_release_notes_proof_missing.nano")
    copy = _load("watchdog/aether_release_notes_copy_unvalidated.nano")

    for has_proof in (0.0, 1.0):
        for validated in (0.0, 1.0):
            frame = _one_bar(
                {
                    "PUBLIC_CANDIDATE": 1.0,
                    "REQUIRED_PROOF_AVAILABLE": has_proof,
                    "COPY_VALIDATED": validated,
                }
            )
            held = [
                intent.action
                for graph in (proof, copy)
                for intent in execute(graph, frame).intents
            ]
            if has_proof and validated:
                assert held == [], "a fully proven public release was still held"
            else:
                assert "PAUSE" in held, (
                    f"REQUIRED_PROOF_AVAILABLE={has_proof} COPY_VALIDATED="
                    f"{validated} is not proven, and neither entry held it"
                )

    # An internal candidate is outside both rules by construction.
    internal = _one_bar(
        {
            "PUBLIC_CANDIDATE": 0.0,
            "REQUIRED_PROOF_AVAILABLE": 0.0,
            "COPY_VALIDATED": 0.0,
        }
    )
    assert execute(proof, internal).intents == ()
    assert execute(copy, internal).intents == ()


def test_every_release_note_hold_states_the_boundary_it_must_not_cross():
    """A `pause` here holds publication, never an engineering merge.

    The distinction is not enforceable inside Nano - a proposal is a proposal and
    the host decides what to do with it - so the boundary lives in the header,
    where the person wiring the rule into a gate will read it. That makes it
    prose, and prose rots, so it gets a guard like every other claim the library
    makes about itself.
    """
    for nano_path in RELEASE_NOTE_ENTRIES:
        source = nano_path.read_text(encoding="utf-8")
        if "PAUSE" not in {i.action for i in compile_source(source).intents}:
            continue
        header = "\n".join(
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith("//")
        )
        assert "release-note publication and nothing else" in header, (
            f"{nano_path.name} proposes a hold without saying what it may hold. "
            "Say so in the header: these rules gate publication, never a branch, "
            "a deploy, or an engineering merge."
        )
