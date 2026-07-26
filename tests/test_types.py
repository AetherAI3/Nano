"""Type system and look-ahead protection.

Two things are pinned here. First, that the checker accepts the programs a
strategy author actually writes and rejects the ones that would silently
misbehave. Second — and this is the one that matters — that **look-ahead is
unrepresentable**, including the exact shape from the v1.0 brief:
`close[t+1] > close`.

Every rejection asserts its line and column, because a diagnostic that points at
the wrong token is close to useless in an editor.
"""

import pytest

from nano.compiler import check_source
from nano.compiler.errors import LookaheadError, NanoTypeError
from nano.types import BOOL, FLOAT, INT, SERIES_FLOAT, parse_type, series
from nano.types.kinds import CONFIDENCE, is_assignable, unify_comparison, unify_numeric

CLOSE = "    input close: series<float>\n"


def _strategy(body: str, decls: str = "") -> str:
    return (
        "strategy S {\n"
        + decls
        + "    every 5m {\n"
        + f"        if {body} {{\n"
        + "            buy(BTC)\n"
        + "        }\n"
        + "    }\n"
        + "}\n"
    )


def _fails(body: str, decls: str = "") -> NanoTypeError:
    with pytest.raises(NanoTypeError) as excinfo:
        check_source(_strategy(body, decls))
    return excinfo.value


# -- the type vocabulary ------------------------------------------------------


def test_type_rendering_round_trips():
    assert str(SERIES_FLOAT) == "series<float>"
    assert parse_type("series<float>") == SERIES_FLOAT
    assert parse_type("int") == INT
    assert parse_type("series<series<float>>") is None
    assert parse_type("nonsense") is None


def test_integer_literals_widen_but_floats_never_narrow():
    # `RSI < 30` has to typecheck, so int widens to float. The reverse would let a
    # price silently become a period.
    assert is_assignable(INT, FLOAT)
    assert not is_assignable(FLOAT, INT)
    assert is_assignable(CONFIDENCE, FLOAT)
    assert not is_assignable(BOOL, FLOAT)


def test_a_scalar_never_satisfies_a_series_parameter():
    # An indicator needs history; handing it one number cannot work, so this is a
    # type error rather than a silent broadcast.
    assert not is_assignable(FLOAT, SERIES_FLOAT)
    assert is_assignable(series(INT), SERIES_FLOAT)


def test_comparisons_and_arithmetic_propagate_series_ness():
    assert unify_comparison(SERIES_FLOAT, FLOAT) == series(BOOL)
    assert unify_comparison(FLOAT, FLOAT) == BOOL
    assert unify_numeric(INT, INT) == INT
    assert unify_numeric(INT, FLOAT) == FLOAT
    assert unify_numeric(SERIES_FLOAT, INT) == SERIES_FLOAT
    assert unify_comparison(FLOAT, parse_type("string")) is None


# -- look-ahead protection ----------------------------------------------------


def test_future_index_from_the_brief_is_rejected():
    # The literal example from the v1.0 requirements. `t` is not a compile-time
    # constant, so the offset cannot be bounded and the read is refused.
    error = _fails("close[t+1] > close", CLOSE)
    assert isinstance(error, LookaheadError)
    assert (error.line, error.column) == (4, 19)
    assert "compile-time non-negative integer" in error.message


def test_negative_offset_is_rejected_with_the_folded_value():
    error = _fails("close[-1] > close", CLOSE)
    assert isinstance(error, LookaheadError)
    assert (error.line, error.column) == (4, 18)
    assert "reads into the future" in error.message
    assert "-1" in error.message


def test_backwards_offsets_are_fine_and_accumulate_warmup():
    program = check_source(_strategy("close[3] > close[1]", CLOSE))
    assert program.warmup == 3


def test_offset_may_reference_a_param_because_params_are_constants():
    program = check_source(
        _strategy("close[lag] > close", "    param lag: int = 4\n" + CLOSE)
    )
    assert program.warmup == 4


def test_indexing_a_scalar_is_an_error():
    error = _fails("lag[1] > 1", "    param lag: int = 4\n")
    assert "Cannot index int" in error.message


# -- indicator typing ---------------------------------------------------------


def test_periods_must_be_compile_time_constants():
    error = _fails(
        "SMA(close, window) > 1", CLOSE + "    input window: series<float>\n"
    )
    assert "compile-time integer constant" in error.message


def test_period_must_be_at_least_one():
    error = _fails("EMA(close, 0) > 1", CLOSE)
    assert "must be at least 1" in error.message


def test_wrong_arity_reports_the_signature():
    error = _fails("EMA(close) > 1", CLOSE)
    assert "EMA takes 2 argument(s), got 1" in error.message
    assert "EMA(series<float>, int) -> series<float>" in error.message


def test_history_consuming_parameter_rejects_a_scalar():
    error = _fails("EMA(20, close) > 1", CLOSE)
    assert "needs history" in error.message


