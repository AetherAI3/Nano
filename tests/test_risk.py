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

from nano.compiler import compile_module
from nano.compiler.errors import NanoSyntaxError
from nano.ir import IRNode, NanoModule, load_module
from nano.ir.schema import RISK_LIMITS
from nano.runtime.interpreter import MarketFrame
from nano.runtime.risk import (
    ACTUATING_ACTIONS,
    ENFORCED_LIMITS,
    HOST_ENFORCED_LIMITS,
    MEASUREMENT_PREFIX,
    measurement_for,
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
    assert measurement_for("max_drawdown") == "risk.drawdown"
    assert measurement_for("min_confidence") is None


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


def test_max_orders_per_day_allows_the_limit_exactly():
    result = _run(
        _strategy("        max_orders_per_day 5"),
        **{"risk.orders_today": (4.0, 5.0, 6.0)},
    )
    assert _emitted_bars(result) == [0, 1]


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


# ---------------------------------------------------------------------------
# missing and unusable measurements — fail closed
# ---------------------------------------------------------------------------


def test_a_limit_whose_measurement_the_host_never_supplied_suppresses_everything():
    result = _run(_strategy("        max_drawdown 0.05"))
    assert _emitted_bars(result) == []
    detail = _events(result, "risk.violation")[0].detail
    assert "risk.drawdown" in detail and "unmeasured" in detail


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
    result = _run(_strategy("        min_confidence 0.6", action="buy(BTC)"), length=1)
    assert _emitted_bars(result) == []
    assert "no confidence" in _events(result, "risk.violation")[0].detail


def test_the_confidence_builtin_does_not_stand_in_for_a_declared_intent_confidence():
    """A `confidence` series on the frame is not the intent's own confidence.

    Letting it substitute would make `min_confidence` pass on the strength of a
    number the author never attached to the action.
    """
    result = _run(
        _strategy("        min_confidence 0.6", action="buy(BTC)"),
        length=1,
        confidence=(1.0,),
    )
    assert _emitted_bars(result) == []


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


def test_two_risk_blocks_in_one_document_merge_rather_than_race():
    """Hand-written IR may carry several `risk.limits` nodes; all of them apply."""
    document = _raw_module({"max_drawdown": 0.05}).to_dict(include_hash=False)
    document["nodes"].insert(
        1,
        {
            "id": "r2",
            "op": "risk.limits",
            "inputs": [],
            "attrs": {"limits": {"max_daily_loss": 0.02}},
        },
    )
    module = load_module(document)
    frame = _frame(1, **{"risk.drawdown": (0.01,), "risk.daily_loss": (0.9,)})
    result = run_module(module, frame)
    assert result.intents == ()
    assert [e.detail.split()[0] for e in _events(result, "risk.violation")] == [
        "max_daily_loss"
    ]


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


def test_min_confidence_makes_execute_unreachable_and_that_is_deliberate():
    """`execute()` has no confidence slot in the grammar, so it cannot clear one.

    Pinned rather than special-cased. Exempting `EXECUTE` would mean one
    actuating intent quietly ignores a limit the author declared, which is the
    exact shape of dishonesty this module exists to remove — and the failure is
    loud: every `execute()` is withheld, and the log says why on the first bar.
    """
    result = _run(
        _strategy("        min_confidence 0.6", action="execute()"), length=1
    )
    assert result.intents == ()
    assert "no confidence" in _events(result, "risk.violation")[0].detail
    # Positive control: the same action clears the same gate once the limit that
    # cannot be expressed for it is not the one declared.
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
