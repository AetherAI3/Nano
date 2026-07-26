"""Regression coverage for the marked Nano example in the repository README."""

from pathlib import Path
import re

from nano.compiler import compile_source


_ROOT = Path(__file__).resolve().parents[1]
_START = "<!-- README-EXAMPLE:START -->"
_END = "<!-- README-EXAMPLE:END -->"


def _readme_example() -> str:
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(
        rf"{re.escape(_START)}\s*```nano\s*\n(?P<source>.*?)\n```\s*{re.escape(_END)}",
        text,
        re.DOTALL,
    )
    assert match is not None, "README must contain a marked Nano example"
    return match.group("source")


def test_readme_nano_example_compiles_to_the_documented_strategy() -> None:
    graph = compile_source(_readme_example())

    assert graph.name == "Momentum"
    assert graph.schedule is not None
    assert graph.schedule.interval == "5m"
    assert [(condition.signal, condition.operator, condition.value) for condition in graph.conditions] == [
        ("RSI", "<", 30)
    ]
    assert [intent.to_dict() for intent in graph.intents] == [
        {"type": "Intent", "action": "BUY", "asset": "BTC", "confidence": 0.91}
    ]
