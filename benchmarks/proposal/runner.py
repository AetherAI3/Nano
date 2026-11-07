"""Deterministic benchmark runner: three arms over frozen splits, canonical report, separate telemetry."""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from . import REPORT_SCHEMA, RUNNER_VERSION, TELEMETRY_SCHEMA
from .metrics import aggregate, check_integrity, hard_acceptance, score_row
from .pipeline import NO_TRANSLATOR, Admission, admit_bytes
from .repair import RepairDecision, decide
from .schema import DATA, ROOT, canonical_text, file_sha256, load_json, load_split, sha256_text
from .verifier import Verdict, verify

ARMS = ("no_model", "recorded_proposal", "recorded_repair")
REPO = ROOT.parents[1]
ATS_FIXTURE = REPO / "nano" / "intent" / "fixtures" / "atsv2-golden-0.1.0.json"
ATS_FIXTURE_SHA256 = "6a2b3824af76f791d107a3000cec6e0dbc668eb6df7ee30cbbfe66b913d62eb4"
BASELINE_COMPOSITION = {
    "reviewed_baseline": "cb6ea884d63dcaa8cc0e7c0b952bb88784e8219f",
    "includes_fixes": {"#55": "PR #68 (de027a6, editor typed v1 diagnostics)",
                       "#56": "PR #69 (0853b4c, intent rejects invalid types)",
                       "#57": "PR #70 (bf165fe, reasoning outputs validated)"},
    "note": "Local wave0-combined base: origin/main plus the three PRs rebased; they are not yet merged on main.",
}
LIMITATIONS = (
    "The locked eval split is visible to implementers in the repository; it is a locked regression set, "
    "not a blinded experiment.",
    "Recorded proposals are hand-authored stand-ins for model output; no model was run or qualified.",
    "Command-document proposals were materialized once at freeze time from proposal source, then edited.",
    "Nano admission cannot know the requester's task; task binding is enforced by the independent verifier.",
    "Latency is local telemetry for this environment only and is excluded from the canonical report.",
    "No claim of open-ended language accuracy, GPU savings, quantum advantage or hardware results.",
)
_NOT_ADMITTED = Verdict(False, ("not_admitted",))


def _payload(case: dict[str, Any], arm: str) -> tuple[str, str, str | None]:
    if arm == "no_model":
        return "quantum_source", case["request"], None
    repair = case["recorded_repair"]
    return case["proposal"]["kind"], case["proposal"]["text"], repair["text"] if repair else None


def run_case(case: dict[str, Any], arm: str, policy: str, catalog: dict[str, Any]) -> dict[str, Any]:
    kind, text, candidate = _payload(case, arm)
    if arm == "no_model" and case["subset"] == "nano_strategy":
        first: Admission = NO_TRANSLATOR
    else:
        first = admit_bytes(kind, text.encode("utf-8"), catalog)
    decision = (RepairDecision(False, False, False, None, "no_candidate") if arm == "no_model"
                else decide(policy if arm == "recorded_repair" else "none", kind, text, first, candidate))
    final = admit_bytes(kind, candidate.encode("utf-8"), catalog) if decision.accepted and candidate else first
    verdict = verify(case["task"], case["subset"], final.document) if final.executable else _NOT_ADMITTED
    sizes = (len(text.encode("utf-8")), len(candidate.encode("utf-8")) if candidate else 0)
    return score_row(case, arm, sha256_text(text), first, decision, final, verdict, sizes)


