"""Golden, gap, causality, typing, and replay tests for series primitives."""

from pathlib import Path

import pytest

from nano.compiler import NanoTypeError, compile_module
from nano.indicators import evaluate, lookup, names
from nano.runtime import MarketFrame, run_module


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _frame(**signals):
    length = len(next(iter(signals.values())))
    return MarketFrame(
        timestamps=tuple(index * 60 for index in range(length)), signals=signals
    )


def test_bars_since_hand_computed_golden_and_first_event_boundary():
    assert evaluate(
        "BARS_SINCE",
        [(False, True, False, False, True, False)],
        length=6,
    ) == (None, 0.0, 1.0, 2.0, 0.0, 1.0)


def test_bars_since_gap_clears_event_memory():
    assert evaluate(
        "BARS_SINCE",
        [(True, False, None, False, True, False)],
        length=6,
    ) == (0.0, 1.0, None, None, 0.0, 1.0)


def test_count_true_hand_computed_golden():
    assert evaluate(
        "COUNT_TRUE",
        [(True, False, True, True, False), 3],
        length=5,
    ) == (None, None, 2.0, 2.0, 2.0)


def test_count_true_requires_a_full_gap_free_window():
    assert evaluate(
        "COUNT_TRUE",
        [(True, False, None, True, True, False), 2],
        length=6,
    ) == (None, 1.0, None, None, 2.0, 1.0)


def test_count_true_period_one_boundary():
    assert evaluate(
        "COUNT_TRUE", [(True, False, None), 1], length=3
    ) == (1.0, 0.0, None)


def test_rising_and_falling_use_strict_pairwise_monotonicity():
    values = (1.0, 2.0, 3.0, 3.0, 4.0, 5.0)
    assert evaluate("RISING", [values, 3], length=6) == (
        None,
        None,
        True,
        False,
        False,
        True,
    )
    assert evaluate("FALLING", [tuple(reversed(values)), 3], length=6) == (
        None,
        None,
        True,
        False,
        False,
        True,
    )

    # The current value is a four-bar high, but the window is not monotone.
    # This distinguishes Nano's convention from a current-vs-prior-maximum test.
    assert evaluate("RISING", [(5.0, 1.0, 4.0, 6.0), 4], length=4)[-1] is False


def test_monotone_period_one_and_gap_boundaries_are_pinned():
    values = (1.0, None, 2.0)
    assert evaluate("RISING", [values, 1], length=3) == (True, None, True)
    assert evaluate("FALLING", [values, 1], length=3) == (True, None, True)
    assert evaluate("RISING", [(1.0, 2.0, None, 3.0, 4.0), 2], length=5) == (
        None,
        True,
        None,
        None,
        True,
    )


def test_percentrank_hand_computed_golden_scale_and_ties():
    result = evaluate(
        "PERCENTRANK",
        [(4.0, 1.0, 3.0, 2.0, 5.0, 5.0, 0.0), 3],
        length=7,
    )
    assert result[:3] == (None, None, None)
    assert result[3] == pytest.approx(1.0 / 3.0)
    assert result[4:] == (1.0, 1.0, 0.0)


def test_percentrank_excludes_current_and_resets_across_a_gap():
    # Excluding the current value makes zero reachable. Inclusive ties make the
    # second 5.0 rank at one rather than two-thirds.
    assert evaluate("PERCENTRANK", [(5.0, 4.0, 4.0), 1], length=3) == (
        None,
        0.0,
        1.0,
    )
    assert evaluate(
        "PERCENTRANK",
        [(1.0, 2.0, None, 3.0, 4.0, 5.0), 2],
        length=6,
    ) == (None, None, None, None, None, 1.0)


