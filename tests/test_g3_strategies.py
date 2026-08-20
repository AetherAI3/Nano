"""Adversarial controls for the deliberately small G3 strategy batch.

Every negative frame reaches a warmed condition and differs from a positive
control in one essential hypothesis.  Term-removal mutants prove those frames
would fail if the corresponding condition disappeared.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nano.compiler import compile_module, compile_to_dict
from nano.indicators import evaluate
from nano.ir.module import NanoModule
from nano.runtime import MarketFrame, run_module


LIBRARY = Path(__file__).resolve().parents[1] / "nano" / "library"


def _source(relative: str) -> str:
    return (LIBRARY / relative).read_text(encoding="utf-8")


def _module(relative: str) -> NanoModule:
    return compile_module(_source(relative))


def _frame(**signals) -> MarketFrame:
    length = len(next(iter(signals.values())))
    return MarketFrame(
        timestamps=tuple(index * 86400 for index in range(length)),
        signals={
            name: tuple(None if cell is None else float(cell) for cell in values)
            for name, values in signals.items()
        },
    )


def _fires(module: NanoModule, frame: MarketFrame) -> list[tuple[str, int]]:
    return [
        (intent.action, intent.timestamp // 86400)
        for intent in run_module(module, frame).intents
    ]


def _breakout_frame(*, volume: float, breakout: bool = True) -> MarketFrame:
    closes = [100.0] * 20 + [102.0 if breakout else 100.0]
    highs = [100.5] * 20 + [102.5 if breakout else 100.5]
    volumes = [1000.0] * 20 + [volume]
    return _frame(high=highs, close=closes, volume=volumes)


_RECLAIM_BASE = [
    100.0 + (0.3 if index % 2 else -0.3) for index in range(130)
]


def _reclaim_frame(tail: list[float], *, wide_history: bool) -> MarketFrame:
    """Build the same close shape in ordinary or extreme current volatility.

    Wide early ranges make the final ATR ordinary relative to its preceding
    hundred values.  Calm history makes the excursion's final ATR the unique
    maximum, so its inclusive rank is exactly 1.0.  Bollinger bands read close
    only, leaving the reclaim shape identical between the two frames.
    """

    closes = _RECLAIM_BASE + tail
    widths = [
        10.0 if wide_history and index < 100 else 0.5
        for index in range(len(closes))
    ]
    highs = [close + width for close, width in zip(closes, widths)]
    lows = [close - width for close, width in zip(closes, widths)]
    return _frame(high=highs, low=lows, close=closes)


def _final_atr_rank(frame: MarketFrame) -> float:
    length = len(frame.timestamps)
    atr = evaluate(
        "ATR",
        [
            frame.signals["high"],
            frame.signals["low"],
            frame.signals["close"],
            14,
        ],
        length=length,
    )
    ranks = evaluate("PERCENTRANK", [atr, 100], length=length)
    assert ranks[-1] is not None
    return float(ranks[-1])


@pytest.mark.parametrize(
    "relative",
    [
        "trend/volume_confirmed_channel_breakout.nano",
        "mean_reversion/volatility_percentile_reclaim.nano",
    ],
)
def test_g3_sources_compile_to_their_canonical_v1_ir(relative: str):
    source_path = LIBRARY / relative
    ir_path = source_path.with_name(f"{source_path.stem}_ir.json")
    compiled = compile_to_dict(source_path.read_text(encoding="utf-8"))

    assert compiled["nanoIrVersion"] == "1.0.0"
    assert compiled == json.loads(ir_path.read_text(encoding="utf-8"))


def test_g3_warmups_are_derived_from_the_landed_registry_contracts():
    breakout = _module("trend/volume_confirmed_channel_breakout.nano")
    reclaim = _module("mean_reversion/volatility_percentile_reclaim.nano")

    # HIGHEST(20) and SMA(20) each look back 19 bars; reading both at [1]
    # raises the module warmup to 20.
    assert breakout.warmup == 20
    breakout_lookbacks = {
        node.attrs["name"]: node.attrs["lookback"]
        for node in breakout.of_op("indicator")
    }
    assert breakout_lookbacks == {"HIGHEST": 19, "SMA": 19}

    # ATR(14) first exists after 14 bars. PERCENTRANK then requires exactly
    # one hundred preceding ATR cells, so its cumulative lookback is 114.
    assert reclaim.warmup == 114
    reclaim_lookbacks = {
        node.attrs["name"]: node.attrs["lookback"]
        for node in reclaim.of_op("indicator")
    }
    assert reclaim_lookbacks == {
        "BB_LOWER": 19,
        "ATR": 14,
        "PERCENTRANK": 114,
    }


def test_volume_breakout_requires_price_and_prior_volume_confirmation():
    relative = "trend/volume_confirmed_channel_breakout.nano"
    source = _source(relative)
    module = compile_module(source)

    # 1500 is exactly 1.5 times the untouched prior average of 1000. Reading
    # the unshifted average would dilute the boundary with the current bar and
    # incorrectly reject this positive control.
    positive = _breakout_frame(volume=1500.0)
    assert _fires(module, positive) == [("BUY", 20)]

    ordinary_volume = _breakout_frame(volume=1000.0)
    no_price_break = _breakout_frame(volume=1500.0, breakout=False)
    assert _fires(module, ordinary_volume) == []
    assert _fires(module, no_price_break) == []

    no_volume_term = source.replace(
        " and volume >= prior_volume * volume_multiple", ""
    )
    assert no_volume_term != source
    assert _fires(compile_module(no_volume_term), ordinary_volume) == [("BUY", 20)]

    no_price_term = source.replace("close > prior_top and ", "")
    assert no_price_term != source
    assert _fires(compile_module(no_price_term), no_price_break) == [("BUY", 20)]


def test_percentile_reclaim_vetoes_the_same_shape_at_extreme_atr_rank():
    relative = "mean_reversion/volatility_percentile_reclaim.nano"
    source = _source(relative)
    module = compile_module(source)

    ordinary = _reclaim_frame([92.0, 99.5], wide_history=True)
    extreme = _reclaim_frame([92.0, 99.5], wide_history=False)

    assert _final_atr_rank(ordinary) < 0.9
    assert _final_atr_rank(extreme) == 1.0
    assert _fires(module, ordinary) == [("BUY", 131)]
    assert _fires(module, extreme) == []

    no_percentile_gate = source.replace(" and atr_rank < max_atr_rank", "")
    assert no_percentile_gate != source
    assert _fires(compile_module(no_percentile_gate), extreme) == [("BUY", 131)]


def test_percentile_reclaim_requires_both_halves_of_the_strict_reclaim():
    relative = "mean_reversion/volatility_percentile_reclaim.nano"
    source = _source(relative)
    module = compile_module(source)

    # The prior close is outside, but the current close deepens the excursion.
    # Removing only the current reclaim term converts that invalidation to a buy.
    deepening = _reclaim_frame([92.0, 91.0], wide_history=True)
    assert _fires(module, deepening) == []
    no_current_reclaim = source.replace("close > lower and ", "")
    assert no_current_reclaim != source
    assert _fires(compile_module(no_current_reclaim), deepening) == [("BUY", 131)]

    # Both bars are inside the band, so there was no excursion to reclaim.
    # Removing the prior-excursion half makes ordinary inside-band bars fire.
    no_excursion = _reclaim_frame([99.5], wide_history=True)
    assert _fires(module, no_excursion) == []
    no_prior_excursion = source.replace(" and close[1] <= lower[1]", "")
    assert no_prior_excursion != source
    assert ("BUY", 130) in _fires(compile_module(no_prior_excursion), no_excursion)


def test_percentile_reclaim_rebuilds_its_full_history_after_a_gap():
    module = _module("mean_reversion/volatility_percentile_reclaim.nano")
    continuous = _reclaim_frame([92.0, 99.5], wide_history=True)

    signals = {name: list(values) for name, values in continuous.signals.items()}
    for values in signals.values():
        values[100] = None
    gapped = _frame(**signals)

    # The identical final reclaim remains inside the 114-bar post-gap warmup.
    # It must stay absent rather than reusing percentile history from before
    # the missing cell.
    assert _fires(module, continuous) == [("BUY", 131)]
    assert _fires(module, gapped) == []


@pytest.mark.parametrize(
    ("relative", "frame"),
    [
        (
            "trend/volume_confirmed_channel_breakout.nano",
            _breakout_frame(volume=1500.0),
        ),
        (
            "mean_reversion/volatility_percentile_reclaim.nano",
            _reclaim_frame([92.0, 99.5], wide_history=True),
        ),
    ],
)
def test_g3_positive_controls_replay_deterministically(
    relative: str, frame: MarketFrame
):
    module = _module(relative)
    first = run_module(module, frame)
    second = run_module(module, frame)

    assert first.intents
    assert first.to_dict() == second.to_dict()
