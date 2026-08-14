"""Conformance + execution suite for the Nano strategy library.

Mirrors tests/test_conformance.py but globs nano/library/** — every published
library strategy is a `.nano`/`_ir.json` pair that must compile bit-identically
and round-trip through StrategyGraph. Representative strategies also get
execution tests against crafted MarketFrames asserting exact fire/no-fire
behavior.
"""

import json
from pathlib import Path

import pytest

from nano.compiler import compile_source, compile_to_dict
from nano.ir.graph import StrategyGraph
from nano.runtime.interpreter import MarketFrame, execute

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
    frame = MarketFrame(
        timestamps=(0, 300, 600, 900, 1200, 1500, 1800),
        signals={"CONSECUTIVE_LOSSES": (1.0, 2.0, 3.0, 0.0, 1.0, 2.0, 3.0)},
    )
    assert execute(graph, frame).intents == ()


def test_correlation_cluster_guard_ignores_a_diversified_book():
    graph = _load("risk/correlation_cluster_guard.nano")
    frame = MarketFrame(
        timestamps=(0, 900, 1800),
        signals={"CLUSTER_EXPOSURE_PCT": (12.0, 28.0, 39.9)},  # never >= 40
    )
    assert execute(graph, frame).intents == ()


def test_atr_halt_emits_no_intent_in_calm_regime():
    graph = _load("volatility/atr_volatility_halt.nano")
    frame = MarketFrame(
        timestamps=(0, 300, 600),
        signals={"ATR_PCT": (1.2, 3.0, 5.0)},  # never > 5
    )
    result = execute(graph, frame)
    assert result.intents == ()
