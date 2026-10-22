import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.engine
pytest.importorskip("qiskit_aer")
pytest.importorskip("aether_atlas")

from nano_qsearch.canonical import canonical_digest           # noqa: E402
from nano_qsearch.job import parse_job                        # noqa: E402
from nano_qsearch._engine import run_engine                    # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "nano_qsearch" / "_engine.py"


def _job(fixtures_dir, name):
    body = json.loads((fixtures_dir / name).read_text(encoding="utf-8"))
    return body, parse_job(body)


def _child_env():
    parts = [str(ENGINE.parents[1])]
    if os.environ.get("NANO_QSEARCH_ATLAS_PATH"):
        parts.insert(0, os.environ["NANO_QSEARCH_ATLAS_PATH"])
    if os.environ.get("PYTHONPATH"):
        parts.append(os.environ["PYTHONPATH"])
    return {**os.environ, "OMP_NUM_THREADS": "1", "PYTHONPATH": os.pathsep.join(parts)}


@pytest.mark.parametrize("kind", ["statevector", "mps"])
def test_four_variable_fixture(fixtures_dir, kind):
    body, job = _job(fixtures_dir, "job_4var.json")
    res = run_engine(body, kind)
    assert "error" not in res and res["kind"] == kind
    assert res["best_sample_energy"] >= job.classical_min_energy - 1e-9
    assert abs(res["exact_min_energy"] - job.classical_min_energy) <= 1e-9
    assert sum(c for _, c, _ in res["samples"]) == job.shots
    for bits, _, energy in res["samples"]:
        assert len(bits) == 4 and abs(job.energy([int(b) for b in bits]) - energy) <= 1e-9
    assert res["samples"] == sorted(res["samples"], key=lambda r: (r[2], r[0]))
    assert 0.0 <= res["tvd_ideal"] <= 1.0 and math.isfinite(res["mean_energy_ideal"])
    if kind == "mps":
        assert res["chi_reached"] >= 1 and res["chi_max"] == job.chi_max and res["truncated"] is False
    else:
        assert res["chi_reached"] is None and res["truncated"] is None


def test_pair02_statevector_and_mps_agree_on_distribution(fixtures_dir):
    body, job = _job(fixtures_dir, "job_pair02.json")
    sv, mps = run_engine(body, "statevector"), run_engine(body, "mps")
    assert sv["variant"] == mps["variant"] and sv["compiled_plan_digest"] == mps["compiled_plan_digest"]
    # pair02 at chi 64 is exact: MPS TVD to ideal within 0.01 of the statevector sampling TVD (spec B7)
    assert abs(sv["tvd_ideal"] - mps["tvd_ideal"]) <= 0.01
    assert abs(sv["mean_energy_sample"] - sv["mean_energy_ideal"]) <= 0.1


def test_deterministic_for_same_seed(fixtures_dir):
    body, _ = _job(fixtures_dir, "job_4var.json")
    a, b = run_engine(body, "statevector"), run_engine(body, "statevector")
    assert a["samples"] == b["samples"]


def test_truncation_reported_at_tiny_chi(fixtures_dir):
    body, _ = _job(fixtures_dir, "job_pair02.json")
    body["backend"]["chi_max"] = 1
    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    body = {**unsigned, "job_digest": canonical_digest(unsigned)}
    res = run_engine(body, "mps")
    assert res["chi_max"] == 1 and res["truncated"] is True


def test_cli_writes_result_file(fixtures_dir, tmp_path):
    body, _ = _job(fixtures_dir, "job_4var.json")
    job_path, out_path = tmp_path / "job.json", tmp_path / "out.json"
    job_path.write_text(json.dumps(body), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ENGINE), "--job", str(job_path), "--out", str(out_path), "--kind", "statevector"],
                          env=_child_env(), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out_path.read_text(encoding="utf-8"))["kind"] == "statevector"


def test_cli_reports_error_class_not_message(tmp_path):
    job_path, out_path = tmp_path / "job.json", tmp_path / "out.json"
    job_path.write_text('{"schema": "nope"}', encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ENGINE), "--job", str(job_path), "--out", str(out_path), "--kind", "mps"],
                          env=_child_env(), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0
    assert json.loads(out_path.read_text(encoding="utf-8")) == {"error": {"type": "JobValidationError"}}
