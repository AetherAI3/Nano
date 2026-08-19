"""Indicator kernels and the v1.0 VM.

The kernels are checked against values computed by hand rather than against
themselves, because a self-consistent indicator that is wrong is exactly the
failure mode a trading system cannot afford. Warm-up gets the same scrutiny as
the arithmetic: a fabricated early value inflates a backtest the way look-ahead
does.
"""

import pytest

from nano.compiler import compile_module, compile_source
from nano.indicators import evaluate, lookup
from nano.indicators.compute import (
    atr,
    ema,
    obv,
    rsi,
    sma,
    stddev,
    supertrend,
    supertrend_dir,
    true_range,
)
from nano.runtime.interpreter import MarketFrame, RuntimeError_, execute
from nano.runtime.vm import run_module


def _frame(**signals):
    length = len(next(iter(signals.values())))
    return MarketFrame(timestamps=tuple(i * 60 for i in range(length)), signals=signals)


# -- kernels ------------------------------------------------------------------


def test_sma_warms_up_then_averages():
    assert sma((1.0, 2.0, 3.0, 4.0), 3) == (None, None, 2.0, 3.0)


def test_ema_seeds_on_the_first_full_window():
    # alpha = 2/(3+1) = 0.5, seeded with mean(1,2,3) = 2.0.
    assert ema((1.0, 2.0, 3.0, 4.0, 5.0), 3) == (None, None, 2.0, 3.0, 4.0)


def test_stddev_is_population_not_sample():
    # mean 2, deviations -1/0/+1 -> variance 2/3, not 1.0.
    assert stddev((1.0, 2.0, 3.0), 3)[2] == pytest.approx((2 / 3) ** 0.5)


def test_rsi_reports_100_when_nothing_falls():
    # A window of pure gains has zero average loss; the ratio is undefined and the
    # pinned convention is 100.
    result = rsi((1.0, 2.0, 3.0, 4.0), 3)
    assert result[:3] == (None, None, None)
    assert result[3] == 100.0


def test_true_range_needs_a_previous_close():
    result = true_range((10.0, 12.0), (8.0, 9.0), (9.0, 11.0))
    assert result[0] is None
    assert result[1] == 3.0  # max(12-9, |12-9|, |9-9|)


# A six-bar tape small enough to check with a pencil. Every ATR and SuperTrend
# value asserted below is derived in the comment beside it.
#
#   bar   high    low   close   TR                                ATR(2)
#   0     10.0    8.0     9.0   absent (no previous close)         absent
#   1     11.0    9.0    10.0   max(2, |11-9|,  |9-9|)   = 2.0     absent
#   2     12.0   10.0    11.0   max(2, |12-10|, |10-10|) = 2.0     (2+2)/2   = 2.0
#   3     12.0    8.0     8.5   max(4, |12-11|, |8-11|)  = 4.0     (2*1+4)/2 = 3.0
#   4     11.0    7.0     7.5   max(4, |11-8.5|,|7-8.5|) = 4.0     (3*1+4)/2 = 3.5
#   5     14.0   12.0    13.5   max(2, |14-7.5|,|12-7.5|)= 6.5     (3.5*1+6.5)/2 = 5.0
_HIGH = (10.0, 11.0, 12.0, 12.0, 11.0, 14.0)
_LOW = (8.0, 9.0, 10.0, 8.0, 7.0, 12.0)
_CLOSE = (9.0, 10.0, 11.0, 8.5, 7.5, 13.5)


def test_atr_wilder_smooths_hand_computed_true_ranges():
    assert true_range(_HIGH, _LOW, _CLOSE) == (None, 2.0, 2.0, 4.0, 4.0, 6.5)
    # Seeded with the mean of the first two ranges, then Wilder-smoothed. The two
    # absent bars are the warm-up: there is no second range to average yet.
    assert atr(_HIGH, _LOW, _CLOSE, 2) == (None, None, 2.0, 3.0, 3.5, 5.0)


