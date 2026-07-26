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
from nano.indicators.compute import ema, obv, rsi, sma, stddev, true_range
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
