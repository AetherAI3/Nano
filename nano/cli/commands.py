"""Command implementations for the `nano` CLI.

Each command is a function of parsed arguments returning an exit code, writing
through an injected stream. That shape keeps argument parsing (``main.py``)
separate from the work, and lets the tests exercise every command without
spawning a subprocess.

Exit codes are part of the interface, because CI reads them:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | the source or the run was rejected — diagnostics printed |
| 2 | the command was used wrongly (argparse's own code) |
| 3 | an input could not be read |

`compile` and `check` run the same pipeline and differ only in what they emit, so
`nano check` passing means `nano compile` will not fail on types. Splitting them
gives a pre-commit hook something fast and silent to call.

Diagnostics print as `file:line:col: error: message`, the form editors and CI
annotators already parse — the positions come from the compiler unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, TextIO

from ..compiler import (
    NanoCompileError,
    check_source,
    compile_module,
    compile_to_dict,
    required_ir_version,
)
from ..data import FeedError, load_frame, parse_date
from ..indicators.registry import INDICATORS, names as indicator_names
from ..ir.schema import SUPPORTED_IR_VERSIONS
from ..runtime.vm import run_module
from ..types.env import KIND_FEED, KIND_INPUT, KIND_LET, KIND_PARAM
from .render import FORMATS, render, summarise_run

EXIT_OK = 0
EXIT_DIAGNOSTICS = 1
EXIT_USAGE = 2
EXIT_IO = 3


@dataclass(frozen=True)
class Console:
    """Where a command writes. Injected so tests need no subprocess."""

    out: TextIO
    err: TextIO

    def say(self, message: str = "") -> None:
        print(message, file=self.out)

    def warn(self, message: str) -> None:
        print(message, file=self.err)


def _read_source(path: Path, console: Console) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        console.warn(f"error: cannot read {path}: {exc}")
        return None


def _report_compile_error(path: Path, error: NanoCompileError, console: Console) -> int:
    console.warn(f"{path}:{error.line}:{error.column}: error: {error.message}")
    return EXIT_DIAGNOSTICS


# ---------------------------------------------------------------------------
# nano check
# ---------------------------------------------------------------------------


def command_check(args: Any, console: Console) -> int:
    """Type-check one or more files. Silent on success, like a linter."""
    failed = 0
    for path in args.files:
        source = _read_source(path, console)
        if source is None:
            return EXIT_IO
        try:
            program = check_source(source)
        except NanoCompileError as error:
            _report_compile_error(path, error, console)
            failed += 1
            continue
        if args.verbose:
            console.say(
                f"{path}: ok — tier {program.tier}, "
                f"warmup {program.warmup} bar(s), "
                f"effects {', '.join(program.effects)}, "
                f"ir {required_ir_version(program)}"
            )
    if failed:
        console.warn(f"{failed} of {len(args.files)} file(s) failed")
        return EXIT_DIAGNOSTICS
    return EXIT_OK


# ---------------------------------------------------------------------------
# nano compile
# ---------------------------------------------------------------------------


def _emit_types(program, console: Console) -> None:
    """Print the resolved type of every declared name, plus warm-up."""
    console.say(f"strategy {program.strategy.name} (tier {program.tier})")
    for kind, heading in (
        (KIND_PARAM, "params"),
        (KIND_INPUT, "inputs"),
        (KIND_LET, "derived"),
        (KIND_FEED, "feed signals"),
    ):
        symbols = program.of_kind(kind)
        if not symbols:
            continue
        console.say(f"  {heading}:")
        for symbol in symbols:
            suffix = f"  (warmup {symbol.lookback})" if symbol.lookback else ""
            console.say(f"    {symbol.describe()}{suffix}")
    console.say(f"  effects: {', '.join(program.effects)}")
    console.say(f"  warmup:  {program.warmup} bar(s)")


def command_compile(args: Any, console: Console) -> int:
    """Validate a strategy and emit its execution plan."""
    source = _read_source(args.file, console)
    if source is None:
        return EXIT_IO

    try:
        if args.emit == "types":
            _emit_types(check_source(source), console)
            return EXIT_OK
        if args.emit == "plan":
            console.say(render(compile_module(source), "ascii"))
            return EXIT_OK
        document = compile_to_dict(source, ir_version=args.ir_version)
    except NanoCompileError as error:
        return _report_compile_error(args.file, error, console)
    except ValueError as error:
        # IRValidationError / IRVersionError: the program is fine, but the shape
        # that was asked for cannot hold it.
        console.warn(f"{args.file}: error: {error}")
        return EXIT_DIAGNOSTICS

    rendered = json.dumps(document, indent=2)
    if args.output is None:
        console.say(rendered)
        return EXIT_OK

    try:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    except OSError as exc:
        console.warn(f"error: cannot write {args.output}: {exc}")
        return EXIT_IO
    console.warn(
        f"{args.file} -> {args.output} "
        f"(nanoIrVersion {document['nanoIrVersion']}, {len(document['nodes'])} nodes)"
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# nano replay
# ---------------------------------------------------------------------------


def _missing_signals(module, available: Sequence[str]) -> List[str]:
    """Names the module reads that the data file does not provide."""
    have = set(available)
    needed = [i.name for i in module.inputs] + list(module.signals)
    return [name for name in needed if name not in have]


def _print_text_report(
    module, loaded, result, console: Console, *, verified: bool
) -> None:
    filtered = (
        f"  ({loaded.rows_filtered} row(s) filtered)" if loaded.rows_filtered else ""
    )
    console.say(f"strategy {module.name}  ({module.content_hash()[:23]}...)")
    console.say(f"  bars      {len(loaded.frame.timestamps)}{filtered}")
    console.say(f"  warmup    {module.warmup} declared bar(s)")
    console.say(
        "  result    "
        + summarise_run(result.intents, result.escalations, result.warmup_bars_skipped)
    )
    if verified:
        console.say("  replay    deterministic (verified over two runs)")

    if result.intents:
        console.say("")
        console.say("  intents:")
        for intent in result.intents:
            detail = intent.to_dict()
            asset = f" {detail['asset']}" if "asset" in detail else ""
            confidence = f" @{detail['confidence']}" if "confidence" in detail else ""
            console.say(
                f"    {detail['timestamp']}  {detail['intent']}{asset}{confidence}"
            )

    if result.escalations:
        console.say("")
        console.say("  escalations:")
        for escalation in result.escalations:
            console.say(
                f"    {escalation.timestamp}  -> {escalation.target}"
                f"  ({escalation.reason})"
            )


def command_replay(args: Any, console: Console) -> int:
    """Run a strategy against recorded data and report what it proposed."""
    source = _read_source(args.file, console)
    if source is None:
        return EXIT_IO

    try:
        module = compile_module(source)
    except NanoCompileError as error:
        return _report_compile_error(args.file, error, console)

    try:
        on_date = parse_date(args.date) if args.date else None
        loaded = load_frame(args.data, on_date=on_date)
    except FeedError as exc:
        console.warn(f"error: {exc}")
        return EXIT_IO

    if not loaded.frame.timestamps:
        console.warn(
            "error: no rows to replay"
            + (f" for {args.date}" if args.date else "")
            + f" ({loaded.rows_read} row(s) read, "
            f"{loaded.rows_filtered} filtered out)"
        )
        return EXIT_IO

    missing = _missing_signals(module, loaded.signal_names)
    if missing:
        console.warn(
            f"error: {args.data} does not supply {', '.join(missing)} "
            f"(it has: {', '.join(loaded.signal_names)})"
        )
        return EXIT_IO

    try:
        result = run_module(module, loaded.frame)
    except Exception as exc:  # noqa: BLE001 - reported as a diagnostic, not a crash
        console.warn(f"error: replay failed: {exc}")
        return EXIT_DIAGNOSTICS

    if args.verify:
        # Same module, same frame, twice. A divergence means something in the
        # chain is not a pure function of its inputs, which invalidates every
        # number the run produced -- so it fails rather than warns.
        again = run_module(module, loaded.frame)
        if again.to_dict() != result.to_dict():
            console.warn(
                "error: replay is not deterministic — two identical runs produced "
                "different results"
            )
            return EXIT_DIAGNOSTICS

    if args.report == "json":
        console.say(
            json.dumps(
                {
                    "strategy": module.name,
                    "moduleHash": module.content_hash(),
                    "sourceHash": module.source_hash,
                    "bars": len(loaded.frame.timestamps),
                    "rowsRead": loaded.rows_read,
                    "rowsFiltered": loaded.rows_filtered,
                    "warmupDeclared": module.warmup,
                    "replayVerified": bool(args.verify),
                    **result.to_dict(),
                },
                indent=2,
            )
        )
        return EXIT_OK

    _print_text_report(module, loaded, result, console, verified=bool(args.verify))
    return EXIT_OK


# ---------------------------------------------------------------------------
# nano visualize
# ---------------------------------------------------------------------------


def command_visualize(args: Any, console: Console) -> int:
    """Render a strategy's execution graph."""
    source = _read_source(args.file, console)
    if source is None:
        return EXIT_IO
    try:
        module = compile_module(source)
    except NanoCompileError as error:
        return _report_compile_error(args.file, error, console)

    rendered = render(module, args.format)
    if args.output is None:
        console.say(rendered)
        return EXIT_OK
    try:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    except OSError as exc:
        console.warn(f"error: cannot write {args.output}: {exc}")
        return EXIT_IO
    console.warn(f"{args.file} -> {args.output} ({args.format})")
    return EXIT_OK


