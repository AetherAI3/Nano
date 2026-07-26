"""Compile and run the bundled Momentum strategy with host-supplied RSI data."""

from pathlib import Path

from nano.compiler import compile_source
from nano.runtime import MarketFrame, execute


_ROOT = Path(__file__).resolve().parents[1]


def run_demo() -> tuple[dict[str, object], ...]:
    """Return the proposals emitted by the checked-in Momentum fixture."""
    source = (_ROOT / "nano" / "examples" / "momentum.nano").read_text(encoding="utf-8")
    graph = compile_source(source)
    frame = MarketFrame(
        timestamps=(0, 300),
        signals={"RSI": (45.0, 22.0)},
    )
    return tuple(intent.to_dict() for intent in execute(graph, frame).intents)


def main() -> None:
    for intent in run_demo():
        print(
            f"{intent['intent']} {intent['asset']} at timestamp={intent['timestamp']} "
            f"(confidence={intent['confidence']})"
        )


if __name__ == "__main__":
    main()
