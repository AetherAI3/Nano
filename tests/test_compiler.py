"""`.nano` -> IR compiler tests (Milestone 4).

Conformance rule: compiled IR must be byte-identical to the hand-written
example corpus, and every compile error must carry an exact 1-based
line/column.
"""

import json
from pathlib import Path

import pytest

from nano.compiler import (
    NanoSyntaxError,
    compile_source,
    compile_to_dict,
    parse,
    tokenize,
)
from nano.runtime.interpreter import MarketFrame, execute

EXAMPLES = Path(__file__).resolve().parent.parent / "nano" / "examples"

MOMENTUM_SRC = (
    "strategy Momentum {\n"
    "    every 5m {\n"
    "        if RSI(14) < 30 {\n"
    "            buy(BTC, 0.91)\n"
    "        }\n"
    "    }\n"
    "}\n"
)


def _compile_error(source: str) -> NanoSyntaxError:
    with pytest.raises(NanoSyntaxError) as excinfo:
        compile_to_dict(source)
    return excinfo.value


# -- canonical output ---------------------------------------------------------


def test_momentum_compiles_to_exact_example_ir():
    expected = json.loads((EXAMPLES / "momentum_ir.json").read_text())
    assert compile_to_dict(MOMENTUM_SRC) == expected


def test_momentum_condition_value_stays_int():
    nodes = compile_to_dict(MOMENTUM_SRC)["nodes"]
    value = nodes[1]["value"]
    assert isinstance(value, int) and not isinstance(value, bool)
    confidence = nodes[2]["confidence"]
    assert isinstance(confidence, float)


def test_round_trip_is_fixed_point():
    assert compile_source(MOMENTUM_SRC).to_dict() == compile_to_dict(MOMENTUM_SRC)


def test_effects_manifest_is_locked():
    assert compile_to_dict(MOMENTUM_SRC)["effects"] == ["intent.emit", "log.append"]


# -- surface coverage ---------------------------------------------------------


def test_multi_condition_and_multi_action_body():
    src = (
        "strategy Multi {\n"
        "    every 1h {\n"
        "        if RSI(14) < 30 and Volume >= 1000 {\n"
        "            buy(ETH, 0.5)\n"
        "            sell(BTC)\n"
        "            execute()\n"
        "            pause()\n"
        "            observe()\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    assert compile_to_dict(src) == {
        "type": "Strategy",
        "nanoIrVersion": "0.1.0",
        "name": "Multi",
        "effects": ["intent.emit", "log.append"],
        "nodes": [
            {"type": "Schedule", "interval": "1h"},
            {"type": "Condition", "signal": "RSI", "operator": "<", "value": 30},
            {"type": "Condition", "signal": "Volume", "operator": ">=", "value": 1000},
            {"type": "Intent", "action": "BUY", "asset": "ETH", "confidence": 0.5},
            {"type": "Intent", "action": "SELL", "asset": "BTC"},
            {"type": "Intent", "action": "EXECUTE"},
            {"type": "Intent", "action": "PAUSE"},
            {"type": "Intent", "action": "OBSERVE"},
        ],
    }


def test_agent_declarations_in_source_order():
    src = (
        "strategy Agents {\n"
        "    agent RiskManager\n"
        "    every 5m {\n"
        "    }\n"
        "    agent Watcher\n"
        "}\n"
    )
    assert compile_to_dict(src)["nodes"] == [
        {"type": "Schedule", "interval": "5m"},
        {"type": "Agent", "name": "RiskManager"},
        {"type": "Agent", "name": "Watcher"},
    ]