# ---------------------------------------------------------------------------
# nano indicators / nano version
# ---------------------------------------------------------------------------


def command_indicators(args: Any, console: Console) -> int:
    """List the indicators a strategy may compute, or describe one."""
    if args.name:
        spec = INDICATORS.get(args.name)
        if spec is None:
            console.warn(
                f"error: unknown indicator {args.name!r} "
                "(try `nano indicators` for the full list)"
            )
            return EXIT_DIAGNOSTICS
        console.say(spec.signature_text())
        console.say(f"  {spec.doc}")
        if spec.period_indices:
            positions = ", ".join(str(i + 1) for i in spec.period_indices)
            console.say(
                f"  period argument(s) at position {positions} must be "
                "compile-time constants"
            )
        return EXIT_OK

    for name in indicator_names():
        console.say(INDICATORS[name].signature_text())
    return EXIT_OK


def command_version(args: Any, console: Console) -> int:
    """Print component versions — useful when a host reports a mismatch."""
    from .. import __version__
    from ..ir.module import COMPILER_NAME, COMPILER_VERSION

    console.say(f"nano {__version__}")
    console.say(f"  compiler        {COMPILER_NAME} {COMPILER_VERSION}")
    console.say(f"  ir versions     {', '.join(SUPPORTED_IR_VERSIONS)}")
    console.say(f"  indicators      {len(INDICATORS)}")
    return EXIT_OK


__all__ = [
    "Console",
    "EXIT_DIAGNOSTICS",
    "EXIT_IO",
    "EXIT_OK",
    "EXIT_USAGE",
    "FORMATS",
    "command_check",
    "command_compile",
    "command_indicators",
    "command_replay",
    "command_version",
    "command_visualize",
]
