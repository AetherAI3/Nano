"""Runner, metrics, controls and determinism for the #87 proposal benchmark (dev split only)."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.proposal.metrics import IntegrityError, aggregate, check_integrity, hard_acceptance  # noqa: E402
from benchmarks.proposal.runner import evaluate, latency  # noqa: E402
from benchmarks.proposal.schema import ROOT as BENCH, canonical_text, load_json  # noqa: E402


@pytest.fixture(scope="module")
def baseline():
    return evaluate(["dev"], "none", "test")


def _row(report, arm, case_id):
    return next(r for r in report["arms"][arm]["dev"]["rows"] if r["id"] == case_id)


def test_repeat_runs_are_byte_identical_in_process(baseline):
    assert canonical_text(evaluate(["dev"], "none", "test")) == canonical_text(baseline)


def test_report_bytes_do_not_depend_on_hash_seed(tmp_path):
    digests = []
    for seed in ("0", "12345"):
        output = tmp_path / f"seed-{seed}.json"
        env = {**os.environ, "PYTHONHASHSEED": seed}
        subprocess.run([sys.executable, str(ROOT / "scripts" / "run_proposal_benchmark.py"), "--splits", "dev",
                        "--repair-policy", "none", "--label", "test", "--output", str(output)],
                       cwd=ROOT, env=env, check=True, capture_output=True)
        digests.append(output.read_bytes())
    assert digests[0] == digests[1]


def test_canonical_report_excludes_performance_telemetry(baseline):
    text = canonical_text(baseline)
    for key in ("latency", "p50", "p95", "perf_counter", "platform", "cpu_count"):
        assert key not in text
    telemetry = latency(["dev"], 0, 1)
    assert set(telemetry["latency_ms"]["dev"]) == {"quantum", "nano_strategy"}
    assert telemetry["latency_ms"]["dev"]["quantum"]["samples"] == 51
    assert telemetry["policy"]["warmup_per_case"] == 0 and telemetry["environment"]["python"]


def test_hard_acceptance_and_market_baseline_hold_on_dev(baseline):
    assert baseline["market_baseline"]["unchanged"] is True
    for arm in ("no_model", "recorded_proposal", "recorded_repair"):
        verdict = baseline["arms"][arm]["dev"]["hard_acceptance"]
        assert verdict["passed"], (arm, verdict)
        assert verdict["negative_class_cases"] == 40


def test_quantum_and_strategy_denominators_are_separate(baseline):
    metrics = baseline["arms"]["recorded_proposal"]["dev"]["metrics"]
    assert metrics["quantum"]["all"]["cases"] == 51
    assert metrics["nano_strategy"]["all"]["cases"] == 9
    for subset in ("quantum", "nano_strategy"):
        per_class = [metrics[subset][k]["cases"] for k in ("supported_unambiguous", "ambiguous_or_unsupported",
                                                            "adversarial_or_malformed")]
        assert sum(per_class) == metrics[subset]["all"]["cases"]


def test_negative_control_wrong_but_valid_operation_is_scored_as_failure(baseline):
    for case_id in ("dev-q-m04", "dev-n-m02"):
        row = _row(baseline, "recorded_proposal", case_id)
        assert row["final"]["executable"] is True          # parser/compiler accepted it
        assert row["verifier"]["bound"] is False           # but it is not the requested task
        assert row["outcome"] == "abstain" and row["observed_category"] == "task_binding"
        assert row["task_success"] is False and row["semantic_false_acceptance"] is True
        assert row["unauthorized_final_executable"] is False


def test_negative_control_fabricated_zero_budget_report_fails_integrity(baseline):
    manifest = load_json(BENCH / "manifest.json")
    check_integrity(baseline, manifest)
    empty_rows: list = []
    fabricated = copy.deepcopy(baseline)
    for arm in fabricated["arms"].values():
        arm["dev"] = {"rows": empty_rows, "metrics": aggregate(empty_rows),
                      "hard_acceptance": {**hard_acceptance(empty_rows), "passed": True}}
    with pytest.raises(IntegrityError):
        check_integrity(fabricated, manifest)
    with pytest.raises(IntegrityError):
        check_integrity({**baseline, "splits": []}, manifest)
    inflated = copy.deepcopy(baseline)
    inflated["arms"]["recorded_repair"]["dev"]["metrics"]["quantum"]["all"]["supported_task_success"] += 1
    with pytest.raises(IntegrityError):
        check_integrity(inflated, manifest)


def test_fabricated_and_malformed_proposals_are_never_executable(baseline):
    for row in baseline["arms"]["recorded_proposal"]["dev"]["rows"]:
        if row["expected_outcome"] == "abstain" and row["expected_category"] != "task_binding":
            assert row["final"]["executable"] is False, row["id"]


def test_no_model_control_compiles_requests_without_any_translator(baseline):
    for row in baseline["arms"]["no_model"]["dev"]["rows"]:
        assert row["repair"]["candidate"] is False
        if row["subset"] == "nano_strategy":
            assert row["final"]["category"] == "no_translator" and row["outcome"] == "abstain"
        if not row["task_supported"]:
            assert row["outcome"] == "abstain"


def test_lenient_decoding_exposure_is_measured_not_hidden(baseline):
    duplicate = _row(baseline, "recorded_proposal", "dev-q-m09")
    assert duplicate["first"]["category"] == "json_duplicate_key"
    assert duplicate["lenient_executable"] is True  # a last-wins decoder would admit it
    assert baseline["arms"]["recorded_proposal"]["dev"]["metrics"]["quantum"]["all"]["lenient_decode_exposure"] >= 1


def test_raw_outputs_are_hashed_per_case(baseline):
    for row in baseline["arms"]["recorded_proposal"]["dev"]["rows"]:
        for stage in ("first", "final"):
            assert row[stage]["output_sha256"].startswith("sha256:")
        assert row["input_sha256"].startswith("sha256:")


def test_cli_refuses_the_locked_split_without_explicit_flag(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_proposal_benchmark.py"),
                             "--splits", "eval", "--output", str(tmp_path / "x.json")],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 2 and not (tmp_path / "x.json").exists()


def test_committed_baseline_report_replays_byte_for_byte():
    committed = BENCH / "reports" / "baseline-dev.json"
    expected = json.loads(committed.read_text(encoding="utf-8"))
    replay = canonical_text(evaluate(["dev"], expected["repair_policy"]["id"], expected["label"])) + "\n"
    assert replay == committed.read_text(encoding="utf-8").replace("\r\n", "\n")
