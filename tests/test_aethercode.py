"""Aether Code tooling tests — Milestone 6.

The tooling layer is pure functions over the compiler API: semantic tokens,
diagnostics, and IR preview. The contract under test: positions are the
lexer's exact 1-based line/column, valid source yields zero diagnostics,
invalid source never raises, and every rendering is deterministic.
"""

import json
from pathlib import Path

import pytest

from nano.aethercode import (
    Diagnostic,
    SemanticToken,
    diagnostics,
    ir_json,
    ir_preview,
    semantic_tokens,
)

EXAMPLES = Path(__file__).resolve().parent.parent / "nano" / "examples"
MOMENTUM_SOURCE = (EXAMPLES / "momentum.nano").read_text()
MOMENTUM_IR = json.loads((EXAMPLES / "momentum_ir.json").read_text())

NANO_SOURCES = sorted(EXAMPLES.glob("*.nano"))


# -- semantic tokens ---------------------------------------------------------


def test_momentum_semantic_token_kinds_and_positions():
    tokens = semantic_tokens(MOMENTUM_SOURCE)
    # Comment on line 1 emits no tokens; the stream starts at 'strategy'.
    assert tokens[0] == SemanticToken(line=2, column=1, length=8, kind="keyword")
    assert tokens[1] == SemanticToken(line=2, column=10, length=8, kind="identifier")
    assert tokens[2].kind == "punctuation"  # '{'
    assert tokens[3] == SemanticToken(line=4, column=5, length=5, kind="keyword")
    assert tokens[4] == SemanticToken(line=4, column=11, length=2, kind="interval")
    assert tokens[6].kind == "keyword"  # 'if'
    assert tokens[7].kind == "identifier"  # RSI — a signal, not a keyword
    assert tokens[11] == SemanticToken(line=6, column=20, length=1, kind="operator")
    assert tokens[12] == SemanticToken(line=6, column=22, length=2, kind="number")
    assert tokens[14] == SemanticToken(line=8, column=13, length=3, kind="action")
    assert tokens[16].kind == "identifier"  # BTC
    assert tokens[18] == SemanticToken(line=8, column=22, length=4, kind="number")


def test_momentum_semantic_tokens_are_in_source_order():
    tokens = semantic_tokens(MOMENTUM_SOURCE)
    positions = [(t.line, t.column) for t in tokens]
    assert positions == sorted(positions)
    assert all(t.line >= 1 and t.column >= 1 for t in tokens)


def test_semantic_tokens_for_invalid_but_lexable_source():
    tokens = semantic_tokens("strategy { { 5m")
    assert [t.kind for t in tokens] == [
        "keyword",
        "punctuation",
        "punctuation",
        "interval",
    ]


def test_semantic_tokens_survive_lexer_failure():
    # '$' is unlexable; the tokens before it are still classified.
    tokens = semantic_tokens("strategy S $ 5")
    assert [t.kind for t in tokens] == ["keyword", "identifier"]


def test_assignment_is_lexable_since_params_and_lets_use_it():
    # '=' was unlexable in v0.1.0. v1.0 binds params and lets with it, so it is a
    # real operator token, and `if x = 1` becomes a parser diagnostic rather than
    # a lexer one -- reported at the same position either way.
    tokens = semantic_tokens("param fast = 20")
    assert [t.kind for t in tokens] == ["keyword", "identifier", "operator", "number"]


# -- diagnostics -------------------------------------------------------------


def test_valid_source_has_no_diagnostics():
    assert diagnostics(MOMENTUM_SOURCE) == ()


@pytest.mark.parametrize("nano_path", NANO_SOURCES, ids=lambda p: p.stem)
def test_every_example_is_diagnostic_free(nano_path: Path):
    assert diagnostics(nano_path.read_text()) == ()


def test_missing_brace_yields_one_error_with_exact_position():
    result = diagnostics("strategy S {")
    assert len(result) == 1
    diagnostic = result[0]
    assert diagnostic.severity == "error"
    assert (diagnostic.line, diagnostic.column) == (1, 13)
    assert "missing '}'" in diagnostic.message


def test_confidence_out_of_range_yields_error():
    source = "strategy S { every 5m { if RSI < 30 { buy(BTC, 1.5) } } }"
    result = diagnostics(source)
    assert len(result) == 1
    assert result[0].severity == "error"
    assert "out of range" in result[0].message
    assert (result[0].line, result[0].column) == (1, 48)


def test_ir_level_failure_is_anchored_at_line_one():
    # Grammatical, but the IR contract rejects an empty node list.
    result = diagnostics("strategy S { }")
    assert len(result) == 1
    assert result[0].severity == "error"
    assert (result[0].line, result[0].column) == (1, 1)


def test_unlexable_source_never_raises():
    result = diagnostics("strategy S? {")
    assert len(result) == 1
    assert result[0].severity == "error"


def test_diagnostic_is_frozen():
    diagnostic = Diagnostic(line=1, column=1, message="x", severity="error")
    with pytest.raises(AttributeError):
        diagnostic.line = 2  # type: ignore[misc]


# -- IR preview --------------------------------------------------------------


def test_momentum_ir_preview_renders_graph_nodes():
    preview = ir_preview(MOMENTUM_SOURCE)
    lines = preview.split("\n")
    assert lines[0] == "Strategy: Momentum"
    assert "  Effects: intent.emit, log.append" in lines
    assert "  Schedule: every 5m" in lines
    assert "  Condition: RSI < 30" in lines
    assert "  Intent: BUY BTC @0.91" in lines


def test_momentum_ir_json_round_trips_to_handwritten_ir():
    assert json.loads(ir_json(MOMENTUM_SOURCE)) == MOMENTUM_IR


def test_ir_json_is_two_space_indented():
    assert ir_json(MOMENTUM_SOURCE).startswith('{\n  "type": "Strategy"')


def test_preview_of_invalid_source_renders_errors():
    preview = ir_preview("strategy S {")
    assert preview.startswith("error: 1:13: ")
    assert "\n" not in preview  # one diagnostic, one line


def test_ir_json_of_invalid_source_renders_errors():
    assert ir_json("strategy S {").startswith("error: 1:13: ")


def test_ir_preview_is_deterministic():
    assert ir_preview(MOMENTUM_SOURCE) == ir_preview(MOMENTUM_SOURCE)
    assert ir_json(MOMENTUM_SOURCE) == ir_json(MOMENTUM_SOURCE)
