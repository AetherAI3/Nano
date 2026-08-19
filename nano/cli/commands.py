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
| 130 | interrupted with Ctrl-C (the shell convention for SIGINT) |

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
    IRVersionError,
    NanoCompileError,
    check_source,
    compile_module,
    compile_to_dict,
    required_ir_version,
)
from ..data import FeedError, load_frame, parse_date
from ..indicators.registry import INDICATORS, names as indicator_names
from ..ir.schema import SUPPORTED_IR_VERSIONS, IRValidationError
from ..runtime.interpreter import RuntimeError_
from ..runtime.receipt import build_receipt, canonical_bytes, differences
from ..runtime.risk import RiskGate
from ..runtime.vm import run_module
from ..types.env import KIND_FEED, KIND_INPUT, KIND_LET, KIND_PARAM
from .render import render, summarise_run

EXIT_OK = 0
EXIT_DIAGNOSTICS = 1
EXIT_USAGE = 2
EXIT_IO = 3
EXIT_INTERRUPTED = 130


@dataclass(frozen=True)
class Console:
    """Where a command writes. Injected so tests need no subprocess."""

    out: TextIO
    err: TextIO

    def say(self, message: str = "") -> None:
        print(message, file=self.out)

    def warn(self, message: str) -> None:
        print(message, file=self.err)

    def emit(self, payload: bytes) -> None:
        """Write exactly `payload` — no encoding, no newline translation.

        `say` goes through a text wrapper, which rewrites every ``\\n`` to
        ``os.linesep``. On Windows that turns the receipt's single framing byte
        into ``\\r\\n``, so a consumer following ``docs/receipts.md`` §1 and
        digesting everything but the last byte gets a mismatch. A receipt is
        bytes; it has to leave through a byte sink.

        Falls back to the text sink when there is no underlying buffer, which is
        the case for an in-memory stream a test injected.
        """
        buffer = getattr(self.out, "buffer", None)
        if buffer is None:
            self.out.write(payload.decode("ascii"))
            return
        self.out.flush()  # keep any buffered text output ahead of these bytes
        buffer.write(payload)
        buffer.flush()


def _read_source(path: Path, console: Console) -> Optional[str]:
    """Read `.nano` source, or report why it could not be read.

    `UnicodeDecodeError` is caught explicitly because it is a `ValueError`, not an
    `OSError` — a file of binary junk would otherwise escape as a traceback while a
    missing file produced a clean diagnostic.

    `utf-8-sig` and not `utf-8`: several Windows editors write a UTF-8 BOM by
    default, and the lexer reports the leading U+FEFF as `Unexpected character`
    at 1:1 — a diagnostic that points at a perfectly good strategy and names
    something invisible. Stripping the mark on read is not a grammar change; a
    BOM carries no meaning inside a program, and every other byte still has to
    be valid UTF-8.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        console.warn(f"error: cannot read {path}: {exc}")
        return None
    except UnicodeDecodeError:
        console.warn(
            f"error: cannot read {path}: not valid UTF-8 "
            "(Nano source must be UTF-8 encoded text)"
        )
        return None


def _report_compile_error(path: Path, error: NanoCompileError, console: Console) -> int:
    console.warn(f"{path}:{error.line}:{error.column}: error: {error.message}")
    return EXIT_DIAGNOSTICS


class _Rejected(Exception):
    """A command's input was rejected. Carries the exit code to return."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _compile(path: Path, source: str, console: Console):
    """Compile to a module, reporting every rejection as a diagnostic.

    ``compile_module`` round-trips through the IR loader, so it can raise
    ``IRValidationError`` as well as ``NanoCompileError``. Catching only the
    latter — which every command used to do — turned a loader rejection into a
    traceback in `replay` and `visualize` while `compile` reported it cleanly.
    """
    try:
        return compile_module(source)
    except NanoCompileError as error:
        raise _Rejected(_report_compile_error(path, error, console)) from error
    except IRValidationError as error:
        # No source position: the IR contract was violated after lowering, so the
        # locator is the node id the loader names, not a line.
        console.warn(f"{path}: error: {error}")
        raise _Rejected(EXIT_DIAGNOSTICS) from error


