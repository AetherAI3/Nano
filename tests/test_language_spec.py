"""Executable fences for the shipped language reference.

The reference is useful only while its syntax tables and runtime boundaries are
derived from the same contracts as the implementation.  These tests make the
high-drift claims fail loudly: every Nano fence crosses compiler and loader,
every registry/tier/risk vocabulary is matched to its documentation table, and
representative programs cross the VM and receipt boundary.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from nano.compiler import IRVersionError, NanoTypeError, compile_module, compile_to_dict
from nano.indicators import evaluate
from nano.indicators.registry import INDICATORS
from nano.ir import load, load_module
from nano.ir.module import OPS
from nano.ir.schema import (
    AGENT_ROLES,
    EFFECT_ORDER,
    KNOWN_EFFECTS,
    NANO_IR_VERSION_1_0,
    NANO_IR_VERSION_BASELINE,
    RISK_LIMITS,
    SUPPORTED_IR_VERSIONS,
    TIER_REQUIREMENTS,
    TIERS,
)
from nano.runtime import MarketFrame, build_receipt, canonical_bytes, run_module
from nano.runtime.receipt import RECEIPT_VERSION
from nano.runtime.risk import ENFORCED_LIMITS, HOST_ENFORCED_LIMITS


ROOT = Path(__file__).resolve().parent.parent
LANGUAGE = ROOT / "docs" / "language.md"
TEXT = LANGUAGE.read_text(encoding="utf-8")
NANO_FENCE = re.compile(
    r"^```nano[ \t]*$\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL
)


def _table(marker: str) -> tuple[list[str], list[dict[str, str]]]:
    match = re.search(
        rf"<!-- {re.escape(marker)}:start -->(.*?)"
        rf"<!-- {re.escape(marker)}:end -->",
        TEXT,
        re.DOTALL,
    )
    assert match, f"docs/language.md has no {marker!r} table markers"
    lines = [line for line in match.group(1).splitlines() if line.startswith("|")]
    assert len(lines) >= 3, f"{marker!r} is not a populated Markdown table"

    def cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    header = cells(lines[0])
    assert all(set(cell) <= {"-", ":"} for cell in cells(lines[1]))
    rows = [dict(zip(header, cells(line))) for line in lines[2:]]
    assert all(len(cells(line)) == len(header) for line in lines[2:])
    return header, rows


def _unquote(value: str) -> str:
    return value[1:-1] if value.startswith("`") and value.endswith("`") else value


def test_every_language_reference_nano_fence_compiles_and_loads_end_to_end():
    blocks = list(NANO_FENCE.finditer(TEXT))
    assert len(blocks) == 3, "adding or removing a language example is deliberate"

    observed = []
    for block in blocks:
        source = block.group(1)
        line = TEXT.count("\n", 0, block.start()) + 2
        document = compile_to_dict(source)
        loaded = load(document)
        executable = load_module(document)
        direct = compile_module(source)
        assert executable.validate() == executable, f"language.md:{line}"
        assert direct.validate() == direct, f"language.md:{line}"
        observed.append((document["name"], document["nanoIrVersion"], type(loaded)))

    assert [(name, version) for name, version, _ in observed] == [
        ("Truth", NANO_IR_VERSION_1_0),
        ("BaselineMomentum", NANO_IR_VERSION_BASELINE),
        ("GuardedBreakout", NANO_IR_VERSION_1_0),
    ]


def test_construct_matrix_is_complete_and_names_real_examples():
    header, rows = _table("construct-matrix")
    assert header == [
        "Construct",
        "Parsed?",
        "Typed?",
        "Compiled?",
        "IR version",
        "Executed?",
        "Minimum tier",
        "Required effect",
        "Example test",
    ]
    expected = {
        "Strategy and tier",
        "`param`",
        "declared `input`",
        "implicit feed signal",
        "builtin `confidence`",
        "`let`",
        "arithmetic `+ - * / %`",
        "indexing/lookback `series[n]`",
        "computed indicator",
        "multiple schedules",
        "multiple rules",
        "nested `if` and `else`",
        "`and`, `or`, `not`",
        "actions",
        "`risk`",
        "`agent`",
        "`signature`",
        "`infer`",
        "`route`",
        "`escalate`",
    }
    assert {row["Construct"] for row in rows} == expected
    assert all(row["Parsed?"] == "yes" for row in rows)
    for row in rows:
        test_name = _unquote(row["Example test"])
        assert callable(globals().get(test_name)), (
            f"construct {row['Construct']} cites missing {test_name}"
        )


BASELINE = """strategy Baseline {
  every 5m {
    if RSI(14) < 30 and VOLUME > 1000 {
      buy(BTC, 0.8)
    }
  }
}
"""

V1_PARAM = """strategy V1 {
  param threshold: float = 2
  every 5m {
    if RSI > threshold {
      observe()
    }
  }
}
"""


def test_lowest_ir_version_selection_is_pinned():
    assert SUPPORTED_IR_VERSIONS == (
        NANO_IR_VERSION_BASELINE,
        NANO_IR_VERSION_1_0,
    )
    assert compile_to_dict(BASELINE)["nanoIrVersion"] == NANO_IR_VERSION_BASELINE
    assert (
        compile_to_dict(BASELINE, ir_version=NANO_IR_VERSION_1_0)["nanoIrVersion"]
        == NANO_IR_VERSION_1_0
    )
    assert compile_to_dict(V1_PARAM)["nanoIrVersion"] == NANO_IR_VERSION_1_0
    with pytest.raises(IRVersionError):
        compile_to_dict(V1_PARAM, ir_version=NANO_IR_VERSION_BASELINE)

    header, rows = _table("ir-selection-matrix")
    assert header == [
        "Complete source shape",
        "Automatic IR",
        "Can force 0.1.0?",
        "Can force 1.0.0?",
    ]
    assert [row["Automatic IR"] for row in rows] == ["0.1.0", "1.0.0"]


def test_feed_signal_and_computed_indicator_forms_stay_distinct():
    baseline = compile_to_dict(BASELINE)
    conditions = [node for node in baseline["nodes"] if node["type"] == "Condition"]
    assert conditions[0]["signal"] == "RSI"
    assert "period" not in conditions[0]

    computed = compile_module(
        """strategy Computed {
  input close: series<float>
  let rsi = RSI(close, 14)
  every 1m {
    if rsi < 30 and SENTIMENT(1) > 0.5 {
      observe()
    }
  }
}
"""
    )
    indicators = computed.of_op("indicator")
    feeds = computed.of_op("feed.signal")
    assert [(node.attrs["name"], node.attrs["periods"]) for node in indicators] == [
        ("RSI", [14])
    ]
    assert [node.attrs["name"] for node in feeds] == ["SENTIMENT"]


def test_v1_declarations_control_flow_and_warmup_execute():
    source = """strategy Flow {
  param lag: int = 1
  param threshold: float = 0
  input close: series<float>
  input gate: series<bool>
  let change = (((close - close[lag]) * 2) / 1) % 5 + 0
  let inverse = -change

  every 1m {
    if (change > threshold and inverse < 0 and gate) or not gate {
      if close[1] < close {
        buy(BTC, 0.8)
      } else {
        observe()
      }
    } else {
      pause()
    }
  }
}
"""
    module = compile_module(source)
    assert module.warmup == 1
    assert {node.op for node in module.nodes} >= {
        "param.ref",
        "input.ref",
        "let",
        "series.index",
        "arith.add",
        "arith.sub",
        "arith.mul",
        "arith.div",
        "arith.mod",
        "arith.neg",
        "logic.and",
        "logic.or",
        "logic.not",
        "rule",
        "block",
    }
    frame = MarketFrame(
        timestamps=(0, 60, 120, 180),
        signals={
            "close": (1.0, 2.0, 1.0, 3.0),
            "gate": (True, True, True, False),
        },
    )
    result = run_module(module, frame)
    assert [(intent.action, intent.timestamp) for intent in result.intents] == [
        ("BUY", 60),
        ("OBSERVE", 120),
        ("BUY", 180),
    ]
    assert result.warmup_bars_skipped == 1


def test_builtin_confidence_priority_and_default():
    local_source = """strategy LocalConfidence {
  every 1m {
    if confidence < 0.6 { observe() }
  }
}
"""
    local = compile_module(local_source)
    assert local.of_op("builtin.confidence")
    assert compile_to_dict(local_source)["nanoIrVersion"] == NANO_IR_VERSION_1_0
    pure = compile_module(
        "strategy PureConfidence { let low = confidence < 0.6 }\n"
    )
    assert pure.effects == ("log.append",)
    defaulted = run_module(local, MarketFrame(timestamps=(0,), signals={}))
    assert defaulted.intents == ()  # no infer and no frame value -> 1.0
    injected = run_module(
        local,
        MarketFrame(timestamps=(0,), signals={"confidence": (0.4,)}),
    )
    assert [intent.action for intent in injected.intents] == ["OBSERVE"]

    reasoned = compile_module(
        """tier nano+
