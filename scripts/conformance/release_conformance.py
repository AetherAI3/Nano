#!/usr/bin/env python3
"""Offline release conformance from exact source and wheels (issue #88).

    python scripts/conformance/release_conformance.py manifest --out manifest.json
    python scripts/conformance/release_conformance.py run --out-dir DIR [--manifest manifest.json]
    python scripts/conformance/release_conformance.py verify DIR/evidence.json

See docs/release-conformance.md. Stdlib only; no network, credentials or
provider calls. Exit codes: 0 gate PASS, 1 gate FAIL/INCOMPLETE or verify
problems, 2 usage error, 3 the harness could not start.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from rc_common import PASS, HarnessError, canonical_bytes  # noqa: E402
from rc_harness import Options, prepare_manifest, run, verify_evidence  # noqa: E402
from rc_manifest import Checkout  # noqa: E402

EXIT_OK, EXIT_GATE, EXIT_USAGE, EXIT_HARNESS = 0, 1, 2, 3
DEFAULT_TIMEOUT_S = 3600
MAX_WINDOWS_OUT_DIR = 80
CHECKOUT_ENV = {"atlas": "NANO_QSEARCH_ATLAS_PATH", "predator": "NANO_QSEARCH_PREDATOR_PATH"}


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, default=HERE.parents[1], help="repository checkout under test")
    parser.add_argument("--python", default=sys.executable, help="test interpreter")
    parser.add_argument("--node", action="append", default=None,
                        help="Node executable; repeat for a second runtime (default: node on PATH)")
    parser.add_argument("--no-node", action="store_true", help="declare no Node runtime")
    parser.add_argument("--wheel-python", help="interpreter with setuptools for the wheel build")
    parser.add_argument("--suite", action="append", dest="suites", help="select a suite id (repeatable)")
    parser.add_argument("--merge-tree", help="proposed merge tree id that must equal the tested tree")
    parser.add_argument("--atlas-checkout", type=Path, help="caller-supplied ATLAS directory (mode 3)")
    parser.add_argument("--predator-checkout", type=Path, help="caller-supplied Predator checkout (mode 3)")
    parser.add_argument("--npm-cache", type=Path,
                        help="npm cache holding the TypeScript client's locked devDependencies (offline install)")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release_conformance", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    manifest = sub.add_parser("manifest", help="pin the current source and environment")
    _common(manifest)
    manifest.add_argument("--out", type=Path, required=True)
    runner = sub.add_parser("run", help="execute the selected suites and write evidence")
    _common(runner)
    runner.add_argument("--out-dir", type=Path, required=True)
    runner.add_argument("--manifest", type=Path, help="previously written input manifest to enforce")
    runner.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="per-suite timeout in seconds")
    runner.add_argument("--gate", choices=("release", "core"), default="release")
    verify = sub.add_parser("verify", help="re-check a stored evidence document")
    verify.add_argument("evidence", type=Path)
    return parser


def _nodes(args: argparse.Namespace) -> List[str]:
    if args.no_node:
        return []
    if args.node:
        return [shutil.which(node) or node for node in args.node]
    found = shutil.which("node")
    return [found] if found else []


def _checkouts(args: argparse.Namespace) -> List[Checkout]:
    out = []
    for role, path in (("atlas", args.atlas_checkout), ("predator", args.predator_checkout)):
        if path is not None:
            out.append(Checkout(role, path.resolve(), CHECKOUT_ENV[role]))
    return out


def _options(args: argparse.Namespace, out: Path, manifest: Optional[dict]) -> Options:
    return Options(repo=args.repo.resolve(), out=out, python=args.python, manifest=manifest,
                   select=args.suites, nodes=_nodes(args), wheel_python=args.wheel_python,
                   checkouts=_checkouts(args), merge_tree=args.merge_tree,
                   npm_cache=args.npm_cache.resolve() if args.npm_cache else None,
                   timeout_s=getattr(args, "timeout", DEFAULT_TIMEOUT_S))


def _cmd_manifest(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="nano-rc-manifest-") as scratch:
        manifest = prepare_manifest(_options(args, Path(scratch) / "out", None))
    args.out.write_bytes(canonical_bytes(manifest))
    sys.stdout.write(f"wrote {args.out}\n")
    return EXIT_OK


def _check_out_dir_length(out: Path) -> None:
    """Some tests copy the tree into ``OUT/tmp/...`` and run git there; keep Windows under MAX_PATH."""
    resolved = str(out.resolve())
    if os.name == "nt" and len(resolved) > MAX_WINDOWS_OUT_DIR:
        raise HarnessError(f"--out-dir is {len(resolved)} characters; on Windows use at most "
                           f"{MAX_WINDOWS_OUT_DIR} so nested test checkouts stay under MAX_PATH")


def _cmd_run(args: argparse.Namespace) -> int:
    _check_out_dir_length(args.out_dir)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else None
    doc = run(_options(args, args.out_dir, manifest))
    gate = doc["canonical"]["gate"]
    sys.stdout.write(f"core={gate['core']} release={gate['release']} "
                     f"canonical_sha256={doc['canonical_sha256']}\n")
    for suite in doc["canonical"]["suites"]:
        sys.stdout.write(f"  {suite['status']:<12} {suite['id']}\n")
    return EXIT_OK if gate[args.gate] == PASS else EXIT_GATE


def _cmd_verify(args: argparse.Namespace) -> int:
    problems = verify_evidence(json.loads(args.evidence.read_text(encoding="utf-8")))
    for problem in problems:
        sys.stderr.write(f"verify: {problem}\n")
    if not problems:
        sys.stdout.write("evidence verified\n")
    return EXIT_GATE if problems else EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    handlers = {"manifest": _cmd_manifest, "run": _cmd_run, "verify": _cmd_verify}
    try:
        return handlers[args.command](args)
    except HarnessError as exc:
        sys.stderr.write(f"release_conformance: {exc}\n")
        return EXIT_HARNESS


if __name__ == "__main__":
    raise SystemExit(main())
