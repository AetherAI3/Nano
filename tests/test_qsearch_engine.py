import copy
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.engine
pytest.importorskip("qiskit_aer")
pytest.importorskip("aether_atlas")

from nano_qsearch.canonical import canonical_digest           # noqa: E402
from nano_qsearch.job import parse_job                        # noqa: E402
from nano_qsearch import _engine                              # noqa: E402
from nano_qsearch._engine import run_engine                    # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "nano_qsearch" / "_engine.py"


def _job(fixtures_dir, name):
    body = json.loads((fixtures_dir / name).read_text(encoding="utf-8"))
    return body, parse_job(body)


def _redigest(body):
    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    return {**unsigned, "job_digest": canonical_digest(unsigned)}


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
        assert res["chi_source"] == "peak_log"
    else:
        assert res["chi_reached"] is None and res["truncated"] is None and res["chi_source"] is None


def test_pair02_statevector_and_mps_agree_on_distribution(fixtures_dir):
    body, job = _job(fixtures_dir, "job_pair02.json")
    sv, mps = run_engine(body, "statevector"), run_engine(body, "mps")
    assert sv["variant"] == mps["variant"] and sv["compiled_plan_digest"] == mps["compiled_plan_digest"]
    # pair02 at chi 64 is exact: MPS TVD to ideal within 0.01 of the statevector sampling TVD (spec B7)
    assert abs(sv["tvd_ideal"] - mps["tvd_ideal"]) <= 0.01
    assert abs(sv["mean_energy_sample"] - sv["mean_energy_ideal"]) <= 0.1


def test_pair02_mps_reports_peak_bond_dimension_from_the_log(fixtures_dir):
    body, job = _job(fixtures_dir, "job_pair02.json")
    res = run_engine(body, "mps")
    assert res["chi_source"] == "peak_log"
    plan = _engine.compile_program(job)
    final, _ = _engine._mps_snapshot(plan.logical, _engine._simulator("mps", job.chi_max))
    # pair02's bonds peak mid-circuit above where they end: the final bonds alone under-report
    assert max(final) < res["chi_reached"] <= job.chi_max and res["truncated"] is False


def test_peak_from_log_takes_only_this_runs_suffix():
    before = "{I0:cx on qubits 1,0, BD=[9 9],  }"
    after = "{I0:cx on qubits 1,0, BD=[9 9],  I0:cx on qubits 1,0, BD=[2 1],  internal_swap on qubits 2,1, BD=[2 4],  " \
            "discarded_value=0.01, I1:rzz on qubits 1,2, BD=[2 2],  }"
    assert _engine._peak_from_log(before, after, [2, 2]) == 4
    assert _engine._peak_from_log("{}", "{I0:cx on qubits 1,0, BD=[3 2],  }", [3, 2]) == 3


@pytest.mark.parametrize("before, after, final", [
    (None, "{I0:cx on qubits 1,0, BD=[2 1],  }", [2, 1]),             # no snapshot
    ("{}", None, [2, 1]),                                              # no log
    ("{}", "{I0:cx on qubits 1,0, BD=[2 1],  }", [2, 2]),              # last logged bonds are not the final bonds
    ("{x}", "{y I0:cx on qubits 1,0, BD=[2 1],  }", [2, 1]),           # snapshot is not a prefix
    ("{}", "{internal_swap on qubits 1,0}", [1, 1]),                   # nothing parseable
])
def test_peak_from_log_refuses_untrustworthy_logs(before, after, final):
    assert _engine._peak_from_log(before, after, final) is None


def test_deterministic_for_same_seed(fixtures_dir):
    body, _ = _job(fixtures_dir, "job_4var.json")
    a, b = run_engine(body, "statevector"), run_engine(body, "statevector")
    assert a["samples"] == b["samples"]


def test_truncation_reported_at_tiny_chi(fixtures_dir):
    body, _ = _job(fixtures_dir, "job_pair02.json")
    body["backend"]["chi_max"] = 1
    res = run_engine(_redigest(body), "mps")
    assert res["chi_max"] == 1 and res["truncated"] is True and res["chi_reached"] == 1


def test_logical_probabilities_follow_the_mapping():
    probs = [0.0, 1.0, 0.0, 0.0]                         # all mass on wire 0 = 1
    assert list(_engine._logical_probabilities(probs, [1, 0], 2)) == [0.0, 0.0, 1.0, 0.0]
    assert list(_engine._logical_probabilities(probs, [0, 1], 2)) == [0.0, 1.0, 0.0, 0.0]


def test_ideal_reads_wire_zero_as_logical_one(fixtures_dir):
    from qiskit import QuantumCircuit
    body = json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))
    body["variables"] = ["a", "b"]
    body["qubo"]["q"] = [[-1.0, 0.25], [0.25, -3.0]]
    body["audit"]["classical_min_energy"] = -3.5
    job = parse_job(_redigest(body))
    logical = QuantumCircuit(2)
    logical.x(0)
    stub = SimpleNamespace(logical=logical, mapping=[1, 0])      # wire 0 carries logical variable 1
    energies = _engine._state_energies(job)
    expected = job.energy([0, 1])
    assert expected == -3.0 and len(set(energies.tolist())) == 4    # every assignment distinguishable
    mean, tvd = _engine._ideal(job, stub, [["01", 5, expected]], energies)
    assert mean == pytest.approx(expected) and tvd == pytest.approx(0.0)


def test_target_unavailable_is_reported_by_class(fixtures_dir, monkeypatch):
    body = json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))
    body["compile"]["target"] = "fake_fez"
    monkeypatch.setitem(sys.modules, "qiskit_ibm_runtime.fake_provider", None)
    assert run_engine(_redigest(body), "statevector") == {"error": {"type": "TargetUnavailable"}}


def test_cli_writes_result_file(fixtures_dir, tmp_path):
    body, _ = _job(fixtures_dir, "job_4var.json")
    job_path, out_path = tmp_path / "job.json", tmp_path / "out.json"
    job_path.write_text(json.dumps(body), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ENGINE), "--job", str(job_path), "--out", str(out_path), "--kind", "statevector"],
                          env=_child_env(), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr
    child = json.loads(out_path.read_text(encoding="utf-8"))
    assert child["kind"] == "statevector"
    assert not (tmp_path / "out.json.tmp").exists()                # written atomically, temp moved into place
    # cross-process determinism: the child compiles the same plan as this interpreter
    assert child["compiled_plan_digest"] == run_engine(copy.deepcopy(body), "statevector")["compiled_plan_digest"]


def test_cli_reports_error_class_not_message(tmp_path):
    job_path, out_path = tmp_path / "job.json", tmp_path / "out.json"
    job_path.write_text('{"schema": "nope"}', encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ENGINE), "--job", str(job_path), "--out", str(out_path), "--kind", "mps"],
                          env=_child_env(), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0
    assert json.loads(out_path.read_text(encoding="utf-8")) == {"error": {"type": "JobValidationError"}}