def test_registry_signatures_and_warmups_are_exact():
    expected = {
        "BARS_SINCE": ("BARS_SINCE(series<bool>) -> series<float>", 0),
        "COUNT_TRUE": ("COUNT_TRUE(series<bool>, int) -> series<float>", 2),
        "RISING": ("RISING(series<float>, int) -> series<bool>", 2),
        "FALLING": ("FALLING(series<float>, int) -> series<bool>", 2),
        "PERCENTRANK": ("PERCENTRANK(series<float>, int) -> series<float>", 3),
    }
    for name, (signature, lookback) in expected.items():
        spec = lookup(name)
        assert spec is not None
        assert spec.signature_text() == signature
        periods = () if name == "BARS_SINCE" else (3,)
        assert spec.lookback(periods) == lookback


def test_public_kernel_count_matches_registry():
    count = len(names())
    assert count == 40

    expected_by_path = {
        "README.md": (f"{count} deterministic kernels",),
        "docs/status.md": (f"{count} deterministic kernels",),
        "nano/library/README.md": (
            f"{count} deterministic kernels",
            f"— {count} of them",
        ),
    }
    for relative_path, expected_phrases in expected_by_path.items():
        text = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert all(phrase in text for phrase in expected_phrases)


@pytest.mark.parametrize(
    "expression",
    [
        "BARS_SINCE(close)",
        "COUNT_TRUE(close, 3)",
        "RISING(close > 0, 3)",
        "FALLING(close > 0, 3)",
        "PERCENTRANK(close > 0, 3)",
    ],
)
def test_primitive_series_element_types_are_enforced(expression):
    source = (
        "strategy S {\n"
        "    input close: series<float>\n"
        f"    let value = {expression}\n"
        "    every 1m { observe() }\n"
        "}\n"
    )
    with pytest.raises(NanoTypeError, match="Argument 1"):
        compile_module(source)


@pytest.mark.parametrize(
    ("name", "series", "period"),
    [
        ("BARS_SINCE", (False, True, False, None, True, False), None),
        ("COUNT_TRUE", (False, True, False, True, True, False), 3),
        ("RISING", (3.0, 1.0, 2.0, 4.0, 4.0, 5.0), 3),
        ("FALLING", (3.0, 4.0, 2.0, 1.0, 1.0, 0.0), 3),
        ("PERCENTRANK", (3.0, 1.0, 2.0, 4.0, 2.0, 5.0), 3),
    ],
)
def test_every_primitive_is_prefix_causal(name, series, period):
    args = [series] if period is None else [series, period]
    full = evaluate(name, args, length=len(series))
    for end in range(1, len(series) + 1):
        prefix_args = [series[:end]] if period is None else [series[:end], period]
        prefix = evaluate(name, prefix_args, length=end)
        assert prefix[-1] == full[end - 1]


def test_selected_pack_compiles_and_replays_deterministically():
    module = compile_module(
        "strategy TemporalPack {\n"
        "    input close: series<float>\n"
        "    input volume: series<float>\n"
        "    let impulse = volume > SMA(volume, 3) * 2\n"
        "    let recent = BARS_SINCE(impulse) <= 2\n"
        "    let count = COUNT_TRUE(impulse, 3)\n"
        "    let trend = RISING(close, 3)\n"
        "    let not_falling = FALLING(close, 3) == false\n"
        "    let regime = PERCENTRANK(close, 3) > 0.75\n"
        "    every 1m {\n"
        "        if recent and count >= 1 and trend and not_falling and regime {\n"
        "            buy(BTC, 0.5)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    frame = _frame(
        close=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0),
        volume=(1.0, 1.0, 10.0, 1.0, 1.0, 1.0),
    )

    first = run_module(module, frame)
    second = run_module(module, frame)

    assert [intent.timestamp for intent in first.intents] == [240]
    assert first.to_dict() == second.to_dict()
    assert module.warmup == 4

    selected = {"BARS_SINCE", "COUNT_TRUE", "RISING", "FALLING", "PERCENTRANK"}
    nodes = {node.attrs["name"]: node for node in module.of_op("indicator")}
    assert module.to_dict()["nanoIrVersion"] == "1.0.0"
    assert selected <= nodes.keys()
    for name in selected:
        assert set(nodes[name].attrs) == {"name", "periods", "lookback", "lifted"}
