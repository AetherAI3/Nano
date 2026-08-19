"""Conformance + execution suite for the Nano strategy library.

Mirrors tests/test_conformance.py but globs nano/library/** — every published
library strategy is a `.nano`/`_ir.json` pair that must compile bit-identically
and round-trip through its IR loader. Representative strategies also get
execution tests against crafted MarketFrames asserting exact fire/no-fire
behavior.

The library holds **two** corpora and the split is deliberate:

* **baseline entries** compare host-supplied named signals with numeric
  literals. They compile to v0.1.0 IR, they are the oldest artifacts in the
  repository, and their checked-in fixtures are byte-stable — a change to one is
  a compiler regression, not a library edit. ``test_baseline_entries_stay_on_
  baseline_ir`` is the guard that makes that claim testable rather than assumed.
* **v1 entries** declare their own `input`s and let Nano compute the indicators.
  They compile to 1.0.0 IR, carry a `sourceHash`, and run on the VM rather than
  the reference interpreter.

The partition is read from the *checked-in* fixture, not from the compiler. If
the compiler ever started emitting a different version for an existing entry,
deriving the partition from it would quietly re-route the entry to the other set
of tests; reading the fixture instead turns that into a visible failure.

Every no-fire assertion below is paired with a positive control on the **same**
strategy. A no-fire test alone passes just as well against a rule that can never
fire at all, which is the one way a library test stays green while being worthless.
"""

import json
import math
import re
from pathlib import Path

import pytest

from nano.compiler import compile_module, compile_source, compile_to_dict
from nano.ir.graph import StrategyGraph
from nano.ir.module import NanoModule
from nano.runtime.interpreter import MarketFrame, RuntimeError_, execute
from nano.runtime.vm import run_module
from scripts.check_contribution import (
    baseline_control_frames,
    module_control_frame,
    source_provenance_issues,
)

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


def _pinned_ir_version(nano_path: Path) -> str:
    return json.loads(_ir_path(nano_path).read_text())["nanoIrVersion"]


BASELINE_SOURCES = [p for p in NANO_SOURCES if _pinned_ir_version(p) == "0.1.0"]
V1_SOURCES = [p for p in NANO_SOURCES if _pinned_ir_version(p) == "1.0.0"]


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


def test_both_corpora_are_populated():
    """Neither half of the split may quietly empty out.

    Every parametrised test below is keyed off one of these two lists, and a
    parametrisation over an empty list collects zero cases and reports success.
    If a refactor ever mis-detected the version, one of these lists would go to
    zero and a whole family of assertions would vanish without a failure — so the
    lists are asserted directly, and their sum is asserted against the file count
    so an entry cannot fall out of both.
    """
    assert BASELINE_SOURCES, "no baseline (v0.1.0) library entries were detected"
    assert V1_SOURCES, "no v1.0 library entries were detected"
    assert len(BASELINE_SOURCES) + len(V1_SOURCES) == len(NANO_SOURCES)


def test_no_orphan_ir_files():
    for ir_path in LIBRARY.glob("**/*_ir.json"):
        partner = ir_path.with_name(ir_path.name[: -len("_ir.json")] + ".nano")
        assert partner.exists(), f"{ir_path.name} has no .nano partner"


@pytest.mark.parametrize("nano_path", NANO_SOURCES, ids=_id)
def test_compiled_ir_matches_handwritten_ir(nano_path: Path):
    compiled = compile_to_dict(nano_path.read_text())
    handwritten = json.loads(_ir_path(nano_path).read_text())
    assert compiled == handwritten


@pytest.mark.parametrize("nano_path", BASELINE_SOURCES, ids=_id)
def test_baseline_entries_stay_on_baseline_ir(nano_path: Path):
    """A v0.1.0 entry must keep compiling to v0.1.0.

    Byte-stability of the baseline corpus is the library's oldest promise: hosts
    pinned these fixtures. The compiler emits the lowest version that can express
    a program, so an entry drifting up to `1.0.0` means either the source gained
    a v1 construct or version inference regressed. Both are worth failing on.
    """
    assert compile_to_dict(nano_path.read_text())["nanoIrVersion"] == "0.1.0"


