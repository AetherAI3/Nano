"""The `nano` command-line entry point.

Argument parsing lives here and the work lives in ``commands.py``, so a command
can be called directly in a test with a captured ``Console`` instead of a
subprocess.

The command set is deliberately small and answers the questions a developer
actually asks while writing a strategy:

    nano check strategy.nano                 does this compile?
    nano compile strategy.nano -o ir.json    what is the artifact?
    nano compile strategy.nano --emit types  what did the types resolve to?
    nano replay strategy.nano --data bars.csv --date 2026-01-15
                                             what would it have proposed?
    nano visualize strategy.nano             what does the graph look like?
    nano indicators [NAME]                   what can I compute, and how long is
                                             its warm-up?

Nothing here acts on a market. `replay` reads recorded data and prints proposed
intents; there is no `nano trade`, and there will not be one, because emitting an
intent and acting on it are different jobs owned by different systems.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from ..ir.schema import SUPPORTED_IR_VERSIONS
from .commands import (
    EXIT_OK,
    Console,
    command_check,
    command_compile,
    command_indicators,
    command_replay,
    command_version,
    command_visualize,
)
from .render import FORMATS

_EPILOG = """\
exit codes:
  0  success
  1  the source or the run was rejected (diagnostics printed to stderr)
  2  the command was used wrongly
  3  an input could not be read

examples:
  nano check src/*.nano
  nano compile strategy.nano -o strategy_ir.json
  nano compile strategy.nano --emit types
  nano replay strategy.nano --data bars.csv --date 2026-01-15 --verify
  nano visualize strategy.nano --format mermaid
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nano",
        description=(
            "Compile, check, replay, and visualise Nano strategies. "
            "Nano proposes intents; it never places an order."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND")

    check = subcommands.add_parser(
        "check", help="type-check strategies; silent on success"
    )
    check.add_argument("files", nargs="+", type=Path, metavar="FILE")
    check.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="report tier, warm-up, and effects for each file",
    )
    check.set_defaults(handler=command_check)

    compile_command = subcommands.add_parser(
        "compile", help="validate a strategy and emit its execution plan"
    )
    compile_command.add_argument("file", type=Path, metavar="FILE")
    compile_command.add_argument(
        "-o", "--output", type=Path, help="write IR here instead of stdout"
    )
    compile_command.add_argument(
        "--emit",
        choices=("ir", "types", "plan"),
        default="ir",
        help="ir: the IR document (default); types: resolved types and warm-up; "
        "plan: the execution graph as a tree",
    )
    compile_command.add_argument(
        "--ir-version",
        choices=SUPPORTED_IR_VERSIONS,
        default=None,
        help="force an IR version. By default the compiler emits the lowest version "
        "that can express the program, so v0.1.0-era strategies stay byte-identical",
    )
    compile_command.set_defaults(handler=command_compile)

    replay = subcommands.add_parser(
        "replay", help="run a strategy against recorded data"
    )
    replay.add_argument("file", type=Path, metavar="FILE")
    replay.add_argument(
        "--data",
        type=Path,
        required=True,
        metavar="PATH",
        help="recorded bars as .csv or .json",
    )
    replay.add_argument(
        "--date", metavar="YYYY-MM-DD", help="replay only this UTC calendar date"
    )
    replay.add_argument(
        "--report",
        choices=("text", "json"),
        default="text",
        help="text for a human summary (default), json for the full audit log",
    )
    replay.add_argument(
        "--verify",
        action="store_true",
        help="run twice and fail if the two runs differ",
    )
    replay.set_defaults(handler=command_replay)

    visualize = subcommands.add_parser(
        "visualize", help="render the strategy's execution graph"
    )
    visualize.add_argument("file", type=Path, metavar="FILE")
    visualize.add_argument(
        "-f",
        "--format",
        choices=FORMATS,
        default="ascii",
        help="ascii (default), mermaid, dot, or json",
    )
    visualize.add_argument(
        "-o", "--output", type=Path, help="write here instead of stdout"
    )
    visualize.set_defaults(handler=command_visualize)

    indicators = subcommands.add_parser(
        "indicators", help="list computable indicators, or describe one"
    )
    indicators.add_argument("name", nargs="?", metavar="NAME")
    indicators.set_defaults(handler=command_indicators)

    version = subcommands.add_parser("version", help="print component versions")
    version.set_defaults(handler=command_version)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI. Returns an exit code rather than calling sys.exit."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if getattr(args, "handler", None) is None:
        # No subcommand: print help and succeed. A bare `nano` is someone finding
        # their way around, not an error.
        parser.print_help()
        return EXIT_OK

    console = Console(out=sys.stdout, err=sys.stderr)
    return args.handler(args, console)


def run(argv: Optional[List[str]] = None) -> None:  # pragma: no cover - thin shim
    """Entry point for `python -m nano.cli`."""
    raise SystemExit(main(argv))


if __name__ == "__main__":  # pragma: no cover
    run()
