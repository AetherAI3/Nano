#!/usr/bin/env python3
"""Run Nano's deterministic compiler/IR adversarial campaign.

The parent process restarts the same generated corpus under several
``PYTHONHASHSEED`` values.  Optional git refs are tested from read-only archives
overlaid with this harness, so an active PR branch is never checked out or
edited.  A loopstate JSON document is written even when no defects are found.
"""

from __future__ import annotations

import argparse
import io
import inspect
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "_loopstate" / "g5-compiler-fuzz.json"
DEFAULT_HASH_SEEDS = ("0", "1", "7", "42", "4294967295")

# Direct script execution puts ``scripts/`` rather than the repository root at
# sys.path[0]. Keep the worker pointed at the target archive's Nano package.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _git(*args: str, cwd: Path = ROOT, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _parse_hash_seeds(raw: str) -> tuple[str, ...]:
    seeds = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not seeds:
        raise argparse.ArgumentTypeError("at least one PYTHONHASHSEED is required")
    for seed in seeds:
        if not seed.isdigit() or not 0 <= int(seed) <= 4_294_967_295:
            raise argparse.ArgumentTypeError(
                f"invalid PYTHONHASHSEED {seed!r}; expected 0..4294967295"
            )
    return seeds


def _worker(seed: int, cases: int) -> int:
    from nano.fuzzing import run_campaign

    result = run_campaign(seed=seed, cases=cases)
    payload = {
        "pythonHashSeed": os.environ.get("PYTHONHASHSEED"),
        "campaign": result.to_dict(),
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 1 if result.defects else 0


def _safe_extract(archive_bytes: bytes, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if destination_resolved not in (target, *target.parents):
                raise RuntimeError(f"git archive contains unsafe path {member.name!r}")
        kwargs = (
            {"filter": "data"}
            if "filter" in inspect.signature(archive.extractall).parameters
            else {}
        )
        archive.extractall(destination, **kwargs)


@contextmanager
def _target_checkout(ref: str) -> Iterator[tuple[Path, Path, str]]:
    """Yield (root, script, sha) without ever checking out or editing ``ref``."""
    sha = _git("rev-parse", f"{ref}^{{commit}}")
    if ref in ("HEAD", "."):
        yield ROOT, Path(__file__).resolve(), sha
        return

    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", ref],
        check=True,
        capture_output=True,
    ).stdout
    with tempfile.TemporaryDirectory(prefix="nano-g5-ref-") as temporary:
        target_root = Path(temporary) / "Nano"
        target_root.mkdir()
        _safe_extract(archive, target_root)

        # Overlay only G5-owned harness paths. The archived branch remains a
        # read-only source of production code and receives no git/worktree edit.
        shutil.copytree(
            ROOT / "nano" / "fuzzing",
            target_root / "nano" / "fuzzing",
            dirs_exist_ok=True,
        )
        (target_root / "scripts").mkdir(exist_ok=True)
        target_script = target_root / "scripts" / "compiler_fuzz.py"
        shutil.copy2(Path(__file__).resolve(), target_script)
        yield target_root, target_script, sha


def _run_seed(
    *, target_root: Path, script: Path, seed: int, cases: int, hash_seed: str
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = hash_seed
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--worker",
            "--seed",
            str(seed),
            "--cases",
            str(cases),
        ],
        cwd=target_root,
        env=environment,
        capture_output=True,
        text=True,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(
            f"worker produced no JSON (exit {completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"worker emitted malformed JSON: {lines[-1]!r}; stderr={completed.stderr!r}"
        ) from error
    payload["exitCode"] = completed.returncode
    if completed.stderr.strip():
        payload["stderr"] = completed.stderr.strip()
    return payload


def _cross_seed_defect(
    *, ref: str, seed: int, cases: int, workers: Sequence[dict[str, Any]]
) -> dict[str, str]:
    observed = {
        worker["pythonHashSeed"]: {
            "corpusDigest": worker["campaign"]["corpusDigest"],
            "semanticDigest": worker["campaign"]["semanticDigest"],
        }
        for worker in workers
    }
    return {
        "id": "G5-cross-seed-" + ref.replace("/", "-").replace("\\", "-"),
        "property": "serialization-hash-seed-independent",
        "severity": "high",
        "owning_subsystem": "compiler.codegen / ir.serialization",
        "case_id": f"ref={ref}",
        "minimal_reproducer": (
            f"{sys.executable} scripts/compiler_fuzz.py --seed {seed} "
            f"--cases {cases} --hash-seeds "
            + ",".join(worker["pythonHashSeed"] for worker in workers)
        ),
        "observed": json.dumps(observed, sort_keys=True),
        "suggested_minimal_fix": (
            "Replace set/hash-map iteration in the first divergent serializer or "
            "lowering pass with a canonical order."
        ),
    }


def _target_result(
    *, ref: str, seed: int, cases: int, hash_seeds: Sequence[str]
) -> dict[str, Any]:
    with _target_checkout(ref) as (target_root, script, sha):
        workers = [
            _run_seed(
                target_root=target_root,
                script=script,
                seed=seed,
                cases=cases,
                hash_seed=hash_seed,
            )
            for hash_seed in hash_seeds
        ]

    pairs = {
        (
            worker["campaign"]["corpusDigest"],
            worker["campaign"]["semanticDigest"],
        )
        for worker in workers
    }
    defects: dict[str, dict[str, Any]] = {}
    for worker in workers:
        for defect in worker["campaign"]["defects"]:
            defects.setdefault(defect["id"], defect)
    if len(pairs) != 1:
        cross_seed = _cross_seed_defect(
            ref=ref, seed=seed, cases=cases, workers=workers
        )
        defects[cross_seed["id"]] = cross_seed

    return {
        "ref": ref,
        "resolvedSha": sha,
        "stableAcrossHashSeeds": len(pairs) == 1,
        "workers": workers,
        "defects": list(defects.values()),
    }


def _repository_state() -> dict[str, Any]:
    return {
        "branch": _git("branch", "--show-current"),
        "head": _git("rev-parse", "HEAD"),
        "mainMergeBase": _git("merge-base", "HEAD", "origin/main"),
        "status": _git(
            "status",
            "--short",
            "--",
            "nano/fuzzing",
            "scripts/compiler_fuzz.py",
            "tests/test_compiler_fuzz.py",
            "_loopstate/g5-compiler-fuzz.json",
        ).splitlines(),
    }


def _parent(args: argparse.Namespace) -> int:
    targets: list[dict[str, Any]] = []
    orchestration_defects: list[dict[str, str]] = []
    for ref in args.refs:
        try:
            targets.append(
                _target_result(
                    ref=ref,
                    seed=args.seed,
                    cases=args.cases,
                    hash_seeds=args.hash_seeds,
                )
            )
        except Exception as error:
            orchestration_defects.append(
                {
                    "id": "G5-target-" + ref.replace("/", "-").replace("\\", "-"),
                    "property": "target-campaign-completes",
                    "severity": "medium",
                    "owning_subsystem": "fuzz.harness",
                    "case_id": f"ref={ref}",
                    "minimal_reproducer": (
                        f"{sys.executable} scripts/compiler_fuzz.py --refs {ref}"
                    ),
                    "observed": f"{type(error).__name__}: {error}",
                    "suggested_minimal_fix": (
                        "Resolve the target ref or the smallest harness compatibility "
                        "break, then rerun without changing the target branch."
                    ),
                }
            )

    defects_by_id: dict[str, dict[str, Any]] = {}
    for defect in [
        item for target in targets for item in target["defects"]
    ] + orchestration_defects:
        defects_by_id.setdefault(defect["id"], defect)
    defects = list(defects_by_id.values())
    worker_count = sum(len(target["workers"]) for target in targets)
    loopstate = {
        "schemaVersion": 2,
        "loop": "NANO-G5-COMPILER-IR-ADVERSARY",
        "status": "defects-found" if defects else "pass",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "repository": _repository_state(),
        "configuration": {
            "generatorSeed": args.seed,
            "validCases": args.cases,
            "oneMutationInvalidCases": args.cases,
            "semanticEquivalentCases": max(4, args.cases // 3),
            "pythonHashSeeds": list(args.hash_seeds),
            "refs": list(args.refs),
        },
        "summary": {
            "requestedTargets": len(args.refs),
            "targets": len(targets),
            "workers": worker_count,
            "defects": len(defects),
        },
        "coverage": (
            targets[0]["workers"][0]["campaign"]["coverage"]
            if targets and targets[0]["workers"]
            else {}
        ),
        "targets": targets,
        "defects": defects,
        "coordination": {
            "lane": "G5",
            "ownedPaths": [
                "nano/fuzzing/**",
                "scripts/compiler_fuzz.py",
                "tests/test_compiler_fuzz.py",
                "_loopstate/g5-compiler-fuzz.json",
            ],
            "productionFixPolicy": (
                "Minimized defects receive separate narrow follow-up commits/PRs."
            ),
            "mergeConstraint": (
                "Source/IR acceptance parity 1.0.7 has landed. Merge entry remains "
                "frozen behind receipt canonical limits 1.0.8; after they land, "
                "rebase before provisional G5 1.0.9. No version, push, PR, or merge "
                "before G0 review."
            ),
            "currentBlocker": (
                "Receipt 641-digit and 65-container fail-open cases are minimized "
                "in the G5 ledger and expected until the separately owned 640-digit "
                "/ 64-container canonical-limit repair lands."
            ),
        },
    }

    output: Path = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(loopstate, indent=2) + "\n", encoding="utf-8", newline="\n"
    )

    print(
        f"G5 {loopstate['status']}: {len(targets)} target(s), "
        f"{worker_count} worker(s), {len(defects)} defect(s)"
    )
    print(f"loopstate: {output}")
    for target in targets:
        print(
            f"  {target['ref']}@{target['resolvedSha'][:12]}: "
            f"stable={target['stableAcrossHashSeeds']} "
            f"defects={len(target['defects'])}"
        )
    return 1 if defects else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--seed", type=int, default=20260819)
    parser.add_argument("--cases", type=int, default=128)
    parser.add_argument(
        "--hash-seeds",
        type=_parse_hash_seeds,
        default=DEFAULT_HASH_SEEDS,
        help="comma-separated PYTHONHASHSEED values",
    )
    parser.add_argument(
        "--refs",
        nargs="+",
        default=["HEAD", "origin/main"],
        help="git refs to test read-only (default: HEAD origin/main)",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cases < 1:
        raise SystemExit("--cases must be at least 1")
    if args.worker:
        return _worker(args.seed, args.cases)
    return _parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