def test_supertrend_line_and_direction_match_the_hand_computation():
    # mult = 1.0, so the bands sit one ATR either side of hl2.
    #
    #  bar  hl2   basic upper / lower   final upper / lower   trend      line
    #   2   11.0    13.0 /  9.0           13.0  /  9.0        seed up     9.0
    #   3   10.0    13.0 /  7.0           13.0  /  9.0        8.5 < 9.0   13.0
    #                                                         -> down
    #   4    9.0    12.5 /  5.5           12.5  /  5.5        7.5 < 12.5  12.5
    #                                                         -> still down
    #   5   13.0    18.0 /  8.0           12.5  /  8.0        13.5 > 12.5  8.0
    #                                                         -> up
    assert supertrend(_HIGH, _LOW, _CLOSE, 2, 1.0) == (None, None, 9.0, 13.0, 12.5, 8.0)
    assert supertrend_dir(_HIGH, _LOW, _CLOSE, 2, 1.0) == (
        None,
        None,
        True,
        False,
        False,
        True,
    )


def test_supertrend_seeds_its_first_warm_bar_from_the_close_inside_the_bar():
    # Bar 2 closes at 11.0, exactly its own hl2, and the pinned convention seeds
    # up on the tie -- so the line is the lower band, 9.0.
    assert supertrend_dir(_HIGH, _LOW, _CLOSE, 2, 1.0)[2] is True
    assert supertrend(_HIGH, _LOW, _CLOSE, 2, 1.0)[2] == 9.0

    # One tick below hl2 on the same bar seeds down instead, onto the upper band.
    # Bar 2's own true range is unchanged by its close, so ATR(2) is still 2.0 and
    # the bands are the same two numbers -- only the seed reading moved.
    lower_close = (9.0, 10.0, 10.9, 8.5, 7.5, 13.5)
    assert supertrend_dir(_HIGH, _LOW, lower_close, 2, 1.0)[2] is False
    assert supertrend(_HIGH, _LOW, lower_close, 2, 1.0)[2] == 13.0


def test_supertrend_survives_a_close_resting_exactly_on_its_band():
    # Bar 3's final lower band is 9.0. A close of exactly 9.0 has not broken it,
    # so the uptrend holds -- the pinned convention is a strict break.
    resting = (9.0, 10.0, 11.0, 9.0, 7.5, 13.5)
    assert supertrend_dir(_HIGH, _LOW, resting, 2, 1.0)[3] is True

    # Positive control: half a point lower is a break, on an otherwise identical
    # tape. Without it the assertion above passes equally well against a kernel
    # that never flips at all.
    assert supertrend_dir(_HIGH, _LOW, _CLOSE, 2, 1.0)[3] is False


def test_a_gap_reseeds_supertrend_rather_than_smoothing_across_it():
    # Two more bars so the kernel has room to warm up again after the hole.
    high = _HIGH + (15.0, 16.0)
    low = _LOW + (13.0, 14.0)
    clean = _CLOSE + (14.5, 15.5)
    gapped = (9.0, 10.0, 11.0, None, 7.5, 13.5, 14.5, 15.5)

    # The gapped run goes absent at the hole and stays absent until ATR(2) has two
    # fresh ranges again. Nothing is carried over the hole and nothing is invented.
    assert supertrend(high, low, gapped, 2, 1.0)[3:6] == (None, None, None)

    # Bars 6 and 7 are identical in both tapes, yet the values differ: the gapped
    # run re-seeded from a fresh ATR (4.25, from ranges 6.5 and 2.0) instead of
    # continuing the old recursion. That difference is the point -- a kernel that
    # smoothed across the hole would return the clean numbers here.
    assert supertrend(high, low, clean, 2, 1.0)[6:] == (10.5, 12.25)
    assert supertrend(high, low, gapped, 2, 1.0)[6:] == (9.75, 11.875)


def test_supertrend_warmup_matches_its_registry_rule():
    for name in ("SUPERTREND", "SUPERTREND_DIR"):
        spec = lookup(name)
        assert spec.lookback((10,)) == 10
        assert spec.series_indices == (0, 1, 2)
        assert spec.period_indices == (3,)

    # And the dispatch table reaches the same kernels the direct import did.
    assert evaluate("SUPERTREND", [_HIGH, _LOW, _CLOSE, 2, 1.0], length=6) == (
        None,
        None,
        9.0,
        13.0,
        12.5,
        8.0,
    )


