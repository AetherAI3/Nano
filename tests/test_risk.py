"""Runtime enforcement of the `risk { ... }` block.

Before this suite existed, `risk { max_drawdown 0.05 }` parsed, type-checked,
range-checked, and landed in the IR — and then nothing read it. A limit that
reaches the IR and stops there is worse than no limit at all, because the
document says the strategy is guarded and the run is not.

So every assertion here is about what the *runtime* does, and every negative
assertion is paired with a positive control that fires the same rule. A test
that says "nothing was suppressed" proves nothing unless the same limit,
breached, does suppress.

The two limits Nano deliberately does **not** enforce (`max_position_size`,
`max_open_positions`) are tested too — for the fact that they are announced as
unenforced in the log rather than silently ignored.
"""

import math

import pytest

from nano.cli.commands import EXIT_DIAGNOSTICS, EXIT_OK
from nano.cli.main import main
from nano.compiler import compile_module
from nano.compiler.errors import NanoSyntaxError, NanoTypeError
from nano.ir import IRNode, NanoModule, load_module
from nano.ir.schema import (
    INTEGER_RISK_LIMITS,
    RISK_LIMITS,
    IRValidationError,
    validate_risk_limit,
)
from nano.runtime.effects import Intent
from nano.runtime.interpreter import MarketFrame
from nano.runtime.risk import (
    ACTUATING_ACTIONS,
    ENFORCED_LIMITS,
    HOST_ENFORCED_LIMITS,
    MEASUREMENT_PREFIX,
    RiskGate,
)
from nano.runtime.vm import run_module

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _frame(length: int, **signals):
    """A frame of `length` bars at 60s spacing, plus whatever signals are given."""
    return MarketFrame(
        timestamps=tuple(i * 60 for i in range(length)), signals=dict(signals)
    )


def _strategy(risk_body: str, action: str = "buy(BTC, 1.0)") -> str:
    """A strategy whose rule always fires, so only the risk gate can stop it."""
    risk = f"    risk {{\n{risk_body}\n    }}\n" if risk_body else ""
    return (
        "strategy Guarded {\n"
        f"{risk}"
        "    every 1m {\n"
        "        if TRIGGER > 0 {\n"
        f"            {action}\n"
        "        }\n"
        "    }\n"
        "}\n"
    )


def _run(source: str, length: int = 3, **signals):
    module = compile_module(source)
    frame = _frame(length, TRIGGER=(1.0,) * length, **signals)
    return run_module(module, frame)


def _events(result, name: str):
    return [e for e in result.log if e.event == name]


