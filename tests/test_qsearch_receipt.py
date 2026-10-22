# tests/test_qsearch_receipt.py
import hashlib
import hmac
import json

import pytest

from nano_qsearch.canonical import canonical_json
from nano_qsearch.job import parse_job
from nano_qsearch.receipt import (
    RECEIPT_SCHEMA, SEAL_PREFIX, SealError, not_run_receipt, ok_receipt, refused_receipt,
    seal_receipt, verify_seal,
)

KEY = b"k" * 32


@pytest.fixture
def job(fixtures_dir):
    return parse_job(json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8")))


def _engine_result(job, best):
    return {"kind": "statevector", "backend_id": "qiskit-aer 0.17.2 statevector",
            "variant": "ordinary", "nuclei": [], "compiled_plan_digest": "sha256:" + "1" * 64,
            "compile": {"target": job.target, "coupling_map": "heavy_hex_d3", "physical_qubits": 19,
                        "basis": ["cz", "rz", "sx", "x"], "opt_level": 3, "seed": 11,
                        "candidates": {"ordinary": {"logical_2q": 4, "native_2q": 9, "native_depth": 7}, "joint5": None}},
            "metrics": {"logical_2q": 4, "native_2q": 9, "native_depth": 7},
            "chi_max": None, "chi_reached": None, "truncated": None,
            "shots": 1024, "seed": 7, "wall_s": 0.4, "attempts": 1,
            "samples": [["1100", 600, best], ["0000", 424, 0.0]],
            "best_sample_energy": best, "mean_energy_sample": best * 600 / 1024,
            "mean_energy_ideal": best * 0.6, "tvd_ideal": 0.01, "exact_min_energy": job.classical_min_energy}


def test_seal_round_trip_and_independent_hmac(job):
    body = not_run_receipt(job, "engine: TimeoutExpired")
    sealed = seal_receipt(body, KEY)
    assert sealed["seal"].startswith(SEAL_PREFIX)
    expected = hmac.new(KEY, canonical_json({k: v for k, v in sealed.items() if k != "seal"}).encode("utf-8"),
                        hashlib.sha256).hexdigest()
    assert sealed["seal"] == SEAL_PREFIX + expected
    verify_seal(sealed, KEY)      # no raise
    with pytest.raises(SealError):
        verify_seal({**sealed, "status": "OK"}, KEY)
    with pytest.raises(SealError):
        verify_seal(sealed, b"x" * 32)
    with pytest.raises(ValueError):
        seal_receipt(body, b"short")


def test_not_run_and_refused_shape(job):
    nr = not_run_receipt(job, "engine: OSError")
    assert nr == {"schema": RECEIPT_SCHEMA, "job_digest": job.job_digest, "status": "NOT_RUN",
                  "backend": {"kind": "none"}, "reason": "engine: OSError"}
    rf = refused_receipt(job, "limits: 41 variables exceed service cap 40")
    assert rf["status"] == "REFUSED" and rf["backend"] == {"kind": "none"} and rf["job_digest"] == job.job_digest


def test_ok_receipt_agree_within_tolerance(job):
    body = ok_receipt(job, _engine_result(job, job.classical_min_energy + 5e-10))
    assert body["status"] == "OK" and body["audit"]["agree"] is True
    assert body["audit"]["classical_min_energy"] == job.classical_min_energy
    assert body["audit"]["energy_gap_best"] == pytest.approx(5e-10 / abs(job.classical_min_energy), abs=1e-12)
    assert body["backend"]["kind"] == "statevector" and body["backend"]["chi_max"] is None
    assert body["samples"]["top"][0] == {"bits": "1100", "count": 600, "energy": job.classical_min_energy + 5e-10}
    assert body["samples"]["bit_order"] == "x0_first"
    assert body["samples"]["distribution_digest"].startswith("sha256:")
    assert body["variant"] == "ordinary" and body["metrics"]["native_2q"] == 9
    assert "seal" not in body


def test_ok_receipt_disagree_and_truncated(job):
    res = _engine_result(job, job.classical_min_energy + 0.5)
    res.update(kind="mps", chi_max=8, chi_reached=8, truncated=True)
    body = ok_receipt(job, res)
    assert body["status"] == "TRUNCATED" and body["audit"]["agree"] is False
    assert body["backend"]["chi_reached"] == 8 and body["backend"]["truncated"] is True


def test_ok_receipt_top_is_capped_at_eight(job):
    res = _engine_result(job, -1.0)
    res["samples"] = [[format(i, "04b"), 10, -1.0 + i] for i in range(12)]
    assert len(ok_receipt(job, res)["samples"]["top"]) == 8


def test_zero_classical_min_gives_zero_gap(job, fixtures_dir):
    body = json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))
    # build a job whose classical min is exactly 0 by editing and re-signing
    from nano_qsearch.canonical import canonical_digest
    body["audit"]["classical_min_energy"] = 0.0
    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    zero_job = parse_job({**unsigned, "job_digest": canonical_digest(unsigned)})
    assert ok_receipt(zero_job, _engine_result(zero_job, 0.0))["audit"]["energy_gap_best"] == 0.0