strategy ReasonedConfidence {
  input score: series<float>
  signature Judge {
    input value: float
    output confidence: confidence
  }
  let verdict = infer(Judge, score)
  every 1m {
    if confidence < 0.6 { observe() }
  }
}
"""
    )

    class Provider:
        def infer(self, signature, inputs, *, timestamp):
            return {"confidence": 0.4}

    frame = MarketFrame(timestamps=(0,), signals={"score": (1.0,)})
    derived = run_module(reasoned, frame, provider=Provider())
    assert [intent.action for intent in derived.intents] == ["OBSERVE"]
    absent = run_module(reasoned, frame)
    assert absent.intents == ()  # infer exists but has no provider -> absence
    overridden = run_module(
        reasoned,
        MarketFrame(
            timestamps=(0,), signals={"score": (1.0,), "confidence": (0.9,)}
        ),
        provider=Provider(),
    )
    assert overridden.intents == ()  # frame confidence has first priority

    with pytest.raises(NanoTypeError, match="already declared"):
        compile_module("strategy Reserved { param confidence = 1 every 1m {} }")


def test_recursive_unary_operators_and_empty_risk_grammar_are_exact():
    module = compile_module(
        """tier nano+
strategy Unary {
  param one: int = 1
  signature Echo {
    input value: int
    output ok: bool
  }
  let restored = --one
  let echoed = infer(Echo, restored)
  every 1m {
    if not not (restored == 1) { observe() }
  }
}
"""
    )
    assert [node.op for node in module.nodes].count("arith.neg") == 2
    assert [node.op for node in module.nodes].count("logic.not") == 2

    seen = []

    class Provider:
        def infer(self, signature, inputs, *, timestamp):
            seen.append((signature, inputs["value"], type(inputs["value"])))
            return {"ok": True}

    result = run_module(
        module, MarketFrame(timestamps=(0,), signals={}), provider=Provider()
    )
    assert [intent.action for intent in result.intents] == ["OBSERVE"]
    # The checker keeps --one typed as int, while VM arithmetic uses floats.
    assert seen == [("Echo", 1.0, float)]

    # Parsing admits an empty risk body; the checker applies the documented
    # semantic requirement that a declared block contain at least one limit.
    assert 'risk          := "risk" "{" (IDENT signedNumber)* "}"' in TEXT
    assert 'notExpression := "not" notExpression | comparison' in TEXT
    assert 'unary         := "-" unary | postfix' in TEXT


def test_multiple_schedules_and_rules_execute():
    module = compile_module(
        """strategy Many {
  input close: series<float>
  every 1m {
    if close > 1 { buy(BTC, 0.8) }
    if close < 3 { sell(BTC, 0.7) }
  }
  every 2m {
    observe()
  }
}
"""
    )
    assert len(module.of_op("schedule")) == 2
    assert len(module.of_op("rule")) == 3
    result = run_module(
        module,
        MarketFrame(timestamps=(0, 60, 120), signals={"close": (2.0, 4.0, 0.0)}),
    )
    assert [(intent.action, intent.timestamp) for intent in result.intents] == [
        ("BUY", 0),
        ("BUY", 60),
        ("SELL", 0),
        ("SELL", 120),
        ("OBSERVE", 0),
        ("OBSERVE", 120),
    ]


def test_tier_and_effect_matrix_matches_schema():
    assert TIERS == ("nano", "nano+", "nano++")
    assert TIER_REQUIREMENTS == {
        "signature": "nano+",
        "route": "nano+",
        "escalate": "nano+",
        "infer": "nano+",
    }
    assert EFFECT_ORDER == (
        "intent.emit",
        "llm.call",
        "llmre.escalate",
        "sign.emit",
        "log.append",
    )
    assert set(EFFECT_ORDER) == KNOWN_EFFECTS

    _, rows = _table("tier-effect-matrix")
    by_construct = {row["Source construct"]: row for row in rows}
    assert set(by_construct) == {
        "pure declarations, expressions, indicators, schedules, agents, risk",
        "builtin `confidence`",
        "any action",
        "`signature`",
        "`infer`",
        "`route`",
        "`escalate`",
        "`nano++` header",
    }
    for construct in ("signature", "infer", "route", "escalate"):
        assert by_construct[f"`{construct}`"]["Minimum tier"] == "`nano+`"
    assert by_construct["`nano++` header"]["Effect added when used"] == "none by itself"
    assert by_construct["builtin `confidence`"]["Minimum tier"] == "`nano`"

    highest = compile_module("tier nano++\nstrategy T { every 1m {} }\n")
    assert highest.tier == "nano++"
    assert highest.effects == ("log.append",)


def test_agent_roles_match_schema():
    assert AGENT_ROLES == ("research", "validation", "execution", "observer")
    source = "strategy Roles {\n" + "\n".join(
        f"  agent A{index} {{ role {role} }}"
        for index, role in enumerate(AGENT_ROLES)
    ) + "\n}\n"
    module = compile_module(source)
    assert [node.attrs["role"] for node in module.of_op("agent")] == list(AGENT_ROLES)


def test_reasoning_route_and_escalation_runtime_boundaries():
    source = """tier nano+
