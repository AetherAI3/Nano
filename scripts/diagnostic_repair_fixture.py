#!/usr/bin/env python3
"""Run the bounded JSON diagnostic repair fixture without a model or provider."""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nano.cli.commands import Console
from nano.cli.main import build_parser
from nano.fuzzing.diagnostic_repair import run_repair_fixture


def run() -> dict:
    sources = {
        "recover-observe": "strategy Repair { every 1m { if (RSI < 30 { observe() } } }",
        "valid-wrong-task": "strategy Repair { every 1m { if RSI < 30 { buy(BTC) } } }",
        "recover-wrong-task": "strategy Repair { every 1m { if (RSI < 30 { buy(BTC) } } }",
    }
    with tempfile.TemporaryDirectory(prefix="nano-diagnostic-fixture-") as temporary:
        path = Path(temporary) / "fixture.nano"

        def check_json(source: str) -> str:
            path.write_text(source, encoding="utf-8", newline="\n")
            args = build_parser().parse_args(["check", str(path), "--json"])
            output, errors = io.StringIO(), io.StringIO()
            code = args.handler(args, Console(out=output, err=errors))
            if code not in (0, 1) or errors.getvalue():
                raise RuntimeError(f"Fixture check failed operationally: {errors.getvalue()}")
            return output.getvalue()

        results = {name: run_repair_fixture(source, check_json)
                   for name, source in sources.items()}
    return {
        "fixtureVersion": "1.0.0", "policy": "one-missing-rparen-before-lbrace",
        "results": results,
        "summary": {
            "cases": len(results),
            "validProgramRecoveries": sum(item["validProgramRecovered"] for item in results.values()),
            "semanticTaskCorrect": sum(item["semanticTaskCorrect"] for item in results.values()),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    document = run()
    encoded = json.dumps(document, sort_keys=True, indent=2) + "\n"
    if args.output is None:
        sys.stdout.write(encoded)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8", newline="\n")
    return 0 if document["summary"] == {
        "cases": 3, "validProgramRecoveries": 2, "semanticTaskCorrect": 1,
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