def _emitted_bars(result):
    """The bar index of every intent that survived the gate."""
    return [entry.timestamp // 60 for entry in result.intents]


# ---------------------------------------------------------------------------
# the vocabulary table itself
# ---------------------------------------------------------------------------


def test_every_declared_risk_limit_is_either_enforced_or_named_unenforceable():
    """No limit may fall between the two lists.

    This is the guard against the failure the whole lane exists to fix: a
    keyword added to the grammar, forgotten by the runtime, and silently
    believed by everyone reading the IR.
    """
    enforced = {rule.limit for rule in ENFORCED_LIMITS}
    host = {name for name, _ in HOST_ENFORCED_LIMITS}
    assert enforced.isdisjoint(host)
    assert enforced | host == set(RISK_LIMITS)


def test_enforced_limits_keep_the_declared_vocabulary_order():
    """Violation order is the schema's order, not dictionary luck.

    Two simultaneous breaches must log in the same order on every run and every
    interpreter, so the order is pinned to one visible list.
    """
    declared = [name for name in RISK_LIMITS if name in {r.limit for r in ENFORCED_LIMITS}]
    assert [rule.limit for rule in ENFORCED_LIMITS] == declared


def test_measurement_names_live_in_a_namespace_source_cannot_spell():
    """`risk.drawdown` contains a dot, and no Nano identifier can.

    That is what keeps a host measurement from ever colliding with a feed
    signal a strategy reads by name.
    """
    assert "." in MEASUREMENT_PREFIX
    for rule in ENFORCED_LIMITS:
        if rule.measurement is not None:
            assert rule.measurement.startswith(MEASUREMENT_PREFIX)
    with pytest.raises(NanoSyntaxError):
        compile_module(
            "strategy S {\n"
            "    every 1m {\n"
            "        if risk.drawdown > 1 {\n"
            "            observe()\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
    assert [rule.measurement for rule in ENFORCED_LIMITS if rule.measurement] == [
        "risk.daily_loss",
        "risk.drawdown",
        "risk.orders_today",
        "risk.consecutive_losses",
    ]


# ---------------------------------------------------------------------------
# baseline compatibility — a module with no risk block is untouched
# ---------------------------------------------------------------------------


def test_a_strategy_without_a_risk_block_gains_no_risk_events():
    result = _run(_strategy(""))
    assert _emitted_bars(result) == [0, 1, 2]
    assert [e for e in result.log if e.event.startswith("risk.")] == []
    assert _events(result, "intent.suppressed") == []


def test_positive_control_the_same_strategy_with_a_breached_limit_is_gated():
    """Proves the test above is not vacuous: the rule can be stopped."""
    result = _run(
        _strategy("        max_drawdown 0.05"),
        **{"risk.drawdown": (0.9, 0.9, 0.9)},
    )
    assert _emitted_bars(result) == []
    assert len(_events(result, "intent.suppressed")) == 3


def test_a_baseline_graph_lifted_into_the_vm_stays_bit_identical():
    """Baseline IR has no risk node, so the gate must be invisible to it."""
    source = (
        "strategy Baseline {\n"
        "    every 1m {\n"
        "        if DRAWDOWN >= 3 {\n"
        "            pause()\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    module = compile_module(source)
    frame = _frame(3, DRAWDOWN=(1.0, 3.0, 5.0))
    first = run_module(module, frame)
    second = run_module(module, frame)
    assert first.to_dict() == second.to_dict()
    assert [i.action for i in first.intents] == ["PAUSE", "PAUSE"]
    assert [e for e in first.log if e.event.startswith("risk.")] == []


# ---------------------------------------------------------------------------
# boundary: the allowed band is inclusive, breach is strictly outside it
# ---------------------------------------------------------------------------


def test_max_drawdown_allows_the_limit_exactly_and_breaches_just_above():
    just_below = math.nextafter(0.05, 0.0)
    just_above = math.nextafter(0.05, 1.0)
    result = _run(
        _strategy("        max_drawdown 0.05"),
        **{"risk.drawdown": (just_below, 0.05, just_above)},
    )
    assert _emitted_bars(result) == [0, 1]


def test_max_daily_loss_allows_the_limit_exactly_and_breaches_just_above():
    just_above = math.nextafter(0.02, 1.0)
    result = _run(
        _strategy("        max_daily_loss 0.02"),
        **{"risk.daily_loss": (0.019, 0.02, just_above)},
    )
    assert _emitted_bars(result) == [0, 1]


def test_max_orders_per_day_breaches_at_the_limit_not_after_it():
    result = _run(
        _strategy("        max_orders_per_day 5"),
        **{"risk.orders_today": (4.0, 5.0, 6.0)},
    )
    assert _emitted_bars(result) == [0]


def test_max_orders_per_day_accounts_for_intents_already_emitted_this_frame():
    source = (
        "strategy Capacity {\n"
        "    risk {\n"
        "        max_orders_per_day 2\n"
        "    }\n"
        "    every 1m {\n"
        "        if TRIGGER > 0 {\n"
        "            buy(BTC, 1.0)\n"
        "            sell(ETH, 1.0)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    result = run_module(
        compile_module(source),
        _frame(1, TRIGGER=(1.0,), **{"risk.orders_today": (1.0,)}),
    )

    assert [(intent.action, intent.asset) for intent in result.intents] == [
        ("BUY", "BTC")
    ]
    assert len(_events(result, "intent.suppressed")) == 1
    assert "max_orders_per_day" in _events(result, "intent.suppressed")[0].detail


@pytest.mark.parametrize(
    "hostile",
    [True, -1, 1.5, float("nan"), float("inf"), "1", None, 10**1000],
    ids=[
        "bool",
        "negative",
        "fractional",
        "nan",
        "inf",
        "string",
        "none",
        "oversized",
    ],
)
def test_max_orders_per_day_rejects_malformed_emitted_capacity(hostile):
    gate = RiskGate(
        {"max_orders_per_day": 5},
        _frame(1, **{"risk.orders_today": (0.0,)}),
    )

    violations = gate.review(
        Intent("BUY", 0, "BTC", 1.0), 0, emitted_capacity=hostile
    )

    assert [violation.limit for violation in violations] == ["max_orders_per_day"]
    assert "accepted intent capacity outside valid domain" in violations[0].detail
    assert "nonnegative integer" in violations[0].detail
    assert "fail-closed" in violations[0].detail


def test_emitted_capacity_is_irrelevant_to_rules_that_do_not_count_orders():
    gate = RiskGate(
        {"max_drawdown": 0.05},
        _frame(1, **{"risk.drawdown": (0.01,)}),
    )

    assert gate.review(
        Intent("BUY", 0, "BTC", 1.0), 0, emitted_capacity=float("nan")
    ) == ()


def test_stop_trading_after_losses_breaches_at_the_count_not_after_it():
    """`stop_trading_after_losses 3` means the third loss stops trading.

    The allowed band is still inclusive — it is just the band below the count,
    [0, 2], because the limit names the first unacceptable value rather than the
    last acceptable one.
    """
    result = _run(
        _strategy("        stop_trading_after_losses 3"),
        **{"risk.consecutive_losses": (2.0, 3.0, 4.0)},
    )
    assert _emitted_bars(result) == [0]


def test_min_confidence_allows_the_limit_exactly_and_breaches_just_below():
    at_limit = _run(
        _strategy("        min_confidence 0.6", action="buy(BTC, 0.6)"),
        length=1,
    )
    assert _emitted_bars(at_limit) == [0]

    below = _run(
        _strategy("        min_confidence 0.6", action="buy(BTC, 0.5)"),
        length=1,
    )
    assert _emitted_bars(below) == []


# ---------------------------------------------------------------------------
# units — fractions are fractions
# ---------------------------------------------------------------------------


def test_a_fraction_limit_reads_the_measurement_as_a_fraction_not_a_percent():
    """`max_drawdown 0.05` is 5 percent, so a 4 percent book is fine.

    Feeding the same book as the number `4` — the percent spelling used by the
    library's `DRAWDOWN` feed signal — is an 400 percent drawdown and breaches.
    """
    fraction = _run(
        _strategy("        max_drawdown 0.05"), length=1, **{"risk.drawdown": (0.04,)}
    )
    assert _emitted_bars(fraction) == [0]

    percent = _run(
        _strategy("        max_drawdown 0.05"), length=1, **{"risk.drawdown": (4.0,)}
    )
    assert _emitted_bars(percent) == []


def test_a_percent_feed_signal_and_a_fraction_risk_limit_coexist():
    """A strategy may read `DRAWDOWN` in percent while its risk block is fractional.

    The two never touch: the risk gate reads `risk.drawdown` only.
    """
    source = (
        "strategy Both {\n"
        "    risk {\n"
        "        max_drawdown 0.05\n"
        "    }\n"
        "    every 1m {\n"
        "        if DRAWDOWN >= 3 {\n"
        "            sell(BTC, 1.0)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    module = compile_module(source)
    permitted = run_module(
        module, _frame(2, DRAWDOWN=(4.0, 4.0), **{"risk.drawdown": (0.04, 0.04)})
    )
    assert [i.action for i in permitted.intents] == ["SELL", "SELL"]

    gated = run_module(
        module, _frame(2, DRAWDOWN=(4.0, 4.0), **{"risk.drawdown": (0.06, 0.06)})
    )
    assert gated.intents == ()


def test_a_loss_reported_as_a_negative_number_is_not_a_breach():
    """The sign convention is a positive magnitude; a profit cannot breach."""
    result = _run(
        _strategy("        max_daily_loss 0.02"),
        length=1,
        **{"risk.daily_loss": (-0.30,)},
    )
    assert _emitted_bars(result) == [0]


def test_a_negative_drawdown_is_invalid_and_fails_closed():
    result = _run(
        _strategy("        max_drawdown 0.05"),
        length=1,
        **{"risk.drawdown": (-0.01,)},
    )
    assert result.intents == ()
    detail = _events(result, "risk.violation")[0].detail
    assert "drawdown observed -0.01 outside valid domain >= 0.0" in detail
    assert "fail-closed" in detail


@pytest.mark.parametrize(
    ("limit", "measurement", "observation"),
    [
        ("max_orders_per_day 5", "risk.orders_today", "order count"),
        (
            "stop_trading_after_losses 3",
            "risk.consecutive_losses",
            "consecutive losses",
        ),
    ],
    ids=["orders_today", "consecutive_losses"],
)
def test_a_negative_count_measurement_is_invalid_and_fails_closed(
    limit, measurement, observation
):
    result = _run(_strategy(f"        {limit}"), length=1, **{measurement: (-1,)})
    assert result.intents == ()
    detail = _events(result, "risk.violation")[0].detail
    assert f"{observation} observed -1.0 outside valid domain >= 0.0" in detail
    assert "fail-closed" in detail


# ---------------------------------------------------------------------------
# missing and unusable measurements — fail closed
# ---------------------------------------------------------------------------


def test_a_limit_whose_measurement_the_host_never_supplied_suppresses_everything():
    result = _run(_strategy("        max_drawdown 0.05"))
    assert _emitted_bars(result) == []
    detail = _events(result, "risk.violation")[0].detail
    assert "drawdown is unmeasured" in detail


def test_positive_control_supplying_that_measurement_lets_the_intent_through():
    result = _run(
        _strategy("        max_drawdown 0.05"), **{"risk.drawdown": (0.01, 0.01, 0.01)}
    )
    assert _emitted_bars(result) == [0, 1, 2]


def test_an_absent_cell_in_the_measurement_series_suppresses_only_that_bar():
    result = _run(
        _strategy("        max_drawdown 0.05"),
        **{"risk.drawdown": (0.01, None, 0.01)},
    )
    assert _emitted_bars(result) == [0, 2]


@pytest.mark.parametrize(
    "hostile", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)
def test_a_non_finite_measurement_is_unmeasured_rather_than_safely_below(hostile):
    """NaN compares false to everything and -inf compares below everything.

    Either one would sail through a naive `observed > limit` check, so both are
    rejected as unusable instead.
    """
    result = _run(
        _strategy("        max_drawdown 0.05"), length=1, **{"risk.drawdown": (hostile,)}
    )
    assert _emitted_bars(result) == []
    assert "unmeasured" in _events(result, "risk.violation")[0].detail


def test_an_intent_that_declares_no_confidence_cannot_satisfy_min_confidence():
    """Reachable only through raw IR now, and it must still fail closed.

    The compiler rejects this pairing at the door (see the `min_confidence`
    diagnostics below), so the runtime guard has no source-level path to it. It
    still has to hold: `run_module` is a public entry point, and a hand-built
    document must not be able to satisfy a floor by declining to state a number.
    """
    module = _raw_module({"min_confidence": 0.6}, confidence=None)
    result = run_module(module, _frame(1))
    assert result.intents == ()
    assert (
        "the intent's declared confidence is unmeasured"
        in _events(result, "risk.violation")[0].detail
    )


def test_the_confidence_builtin_does_not_stand_in_for_a_declared_intent_confidence():
    """A `confidence` series on the frame is not the intent's own confidence.

    Letting it substitute would make `min_confidence` pass on the strength of a
    number the author never attached to the action.
    """
    module = _raw_module({"min_confidence": 0.6}, confidence=None)
    result = run_module(module, _frame(1, confidence=(1.0,)))
    assert result.intents == ()


def test_positive_control_a_declared_confidence_does_satisfy_the_same_limit():
    result = _run(
        _strategy("        min_confidence 0.6", action="buy(BTC, 0.9)"), length=1
    )
    assert _emitted_bars(result) == [0]


# ---------------------------------------------------------------------------
# which intents the gate touches
# ---------------------------------------------------------------------------


def test_pause_survives_a_breach_because_suppressing_it_would_invert_the_control():
    """A breaker that silences its own halt is worse than no breaker."""
    result = _run(
        _strategy("        max_drawdown 0.05", action="pause()"),
        length=1,
        **{"risk.drawdown": (0.9,)},
    )
    assert [i.action for i in result.intents] == ["PAUSE"]
    assert _events(result, "intent.suppressed") == []


def test_observe_survives_a_breach():
    result = _run(
        _strategy("        max_drawdown 0.05", action="observe()"),
        length=1,
        **{"risk.drawdown": (0.9,)},
    )
    assert [i.action for i in result.intents] == ["OBSERVE"]


@pytest.mark.parametrize(
    "action, expected",
    [("buy(BTC, 1.0)", "BUY"), ("sell(BTC, 1.0)", "SELL"), ("execute()", "EXECUTE")],
)
def test_every_actuating_intent_is_suppressed_by_a_breach(action, expected):
    assert expected in ACTUATING_ACTIONS
    breached = _run(
        _strategy("        max_drawdown 0.05", action=action),
        length=1,
        **{"risk.drawdown": (0.9,)},
    )
    assert breached.intents == ()
    # Positive control on the same action: within the limit it is proposed.
    permitted = _run(
        _strategy("        max_drawdown 0.05", action=action),
        length=1,
        **{"risk.drawdown": (0.01,)},
    )
    assert [i.action for i in permitted.intents] == [expected]


def test_the_gate_reaches_an_intent_inside_an_else_branch():
    source = (
        "strategy Nested {\n"
        "    risk {\n"
        "        max_drawdown 0.05\n"
        "    }\n"
        "    every 1m {\n"
        "        if TRIGGER > 0 {\n"
        "            observe()\n"
        "        } else {\n"
        "            buy(BTC, 1.0)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    module = compile_module(source)
    breached = run_module(
        module, _frame(1, TRIGGER=(0.0,), **{"risk.drawdown": (0.9,)})
    )
    assert breached.intents == ()
    permitted = run_module(
        module, _frame(1, TRIGGER=(0.0,), **{"risk.drawdown": (0.01,)})
    )
    assert [i.action for i in permitted.intents] == ["BUY"]


# ---------------------------------------------------------------------------
# the log is the record
# ---------------------------------------------------------------------------


def test_a_suppression_names_the_action_the_limit_and_the_observation():
    result = _run(
        _strategy("        max_drawdown 0.05"),
        length=1,
        **{"risk.drawdown": (0.07,)},
    )
    violation = _events(result, "risk.violation")[0]
    assert violation.timestamp == 0
    assert "max_drawdown" in violation.detail
    assert "0.07" in violation.detail
    suppressed = _events(result, "intent.suppressed")[0]
    assert "BUY" in suppressed.detail
    assert "max_drawdown" in suppressed.detail


def test_simultaneous_violations_log_in_the_declared_vocabulary_order():
    body = "\n".join(
        [
            "        max_daily_loss 0.02",
            "        max_drawdown 0.05",
            "        max_orders_per_day 5",
            "        stop_trading_after_losses 3",
            "        min_confidence 0.9",
        ]
    )
    result = _run(
        _strategy(body, action="buy(BTC, 0.1)"),
        length=1,
        **{
            "risk.daily_loss": (0.5,),
            "risk.drawdown": (0.5,),
            "risk.orders_today": (99.0,),
            "risk.consecutive_losses": (9.0,),
        },
    )
    assert result.intents == ()
    logged = [e.detail.split()[0] for e in _events(result, "risk.violation")]
    assert logged == [
        "max_daily_loss",
        "max_drawdown",
        "max_orders_per_day",
        "stop_trading_after_losses",
        "min_confidence",
    ]


def test_an_armed_gate_announces_the_measurements_it_needs():
    result = _run(
        _strategy("        max_drawdown 0.05"), **{"risk.drawdown": (0.0, 0.0, 0.0)}
    )
    armed = _events(result, "risk.armed")
    assert len(armed) == 1
    assert "max_drawdown" in armed[0].detail
    assert "risk.drawdown" in armed[0].detail


def test_enforcement_replays_bit_identically():
    module = compile_module(_strategy("        max_drawdown 0.05"))
    frame = _frame(3, TRIGGER=(1.0, 1.0, 1.0), **{"risk.drawdown": (0.01, 0.9, 0.01)})
    assert run_module(module, frame).to_dict() == run_module(module, frame).to_dict()


# ---------------------------------------------------------------------------
# the honest gap
# ---------------------------------------------------------------------------


def test_the_two_sizing_limits_are_announced_as_unenforced_rather_than_ignored():
    """Nano carries them to the host and says out loud that it is not applying them."""
    body = "\n".join(
        ["        max_position_size 0.1", "        max_open_positions 3"]
    )
    result = _run(_strategy(body), length=1)
    notices = _events(result, "risk.unenforced")
    assert [n.detail.split()[0] for n in notices] == [
        "max_position_size",
        "max_open_positions",
    ]
    # Declaring them does not gate anything, and the log says so.
    assert [i.action for i in result.intents] == ["BUY"]
    assert _events(result, "risk.armed") == []


def test_positive_control_an_enforceable_limit_beside_them_still_gates():
    body = "\n".join(
        [
            "        max_position_size 0.1",
            "        max_drawdown 0.05",
            "        max_open_positions 3",
        ]
    )
    result = _run(_strategy(body), length=1, **{"risk.drawdown": (0.9,)})
    assert result.intents == ()
    assert len(_events(result, "risk.unenforced")) == 2
    assert len(_events(result, "risk.armed")) == 1


def test_the_unenforced_notice_says_why():
    result = _run(_strategy("        max_position_size 0.1"), length=1)
    detail = _events(result, "risk.unenforced")[0].detail
    assert "size" in detail
    assert "host" in detail


# ---------------------------------------------------------------------------
# raw IR — the loader and the gate must not depend on the compiler
# ---------------------------------------------------------------------------


def _raw_module(limits: dict, *, action: str = "BUY", confidence=1.0):
    """A hand-built v1.0 document that always fires one intent.

    Built as a dict rather than compiled, for the reason `tests/test_module.py`
    gives: going through the compiler can only ever produce documents the
    compiler thinks are valid, which tests the compiler rather than the loader
    and the gate behind it.
    """
    intent_attrs = {"action": action, "asset": "BTC"}
    if confidence is not None:
        intent_attrs["confidence"] = confidence
    return load_module(
        {
            "type": "Strategy",
            "nanoIrVersion": "1.0.0",
            "tier": "nano",
            "name": "Raw",
            "effects": ["intent.emit", "log.append"],
            "nodes": [
                {"id": "r", "op": "risk.limits", "inputs": [], "attrs": {"limits": limits}},
                {"id": "s", "op": "schedule", "inputs": [], "attrs": {"interval": "1m"}},
                {"id": "c", "op": "const", "inputs": [], "attrs": {"value": True}},
                {"id": "i", "op": "intent.emit", "inputs": [], "attrs": intent_attrs},
                {"id": "b", "op": "block", "inputs": ["i"], "attrs": {}},
                {"id": "rule", "op": "rule", "inputs": ["s", "c", "b"], "attrs": {}},
            ],
            "entries": ["rule"],
        }
    )


def test_a_hand_built_document_is_gated_the_same_way_a_compiled_one_is():
    module = _raw_module({"max_drawdown": 0.05})
    breached = run_module(module, _frame(1, **{"risk.drawdown": (0.9,)}))
    assert breached.intents == ()
    permitted = run_module(module, _frame(1, **{"risk.drawdown": (0.01,)}))
    assert [i.action for i in permitted.intents] == ["BUY"]


def test_a_second_risk_limits_node_is_rejected_at_load():
    """Two blocks would be a fail-open path inside a fail-closed feature.

    Whichever node a runtime read last would decide the limits, so a looser
    second declaration could silently replace a tighter first one with nothing in
    the log to show it. The parser already refuses two `risk` blocks; raw IR now
    gets the same answer.
    """
    document = _raw_module({"max_drawdown": 0.01}).to_dict(include_hash=False)
    document["nodes"].insert(
        1,
        {
            "id": "r2",
            "op": "risk.limits",
            "inputs": [],
            "attrs": {"limits": {"max_drawdown": 0.99}},
        },
    )
    with pytest.raises(IRValidationError, match="second risk.limits"):
        load_module(document)


def test_a_loosening_second_block_cannot_reach_the_gate_even_unvalidated():
    """First declaration binds, for the one caller that skips the loader."""
    document = _raw_module({"max_drawdown": 0.01}).to_dict(include_hash=False)
    module = load_module(document)
    loosened = NanoModule(
        name=module.name,
        tier=module.tier,
        effects=module.effects,
        nodes=module.nodes
        + (
            IRNode(
                id="r2", op="risk.limits", attrs={"limits": {"max_drawdown": 0.99}}
            ),
        ),
        entries=module.entries,
    )
    result = run_module(
        loosened, _frame(1, **{"risk.drawdown": (0.5,)}), validate=False
    )
    assert result.intents == ()
    assert "max_drawdown <= 0.01" in _events(result, "risk.armed")[0].detail


@pytest.mark.parametrize(
    "order",
    [
        ("max_drawdown", "max_daily_loss", "min_confidence"),
        ("min_confidence", "max_drawdown", "max_daily_loss"),
        ("max_daily_loss", "min_confidence", "max_drawdown"),
    ],
    ids=["a", "b", "c"],
)
def test_violation_order_is_the_vocabulary_order_not_the_declaration_order(order):
    """Permuting how a document spells its limits must not permute the log.

    Declaration order reaches the runtime through a JSON object, and object key
    order is a serialisation accident. Pinning the log to the schema's order is
    what makes two hosts that round-tripped the same module agree on what an
    auditor reads first.
    """
    values = {"max_drawdown": 0.05, "max_daily_loss": 0.02, "min_confidence": 0.9}
    module = _raw_module({key: values[key] for key in order}, confidence=0.1)
    result = run_module(
        module,
        _frame(1, **{"risk.drawdown": (0.9,), "risk.daily_loss": (0.9,)}),
    )
    assert [e.detail.split()[0] for e in _events(result, "risk.violation")] == [
        "max_daily_loss",
        "max_drawdown",
        "min_confidence",
    ]


def test_an_unusable_limit_reaching_an_unvalidated_run_still_fails_closed():
    """`run_module(validate=False)` is the one door past the loader.

    The loader rejects a non-numeric limit, so this module can only be built by
    hand and only be run with validation off. It must still suppress rather than
    treat an uncomparable limit as satisfied.
    """
    module = NanoModule(
        name="Unvalidated",
        tier="nano",
        effects=("intent.emit", "log.append"),
        nodes=(
            IRNode(id="r", op="risk.limits", attrs={"limits": {"max_drawdown": "big"}}),
            IRNode(id="s", op="schedule", attrs={"interval": "1m"}),
            IRNode(id="c", op="const", attrs={"value": True}),
            IRNode(id="i", op="intent.emit", attrs={"action": "BUY", "asset": "BTC"}),
            IRNode(id="b", op="block", inputs=("i",)),
            IRNode(id="rule", op="rule", inputs=("s", "c", "b")),
        ),
        entries=("rule",),
    )
    result = run_module(module, _frame(1), validate=False)
    assert result.intents == ()
    assert "not a usable number" in _events(result, "risk.violation")[0].detail


def test_execute_still_clears_a_limit_it_can_actually_be_measured_against():
    """`execute()` is only unusable under `min_confidence`, not under every limit."""
    permitted = _run(
        _strategy("        max_drawdown 0.05", action="execute()"),
        length=1,
        **{"risk.drawdown": (0.01,)},
    )
    assert [i.action for i in permitted.intents] == ["EXECUTE"]


def test_an_escalation_is_not_gated_by_a_risk_limit():
    """Escalation asks for help; a breached limit is a reason to ask, not to stop."""
    source = (
        "tier nano+\n"
        "strategy Escalating {\n"
        "    risk {\n"
        "        max_drawdown 0.05\n"
        "    }\n"
        "    agent RiskDesk\n"
        "    every 1m {\n"
        "        if TRIGGER > 0 {\n"
        "            buy(BTC, 1.0)\n"
        "            escalate RiskDesk\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    module = compile_module(source)
    result = run_module(
        module, _frame(1, TRIGGER=(1.0,), **{"risk.drawdown": (0.9,)})
    )
    assert result.intents == ()
    assert [e.target for e in result.escalations] == ["RiskDesk"]


# ---------------------------------------------------------------------------
# F4 - a bool is not a measurement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hostile", [True, False], ids=["true", "false"])
def test_a_boolean_measurement_is_unmeasured_not_one_or_zero(hostile):
    """`bool` is a subclass of `int`, so `float(False)` is a very good number.

    A feed that wrote booleans into `risk.orders_today` would otherwise report a
    perfectly satisfied limit on every bar. The guard is one `isinstance`, and
    without a test it is one `isinstance` nobody would miss.
    """
    result = _run(
        _strategy("        max_orders_per_day 10"),
        length=2,
        **{"risk.orders_today": (hostile, hostile)},
    )
    assert result.intents == ()
    assert "order count is unmeasured" in _events(result, "risk.violation")[0].detail
    # Positive control: the same limit, the same bars, a real number under it.
    permitted = _run(
        _strategy("        max_orders_per_day 10"),
        length=2,
        **{"risk.orders_today": (3, 3)},
    )
    assert len(permitted.intents) == 2


# ---------------------------------------------------------------------------
# F5 - the log is the host's only window into suppression
# ---------------------------------------------------------------------------


def test_a_suppression_line_carries_what_intent_emitted_carries():
    """Two intents withheld on one bar must not be two identical lines.

    `intent.emitted` records the asset; without the same fields here a host
    reading the log cannot tell which asset it was not asked to trade.
    """
    source = (
        "strategy TwoAssets {\n"
        "    risk {\n"
        "        max_drawdown 0.05\n"
        "    }\n"
        "    every 1m {\n"
        "        if TRIGGER > 0 {\n"
        "            sell(BTC, 0.9)\n"
        "            sell(ETH, 0.3)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    module = compile_module(source)
    result = run_module(module, _frame(1, TRIGGER=(1.0,), **{"risk.drawdown": (0.9,)}))
    assert result.intents == ()
    lines = [e.detail for e in _events(result, "intent.suppressed")]
    assert lines == [
        "SELL asset=BTC confidence=0.9 withheld by risk limits: max_drawdown",
        "SELL asset=ETH confidence=0.3 withheld by risk limits: max_drawdown",
    ]


def test_the_violation_line_is_exact_and_spells_the_limit_one_way():
    """Pins the sentence, not a substring of it.

    `risk.armed` and `risk.violation` both show the declared value, and they used
    to disagree - one printed `10`, the other `10.0`, because only one had passed
    through the float coercion the comparison needs.
    """
    result = _run(
        _strategy("        max_orders_per_day 10"),
        length=1,
        **{"risk.orders_today": (12,)},
    )
    assert _events(result, "risk.armed")[0].detail == (
        "max_orders_per_day < 10; measurements: risk.orders_today"
    )
    assert _events(result, "risk.violation")[0].detail == (
        "max_orders_per_day 10: order count observed 12.0 (allowed < 10)"
    )


# ---------------------------------------------------------------------------
# F3 - a floor no intent in the program can ever clear is a compile error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action", ["buy(BTC)", "sell(BTC)", "execute()"], ids=["buy", "sell", "execute"]
)
def test_min_confidence_beside_an_intent_that_states_none_is_rejected(action):
    with pytest.raises(NanoTypeError) as excinfo:
        compile_module(_strategy("        min_confidence 0.6", action=action))
    message = excinfo.value.message
    assert "min_confidence 0.6" in message
    assert "suppressed at every bar" in message
    assert excinfo.value.line > 0 and excinfo.value.column > 0


def test_min_confidence_zero_is_the_trap_and_is_rejected_too():
    """`min_confidence 0` is the natural spelling of "no floor", and is in range.

    It compiled, and then suppressed every intent forever, because absence fails
    closed. The loader already refuses `min_confidence 5` for suppressing
    everything forever; this is the same rule where an author can reach it.
    """
    with pytest.raises(NanoTypeError, match="min_confidence 0"):
        compile_module(_strategy("        min_confidence 0", action="buy(BTC)"))


@pytest.mark.parametrize("action", ["pause()", "observe()"], ids=["pause", "observe"])
def test_min_confidence_does_not_reject_intents_it_never_gates(action):
    """`PAUSE` and `OBSERVE` are never suppressed, so they need no confidence."""
    module = compile_module(_strategy("        min_confidence 0.6", action=action))
    result = run_module(module, _frame(1, TRIGGER=(1.0,)))
    assert len(result.intents) == 1


def test_a_declared_confidence_compiles_and_runs_under_the_same_floor():
    """Positive control for the whole diagnostic: the fix the message names works."""
    result = _run(
        _strategy("        min_confidence 0.6", action="buy(BTC, 0.9)"), length=1
    )
    assert [i.action for i in result.intents] == ["BUY"]


def test_no_risk_block_means_no_confidence_requirement():
    """The diagnostic must not leak into strategies that declared no floor."""
    result = _run(_strategy("", action="buy(BTC)"), length=1)
    assert [i.action for i in result.intents] == ["BUY"]


# ---------------------------------------------------------------------------
# F7 - one validator, two callers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (-0.1, "fraction of equity"),
        (float("nan"), "finite number"),
        (float("inf"), "finite number"),
        (True, "must be numeric"),
    ],
)
def test_one_validator_answers_for_both_front_doors(value, expected):
    """They had drifted: only the loader refused a non-finite limit.

    Source cannot spell `nan`, so the shared function is checked directly for the
    values only raw IR can carry, and through both front doors below for the ones
    an author can write.
    """
    assert expected in (validate_risk_limit("max_drawdown", value) or "")


def test_a_range_the_compiler_rejects_the_loader_rejects_too():
    with pytest.raises(NanoTypeError, match="fraction of equity"):
        compile_module(_strategy("        max_drawdown 1.5"))
    document = _raw_module({"max_drawdown": 0.05}).to_dict(include_hash=False)
    document["nodes"][0]["attrs"]["limits"] = {"max_drawdown": 1.5}
    with pytest.raises(IRValidationError, match="fraction of equity"):
        load_module(document)


# ---------------------------------------------------------------------------
# F1 - the CLI must not let a disarmed run look like a quiet one
# ---------------------------------------------------------------------------


GUARDED = (
    "strategy Guarded {\n"
    "    risk {\n"
    "        max_drawdown 0.05\n"
    "    }\n"
    "    every 1m {\n"
    "        if RSI < 30 {\n"
    "            buy(BTC, 0.9)\n"
    "        }\n"
    "    }\n"
    "}\n"
)


def _cli(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def guarded(tmp_path):
    path = tmp_path / "guarded.nano"
    path.write_text(GUARDED, encoding="utf-8")
    return path


def test_replay_refuses_data_that_omits_a_risk_measurement(guarded, tmp_path, capsys):
    """The silent-disarm case: same strategy, same hash, zero intents, exit 0.

    Fail-closed enforcement means a missing measurement withholds everything, so
    a replay against the wrong file used to read exactly like a strategy that
    found no setup - and `--verify` stamped it deterministic, raising confidence
    in the wrong number.
    """
    path = tmp_path / "nodd.csv"
    path.write_text("timestamp,RSI\n0,20\n60,20\n", encoding="utf-8")
    code, _, err = _cli(
        ["replay", str(guarded), "--data", str(path), "--verify"], capsys
    )
    assert code == EXIT_DIAGNOSTICS
    assert "does not supply risk.drawdown" in err


def test_replay_surfaces_suppression_in_the_default_text_report(
    guarded, tmp_path, capsys
):
    path = tmp_path / "dd.csv"
    path.write_text(
        "timestamp,RSI,risk.drawdown\n0,20,0.01\n60,20,0.9\n", encoding="utf-8"
    )
    code, out, _ = _cli(["replay", str(guarded), "--data", str(path)], capsys)
    assert code == EXIT_OK
    assert "1 intent(s) withheld by risk limits" in out
    assert "BUY BTC @0.9" in out


def test_an_ungated_replay_says_nothing_about_risk(guarded, tmp_path, capsys):
    """Positive control for the row above: it shows only when something was withheld."""
    path = tmp_path / "clean.csv"
    path.write_text(
        "timestamp,RSI,risk.drawdown\n0,20,0.01\n60,20,0.01\n", encoding="utf-8"
    )
    code, out, _ = _cli(["replay", str(guarded), "--data", str(path)], capsys)
    assert code == EXIT_OK
    assert "withheld by risk limits" not in out


# ---------------------------------------------------------------------------
# R1 - the integer tightening is a real behavior change, so it is pinned
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "limit", sorted(INTEGER_RISK_LIMITS), ids=sorted(INTEGER_RISK_LIMITS)
)
def test_a_counting_limit_written_as_a_float_is_refused_by_the_loader(limit):
    """`5.0` is legal JSON that round-trips out of any host that uses floats.

    The compiler always refused it; the loader used to accept it, because it
    compared `float(value) != int(value)` and 5.0 passes that. Sharing one
    validator tightened the loader to match — which is the point of sharing it,
    and is also a document shape that used to load and no longer does.
    """
    document = _raw_module({"max_drawdown": 0.05}).to_dict(include_hash=False)
    document["nodes"][0]["attrs"]["limits"] = {limit: 5.0}
    with pytest.raises(IRValidationError, match="whole number"):
        load_module(document)

    document["nodes"][0]["attrs"]["limits"] = {limit: 5}
    assert load_module(document) is not None


# ---------------------------------------------------------------------------
# R3 - both violation paths, and every rule's noun
# ---------------------------------------------------------------------------


def test_the_unmeasured_line_spells_an_integer_limit_the_same_way():
    """The sibling of the breach assertion above, on the other formatting path.

    Both lines format from the declared value rather than the float the
    comparison needs, so `10` never appears as `10.0` in one of them.
    """
    result = _run(_strategy("        max_orders_per_day 10"), length=1)
    assert _events(result, "risk.violation")[0].detail == (
        "max_orders_per_day 10: order count is unmeasured (fail-closed at bar 0)"
    )


@pytest.mark.parametrize(
    "limit, declared, noun",
    [
        ("max_daily_loss", "0.02", "daily loss"),
        ("max_drawdown", "0.05", "drawdown"),
        ("max_orders_per_day", "10", "order count"),
        ("stop_trading_after_losses", "3", "consecutive losses"),
    ],
    ids=["daily_loss", "drawdown", "orders", "losses"],
)
def test_every_measured_rule_names_its_observation_in_words(limit, declared, noun):
    """A violation line should read as a sentence, not as a signal name.

    Pinned per rule: without this only two of the five nouns were covered, and
    swapping one for its measurement name went unnoticed.
    """
    result = _run(_strategy(f"        {limit} {declared}"), length=1)
    assert _events(result, "risk.violation")[0].detail == (
        f"{limit} {declared}: {noun} is unmeasured (fail-closed at bar 0)"
    )


def test_min_confidence_names_its_observation_in_words_too():
    """The fifth rule, reached through a breach — source can no longer leave it absent."""
    result = _run(
        _strategy("        min_confidence 0.6", action="buy(BTC, 0.1)"), length=1
    )
    assert _events(result, "risk.violation")[0].detail == (
        "min_confidence 0.6: the intent's declared confidence observed 0.1 "
        "(allowed >= 0.6)"
    )