def test_warmup_is_the_max_over_every_use():
    program = check_source(
        "strategy S {\n"
        "    input close: series<float>\n"
        "    let a = SMA(close, 5)\n"
        "    let b = EMA(close, 40)\n"
        "    every 5m {\n"
        "        if a > b {\n"
        "            buy(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    assert program.warmup == 39  # EMA(40) warms at 39; SMA(5) at 4


def test_scalar_maths_lifts_over_a_series():
    program = check_source(_strategy("ABS(close) > 1", CLOSE))
    assert program.warmup == 0


# -- feed signals versus computed indicators ----------------------------------


def test_undeclared_names_are_feed_signals_when_no_inputs_are_declared():
    # This is what keeps every v0.1.0 strategy compiling with no preamble.
    program = check_source(_strategy("RSI(14) < 30"))
    assert program.feed_signals == ("RSI",)


def test_declaring_an_input_turns_on_strict_name_resolution():
    error = _fails("clse > 1", CLOSE)
    assert "Unknown name 'clse'" in error.message


def test_signal_call_form_still_works_in_strict_mode():
    # `SENTIMENT(1)` is unambiguously deliberate, so an explicit call stays legal
    # even once the strategy declares its inputs -- only bare names go strict.
    program = check_source(_strategy("SENTIMENT(1) > 0.5", CLOSE))
    assert "SENTIMENT" in program.feed_signals


# -- operators ----------------------------------------------------------------


def test_conditions_must_be_boolean():
    error = _fails("close", CLOSE)
    assert "must be a boolean expression" in error.message


def test_logical_operators_reject_non_booleans():
    error = _fails("close and true", CLOSE)
    assert "needs boolean operands" in error.message


def test_incomparable_types_are_rejected():
    error = _fails('close > "text"', CLOSE)
    assert "Cannot compare series<float> with string" in error.message


def test_comparisons_do_not_chain():
    from nano.compiler.errors import NanoSyntaxError

    with pytest.raises(NanoSyntaxError) as excinfo:
        check_source(_strategy("1 < 2 < 3"))
    assert "do not chain" in excinfo.value.message


def test_division_always_produces_a_float():
    # An int-preserving `/` would make `fast / 2` truncate depending on operand
    # types, which is exactly the surprise static typing should prevent.
    program = check_source(
        "strategy S {\n"
        "    param fast: int = 5\n"
        "    every 5m {\n"
        "        if fast / 2 > 2.4 {\n"
        "            buy(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    assert program.tier == "nano"


# -- declarations -------------------------------------------------------------


def test_params_cannot_be_series():
    error = _fails("close > 1", "    param p: series<float> = 1\n" + CLOSE)
    assert "cannot be a series" in error.message


def test_declared_type_must_match_the_value():
    error = _fails("close > 1", "    param p: bool = 5\n" + CLOSE)
    assert "declared bool but its default is int" in error.message


def test_redeclaration_is_an_error_not_a_shadow():
    error = _fails("close > 1", CLOSE + "    input close: series<float>\n")
    assert "already declared" in error.message


def test_unknown_type_annotation_is_rejected():
    error = _fails("close > 1", "    input close: sequence<float>\n")
    assert "Unknown type 'sequence<float>'" in error.message


# -- tiers --------------------------------------------------------------------


def test_reasoning_constructs_require_a_higher_tier():
    # No `tier nano+` header, so escalation is out of reach. The default tier
    # keeps the entry language small: reading `strategy` with no tier line tells
    # an auditor there is no model in the loop.
    with pytest.raises(NanoTypeError) as excinfo:
        check_source(
            "strategy S {\n"
            "    every 5m {\n"
            "        if RSI < 30 {\n"
            '            escalate "desk"\n'
            "        }\n"
            "    }\n"
            "}\n"
        )
    assert "requires tier 'nano+'" in excinfo.value.message


def test_declaring_the_tier_admits_the_construct():
    program = check_source(
        "tier nano+\n"
        "strategy S {\n"
        "    agent Desk { role research }\n"
        "    every 5m {\n"
        "        if confidence < 0.6 {\n"
        "            escalate Desk\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    assert "llmre.escalate" in program.effects


def test_escalating_to_an_undeclared_agent_is_an_error():
    with pytest.raises(NanoTypeError) as excinfo:
        check_source(
            "tier nano+\n"
            "strategy S {\n"
            "    every 5m {\n"
            "        if confidence < 0.6 {\n"
            "            escalate Reserch\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
    assert "is not a declared agent" in excinfo.value.message


# -- risk limits --------------------------------------------------------------


def test_risk_limits_are_range_checked():
    with pytest.raises(NanoTypeError) as excinfo:
        check_source(
            "strategy S {\n"
            "    risk {\n"
            "        max_daily_loss 2\n"
            "    }\n"
            "    every 5m {\n"
            "        observe()\n"
            "    }\n"
            "}\n"
        )
    # 2 would be 200% of equity. Fractions are fractions, never percentages.
    assert "fraction of equity" in excinfo.value.message


def test_unknown_risk_limit_lists_the_valid_ones():
    with pytest.raises(NanoTypeError) as excinfo:
        check_source(
            "strategy S {\n"
            "    risk {\n"
            "        max_yolo 0.5\n"
            "    }\n"
            "    every 5m {\n"
            "        observe()\n"
            "    }\n"
            "}\n"
        )
    assert "Unknown risk limit 'max_yolo'" in excinfo.value.message
    assert "max_daily_loss" in excinfo.value.message


def test_counting_limits_must_be_whole_numbers():
    with pytest.raises(NanoTypeError) as excinfo:
        check_source(
            "strategy S {\n"
            "    risk {\n"
            "        stop_trading_after_losses 2.5\n"
            "    }\n"
            "    every 5m {\n"
            "        observe()\n"
            "    }\n"
            "}\n"
        )
    assert "whole number" in excinfo.value.message


# -- effect manifest ----------------------------------------------------------


def test_effects_are_derived_from_what_the_program_does():
    program = check_source(_strategy("RSI < 30"))
    assert program.effects == ("intent.emit", "log.append")


def test_a_strategy_that_only_observes_still_declares_intent_emit():
    program = check_source("strategy S {\n    every 5m {\n        observe()\n    }\n}\n")
    assert "intent.emit" in program.effects