@pytest.mark.parametrize("nano_path", BASELINE_SOURCES, ids=_id)
def test_ir_round_trips(nano_path: Path):
    data = json.loads(_ir_path(nano_path).read_text())
    assert StrategyGraph.from_dict(data).to_dict() == data
    assert data["effects"] == ["intent.emit", "log.append"]


@pytest.mark.parametrize("nano_path", V1_SOURCES, ids=_id)
def test_v1_module_round_trips(nano_path: Path):
    data = json.loads(_ir_path(nano_path).read_text())
    assert NanoModule.from_dict(data).to_dict() == data
    assert data["effects"] == ["intent.emit", "log.append"]


@pytest.mark.parametrize("nano_path", V1_SOURCES, ids=_id)
def test_v1_fixture_matches_its_source_hash(nano_path: Path):
    """The pinned fixture must belong to the pinned source text.

    v1 IR carries a `sourceHash` over the whole `.nano` file, comments included.
    Editing a strategy's documentation without regenerating its fixture leaves a
    document that still loads and still round-trips but no longer describes the
    file beside it — a drift the version and node checks cannot see.
    """
    data = json.loads(_ir_path(nano_path).read_text())
    recompiled = compile_to_dict(nano_path.read_text())
    assert data["provenance"]["sourceHash"] == recompiled["provenance"]["sourceHash"]
    assert data["moduleHash"] == recompiled["moduleHash"]


@pytest.mark.parametrize("nano_path", BASELINE_SOURCES, ids=_id)
def test_compiled_graph_replays_identically(nano_path: Path):
    graph = compile_source(nano_path.read_text())
    signals = {c.signal: (0.0, 1e9) for c in graph.conditions}
    frame = MarketFrame(timestamps=(0, 86400), signals=signals)
    first = execute(graph, frame).to_dict()
    second = execute(graph, frame).to_dict()
    assert first == second


@pytest.mark.parametrize("nano_path", V1_SOURCES, ids=_id)
def test_v1_module_replays_identically(nano_path: Path):
    module = compile_module(nano_path.read_text())
    frame = _ohlcv(_wave(260))
    first = run_module(module, frame).to_dict()
    second = run_module(module, frame).to_dict()
    assert first == second


@pytest.mark.parametrize("nano_path", BASELINE_SOURCES, ids=_id)
def test_contribution_checker_uses_paired_baseline_controls(nano_path: Path):
    graph = compile_source(nano_path.read_text())
    firing, silent = baseline_control_frames(graph)
    assert execute(graph, firing).intents
    assert execute(graph, silent).intents == ()
    assert firing != silent


def test_contribution_checker_solves_repeated_signal_constraints():
    graph = StrategyGraph.from_dict(
        {
            "type": "Strategy",
            "nanoIrVersion": "0.1.0",
            "name": "BoundedControl",
            "effects": ["intent.emit", "log.append"],
            "nodes": [
                {"type": "Schedule", "interval": "1m"},
                {"type": "Condition", "signal": "SCORE", "operator": ">", "value": 0},
                {"type": "Condition", "signal": "SCORE", "operator": "<", "value": 1},
                {"type": "Intent", "action": "OBSERVE"},
            ],
        }
    )
    firing, silent = baseline_control_frames(graph)
    assert execute(graph, firing).intents
    assert execute(graph, silent).intents == ()


@pytest.mark.parametrize("nano_path", V1_SOURCES, ids=_id)
def test_contribution_checker_reaches_past_each_module_warmup(nano_path: Path):
    module = compile_module(nano_path.read_text())
    frame = module_control_frame(module)
    result = run_module(module, frame)

    assert len(frame.timestamps) == module.warmup + 2
    assert any(entry.event == "condition.evaluated" for entry in result.log)
    assert any(len(set(series)) > 1 for series in frame.signals.values())


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (["// REGIME: range"], ()),
        (["// SOURCE: public paper"], ()),
        (["// SOURCE:"], ("empty",)),
        (
            ["// SOURCE: public paper", "// SOURCE: another account"],
            ("more than one",),
        ),
    ],
)
def test_source_provenance_policy_is_precise_and_non_inventive(header, expected):
    issues = source_provenance_issues(header)
    assert len(issues) == len(expected)
    for phrase in expected:
        assert phrase in issues[0]


# --------------------------------------------------------------------------
# Execution tests: crafted MarketFrames with exact fire / no-fire assertions.
# --------------------------------------------------------------------------


