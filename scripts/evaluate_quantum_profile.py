"""Measure the frozen offline command corpus. Does not import a host adapter."""
import argparse
import hashlib
import json
import platform
import statistics
import time
from importlib.resources import files
from pathlib import Path

from nano.intent import compile_intent


def evaluate(repeats: int = 10) -> dict:
    fixture = files("nano.intent").joinpath("fixtures/quantum_commands_v1.json")
    raw = fixture.read_bytes().replace(b"\r\n", b"\n")
    bundle = json.loads(raw)
    catalog = bundle["catalog"]
    counts = dict(total=0, supported=0, abstentions=0, exact_arguments=0,
                  exact_capabilities=0, exact_diagnostics=0, correct_abstentions=0,
                  supported_exact_arguments=0, supported_exact_capabilities=0)
    times = []
    for case in bundle["fixtures"]:
        ir = compile_intent(case["source"], profile="quantum", quantum_catalog=catalog)
        doc = ir.to_dict()
        counts["total"] += 1
        capability = ir.steps[0].capability if ir.steps else None
        diagnostic = ir.diagnostics[0].code if ir.diagnostics else None
        counts["exact_arguments"] += doc["args"] == case["args"]
        counts["exact_capabilities"] += capability == case["capability"]
        counts["exact_diagnostics"] += diagnostic == case["diagnostic"]
        counts["correct_abstentions"] += bool(ir.diagnostics) == bool(case["diagnostic"])
        counts["abstentions" if case["diagnostic"] else "supported"] += 1
        if not case["diagnostic"]:
            counts["supported_exact_arguments"] += doc["args"] == case["args"]
            counts["supported_exact_capabilities"] += capability == case["capability"]
        assert all(e.kind != "security" for e in ir.entities)
        assert all(s.capability.startswith("quantum.") for s in ir.steps)
        assert not ir.diagnostics or not ir.steps
        for _ in range(repeats):
            start = time.perf_counter_ns()
            compile_intent(case["source"], profile="quantum", quantum_catalog=catalog)
            times.append((time.perf_counter_ns() - start) / 1_000_000)
    baseline = files("nano.intent").joinpath("fixtures/atsv2-golden-0.1.0.json").read_bytes()
    baseline = baseline.replace(b"\r\n", b"\n")
    assert hashlib.sha256(baseline).hexdigest() == bundle["marketBaseline"]["sha256"]
    for metric in ("exact_arguments", "exact_capabilities", "exact_diagnostics", "correct_abstentions"):
        assert counts[metric] == counts["total"], (metric, counts)
    ordered = sorted(times)
    return {"schema": "nano.quantum.profile-evaluation.v1", "counts": counts,
            "rates": {k: counts[k] / counts["total"] for k in
                      ("exact_arguments", "exact_capabilities", "exact_diagnostics", "correct_abstentions")},
            "supported_rates": {k: counts[k] / counts["supported"] for k in
                                ("supported_exact_arguments", "supported_exact_capabilities")},
            "market_capability_emissions": 0, "ticker_entity_emissions": 0,
            "corpus_sha256": hashlib.sha256(raw).hexdigest(),
            "ats_fixture_sha256": hashlib.sha256(baseline).hexdigest(),
            "python": platform.python_version(), "latency_ms": {
                "samples": len(times), "p50": statistics.median(times),
                "p95": ordered[max(0, (len(times) * 95 + 99) // 100 - 1)], "max": max(times)},
            "effects": "offline compilation only; no model, credentials or provider calls"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    result = json.dumps(evaluate(args.repeats), indent=2) + "\n"
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    else:
        print(result, end="")