def market_baseline() -> dict[str, Any]:
    digest = hashlib.sha256(ATS_FIXTURE.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return {"fixture": "nano/intent/fixtures/atsv2-golden-0.1.0.json", "sha256": digest,
            "expected_sha256": ATS_FIXTURE_SHA256, "unchanged": digest == ATS_FIXTURE_SHA256}


def evaluate(splits: list[str], policy: str, label: str) -> dict[str, Any]:
    catalog = load_json(DATA / "catalog.json")
    manifest_path = ROOT / "manifest.json"
    arms: dict[str, Any] = {arm: {} for arm in ARMS}
    for split in splits:
        cases = load_split(split)
        for arm in ARMS:
            rows = [run_case(case, arm, policy, catalog) for case in cases]
            arms[arm][split] = {"metrics": aggregate(rows), "hard_acceptance": hard_acceptance(rows), "rows": rows}
    report = {
        "schema": REPORT_SCHEMA, "runner_version": RUNNER_VERSION, "label": label, "splits": splits,
        "repair_policy": {"id": policy, "contract_sha256": file_sha256(ROOT / "policy.json")},
        "dataset": {"manifest_sha256": file_sha256(manifest_path),
                    "splits": {s: file_sha256(DATA / f"{s}.json") for s in splits},
                    "catalog_sha256": file_sha256(DATA / "catalog.json")},
        "baseline_composition": BASELINE_COMPOSITION, "market_baseline": market_baseline(),
        "arms": arms, "limitations": list(LIMITATIONS),
        "effects": "offline admission and verification only; no model, network, credential or provider call",
    }
    check_integrity(report, load_json(manifest_path))
    return report


def _percentile(ordered: list[float], percent: int) -> float:
    return ordered[max(0, (len(ordered) * percent + 99) // 100 - 1)]


def latency(splits: list[str], warmup: int, repeats: int) -> dict[str, Any]:
    """Time first-proposal admission (decode + compile/validate) per case; not part of the report."""
    catalog = load_json(DATA / "catalog.json")
    results: dict[str, Any] = {}
    for split in splits:
        samples: dict[str, list[float]] = {"quantum": [], "nano_strategy": []}
        for case in load_split(split):
            payload = case["proposal"]["text"].encode("utf-8")
            for index in range(warmup + repeats):
                start = time.perf_counter_ns()
                admit_bytes(case["proposal"]["kind"], payload, catalog)
                elapsed = (time.perf_counter_ns() - start) / 1_000_000
                if index >= warmup:
                    samples[case["subset"]].append(elapsed)
        results[split] = {subset: {"samples": len(values), "p50": statistics.median(values),
                                   "p95": _percentile(sorted(values), 95), "max": max(values)}
                          for subset, values in samples.items()}
    return {"schema": TELEMETRY_SCHEMA, "environment": {
        "python": platform.python_version(), "implementation": platform.python_implementation(),
        "platform": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count()},
        "policy": {"warmup_per_case": warmup, "repeats_per_case": repeats, "clock": "time.perf_counter_ns",
                   "scope": "strict decode plus Nano compile/validate of each first proposal, in process"},
        "latency_ms": results,
        "claim": "local measurement for this environment only; no universal speed claim"}


def write_report(report: dict[str, Any], output: Path) -> str:
    text = canonical_text(report) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8", newline="\n")
    digest = sha256_text(text)
    output.with_name(output.name + ".sha256").write_text(f"{digest}  {output.name}\n", encoding="utf-8",
                                                         newline="\n")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline Nano proposal-and-repair benchmark.")
    parser.add_argument("--splits", default="dev", help="comma-separated: dev, eval")
    parser.add_argument("--repair-policy", default="allowlist-v1", choices=("none", "allowlist-v1"))
    parser.add_argument("--label", default="adhoc")
    parser.add_argument("--evaluate-locked-split", action="store_true",
                        help="required to include the locked eval split (evaluate it once, at the end)")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--telemetry", type=Path)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args(argv)
    splits = [s for s in args.splits.split(",") if s]
    if not splits or set(splits) - {"dev", "eval"}:
        parser.error("--splits accepts dev and/or eval")
    if "eval" in splits and not args.evaluate_locked_split:
        parser.error("the locked eval split requires --evaluate-locked-split")
    if args.warmup < 0 or args.repeats < 1:
        parser.error("--warmup must be >= 0 and --repeats >= 1")
    report = evaluate(splits, args.repair_policy, args.label)
    if args.output is None:
        sys.stdout.write(canonical_text(report) + "\n")
    else:
        sys.stdout.write(write_report(report, args.output) + "\n")
    if args.telemetry is not None:
        args.telemetry.parent.mkdir(parents=True, exist_ok=True)
        args.telemetry.write_text(canonical_text(latency(splits, args.warmup, args.repeats)) + "\n",
                                  encoding="utf-8", newline="\n")
    passed = all(report["arms"][arm][s]["hard_acceptance"]["passed"] for arm in ARMS for s in splits)
    return 0 if passed and report["market_baseline"]["unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