def _load(rel: str) -> StrategyGraph:
    return compile_source((LIBRARY / rel).read_text())


def _module(rel: str) -> NanoModule:
    return compile_module((LIBRARY / rel).read_text())


def _ohlcv(closes, *, opens=None, highs=None, lows=None, volumes=None):
    """Build a five-series OHLCV frame from a close path.

    Bars are spaced one full day apart so every cadence in the library — 5m
    through 1d — fires on every bar: the scheduler fires at the first timestamp
    and at any timestamp at least one interval after the previous firing.

    Unstated series are derived rather than invented. The open is the close, and
    the high/low straddle the bar by half a point so no bar has a zero range — a
    zero-range bar makes `(close - low) / (high - low)` absent, which would
    silently disarm any rule reading where the close sits inside its bar.
    """
    closes = tuple(float(c) for c in closes)
    count = len(closes)
    opens = tuple(float(v) for v in opens) if opens is not None else closes
    if highs is None:
        highs = tuple(max(o, c) + 0.5 for o, c in zip(opens, closes))
    else:
        highs = tuple(float(v) for v in highs)
    if lows is None:
        lows = tuple(min(o, c) - 0.5 for o, c in zip(opens, closes))
    else:
        lows = tuple(float(v) for v in lows)
    volumes = (
        tuple(float(v) for v in volumes) if volumes is not None else (1000.0,) * count
    )
    return MarketFrame(
        timestamps=tuple(i * 86400 for i in range(count)),
        signals={
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
    )


def _wave(count: int):
    """A deterministic, non-degenerate price path.

    Drift plus two out-of-phase cycles: moving averages separate, rolling
    dispersion is non-zero, and no bar has a zero range. Nothing random — the
    same path on every machine, which is the premise of every replay assertion.
    """
    return [
        100.0 + 0.05 * i + 6.0 * math.sin(i / 7.0) + 2.0 * math.sin(i / 3.0)
        for i in range(count)
    ]


def _fires(module: NanoModule, frame: MarketFrame):
    """(action, bar index) for every intent — `_ohlcv` lays out one bar per day."""
    return [(i.action, i.timestamp // 86400) for i in run_module(module, frame).intents]


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

# --------------------------------------------------------------------------
# v1 execution tests. Each strategy computes its own indicators from a declared
# OHLCV input, so the frames below are price paths rather than signal levels.
#
# Every one of these is a *pair*: a frame the rule must fire on and a frame it
# must stay silent on. The silent half is the interesting assertion and it is
# also the worthless one on its own — a rule that can never fire passes it
# unchanged — so neither half ships without the other.
# --------------------------------------------------------------------------


def test_ema_pullback_fires_on_a_dip_and_not_in_a_downtrend():
    module = _module("trend/ema_pullback_continuation.nano")

    # Sixty bars of advance, then a four-point-a-day dip that reaches the fast
    # average while the slow one still holds underneath.
    advance = [100.0 + i for i in range(60)]
    dip = [advance[-1] - 4.0 * step for step in range(1, 6)]
    assert _fires(module, _ohlcv(advance + dip)) == [
        ("BUY", 62),
        ("BUY", 63),
        ("BUY", 64),
    ]

    # The mirror regime. The fast average is under the slow one on every warm
    # bar, so the first condition is false throughout — the rule is structurally
    # unable to buy a decline, which is the property worth pinning.
    assert _fires(module, _ohlcv([200.0 - i for i in range(65)])) == []


def test_macd_zero_reclaim_fires_once_at_the_crossing():
    module = _module("trend/macd_zero_line_reclaim.nano")

    decline = [200.0 - 2.0 * i for i in range(45)]
    recovery = [decline[-1] + 4.0 * step for step in range(1, 31)]
    # Thirty bars of recovery, one intent. `line[1] <= 0` is what makes the
    # difference between an edge and a level: without it every bar of the
    # recovery would qualify, since the line stays positive for all of them.
    assert _fires(module, _ohlcv(decline + recovery)) == [("BUY", 55)]

    # No crossing, no intent.
    assert _fires(module, _ohlcv([200.0 - 2.0 * i for i in range(75)])) == []


def test_macd_zero_reclaim_ignores_a_second_cross_under_its_own_signal():
    """The frame that proves `MACD_HIST > 0` is load-bearing, not decoration.

    A revision of this strategy deleted that term, arguing that a line crossing
    up through zero necessarily sits above its own signal EMA. That holds for the
    *first* cross out of a sustained negative stretch and fails for a second one:
    after a rally and a retrace the signal EMA still carries the earlier high
    line values, so it sits above the line at the crossing.

    Here bar 53 is the clean first cross and bar 78 is the second, with
    line = +0.3243, line[1] = -0.0980 and hist = -3.6859 — above zero, below its
    own signal, which is the fading spike the term exists to reject. Without the
    histogram condition this frame fires twice.
    """
    module = _module("trend/macd_zero_line_reclaim.nano")
    closes = [200.0 - 2.0 * i for i in range(45)]
    for step, count in ((6.0, 19), (-6.0, 11), (6.0, 4)):
        for _ in range(count):
            closes.append(closes[-1] + step)
    assert _fires(module, _ohlcv(closes)) == [("BUY", 53)]


def test_donchian_breakout_arms_are_symmetric_and_ranges_are_silent():
    module = _module("trend/donchian_high_breakout.nano")

    base = [100.0 + (1.0 if i % 2 else 0.0) for i in range(30)]
    assert _fires(module, _ohlcv(base + [112.0])) == [("BUY", 30)]
    # The mirror. Asserting only the long arm would leave the short arm — which
    # lives in the else branch and is the newer half of this rule — proven by
    # nothing at all.
    assert _fires(module, _ohlcv(base + [88.0])) == [("SELL", 30)]

    # Sixty bars of a repeating five-bar sawtooth: the channel is exactly the
    # range, so neither arm can ever clear it.
    assert _fires(module, _ohlcv([100.0 + (i % 5) * 0.4 for i in range(60)])) == []

    # Boundary. `_ohlcv` puts the high half a point above the close, so the prior
    # channel top is exactly 101.5. A close *at* the top does not break it —
    # `>` not `>=` — and one tick above does. The baseline corpus pins its
    # boundaries this way (`0 is NOT > 0`); the v1 corpus should too.
    assert _fires(module, _ohlcv(base + [101.5])) == []
    assert _fires(module, _ohlcv(base + [101.6])) == [("BUY", 30)]
    # The same distinction on the short arm: the floor is exactly 99.5.
    assert _fires(module, _ohlcv(base + [99.5])) == []
    assert _fires(module, _ohlcv(base + [99.4])) == [("SELL", 30)]

    # Two bars beyond the channel in a row. Bar 30 breaks out and raises the
    # channel top to its own high of 105.5, so bar 31's close of 105.2 is inside
    # the *new* channel and must not re-fire. Reading the channel at [2] instead
    # of [1] would compare bar 31 against the stale pre-breakout top of 101.5 and
    # signal the same breakout twice — which is how an offset that is off by one
    # bar hides: it stays silent on every frame where nothing changed in between.
    assert _fires(module, _ohlcv(base + [105.0, 105.2])) == [("BUY", 30)]
    # The short arm's mirror, so both offsets are pinned rather than one.
    assert _fires(module, _ohlcv(base + [95.0, 94.8])) == [("SELL", 30)]


def test_absolute_momentum_reports_risk_off_when_either_window_turns():
    module = _module("momentum/absolute_momentum_filter.nano")

    advance = [100.0 + 0.5 * i for i in range(135)]
    assert _fires(module, _ohlcv(advance)) == [("BUY", bar) for bar in range(126, 135)]

    # Both windows negative: the else arm, every period, and never a BUY.
    decline = [200.0 - 0.5 * i for i in range(135)]
    assert _fires(module, _ohlcv(decline)) == [
        ("OBSERVE", bar) for bar in range(126, 135)
    ]

    # Boundary, one operator at a time. A flat tape zeroes BOTH returns, so
    # relaxing either `>` to `>=` on its own leaves the other conjunct false and
    # the rule still reports risk-off. That frame looks like a boundary pin and
    # tests nothing: only the simultaneous mutation of both operators is caught,
    # and nobody writes that mutation. Each operator needs a frame where it alone
    # decides the bar.

    # short_return exactly zero, long_return +50.48: 114 advancing bars then 21
    # flat ones, so bar 134 reads back to bar 113 — the last advancing bar — and
    # the one-month window is flat to the tick while the six-month window is not.
    # `short_return >= 0` turns that final OBSERVE into a BUY.
    short_flat = [100.0 + 0.5 * i for i in range(114)] + [156.5] * 21
    assert _fires(module, _ohlcv(short_flat)) == [
        ("BUY", bar) for bar in range(126, 134)
    ] + [("OBSERVE", 134)]

    # The mirror: long_return exactly zero, short_return +110.53. Nine flat bars,
    # a decline to 47.5, then a recovery that lands bar 134 on exactly the value
    # of bar 126 bars earlier. Every step is a binary-exact half, so the equality
    # is real arithmetic and not a rounding coincidence — `close[134] == close[8]`
    # holds exactly. `long_return >= 0` turns bar 134 into a BUY.
    mirror = (
        [100.0] * 9
        + [100.0 - 0.5 * (i - 8) for i in range(9, 114)]
        + [47.5 + 2.5 * (i - 113) for i in range(114, 135)]
    )
    assert mirror[134] == mirror[8]
    assert _fires(module, _ohlcv(mirror)) == [
        ("OBSERVE", bar) for bar in range(126, 135)
    ]

    # The case the short window exists for: a six-month return still positive
    # only because of where it started, with the last month falling. A filter
    # reading the long window alone would report risk-on across this whole tail.
    rolled_over = [100.0 + 0.8 * i for i in range(110)] + [
        100.0 + 0.8 * 109 - 0.9 * step for step in range(1, 26)
    ]
    assert _fires(module, _ohlcv(rolled_over)) == [
        ("OBSERVE", bar) for bar in range(126, 135)
    ]


def test_stochastic_reclaim_fires_on_the_crossing_bar_only():
    module = _module("momentum/stochastic_reclaim.nano")

    decline = [100.0 - 1.0 * i for i in range(25)]
    recovery = [decline[-1] + 1.5 * step for step in range(1, 11)]
    # Ten bars of recovery, one intent — %K crosses 20 once and then stays above
    # it, which is exactly the fire-rate difference from stochastic_oversold.
    assert _fires(module, _ohlcv(decline + recovery)) == [("BUY", 26)]

    # %K never goes under 20 on a one-way advance, so there is nothing to reclaim.
    assert _fires(module, _ohlcv([100.0 + 1.0 * i for i in range(35)])) == []


def test_bollinger_reclaim_needs_the_excursion_to_end():
    module = _module("mean_reversion/bollinger_lower_reclaim.nano")

    base = [100.0 + (0.3 if i % 2 else -0.3) for i in range(28)]
    # Bar 28 closes far below the lower band; bar 29 closes back inside. Only
    # the second bar fires: entering on bar 28 would be bollinger_band_touch.
    assert _fires(module, _ohlcv(base + [92.0, 99.5])) == [("BUY", 29)]

    # Two bars outside in a row. The excursion has not ended, so the reclaim
    # has not happened — this is what `close > lower` is for, and without the
    # frame the term is unfalsifiable.
    assert _fires(module, _ohlcv(base + [92.0, 91.0, 99.0])) == [("BUY", 30)]

    # Offset consistency. On a flat tape, bar 28 closes at 94.1 against a lower
    # band of 97.13, and bar 29's violent reclaim to 122.0 widens the band so far
    # that the *current* lower band falls to 90.75. The excursion is therefore
    # real against its own bar's band and not against this one, so comparing
    # `close[1]` with the unshifted `lower` would mix two different band widths
    # and lose the signal. A margin of three points either side, not a knife edge.
    assert _fires(module, _ohlcv([100.0] * 28 + [94.1, 122.0])) == [("BUY", 29)]

    # A calm tape never leaves the bands, so there is no excursion to reclaim.
    assert _fires(module, _ohlcv(base + base)) == []


def test_zscore_fade_is_disarmed_by_the_trend_filter():
    module = _module("mean_reversion/zscore_fade_trend_filtered.nano")

    advance = [100.0 + 0.4 * i for i in range(210)]
    flush = [advance[-1] - 6.0 * step for step in range(1, 4)]
    # All three flush bars deepen the z-score. Cheapness that is still getting
    # cheaper is the rule's stated invalidation, so none may emit an intent.
    deepening = _ohlcv(advance + flush)
    assert _fires(module, deepening) == []

    # A one-point reclaim leaves z below -2 while making it less negative than
    # the prior bar. This is the first honest entry bar, and only this bar fires.
    reclaim = _ohlcv(advance + flush + [flush[-1] + 1.0])
    assert _fires(module, reclaim) == [("BUY", 213)]

    # Mutation control: deleting only the anti-deepening conjunct resurrects
    # the falling-knife behavior. The adversarial frame above must kill exactly
    # that mutation rather than passing because some unrelated term is false.
    source = (LIBRARY / "mean_reversion/zscore_fade_trend_filtered.nano").read_text()
    mutant_source = source.replace(" and z > z[1]", "")
    assert mutant_source != source
    assert _fires(compile_module(mutant_source), deepening) == [
        ("BUY", 211),
        ("BUY", 212),
    ]

    # The identical flush, grafted onto a downtrend. The z-score reaches the same
    # place; the 200-bar filter is the entire reason the rule abstains, and this
    # is the pair that proves the filter does work rather than decorate.
    decline = [200.0 - 0.4 * i for i in range(210)]
    bear_flush = [decline[-1] - 6.0 * step for step in range(1, 4)]
    assert _fires(module, _ohlcv(decline + bear_flush)) == []


def test_gap_fade_needs_the_gap_to_fail_not_merely_to_exist():
    module = _module("mean_reversion/opening_gap_fade.nano")

    base = [100.0 + (0.5 if i % 2 else -0.5) for i in range(25)]

    def _session(gap_open: float, gap_close: float):
        closes = base + [gap_close]
        opens = base + [gap_open]
        return _ohlcv(
            closes,
            opens=opens,
            highs=[max(o, c) + 0.5 for o, c in zip(opens, closes)],
            lows=[min(o, c) - 0.5 for o, c in zip(opens, closes)],
        )

    assert _fires(module, _session(108.0, 104.0)) == [("SELL", 25)]
    # The down-gap mirror, so the else arm is proven rather than assumed.
    assert _fires(module, _session(92.0, 96.0)) == [("BUY", 25)]

    # Same gap, held into the close. This is the breakaway case the header warns
    # about, and the close-against-open term is the only thing separating a fade
    # from a chase: without it this frame would fire and the rule would be short
    # a breakout.
    assert _fires(module, _session(108.0, 110.0)) == []

    # The down-gap that holds — the mirror of the case above, and the reason
    # the else arm carries its own `close > open` term.
    assert _fires(module, _session(92.0, 90.0)) == []

    # An ordinary red bar: opens where yesterday closed, closes lower. The
    # direction test is satisfied and the size test is not, which is the whole
    # job of the ATR threshold.
    assert _fires(module, _session(base[-1], base[-1] - 1.0)) == []

    # A small *up* gap that closes higher — an unremarkable green bar. This is
    # the frame that falsifies the unary minus: written `gap <= threshold`
    # instead of `gap <= -threshold`, the else arm buys this bar. Nothing else
    # in the suite can tell the two spellings apart, and the sign is the whole
    # reason the down-gap arm is a down-gap arm.
    assert _fires(module, _session(base[-1] + 0.2, base[-1] + 1.0)) == []

    # A gap of 2.30 against a prior-bar ATR threshold of 2.25 — but the gap bar's
    # own range lifts the unshifted threshold to 2.39. The rule fires because the
    # header's claim is true: the threshold is the *prior* bar's ATR. Reading the
    # current bar's ATR instead would silently raise the bar the gap must clear.
    assert _fires(module, _session(base[-1] + 2.30, base[-1] + 1.90)) == [("SELL", 25)]


def _contracting(count: int, amplitude: float = 6.0, decay: float = 0.94):
    """An oscillation around 100 whose amplitude shrinks every bar.

    Band width therefore falls monotonically, so each bar's width is its own
    rolling minimum — a squeeze that keeps getting tighter and never releases.
    """
    prices = []
    for i in range(count):
        prices.append(100.0 + (amplitude if i % 2 == 0 else -amplitude))
        amplitude *= decay
    return prices


def test_squeeze_release_fires_on_the_expansion_not_the_squeeze():
    module = _module("volatility/squeeze_release_expansion.nano")

    squeezed = _contracting(80)
    # Eighty bars pinned at their own rolling minimum width and not one intent:
    # a squeeze on its own is not a signal here, which is the difference from a
    # rule that tests width against an absolute number.
    assert _fires(module, _ohlcv(squeezed)) == []

    # The release. Bar 80 fires; bar 81 does not, because by then the *prior*
    # bar's width is no longer the fifty-bar minimum.
    assert _fires(module, _ohlcv(squeezed + [104.0, 108.0])) == [("BUY", 80)]

    # The same release, resolving downward. Width expands identically out of an
    # identically-tight squeeze; only the side differs, which is what the middle
    # band test decides. A squeeze is directionless until something picks a side.
    assert _fires(module, _ohlcv(squeezed + [96.0, 92.0])) == []


def test_atr_regime_halt_trips_on_relative_expansion_only():
    module = _module("volatility/atr_regime_halt.nano")

    calm = [100.0 + (0.2 if i % 2 else -0.2) for i in range(125)]
    highs = [c + 0.3 for c in calm]
    lows = [c - 0.3 for c in calm]
    assert _fires(module, _ohlcv(calm, highs=highs, lows=lows)) == []

    # Three bars whose range is an order of magnitude above the baseline. The
    # halt emits PAUSE then OBSERVE, in that order, on each of them: a host
    # reading the log needs the halt before the review flag, not merely both.
    shocked = _ohlcv(
        calm + [100.0, 100.0, 100.0],
        highs=highs + [110.0, 112.0, 114.0],
        lows=lows + [90.0, 88.0, 86.0],
    )
    assert _fires(module, shocked) == [
        ("PAUSE", 125),
        ("OBSERVE", 125),
        ("PAUSE", 126),
        ("OBSERVE", 126),
        ("PAUSE", 127),
        ("OBSERVE", 127),
    ]


def test_volume_climax_needs_all_three_measurements():
    module = _module("volume/volume_climax_reversal.nano")

    decline = [150.0 - 0.8 * i for i in range(58)]
    closes = decline + [decline[-1] - 1.0]
    quiet_volume = [1000.0] * 58

    def _final_bar(high: float, low: float, volume: float):
        return _ohlcv(
            closes,
            opens=closes,
            highs=[c + 0.5 for c in decline] + [high],
            lows=[c - 0.5 for c in decline] + [low],
            volumes=quiet_volume + [volume],
        )

    top = closes[-1] + 1.0
    bottom = closes[-1] - 14.0
    assert _fires(module, _final_bar(top, bottom, 9000.0)) == [("BUY", 58)]

    # Same volume, same range, close at the bottom of it. That is a breakdown
    # bar wearing a climax bar's dimensions, and the close-position term is the
    # only thing that tells them apart.
    assert _fires(module, _final_bar(closes[-1] + 14.0, closes[-1] - 1.0, 9000.0)) == []

    # Same shape, ordinary volume. Without participation it is just a wide bar.
    assert _fires(module, _final_bar(top, bottom, 1200.0)) == []

    # Enormous volume, close three-quarters of the way up the bar, still below
    # trend — but a range of 2.0 against an ATR of ~1.39, so under the 2x bar.
    # Volume without range is churn, not capitulation, and this is the only
    # frame in the pair that can tell the range term from the others.
    assert _fires(module, _final_bar(closes[-1] + 0.5, closes[-1] - 1.5, 9000.0)) == []

    # The identical climax bar at the end of an *advance*. Every term but the
    # trend location is satisfied, so this is the frame that separates a
    # capitulation from a breakout, and the rule must decline it.
    advance = [100.0 + 0.8 * i for i in range(58)]
    rising_closes = advance + [advance[-1] - 1.0]
    rising = _ohlcv(
        rising_closes,
        opens=rising_closes,
        highs=[c + 0.5 for c in advance] + [rising_closes[-1] + 1.0],
        lows=[c - 0.5 for c in advance] + [rising_closes[-1] - 14.0],
        volumes=quiet_volume + [9000.0],
    )
    assert _fires(module, rising) == []


def test_vwap_reversion_needs_a_discount_not_merely_weakness():
    module = _module("volume/vwap_band_reversion.nano")

    base = [100.0 + (0.2 if i % 2 else -0.2) for i in range(26)]
    # Both selloff bars qualify on discount and RSI, but the discount is still
    # widening. That is the header's invalidation, so entry must wait.
    deepening = _ohlcv(base + [98.0, 96.5])
    assert _fires(module, deepening) == []

    # The next close reclaims half a point. The discount remains above one
    # percent and RSI remains weak, but it has finally narrowed: exactly one buy.
    reclaim = _ohlcv(base + [98.0, 96.5, 97.0])
    assert _fires(module, reclaim) == [("BUY", 28)]

    # Mutation control: without the narrowing conjunct the same deepening frame
    # buys both falling bars. This proves the no-fire case exercises the repaired
    # term rather than succeeding through an unrelated guard.
    source = (LIBRARY / "volume/vwap_band_reversion.nano").read_text()
    mutant_source = source.replace(" and discount < discount[1]", "")
    assert mutant_source != source
    assert _fires(compile_module(mutant_source), deepening) == [
        ("BUY", 26),
        ("BUY", 27),
    ]

    # Two bars up instead of down: the close is above the rolling VWAP, so the
    # discount term is negative and the rule cannot fire regardless of RSI.
    assert _fires(module, _ohlcv(base + [100.5, 101.0])) == []

    # A steady advance — no discount at any point.
    assert _fires(module, _ohlcv([100.0 + 0.6 * i for i in range(26)])) == []

    # A slow drift lower: RSI is pinned at zero, so the RSI gate is wide open,
    # and the discount settles at roughly 0.77 percent — under the one-percent
    # threshold. Without this frame the discount term is unfalsifiable, because
    # every other frame here has the two terms agreeing.
    assert _fires(module, _ohlcv([100.0 - 0.08 * i for i in range(30)])) == []

    # The converse: an advance broken by one sharp bar that opens a 1.36 percent
    # discount to rolling VWAP while RSI is still 48. The discount qualifies and
    # the rule must still abstain, which is the only frame that tests the ceiling.
    rally = [100.0 + 0.3 * i for i in range(26)]
    assert _fires(module, _ohlcv(rally + [rally[-1] - 4.2])) == []


def test_stochastic_reclaim_fires_when_k_lands_exactly_on_the_threshold():
    """`k >= oversold` is inclusive, pinned without asserting a float artifact.

    An earlier attempt at this pin was abandoned as unpinnable: a close placed at
    the 20 percent position of a ragged range produced k = 19.999999999999957,
    so the frame would have asserted IEEE754 rather than the operator. That was a
    property of the constants, not of the boundary. With a window whose span is
    exactly 5.0 — every high 105.0, every low 100.0 — and a close exactly 1.0
    above the bottom, the kernel computes (101.0 - 100.0) / 5.0 * 100.0, which is
    exactly 20.0 in binary floating point. The prior bar sits at 100.5, giving
    k = 10.0, so the reclaim edge is real.

    Mutated to `k > oversold` this frame emits nothing.
    """
    module = _module("momentum/stochastic_reclaim.nano")
    closes = [100.5] * 19 + [101.0]
    frame = _ohlcv(closes, highs=[105.0] * 20, lows=[100.0] * 20)
    assert _fires(module, frame) == [("BUY", 19)]


def test_volume_climax_fires_when_volume_lands_exactly_on_the_surge_multiple():
    """`volume >= SMA * surge` is inclusive, pinned exactly.

    The climax bar's own volume sits inside the 20-bar average it is compared
    against, which does not make the boundary unreachable — it only fixes the
    divisor. Nineteen quiet bars at V and a climax at 3V satisfy the equality
    when 17V = 57Q; V = 1700 gives SMA = (19 * 1700 + 5700) / 20 = 1900.0 exactly
    and 3 * 1900.0 = 5700.0 exactly, with no rounding anywhere.

    Mutated to `volume > SMA * surge` this frame emits nothing.
    """
    module = _module("volume/volume_climax_reversal.nano")
    decline = [150.0 - 0.8 * i for i in range(58)]
    closes = decline + [decline[-1] - 1.0]
    frame = _ohlcv(
        closes,
        opens=closes,
        highs=[c + 0.5 for c in decline] + [closes[-1] + 1.0],
        lows=[c - 0.5 for c in decline] + [closes[-1] - 14.0],
        volumes=[1700.0] * 58 + [5700.0],
    )
    assert _fires(module, frame) == [("BUY", 58)]
