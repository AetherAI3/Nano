"""Editor/compiler parity for the v1 language, including model signatures."""

import json
from pathlib import Path

import pytest

from nano.aethercode import diagnostics, ir_json, ir_preview
from nano.compiler import IRVersionError, compile_source, compile_to_dict


TYPED = """strategy Typed {
  param threshold: float = 0.5
  input close: series<float>
  let shifted = close + threshold
  every 1m {
    if shifted > threshold { observe() } else { pause() }
  }
}"""

REASONING = """tier nano+
strategy Reasoning {
  input score: series<float>
  signature Judge {
    input value: float
    output confidence: confidence
  }
  let verdict = infer(Judge, score)
  agent Research { role research }
  every 1m {
    if verdict.confidence < 0.6 { escalate Research }
  }
}"""

LIBRARY = Path(__file__).resolve().parent.parent / "nano" / "library"
SOURCES = sorted(LIBRARY.glob("*/*.nano"))


@pytest.mark.parametrize("source", [TYPED, REASONING], ids=["typed", "reasoning"])
def test_v1_editor_accepts_supported_language_without_baseline_downgrade(source):
    document = compile_to_dict(source)
    assert document["nanoIrVersion"] == "1.0.0"
    assert diagnostics(source) == ()
    assert json.loads(ir_json(source)) == document
    preview = ir_preview(source)
    assert not preview.startswith("error:")
    assert "  IR: 1.0.0" in preview
    # Every node remains visible, including signatures and agents that are not
    # reachable from a schedule's operands. Operand references stay intact.
    node_lines = [line for line in preview.splitlines() if line.startswith("  Node ")]
    assert len(node_lines) == len(document["nodes"])
    for line, node in zip(node_lines, document["nodes"]):
        assert line.startswith(f"  Node {node['id']}: {node['op']}")
        for operand in node.get("inputs", []):
            assert operand in line
    assert preview == ir_preview(source)
    # The compatibility API intentionally remains baseline-only.
    with pytest.raises(IRVersionError):
        compile_source(source)


def test_v1_preview_keeps_types_declarations_and_branches_visible():
    preview = ir_preview(TYPED)
    assert "  Param: threshold: float = 0.5" in preview
    assert "  Input: close: series<float>" in preview
    assert "-> series<float>" in preview
    assert '"OBSERVE"' in preview
    assert '"PAUSE"' in preview


def test_reasoning_preview_is_a_plan_not_a_provider_call():
    preview = ir_preview(REASONING)
    assert "llm.call" in preview
    assert "ai.signature" in preview
    assert "ai.infer" in preview
    assert "llmre.escalate" in preview
    assert '"role": "research"' in preview


@pytest.mark.parametrize("path", SOURCES, ids=lambda path: path.stem)
def test_all_library_programs_have_editor_compiler_parity(path):
    source = path.read_text(encoding="utf-8")
    assert diagnostics(source) == ()
    assert json.loads(ir_json(source)) == compile_to_dict(source)
    assert not ir_preview(source).startswith("error:")


def test_baseline_preview_remains_byte_identical():
    source = "strategy Old { every 5m { if RSI < 30 { buy(BTC, 0.91) } } agent Desk }"
    assert ir_preview(source) == (
        "Strategy: Old\n"
        "  Effects: intent.emit, log.append\n"
        "  Schedule: every 5m\n"
        "  Condition: RSI < 30\n"
        "  Intent: BUY BTC @0.91\n"
        "  Agent: Desk"
    )
    assert compile_to_dict(source)["nanoIrVersion"] == "0.1.0"


def test_v1_unknown_name_keeps_exact_source_position_and_error_preview():
    source = """strategy Bad {
  input close: series<float>
  every 1m {
    if missing > 0 { observe() }
  }
}"""
    result = diagnostics(source)
    assert len(result) == 1
    error = result[0]
    assert (error.line, error.column) == (4, 8)
    assert "missing" in error.message
    expected = f"error: 4:8: {error.message}"
    assert ir_preview(source) == expected
    assert ir_json(source) == expected


def test_v1_string_attributes_stay_on_one_preview_line():
    source = 'tier nano+ strategy S { every 1m { escalate "Desk\\nReview" } }'
    assert diagnostics(source) == ()
    preview = ir_preview(source)
    assert '"target": "Desk\\nReview"' in preview
    assert "\nReview" not in preview
