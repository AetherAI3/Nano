"""`nano package check | build`: a thin adapter over ``compile_package``.

The contract is frozen in ``docs/package-cli.md``. This module adds no language
semantics: it calls the public package API, maps its rejection to the
existing exit-code table, and frames results as canonical JSON bytes.

The helpers above the command functions are public so a later
``nano package test`` reuses the same build, diagnostic and framing rules
instead of re-deriving them.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .. import __version__
from ..compiler import PackageError, compile_package
from ..compiler.package import PACKAGE_VERSION, CompiledPackage
from ..ir.module import COMPILER_VERSION
from .commands import EXIT_DIAGNOSTICS, EXIT_IO, EXIT_OK, EXIT_USAGE, Console

CHECK_SCHEMA_VERSION = "1.0.0"
CHECK_DOCUMENT_TYPE = "NanoPackageCheck"
COMPILER_NAME = "nnc-package"
IO_ERROR_CODE = "IO_ERROR"
IO_CATEGORY = "io"
PACKAGE_ERROR_CODE = "PACKAGE_ERROR"
PACKAGE_CATEGORY = "package"


@dataclass(frozen=True)
class PackageRejection:
    """The API's first rejection, as a contract diagnostic plus its exit code."""

    diagnostic: Dict[str, Any]
    exit_code: int


# -- reusable helpers --------------------------------------------------------

def canonical_bytes(value: Any) -> bytes:
    """Canonical JSON (sorted keys, compact, ASCII) followed by one LF byte."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("ascii") + b"\n"


def compiler_identity() -> Dict[str, str]:
    """The ``compiler`` object every package CLI document carries."""
    return {
        "irCompilerVersion": COMPILER_VERSION,
        "name": COMPILER_NAME,
        "packageVersion": __version__,
        "version": PACKAGE_VERSION,
    }


def _io_cause(error: BaseException) -> Optional[OSError]:
    """The OSError directly behind a package error, if the API saw one."""
    for linked in (error.__cause__, error.__context__):
        if isinstance(linked, OSError):
            return linked
    return None


def _strip_path(error: PackageError) -> str:
    prefix = f"{error.path}: "
    message = error.message
    return message[len(prefix):] if message.startswith(prefix) else message


def _redact_os_detail(message: str, cause: OSError) -> str:
    """Replace an OSError's rendering (which embeds the absolute filename).

    The API formats some I/O failures with ``str(OSError)``; the contract
    forbids absolute paths in the document, so only the OS reason is kept.
    """
    detail = str(cause)
    reason = cause.strerror or type(cause).__name__
    return message.replace(detail, reason) if detail and detail in message else message


def diagnostic_dict(error: PackageError) -> Tuple[Dict[str, Any], int]:
    """Map one ``PackageError`` to its contract diagnostic and exit code."""
    message = _strip_path(error)
    cause = _io_cause(error)
    if cause is not None:
        return {
            "category": IO_CATEGORY, "code": IO_ERROR_CODE,
            "message": _redact_os_detail(message, cause), "path": error.path,
            "severity": "error", "span": None,
        }, EXIT_IO
    position = {"column": error.column, "line": error.line}
    category = PACKAGE_CATEGORY if error.code == PACKAGE_ERROR_CODE else error.category
    return {
        "category": category, "code": error.code, "message": message,
        "path": error.path, "severity": "error",
        "span": {"end": dict(position), "start": dict(position)},
    }, EXIT_DIAGNOSTICS


def format_diagnostic(diagnostic: Dict[str, Any]) -> str:
    """The editor form ``path:line:col: error: message`` (no position for I/O)."""
    span = diagnostic["span"]
    if span is None:
        return f"{diagnostic['path']}: error: {diagnostic['message']}"
    start = span["start"]
    return f"{diagnostic['path']}:{start['line']}:{start['column']}: error: {diagnostic['message']}"


def build_package(manifest: Path, lock: Path) -> Union[CompiledPackage, PackageRejection]:
    """Build through the public API, returning the artifact or its rejection."""
    try:
        return compile_package(manifest, lock_path=lock)
    except PackageError as error:
        diagnostic, exit_code = diagnostic_dict(error)
        return PackageRejection(diagnostic, exit_code)


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a sibling temporary and ``os.replace``."""
    temporary = _write_temporary(Path(path), data)
    try:
        os.replace(temporary, path)
    except BaseException:
        _discard(temporary)
        raise


def _write_temporary(path: Path, data: bytes) -> str:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        _discard(temporary)
        raise
    return temporary


def _discard(temporary: str) -> None:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass


# -- commands ----------------------------------------------------------------

def command_package(args: argparse.Namespace, console: Console) -> int:
    """Dispatch ``nano package <action>``."""
    handlers = {"check": _command_check, "build": _command_build}
    return handlers[args.package_action](args, console)


def _check_document(result: Union[CompiledPackage, PackageRejection]) -> Dict[str, Any]:
    rejected = isinstance(result, PackageRejection)
    envelope = None if rejected else result.to_dict()
    return {
        "compiler": compiler_identity(),
        "diagnostics": [result.diagnostic] if rejected else [],
        "moduleHash": None if rejected else envelope["module"]["moduleHash"],
        "packageHash": None if rejected else envelope["packageHash"],
        "schemaVersion": CHECK_SCHEMA_VERSION,
        "success": not rejected,
        "type": CHECK_DOCUMENT_TYPE,
    }