# ---------------------------------------------------------------------------
# nano check
# ---------------------------------------------------------------------------


def command_check(args: Any, console: Console) -> int:
    """Type-check one or more files. Silent on success, like a linter.

    Every file is attempted even after one fails. Returning at the first
    unreadable file made the exit code depend on argument order — `check bad.nano
    missing.nano` reported an I/O error while `check bad.nano ok.nano` reported a
    diagnostic — and hid the remaining files from anyone running this over a
    directory.
    """
    failed = 0
    unreadable = 0

    for path in args.files:
        source = _read_source(path, console)
        if source is None:
            unreadable += 1
            continue
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

    if failed or unreadable:
        console.warn(
            f"{failed + unreadable} of {len(args.files)} file(s) failed"
            + (f" ({unreadable} unreadable)" if unreadable else "")
        )
        # A rejected program outranks an unreadable file: the diagnostic is the
        # actionable result, and reporting I/O would bury it.
        return EXIT_DIAGNOSTICS if failed else EXIT_IO
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


def _write_or_print(
    text: str, args: Any, console: Console, *, note: str
) -> int:
    """Send `text` to `--output` if given, else to stdout.

    Shared by every emit mode. `--emit types` and `--emit plan` used to return
    before reaching the output block, so `-o` was accepted, silently ignored, and
    the command exited 0 having written nothing — the worst kind of failure,
    because a script would believe it had a file.
    """
    if args.output is None:
        console.say(text)
        return EXIT_OK
    try:
        args.output.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        console.warn(f"error: cannot write {args.output}: {exc}")
        return EXIT_IO
    console.warn(f"{args.file} -> {args.output} ({note})")
    return EXIT_OK


def command_compile(args: Any, console: Console) -> int:
    """Validate a strategy and emit its execution plan."""
    source = _read_source(args.file, console)
    if source is None:
        return EXIT_IO

    try:
        if args.emit == "types":
            program = check_source(source)
            lines: List[str] = []
            _emit_types(program, Console(out=_Collector(lines), err=console.err))
            return _write_or_print(
                "\n".join(lines), args, console, note="types"
            )
        if args.emit == "plan":
            module = _compile(args.file, source, console)
            return _write_or_print(
                render(module, "ascii"), args, console, note="plan"
            )
        document = compile_to_dict(source, ir_version=args.ir_version)
    except _Rejected as rejected:
        return rejected.code
    except NanoCompileError as error:
        return _report_compile_error(args.file, error, console)
    except IRVersionError as error:
        # The program is fine; the version the caller asked for cannot hold it.
        # That is a wrong argument, not a bad strategy.
        console.warn(f"error: {error}")
        return EXIT_USAGE
    except IRValidationError as error:
        console.warn(f"{args.file}: error: {error}")
        return EXIT_DIAGNOSTICS

    return _write_or_print(
        json.dumps(document, indent=2),
        args,
        console,
        note=(
            f"nanoIrVersion {document['nanoIrVersion']}, "
            f"{len(document['nodes'])} nodes"
        ),
    )


class _Collector:
    """A minimal write sink, so `--emit types` can be captured for `-o`."""

    def __init__(self, lines: List[str]) -> None:
        self._lines = lines

    def write(self, text: str) -> int:
        if text != "\n":
            self._lines.append(text.rstrip("\n"))
        return len(text)

    def flush(self) -> None:
        return None


# ---------------------------------------------------------------------------
# nano replay
# ---------------------------------------------------------------------------