strategy Reasoned {
  input score: series<float>
  signature Judge {
    input value: float range [0, 1]
    output confidence: confidence range [0, 1]
  }
  let verdict = infer(Judge, score)
  agent Desk { role validation }
  route Gate {
    execute approved when verdict.confidence >= 0.5
    otherwise { escalate Desk }
  }
  every 1m {
    if score < 0 { escalate "host-desk" }
  }
}
"""
    module = compile_module(source)
    assert module.effects == ("llm.call", "llmre.escalate", "log.append")
    frame = MarketFrame(timestamps=(0, 60), signals={"score": (-1.0, 1.0)})

    absent = run_module(module, frame)
    assert [(item.target, item.is_agent) for item in absent.escalations] == [
        ("host-desk", False),
        ("Desk", True),
        ("Desk", True),
    ]
    assert any(entry.event == "infer.skipped" for entry in absent.log)

    class Provider:
        def infer(self, signature, inputs, *, timestamp):
            assert signature == "Judge"
            return {"confidence": 0.9 if timestamp == 0 else 2.0}

    supplied = run_module(module, frame, provider=Provider())
    # 2.0 is outside the declared range.  The current VM treats ranges as
    # schema metadata and does not validate provider output against them.
    assert [(item.target, item.is_agent) for item in supplied.escalations] == [
        ("host-desk", False)
    ]
    assert supplied.intents == ()  # route execute labels do not dispatch intents
    executed = [entry for entry in supplied.log if entry.event == "route.executed"]
    assert len(executed) == 2
    assert all("approved" in entry.detail for entry in executed)


WARMUP_TEXT = {
    **{name: "p - 1" for name in (
        "SMA", "EMA", "WMA", "STDDEV", "ZSCORE", "SUM", "COUNT_TRUE",
        "RISING", "FALLING", "HIGHEST", "LOWEST", "STOCH_K", "WILLR",
        "CCI", "VWAP", "BB_MIDDLE", "BB_UPPER", "BB_LOWER", "BB_PCT_B",
        "BB_WIDTH",
    )},
    **{name: "p" for name in (
        "PERCENTRANK", "RSI", "ROC", "MOM", "ATR", "SUPERTREND",
        "SUPERTREND_DIR",
    )},
    "BARS_SINCE": "0",
    "CHANGE": "1",
    "TR": "1",
    "OBV": "1",
    "CROSSOVER": "1",
    "CROSSUNDER": "1",
    "ABS": "0",
    "SQRT": "0",
    "MIN": "0",
    "MAX": "0",
    "MACD_LINE": "max(fast, slow) - 1",
    "MACD_SIGNAL": "max(fast, slow) - 1 + signal - 1",
    "MACD_HIST": "max(fast, slow) - 1 + signal - 1",
}


def _period_probe(name: str) -> tuple[tuple[int, ...], int]:
    formula = WARMUP_TEXT[name]
    if formula == "p - 1":
        return (3,), 2
    if formula == "p":
        return (3,), 3
    if formula == "max(fast, slow) - 1":
        return (2, 5), 4
    if formula == "max(fast, slow) - 1 + signal - 1":
        return (2, 5, 3), 6
    return (), int(formula)


def test_indicator_matrix_matches_registry():
    header, rows = _table("indicator-matrix")
    assert header == ["Indicator", "Signature", "Kernel warm-up", "Semantics"]
    by_name = {_unquote(row["Indicator"]): row for row in rows}
    assert len(by_name) == len(INDICATORS) == 40
    assert set(by_name) == set(INDICATORS) == set(WARMUP_TEXT)

    for name, spec in INDICATORS.items():
        row = by_name[name]
        assert _unquote(row["Signature"]) == spec.signature_text()
        assert _unquote(row["Kernel warm-up"]) == WARMUP_TEXT[name]
        periods, expected = _period_probe(name)
        assert spec.lookback(periods) == expected


def test_g1_primitive_semantics_are_pinned():
    assert evaluate(
        "BARS_SINCE", [(False, True, False, None, False, True, False)], length=7
    ) == (None, 0.0, 1.0, None, None, 0.0, 1.0)
    assert evaluate("COUNT_TRUE", [(True, False, None), 1], length=3) == (
        1.0,
        0.0,
        None,
    )
    assert evaluate(
        "COUNT_TRUE", [(True, False, True, True, True, False), 3], length=6
    ) == (None, None, 2.0, 2.0, 3.0, 2.0)
    assert evaluate("RISING", [(1.0, 2.0, 2.0, None, 4.0), 2], length=5) == (
        None,
        True,
        False,
        None,
        None,
    )
    assert evaluate("FALLING", [(2.0, 1.0, 1.0), 2], length=3) == (
        None,
        True,
        False,
    )
    assert evaluate("RISING", [(1.0, None), 1], length=2) == (True, None)
    assert evaluate("PERCENTRANK", [(5.0, 4.0, 4.0), 1], length=3) == (
        None,
        0.0,
        1.0,
    )
    assert evaluate("PERCENTRANK", [(5.0, 4.0, 4.0), 2], length=3) == (
        None,
        None,
        0.5,
    )
    assert evaluate("PERCENTRANK", [(1.0, None, 2.0, 3.0), 2], length=4) == (
        None,
        None,
        None,
        None,
    )


def test_risk_matrix_matches_runtime_contract():
    header, rows = _table("risk-matrix")
    assert header == [
        "Limit",
        "Declared domain",
        "Allowed while",
        "Runtime measurement",
        "Enforcement owner",
    ]
    assert [_unquote(row["Limit"]) for row in rows] == list(RISK_LIMITS)
    assert {rule.limit for rule in ENFORCED_LIMITS} == {
        "max_daily_loss",
        "max_drawdown",
        "max_orders_per_day",
        "stop_trading_after_losses",
        "min_confidence",
    }
    assert {name for name, _ in HOST_ENFORCED_LIMITS} == {
        "max_position_size",
        "max_open_positions",
    }

    module = compile_module(
        """strategy RiskTruth {
  risk {
    max_position_size 0.2
    max_drawdown 0.05
    max_orders_per_day 2
  }
  every 1m {
    buy(BTC, 0.8)
    sell(BTC, 0.8)
    observe()
  }
}
"""
    )
    result = run_module(
        module,
        MarketFrame(
            timestamps=(0,),
            signals={"risk.drawdown": (0.05,), "risk.orders_today": (1,)},
        ),
    )
    assert [intent.action for intent in result.intents] == ["BUY", "OBSERVE"]
    assert [entry.event for entry in result.log].count("risk.unenforced") == 1
    assert [entry.event for entry in result.log].count("risk.violation") == 1

    invalid = run_module(
        module,
        MarketFrame(
            timestamps=(0,),
            signals={"risk.drawdown": (-0.01,), "risk.orders_today": (0,)},
        ),
    )
    assert [intent.action for intent in invalid.intents] == ["OBSERVE"]
    assert any(
        entry.event == "risk.violation" and "outside valid domain" in entry.detail
        for entry in invalid.log
    )

    drawdown_only = compile_module(
        """strategy Closed {
  risk { max_drawdown 0.05 }
  every 1m { buy(BTC, 0.8) }
}
"""
    )
    for signals in ({}, {"risk.drawdown": (float("nan"),)}):
        closed = run_module(
            drawdown_only, MarketFrame(timestamps=(0,), signals=signals)
        )
        assert closed.intents == ()
        assert any(entry.event == "risk.violation" for entry in closed.log)

    profit = compile_module(
        """strategy Profit {
  risk { max_daily_loss 0.02 }
  every 1m { buy(BTC, 0.8) }
}
"""
    )
    permitted = run_module(
        profit,
        MarketFrame(timestamps=(0,), signals={"risk.daily_loss": (-0.3,)}),
    )
    assert [intent.action for intent in permitted.intents] == ["BUY"]


def test_receipt_claims_match_runtime():
    document = compile_to_dict(BASELINE)
    assert document["nanoIrVersion"] == NANO_IR_VERSION_BASELINE
    module = load_module(document)
    frame = MarketFrame(
        timestamps=(0, 300),
        signals={"RSI": (40.0, 20.0), "VOLUME": (2000.0, 2000.0)},
    )
    receipt = build_receipt(module, frame, run_module(module, frame))
    assert receipt["receiptVersion"] == RECEIPT_VERSION == 1
    assert receipt["identity"]["irVersion"] == NANO_IR_VERSION_1_0
    assert receipt["identity"]["reasoningRequired"] is False
    assert set(receipt["run"]) == {
        "intents",
        "escalations",
        "log",
        "warmupBarsSkipped",
    }
    encoded = canonical_bytes(receipt)
    assert not encoded.endswith(b"\n")
    encoded.decode("ascii")

    empty = build_receipt(
        module,
        MarketFrame(timestamps=(), signals={"RSI": (), "VOLUME": ()}),
        run_module(
            module, MarketFrame(timestamps=(), signals={"RSI": (), "VOLUME": ()})
        ),
        host={"deployment": "test"},
    )
    assert "firstTimestamp" not in empty["inputs"]
    assert "lastTimestamp" not in empty["inputs"]
    assert empty["host"] == {"deployment": "test"}
    assert "params" not in empty["identity"]
    tuned = compile_module(V1_PARAM)
    tuned_frame = MarketFrame(timestamps=(0,), signals={"RSI": (3.0,)})
    tuned_receipt = build_receipt(
        tuned, tuned_frame, run_module(tuned, tuned_frame)
    )
    assert tuned_receipt["identity"]["params"] == [
        {"name": "threshold", "type": "float", "value": 2}
    ]
    assert "host" not in tuned_receipt
    assert "fixed vocabulary of members it may emit" in TEXT

    header, rows = _table("receipt-matrix")
    assert header == ["Receipt claim", "Current behavior"]
    assert [row["Receipt claim"] for row in rows] == [
        "format version",
        "executable IR identity",
        "content identity",
        "run account",
        "provenance",
        "canonical bytes",
        "CLI framing",
    ]


def test_future_trade_proposal_is_not_documented_as_shipped():
    assert "trade.propose" not in OPS
    assert "a `trade.propose` source form or IR operation" in TEXT