def test_all_six_operators():
    src = (
        "strategy Ops {\n"
        "    every 5m {\n"
        "        if A < 1 and B <= 2 and C > 3 and D >= 4 and E == 5 and F != 6 {\n"
        "            observe()\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    graph = compile_source(src)
    assert [c.operator for c in graph.conditions] == ["<", "<=", ">", ">=", "==", "!="]
    assert [c.signal for c in graph.conditions] == ["A", "B", "C", "D", "E", "F"]
    assert [c.value for c in graph.conditions] == [1, 2, 3, 4, 5, 6]


def test_decimal_condition_value_stays_float():
    src = (
        "strategy F {\n"
        "    every 5m {\n"
        "        if Spread > 2.5 {\n"
        "            observe()\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    value = compile_to_dict(src)["nodes"][1]["value"]
    assert isinstance(value, float)
    assert value == 2.5


@pytest.mark.parametrize("interval", ["10s", "5m", "2h", "1d"])
def test_interval_units(interval):
    src = "strategy I {\n    every " + interval + " {\n    }\n}\n"
    assert compile_to_dict(src)["nodes"] == [
        {"type": "Schedule", "interval": interval}
    ]


def test_integer_confidence_stays_int_and_in_range():
    src = (
        "strategy C {\n"
        "    every 5m {\n"
        "        if RSI < 30 {\n"
        "            buy(BTC, 1)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    confidence = compile_to_dict(src)["nodes"][2]["confidence"]
    assert confidence == 1
    assert isinstance(confidence, int) and not isinstance(confidence, bool)


def test_comments_are_ignored():
    src = (
        "// leading comment\n"
        "strategy C { // trailing comment\n"
        "    every 5m {\n"
        "        // full-line comment inside a block\n"
        "    }\n"
        "}\n"
    )
    assert compile_to_dict(src)["name"] == "C"


def test_tokenize_and_parse_are_public():
    tokens = tokenize("every 5m")
    assert [(t.type, t.value) for t in tokens] == [
        ("IDENT", "every"),
        ("INTERVAL", "5m"),
        ("EOF", ""),
    ]
    assert (tokens[0].line, tokens[0].column) == (1, 1)
    assert (tokens[1].line, tokens[1].column) == (1, 7)

    ast = parse(MOMENTUM_SRC)
    assert ast.name == "Momentum"
    assert ast.schedule is not None and ast.schedule.interval == "5m"


# -- error cases (line/column asserted) ---------------------------------------


def test_unknown_action():
    err = _compile_error(
        "strategy S {\n"
        "    every 5m {\n"
        "        if RSI < 30 {\n"
        "            jump(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    assert (err.line, err.column) == (4, 13)
    assert "Unknown action 'jump'" in err.message


def test_bad_operator_token():
    err = _compile_error(
        "strategy S {\n"
        "    every 5m {\n"
        "        if RSI = 30 {\n"
        "            buy(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    assert (err.line, err.column) == (3, 16)
    assert "operator" in err.message


def test_confidence_out_of_range():
    err = _compile_error(
        "strategy S {\n"
        "    every 5m {\n"
        "        if RSI < 30 {\n"
        "            buy(BTC, 1.5)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    assert (err.line, err.column) == (4, 22)
    assert "out of range [0, 1]" in err.message


def test_two_schedule_blocks_compile_as_v1_ir():
    # v0.1.0 allowed at most one schedule because its flat document had exactly
    # one Schedule slot. v1.0's DAG holds several, so this now compiles -- and the
    # version bump is the visible evidence that it needed the richer shape.
    ir = compile_to_dict(
        "strategy S {\n"
        "    every 5m {\n"
        "    }\n"
        "    every 1h {\n"
        "    }\n"
        "}\n"
    )
    assert ir["nanoIrVersion"] == "1.0.0"
    assert [n["attrs"]["interval"] for n in ir["nodes"] if n["op"] == "schedule"] == [
        "5m",
        "1h",
    ]


def test_two_rules_in_one_schedule_compile_as_v1_ir():
    # Entry and exit in one schedule is the ordinary case for a real strategy.
    # Baseline IR could not express two independent rules; v1.0 emits one `rule`
    # node per rule, each with its own condition and body.
    ir = compile_to_dict(
        "strategy S {\n"
        "    every 5m {\n"
        "        if RSI < 30 {\n"
        "            buy(BTC)\n"
        "        }\n"
        "        if RSI > 70 {\n"
        "            sell(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    assert ir["nanoIrVersion"] == "1.0.0"
    assert len([n for n in ir["nodes"] if n["op"] == "rule"]) == 2
    assert len(ir["entries"]) == 2
    # One shared feed.signal node: RSI is read twice but is one data source.
    assert len([n for n in ir["nodes"] if n["op"] == "feed.signal"]) == 1


def test_single_rule_strategies_still_emit_baseline_ir():
    # The other half of the contract: reaching v1.0 happens only when the program
    # actually needs it. Anything baseline can express stays byte-identical.
    assert compile_to_dict(MOMENTUM_SRC)["nanoIrVersion"] == "0.1.0"


def test_a_higher_tier_forces_v1_ir_even_with_a_baseline_shaped_body():
    # Baseline IR has no `tier` field, so emitting it for a `nano+` module would
    # silently drop the declaration -- and the tier is an auditable statement about
    # whether a model can be in the loop, not a formatting detail.
    source = "tier nano+\n" + MOMENTUM_SRC
    document = compile_to_dict(source)
    assert document["nanoIrVersion"] == "1.0.0"
    assert document["tier"] == "nano+"


def test_unterminated_block():
    err = _compile_error("strategy S {\n    every 5m {\n")
    assert (err.line, err.column) == (3, 1)
    assert "Unterminated" in err.message


def test_garbage_token():
    err = _compile_error("strategy S { $ }\n")
    assert (err.line, err.column) == (1, 14)
    assert "Unexpected character" in err.message


def test_malformed_interval():
    err = _compile_error("strategy S {\n    every 5x {\n    }\n}\n")
    assert (err.line, err.column) == (2, 11)
    assert "Malformed interval" in err.message


def test_empty_source():
    err = _compile_error("")
    assert (err.line, err.column) == (1, 1)
    assert "strategy" in err.message


def test_missing_strategy_keyword():
    err = _compile_error("momentum { }\n")
    assert (err.line, err.column) == (1, 1)
    assert "Expected 'strategy'" in err.message


# -- compiled graphs execute --------------------------------------------------


def test_compiled_graph_executes():
    src = (
        "strategy Exec {\n"
        "    every 5m {\n"
        "        if RSI < 30 {\n"
        "            buy(BTC, 0.9)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    graph = compile_source(src)
    frame = MarketFrame(timestamps=(0, 300), signals={"RSI": (45.0, 22.0)})
    result = execute(graph, frame)
    assert [i.to_dict() for i in result.intents] == [
        {"intent": "BUY", "timestamp": 300, "asset": "BTC", "confidence": 0.9}
    ]
