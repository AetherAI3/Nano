#!/usr/bin/env python3
"""Check one library contribution — or the whole library — before review.

A library entry is a contract with four parts, and until now only two of them
were checked by anything. `tests/test_library.py` proves the source compiles to
its pinned IR and replays deterministically. It says nothing about the IR file's
house formatting or the comment header every existing entry carries, so a
contributor discovered both in review instead of before pushing.

This script is that missing check, and it is also the fixer:

    python scripts/check_contribution.py nano/library/risk/leverage_ceiling.nano
    python scripts/check_contribution.py --write nano/library/mine/my_rule.nano
    python scripts/check_contribution.py --all

`--write` generates or repairs the `_ir.json` partner in the library's exact
format, so nobody has to hand-reflow JSON to match its neighbours.

Exit code 0 means the entry is shaped like every other entry in the library.
Exit code 1 prints one line per problem, each naming the file and the rule.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
LIBRARY = ROOT / "nano" / "library"

if str(ROOT) not in sys.path:  # so a fresh clone works without `pip install -e .`
    sys.path.insert(0, str(ROOT))

from nano.compiler import compile_source, compile_to_dict  # noqa: E402
from nano.ir.module import NanoModule  # noqa: E402
from nano.ir.schema import NANO_IR_VERSION_BASELINE  # noqa: E402
from nano.runtime.vm import run_module  # noqa: E402
from nano.compiler.errors import NanoCompileError  # noqa: E402
from nano.ir.graph import StrategyGraph  # noqa: E402
from nano.runtime.interpreter import MarketFrame, execute  # noqa: E402

# Every entry in the library carries this header. It is what makes a rule
# reviewable by someone who did not write it: not "what does this compute" —
# the source says that — but when it is meant to fire and when it is wrong.
REQUIRED_HEADER_FIELDS = (
    ("REGIME:", "which market or system state this rule is for"),
    ("CONDITIONS:", "what must already be true before it is armed"),
    ("INVALIDATION:", "what makes it wrong, so a reader can disprove it"),
    ("SHAPE:", "the timeframe and the picture it is describing"),
    ("CALIBRATED ON:", "where the numbers came from, and what does not travel"),
)

# Optional provenance supplied by the contributor. If present it is one
# non-empty header line. Absence means "not recorded", not "original work";
# the checker can validate that shape but cannot certify a historical claim.
PROVENANCE_FIELD = "SOURCE:"

# The only effects a library entry may declare. Nano proposes intents and writes
# its own run log; anything else on this list would mean the language had grown
# a way to reach outside the host, which is the one thing it must not do.
ALLOWED_EFFECTS = ["intent.emit", "log.append"]


def canonical_ir_text(document: dict[str, Any]) -> str:
    """Render IR in the library's house format.

    Scalars one per line, `effects` inline, and one node per line — a strategy
    reads as a list of steps in a diff, which is the whole reason for the format
    and the reason `json.dumps(indent=2)` is not it.
    """
    lines = ["{"]
    for key in ("type", "nanoIrVersion", "name"):
        if key in document:
            lines.append(f"  {json.dumps(key)}: {json.dumps(document[key])},")
    for key, value in document.items():
        if key in ("type", "nanoIrVersion", "name", "effects", "nodes"):
            continue
        lines.append(f"  {json.dumps(key)}: {json.dumps(value)},")
    effects = ", ".join(json.dumps(effect) for effect in document.get("effects", []))
    lines.append(f"  \"effects\": [{effects}],")
    lines.append('  "nodes": [')
    nodes = document.get("nodes", [])
    for index, node in enumerate(nodes):
        body = ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in node.items())
        comma = "," if index < len(nodes) - 1 else ""
        lines.append(f"    {{ {body} }}{comma}")
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def ir_path(nano_path: Path) -> Path:
    return nano_path.with_name(f"{nano_path.stem}_ir.json")


def _comparison_candidates(conditions: list[Any]) -> tuple[float, ...]:
    """Boundary, adjacent, midpoint, and exterior values for linear guards."""
    thresholds = sorted({float(condition.value) for condition in conditions})
    span = max(1.0, *(abs(value) for value in thresholds))
    candidates = {
        value
        for threshold in thresholds
        for value in (
            threshold,
            math.nextafter(threshold, -math.inf),
            math.nextafter(threshold, math.inf),
        )
    }
    candidates.update(
        (left + right) / 2.0
        for left, right in zip(thresholds, thresholds[1:])
    )
    candidates.update((thresholds[0] - span, thresholds[-1] + span))
    return tuple(sorted(candidates))


def baseline_control_frames(graph: StrategyGraph) -> tuple[MarketFrame, MarketFrame]:
    """Return paired fire/no-fire frames for an AND-only baseline graph.

    Repeated constraints on one signal are solved together. An impossible
    conjunction is rejected instead of being replayed as a deterministic no-op.
    """
    if not graph.conditions:
        raise ValueError("baseline control requires at least one condition")

    conditions_by_signal: dict[str, list[Any]] = defaultdict(list)
    for condition in graph.conditions:
        conditions_by_signal[condition.signal].append(condition)

    passing_values: dict[str, float] = {}
    for name, conditions in conditions_by_signal.items():
        value = next(
            (
                candidate
                for candidate in _comparison_candidates(conditions)
                if all(condition.evaluate(candidate) for condition in conditions)
            ),
            None,
        )
        if value is None:
            raise ValueError(f"constraints on signal {name!r} cannot all be true")
        passing_values[name] = value

    passing = {name: (value,) * 2 for name, value in passing_values.items()}
    failing = dict(passing)
    first_name = graph.conditions[0].signal
    first_conditions = conditions_by_signal[first_name]
    failing_value = next(
        candidate
        for candidate in _comparison_candidates(first_conditions)
        if not all(condition.evaluate(candidate) for condition in first_conditions)
    )
    failing[first_name] = (failing_value,) * 2
    timestamps = (0, 86400)
    return (
        MarketFrame(timestamps=timestamps, signals=passing),
        MarketFrame(timestamps=timestamps, signals=failing),
    )


def module_control_frame(module: NanoModule) -> MarketFrame:
    """A non-degenerate frame with two scheduled bars beyond module warm-up."""
    count = module.warmup + 2
    close = tuple(
        100.0
        + 0.05 * index
        + 6.0 * math.sin(index / 7.0)
        + 2.0 * math.sin(index / 3.0)
        for index in range(count)
    )
    open_ = tuple(
        value + (0.1 if index % 2 else -0.1)
        for index, value in enumerate(close)
    )
    candidates = {
        "open": open_,
        "high": tuple(max(o, c) + 0.5 for o, c in zip(open_, close)),
        "low": tuple(min(o, c) - 0.5 for o, c in zip(open_, close)),
        "close": close,
        "volume": tuple(1000.0 + 25.0 * (index % 7) for index in range(count)),
    }
    signals = {
        declaration.name: candidates.get(
            declaration.name,
            tuple(value + position for value in close),
        )
        for position, declaration in enumerate(module.inputs)
    }
    return MarketFrame(
        timestamps=tuple(86400 * bar for bar in range(count)),
        signals=signals,
    )


def source_provenance_issues(header: list[str]) -> tuple[str, ...]:
    """Validate only the mechanically knowable part of optional provenance."""
    source_lines = [
        line for line in header if line.startswith(f"// {PROVENANCE_FIELD}")
    ]
    if len(source_lines) > 1:
        return (
            f"comment header has more than one `// {PROVENANCE_FIELD}` line. "
            "Record one concise provenance claim, or omit it when the source "
            "is not known.",
        )
    if source_lines and not source_lines[0].partition(PROVENANCE_FIELD)[2].strip():
        return (
            f"`// {PROVENANCE_FIELD}` is empty. Name a source you can "
            "truthfully verify, or remove the field; absence means provenance "
            "was not recorded.",
        )
    return ()


def check_entry(nano_path: Path, write: bool, problems: list[str]) -> None:
    rel = nano_path.relative_to(ROOT).as_posix()

    # `utf-8-sig` and not `utf-8`: an editor that saves a BOM is not a broken
    # contribution, and "Unexpected character '﻿'" is not a useful thing to
    # hand someone on their first pull request.
    source = nano_path.read_text(encoding="utf-8-sig")

    header = [line.strip() for line in source.splitlines() if line.strip().startswith("//")]
    header_text = "\n".join(header)
    if not header:
        problems.append(
            f"{rel}: no `//` comment header. Every library entry documents its "
            "signal contract and its invalidation before the source."
        )
    for field, why in REQUIRED_HEADER_FIELDS:
        if field not in header_text:
            problems.append(
                f"{rel}: comment header is missing `// {field}` — {why}. "
                "See nano/library/README.md."
            )

    problems.extend(f"{rel}: {issue}" for issue in source_provenance_issues(header))

    try:
        document = compile_to_dict(source)
        # The library ships two corpora. A baseline entry names the feed signals
        # the host must inject; a v1 entry declares them as typed inputs and
        # computes its indicators from them. Either way the host owes the data,
        # so both are checked against the same documentation rule below —
        # `compile_source` is baseline-only and cannot be called on a v1 entry.
        if document.get("nanoIrVersion") == NANO_IR_VERSION_BASELINE:
            graph = compile_source(source)
            required = tuple(c.signal for c in graph.conditions)
        else:
            graph = None
            required = tuple(i.name for i in NanoModule.from_dict(document).inputs)
    except NanoCompileError as error:
        problems.append(
            f"{rel}:{error.line}:{error.column}: does not compile — {error.message}"
        )
        return

    if document.get("effects") != ALLOWED_EFFECTS:
        problems.append(
            f"{rel}: declares effects {document.get('effects')!r}; a library entry "
            f"may only declare {ALLOWED_EFFECTS!r}. Nano proposes; the host acts."
        )

    # A signal is the host's obligation, so it has to be written down somewhere a
    # host implementer will look: either the file's own header, or the feed
    # convention tables in the library README. Established signals live in the
    # README and are reused across entries; a signal this entry invents has to
    # arrive with its definition.
    conventions = (LIBRARY / "README.md").read_text(encoding="utf-8")
    for name in required:
        if name not in header_text and name not in conventions:
            problems.append(
                f"{rel}: signal `{name}` is documented nowhere. The "
                "host has to implement it, so give it a formula or data source, a "
                "unit or range, and a lookback convention — in this file's header "
                "if it is new, or in the nano/library/README.md table if other "
                "entries will reuse it."
            )

    expected = canonical_ir_text(document)
    partner = ir_path(nano_path)
    if write:
        if not partner.exists() or partner.read_text(encoding="utf-8") != expected:
            partner.write_text(expected, encoding="utf-8", newline="\n")
            print(f"wrote {partner.relative_to(ROOT).as_posix()}")
    elif not partner.exists():
        problems.append(
            f"{rel}: has no `{partner.name}` partner. Run this script with "
            "`--write` to generate it."
        )
    else:
        actual = partner.read_text(encoding="utf-8")
        if json.loads(actual) != document:
            problems.append(
                f"{partner.relative_to(ROOT).as_posix()}: does not match what the "
                "source compiles to. Re-run with `--write` after changing the rule."
            )
        elif actual != expected:
            problems.append(
                f"{partner.relative_to(ROOT).as_posix()}: correct IR, wrong "
                "formatting. The library keeps one node per line so a strategy "
                "reads as a list of steps in a diff. Re-run with `--write`."
            )

    # Both checks below exist for every entry; only the loader differs, because
    # a v1 document is archived and executed as a NanoModule rather than a
    # StrategyGraph. Skipping them for v1 would leave the newer corpus unchecked.
    if graph is not None:
        round_tripped = StrategyGraph.from_dict(document).to_dict()
    else:
        round_tripped = NanoModule.from_dict(document).to_dict()
    if round_tripped != document:
        problems.append(
            f"{rel}: IR does not survive a round trip, so it cannot be archived "
            "and reloaded to the same rule."
        )

    if graph is not None:
        try:
            firing_frame, silent_frame = baseline_control_frames(graph)
        except ValueError as error:
            problems.append(f"{rel}: cannot build baseline replay controls — {error}.")
            return
        first, second = execute(graph, firing_frame), execute(graph, firing_frame)
        silent = execute(graph, silent_frame)
        if not first.intents:
            problems.append(
                f"{rel}: generated positive control emitted no intent. The "
                "contribution check must exercise behavior, not replay a no-op."
            )
        if silent.intents:
            problems.append(
                f"{rel}: generated negative control still emitted an intent. "
                "At least one condition is not independently falsifiable."
            )
    else:
        module = NanoModule.from_dict(document)
        frame = module_control_frame(module)
        first, second = run_module(module, frame), run_module(module, frame)
        evaluated = [entry for entry in first.log if entry.event == "condition.evaluated"]
        if not evaluated:
            problems.append(
                f"{rel}: replay reached no post-warmup condition evaluation "
                f"(declared warmup {module.warmup}, frame bars "
                f"{len(frame.timestamps)}). A deterministic no-op is not a "
                "meaningful replay control."
            )
    if first.to_dict() != second.to_dict():
        problems.append(
            f"{rel}: two runs over one frame produced different results. "
            "Determinism is the contract; this is a compiler or runtime bug — "
            "please open an issue with this file attached."
        )


def collect(targets: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for target in targets:
        path = Path(target).resolve()
        if path.is_dir():
            paths.extend(sorted(path.glob("**/*.nano")))
        else:
            paths.append(path)
    return paths


def check_orphans(problems: list[str]) -> None:
    for orphan in sorted(LIBRARY.glob("**/*_ir.json")):
        partner = orphan.with_name(orphan.name[: -len("_ir.json")] + ".nano")
        if not partner.exists():
            problems.append(
                f"{orphan.relative_to(ROOT).as_posix()}: no `.nano` partner. An IR "
                "file nothing compiles to is a fixture nothing checks."
            )


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):  # pragma: no cover - exotic stream
            pass

    parser = argparse.ArgumentParser(
        description="Check a library contribution before opening a pull request."
    )
    parser.add_argument(
        "paths", nargs="*", help="`.nano` files or directories to check"
    )
    parser.add_argument(
        "--all", action="store_true", help="check every entry in nano/library"
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="generate or repair the `_ir.json` partner in library format",
    )
    args = parser.parse_args()

    if args.all or not args.paths:
        paths = sorted(LIBRARY.glob("**/*.nano"))
    else:
        paths = collect(args.paths)

    if not paths:
        print("error: no .nano files to check", file=sys.stderr)
        return 1

    problems: list[str] = []
    for path in paths:
        if not path.exists():
            problems.append(f"{path}: no such file")
            continue
        check_entry(path, args.write, problems)

    if args.all or not args.paths:
        check_orphans(problems)

    if problems:
        print(f"\n{len(problems)} problem(s):\n", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nThe conventions are in nano/library/README.md and "
            "docs/first-contribution.md.",
            file=sys.stderr,
        )
        return 1

    print(f"{len(paths)} entr{'y' if len(paths) == 1 else 'ies'} ready for review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