def _command_check(args: argparse.Namespace, console: Console) -> int:
    result = build_package(args.manifest, args.lock)
    if args.json_output:
        console.emit(canonical_bytes(_check_document(result)))
        return result.exit_code if isinstance(result, PackageRejection) else EXIT_OK
    if isinstance(result, PackageRejection):
        console.warn(format_diagnostic(result.diagnostic))
        return result.exit_code
    if args.verbose:
        document = _check_document(result)
        console.say(f"ok packageHash={document['packageHash']} moduleHash={document['moduleHash']}")
    return EXIT_OK


def _identity(path: Path) -> str:
    """A comparable spelling of where ``path`` really points."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _requested_outputs(args: argparse.Namespace) -> List[Path]:
    return [p for p in (args.output, args.module_output) if p is not None]


def _refuse_overlap(outputs: Sequence[Path], inputs: Iterable[Path], console: Console) -> bool:
    """Report and return True when an output names an input or another output."""
    taken = {_identity(p) for p in inputs}
    seen = set()
    for output in outputs:
        identity = _identity(output)
        if identity in taken:
            console.warn(f"error: output {output} names a package input; refusing to overwrite it")
            return True
        if identity in seen:
            console.warn("error: -o and --module-output must name different files")
            return True
        seen.add(identity)
    return False


def _manifest_sources(manifest: Path, compiled: CompiledPackage) -> List[Path]:
    root = Path(_identity(manifest)).parent
    names = [compiled.manifest["entry"], *compiled.manifest["modules"].values()]
    return [root / name for name in names]


def _previous_bytes(path: Path) -> Optional[bytes]:
    """What a destination held before this build, refusing a directory early."""
    if path.is_dir():
        raise IsADirectoryError(21, "Is a directory", str(path))
    return path.read_bytes() if path.exists() else None


def _roll_back(committed: Sequence[Tuple[Path, Optional[bytes]]]) -> None:
    """Restore destinations already replaced when a later one fails."""
    for path, previous in reversed(committed):
        if previous is None:
            _discard(str(path))
        else:
            write_bytes_atomic(path, previous)


def _write_outputs(planned: Sequence[Tuple[Path, bytes]], console: Console) -> int:
    """Stage every artifact, then commit all or restore what was there.

    Every destination is checked and staged before any is replaced; if a later
    replace still fails, earlier destinations get their previous bytes back
    (or are removed), so a failed build leaves no partial artifact set.
    """
    staged: List[Tuple[str, Path, Optional[bytes]]] = []
    committed: List[Tuple[Path, Optional[bytes]]] = []
    current: Optional[Path] = None
    try:
        for current, data in planned:
            previous = _previous_bytes(current)
            staged.append((_write_temporary(current, data), current, previous))
        for temporary, current, previous in staged:
            os.replace(temporary, current)
            committed.append((current, previous))
    except BaseException as error:
        for temporary, _, _ in staged:
            _discard(temporary)
        _roll_back(committed)
        if not isinstance(error, OSError):
            raise
        console.warn(f"error: cannot write {current}: {error.strerror or error}")
        return EXIT_IO
    return EXIT_OK


def _command_build(args: argparse.Namespace, console: Console) -> int:
    outputs = _requested_outputs(args)
    if _refuse_overlap(outputs, (args.manifest, args.lock), console):
        return EXIT_USAGE
    result = build_package(args.manifest, args.lock)
    if isinstance(result, PackageRejection):
        console.warn(format_diagnostic(result.diagnostic))
        return result.exit_code
    if _refuse_overlap(outputs, (args.manifest, args.lock, *_manifest_sources(args.manifest, result)), console):
        return EXIT_USAGE
    envelope = canonical_bytes(result.to_dict())
    planned = [(args.output, envelope)] if args.output is not None else []
    if args.module_output is not None:
        planned.append((args.module_output, canonical_bytes(result.module.to_dict())))
    status = _write_outputs(planned, console)
    if status == EXIT_OK and args.output is None:
        console.emit(envelope)
    return status


def add_package_parser(subcommands: Any) -> None:
    """Register ``nano package check | build`` on the top-level subparsers."""
    package = subcommands.add_parser(
        "package", help="check or build an explicit, hash-pinned local package"
    )
    actions = package.add_subparsers(dest="package_action", metavar="ACTION", required=True)
    check = actions.add_parser("check", help="build in memory; silent on success")
    _add_inputs(check)
    check.add_argument("--json", dest="json_output", action="store_true",
                       help="emit one canonical NanoPackageCheck document")
    check.add_argument("-v", "--verbose", action="store_true",
                       help="print packageHash and moduleHash on success")
    check.set_defaults(handler=command_package)
    build = actions.add_parser("build", help="emit the canonical NanoPackage envelope")
    _add_inputs(build)
    build.add_argument("-o", "--output", type=Path, metavar="PATH",
                       help="write the envelope here instead of stdout")
    build.add_argument("--module-output", type=Path, metavar="PATH",
                       help="also write the executable module (canonical JSON)")
    build.set_defaults(handler=command_package)


def _add_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, required=True, metavar="PATH",
                        help="package manifest (no default, no discovery)")
    parser.add_argument("--lock", type=Path, required=True, metavar="PATH",
                        help="lock pinning every manifest source by SHA-256")
