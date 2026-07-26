"""Regression coverage for the README's runnable momentum demo."""

import runpy
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def test_momentum_demo_emits_the_documented_buy_intent() -> None:
    namespace = runpy.run_path(str(_ROOT / "examples" / "momentum_demo.py"))

    assert namespace["run_demo"]() == (
        {
            "intent": "BUY",
            "timestamp": 300,
            "asset": "BTC",
            "confidence": 0.91,
        },
    )
