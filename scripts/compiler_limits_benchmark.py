#!/usr/bin/env python3
"""Measure admitted depth-boundary parsing fixtures; no providers or runtime.

This is a reproducible fixture benchmark, not a global worst-case bound: source
width is deliberately unbounded by the nesting-only admission policy.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nano.compiler import MAX_COMPILER_NESTING, parse, tokenize


def fixtures(width: int) -> dict[str, str]:
    n = MAX_COMPILER_NESTING
    exprs = {
        "parentheses": "(" * n + "true" + ")" * n,
        "unary_not": "not " * n + "true",
        "unary_minus": "-" * n + "1",
        "boolean_chain": " and ".join(["true"] * (n + 1)),
        "arithmetic_chain": " + ".join(["1"] * (n + 1)),
        "nested_calls": "abs(" * n + "1" + ")" * n,
        "nested_indices": "x[" * n + "0" + "]" * n,
        "postfix_chain": "x" + ".field" * n,
        "mixed": "(" * (n // 2) + "not " * n + "true" + ")" * (n // 2),
    }
    result = {name: f"strategy Bench {{ let value = {expr} }}" for name, expr in exprs.items()}
    result["nested_conditionals"] = (
        "strategy Bench { every 1m { " + "if true { " * n
        + "observe() " + "} " * n + "} }"
    )
    # Exercise width at maximum admitted expression depth. Width is a named,
    # recorded workload parameter, not a compiler limit.
    chain = exprs["boolean_chain"]
    result["wide_at_depth_limit"] = (
        "strategy Bench { "
        + " ".join(f"let value{i} = {chain}" for i in range(width)) + " }"
    )
    return result


def measure(source: str, repeats: int) -> dict:
    parse(source)  # warm-up and admission check
    times = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        parse(source)
        times.append((time.perf_counter_ns() - start) / 1_000_000)
    tracemalloc.start()
    ast = parse(source)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    del ast
    return {
        "sourceBytes": len(source.encode("utf-8")),
        "tokensIncludingEof": len(tokenize(source)),
        "parseMsMedian": round(statistics.median(times), 6),
        "parseMsMax": round(max(times), 6),
        "parsePeakTracedBytes": peak,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=25)
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1 or args.width < 1:
        parser.error("--repeats and --width must be positive")
    report = {
        "schemaVersion": "1.0.0",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pythonRecursionLimit": sys.getrecursionlimit(),
        "maxNesting": MAX_COMPILER_NESTING,
        "repeats": args.repeats,
        "width": args.width,
        "scope": "parse including lexing; separate tracemalloc pass; no type checking or lowering",
        "fixtures": {name: measure(source, args.repeats) for name, source in fixtures(args.width).items()},
    }
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