def _missing_signals(module, available: Sequence[str]) -> List[str]:
    """Names the module reads that the data file does not provide.

    Risk measurements count. A `risk { max_drawdown 0.05 }` whose `risk.drawdown`
    column is absent does not run unguarded — it fails closed and withholds every
    actuating intent — so a replay against the wrong file would otherwise report
    zero proposals, exit 0, and look like a strategy that simply found no setup.
    The gate is asked what it needs before any data exists, which is why it takes
    the module alone.
    """
    have = set(available)
    needed = (
        [i.name for i in module.inputs]
        + list(module.signals)
        + list(RiskGate.for_module(module).required_measurements())
    )
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

    withheld = [e for e in result.log if e.event == "intent.suppressed"]
    if withheld:
        # The text report otherwise shows only what survived, so a run that was
        # gated reads exactly like a run that found nothing. The log has the full
        # account; this line is what makes a reader go looking for it.
        console.say(
            f"  risk      {len(withheld)} intent(s) withheld by risk limits "
            "(--report json for the log)"
        )

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
    # `--date` is parsed before anything expensive: a typo should not cost a whole
    # compile, and a malformed date is a usage error rather than an I/O one.
    try:
        on_date = parse_date(args.date) if args.date else None
    except FeedError as exc:
        console.warn(f"error: {exc}")
        return EXIT_USAGE

    source = _read_source(args.file, console)
    if source is None:
        return EXIT_IO

    try:
        module = _compile(args.file, source, console)
    except _Rejected as rejected:
        return rejected.code

    try:
        loaded = load_frame(args.data, on_date=on_date)
    except FeedError as exc:
        console.warn(f"error: {exc}")
        return EXIT_IO

    if not loaded.frame.timestamps:
        # The file read fine; it just holds nothing for this date. That is a
        # result, not a read failure.
        console.warn(
            "error: no rows to replay"
            + (f" for {args.date}" if args.date else "")
            + f" ({loaded.rows_read} row(s) read, "
            f"{loaded.rows_filtered} filtered out)"
        )
        return EXIT_DIAGNOSTICS

    missing = _missing_signals(module, loaded.signal_names)
    if missing:
        # A program/data mismatch: both inputs were readable and one does not fit
        # the other.
        console.warn(
            f"error: {args.data} does not supply {', '.join(missing)} "
            f"(it has: {', '.join(loaded.signal_names)})"
        )
        return EXIT_DIAGNOSTICS

    try:
        result = run_module(module, loaded.frame)
        receipt = build_receipt(module, loaded.frame, result)
        if args.verify:
            # Same module, same frame, twice. A divergence means something in the
            # chain is not a pure function of its inputs, which invalidates every
            # number the run produced -- so it fails rather than warns. Inside the
            # same guard as the first run: a fault on the verify pass is a fault.
            #
            # Compared as canonical bytes rather than as dictionaries: Python
            # holds `True == 1` and `0.0 == -0.0`, so a dictionary comparison
            # passed runs whose serialised form differed.
            again = build_receipt(module, loaded.frame, run_module(module, loaded.frame))
            if canonical_bytes(again) != canonical_bytes(receipt):
                console.warn(
                    "error: replay is not deterministic — two identical runs "
                    "produced different results at "
                    + ", ".join(differences(receipt, again))
                )
                return EXIT_DIAGNOSTICS
    except (RuntimeError_, IRValidationError) as exc:
        console.warn(f"error: replay failed: {exc}")
        return EXIT_DIAGNOSTICS

    if args.report == "receipt":
        # The canonical, versioned artifact -- see docs/receipts.md. Written as
        # bytes through `emit`, not `say`: exactly the canonical bytes plus the
        # one LF that frames them, on every platform. One line, so a stream of
        # runs is valid JSON Lines.
        console.emit(canonical_bytes(receipt) + b"\n")
        return EXIT_OK

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
        module = _compile(args.file, source, console)
    except _Rejected as rejected:
        return rejected.code
    return _write_or_print(
        render(module, args.format), args, console, note=args.format
    )


# ---------------------------------------------------------------------------
# nano indicators / nano version
# ---------------------------------------------------------------------------


def command_indicators(args: Any, console: Console) -> int:
    """List the indicators a strategy may compute, or describe one."""
    # `is not None`, not truthiness: `nano indicators ""` asked about an indicator
    # and should be told there isn't one, not silently handed the whole list.
    if args.name is not None:
        spec = INDICATORS.get(args.name)
        if spec is None:
            console.warn(
                f"error: unknown indicator {args.name!r} "
                "(try `nano indicators` for the full list)"
            )
            return EXIT_USAGE
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
    "EXIT_INTERRUPTED",
    "EXIT_IO",
    "EXIT_OK",
    "EXIT_USAGE",
    "command_check",
    "command_compile",
    "command_indicators",
    "command_replay",
    "command_version",
    "command_visualize",
]