def test_a_gap_resets_a_recursive_kernel():
    # EMA smooths across contiguous runs only. Smoothing over a hole would make the
    # result depend on how the feed happened to be chunked.
    assert ema((1.0, 2.0, 3.0, None, 4.0, 5.0, 6.0), 3) == (
        None,
        None,
        2.0,
        None,
        None,
        None,
        5.0,
    )


def test_obv_accumulates_signed_volume():
    assert obv((10.0, 11.0, 10.5), (100.0, 200.0, 300.0)) == (None, 200.0, -100.0)


def test_absent_cells_never_become_zero():
    assert sma((1.0, None, 3.0), 2) == (None, None, None)


def test_macd_warmup_matches_its_registry_rule():
    spec = lookup("MACD_HIST")
    assert spec.lookback((12, 26, 9)) == 33
    values = tuple(float(i) for i in range(60))
    result = evaluate("MACD_HIST", [values, 12, 26, 9], length=60)
    assert all(cell is None for cell in result[:33])
    assert result[33] is not None


def test_scalar_maths_broadcasts_a_constant():
    assert evaluate("MAX", [(1.0, 5.0), 3.0], length=2) == (3.0, 5.0)


# -- the VM -------------------------------------------------------------------


def test_computed_indicator_drives_a_rule():
    module = compile_module(
        "strategy S {\n"
        "    input close: series<float>\n"
        "    let fast = SMA(close, 2)\n"
        "    every 1m {\n"
        "        if close > fast {\n"
        "            buy(BTC, 0.5)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    # bar 0 unwarmed; bar 1 close=2 vs sma=1.5 fires; bar 2 close=1 vs 1.5 does not.
    result = run_module(module, _frame(close=(1.0, 2.0, 1.0)))
    assert [i.timestamp for i in result.intents] == [60]
    assert result.warmup_bars_skipped == 1


def test_unwarmed_bars_do_not_emit_and_are_counted():
    module = compile_module(
        "strategy S {\n"
        "    input close: series<float>\n"
        "    let slow = SMA(close, 5)\n"
        "    every 1m {\n"
        "        if slow > 0 {\n"
        "            buy(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    result = run_module(module, _frame(close=(1.0, 2.0, 3.0)))
    assert result.intents == ()
    assert result.warmup_bars_skipped == 3


def test_else_branch_runs_when_the_condition_is_false():
    module = compile_module(
        "strategy S {\n"
        "    every 1m {\n"
        "        if RSI < 30 {\n"
        "            buy(BTC)\n"
        "        } else {\n"
        "            observe()\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    result = run_module(module, _frame(RSI=(25.0, 80.0)))
    assert [(i.action, i.timestamp) for i in result.intents] == [
        ("BUY", 0),
        ("OBSERVE", 60),
    ]


def test_series_offset_reads_the_previous_bar():
    module = compile_module(
        "strategy S {\n"
        "    input close: series<float>\n"
        "    every 1m {\n"
        "        if close > close[1] {\n"
        "            buy(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    result = run_module(module, _frame(close=(10.0, 11.0, 9.0)))
    assert [i.timestamp for i in result.intents] == [60]


def test_two_rules_in_one_schedule_both_evaluate():
    module = compile_module(
        "strategy S {\n"
        "    every 1m {\n"
        "        if RSI < 30 {\n"
        "            buy(BTC)\n"
        "        }\n"
        "        if RSI > 70 {\n"
        "            sell(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    result = run_module(module, _frame(RSI=(20.0, 50.0, 90.0)))
    assert [(i.action, i.timestamp) for i in result.intents] == [
        ("BUY", 0),
        ("SELL", 120),
    ]


def test_escalation_is_recorded_with_its_reason():
    module = compile_module(
        "tier nano+\n"
        "strategy S {\n"
        "    agent Desk { role research }\n"
        "    every 1m {\n"
        "        if confidence < 0.6 {\n"
        "            escalate Desk\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    # `confidence` is injected like time and entropy -- here, from the frame.
    result = run_module(module, _frame(confidence=(0.9, 0.4)))
    assert [(e.target, e.timestamp, e.is_agent) for e in result.escalations] == [
        ("Desk", 60, True)
    ]


_REASONING_SOURCE = (
    "tier nano+\n"
    "strategy S {\n"
    "    input close: series<float>\n"
    "    signature Bias {\n"
    "        input price: float\n"
    "        output score: float\n"
    "    }\n"
    "    let call = infer(Bias, close)\n"
    "    every 1m {\n"
    "        if call.score > 0.5 {\n"
    "            buy(BTC)\n"
    "        }\n"
    "    }\n"
    "}\n"
)


def test_a_module_needing_reasoning_with_no_provider_emits_nothing():
    # Absent, not defaulted. A strategy that needed a model and was given none
    # should produce no signal rather than a confident guess.
    result = run_module(compile_module(_REASONING_SOURCE), _frame(close=(1.0, 2.0)))
    assert result.intents == ()
    assert any(entry.event == "infer.skipped" for entry in result.log)


def test_an_injected_provider_makes_reasoning_replayable():
    class Recorded:
        """A provider replaying a fixed transcript, which keeps the run pure."""

        def __init__(self, scores):
            self.scores = scores

        def infer(self, signature, inputs, *, timestamp):
            return {"score": self.scores[timestamp]}

    module = compile_module(_REASONING_SOURCE)
    frame = _frame(close=(1.0, 2.0))
    scores = {0: 0.9, 60: 0.1}

    first = run_module(module, frame, provider=Recorded(scores))
    second = run_module(module, frame, provider=Recorded(scores))

    assert [i.timestamp for i in first.intents] == [0]
    assert first.to_dict() == second.to_dict()


def test_module_hash_is_stable_and_excludes_itself():
    source = (
        "strategy S {\n"
        "    input close: series<float>\n"
        "    every 1m {\n"
        "        if close > 1 {\n"
        "            buy(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    module = compile_module(source)
    assert module.content_hash() == compile_module(source).content_hash()
    assert "moduleHash" not in module.to_dict(include_hash=False)
    assert module.to_dict()["moduleHash"] == module.content_hash()


def test_comment_only_edits_change_source_hash_but_not_module_hash():
    # The two hashes answer different questions -- "was the file edited?" and "was
    # the behavior edited?" -- and collapsing them would lose the distinction.
    base = "strategy S {\n    every 1m {\n        observe()\n    }\n}\n"
    commented = "// a note\n" + base
    assert compile_module(base).content_hash() == compile_module(commented).content_hash()
    assert compile_module(base).source_hash != compile_module(commented).source_hash


def test_replay_is_bit_identical():
    module = compile_module(
        "strategy S {\n"
        "    input close: series<float>\n"
        "    let z = ZSCORE(close, 3)\n"
        "    every 1m {\n"
        "        if z > 0 {\n"
        "            buy(BTC, 0.7)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    frame = _frame(close=(1.0, 3.0, 2.0, 8.0, 5.0))
    assert run_module(module, frame).to_dict() == run_module(module, frame).to_dict()


def test_vm_reports_a_missing_signal_rather_than_guessing():
    module = compile_module(
        "strategy S {\n"
        "    every 1m {\n"
        "        if RSI < 30 {\n"
        "            buy(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    with pytest.raises(RuntimeError_, match="RSI"):
        run_module(module, _frame(close=(1.0,)))


# -- the anti-drift weld ------------------------------------------------------


def test_lifted_baseline_graph_matches_the_reference_interpreter():
    """A baseline graph must behave identically through both execution paths.

    This is the guarantee that lets v1.0 exist without invalidating v0.1.0
    artifacts: the reference interpreter defines correct behavior, and the VM
    reproduces it for anything baseline can express.
    """
    graph = compile_source(
        "strategy S {\n"
        "    every 15m {\n"
        "        if RSI(14) < 30 and Volume > 1000 {\n"
        "            buy(BTCUSD, 0.85)\n"
        "            observe()\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    frame = MarketFrame(
        timestamps=(0, 900, 1800),
        signals={"RSI": (50.0, 25.0, 20.0), "Volume": (2000.0, 2000.0, 500.0)},
    )
    reference = [i.to_dict() for i in execute(graph, frame).intents]
    through_vm = [i.to_dict() for i in run_module(graph.to_module(), frame).intents]
    assert reference == through_vm
    assert len(reference) == 2  # both actions fire, on the middle bar only
