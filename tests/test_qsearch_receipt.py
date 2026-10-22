# tests/test_qsearch_receipt.py
import hashlib
import hmac
import itertools
import json

import pytest

from nano_qsearch.canonical import canonical_digest, canonical_json
from nano_qsearch.job import parse_job
from nano_qsearch.receipt import (
    RECEIPT_SCHEMA, SEAL_PREFIX, ReceiptAuditError, SealError, not_run_receipt, ok_receipt, refused_receipt,
    seal_receipt, verify_seal,
)

KEY = b"k" * 32


@pytest.fixture
def job(fixtures_dir):
    return parse_job(json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8")))


def _job_with_floor(fixtures_dir, floor):
    """The 4var job with ``audit.classical_min_energy`` replaced and the digest re-signed."""
    body = json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))
    body["audit"]["classical_min_energy"] = floor
    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    return parse_job({**unsigned, "job_digest": canonical_digest(unsigned)})


def _energy(job, bits):
    return job.energy([int(c) for c in bits])


def _minimising_bits(job):
    """Enumerate every assignment and return the bit string with the lowest energy."""
    return min(("".join(map(str, x)) for x in itertools.product((0, 1), repeat=job.n)),
               key=lambda bits: _energy(job, bits))


def _engine_result(job, rows=(("1100", 600), ("0000", 424))):
    samples = [[bits, count, _energy(job, bits)] for bits, count in rows]
    best = min(e for _, _, e in samples)
    total = sum(c for _, c, _ in samples)
    return {"kind": "statevector", "backend_id": "qiskit-aer 0.17.2 statevector",
            "variant": "ordinary", "nuclei": [], "compiled_plan_digest": "sha256:" + "1" * 64,
            "compile": {"target": job.target, "coupling_map": "heavy_hex_d3", "physical_qubits": 19,
                        "basis": ["cz", "rz", "sx", "x"], "opt_level": 3, "seed": 11,
                        "candidates": {"ordinary": {"logical_2q": 4, "native_2q": 9, "native_depth": 7}, "joint5": None}},
            "metrics": {"logical_2q": 4, "native_2q": 9, "native_depth": 7},
            "chi_max": None, "chi_reached": None, "truncated": None,
            "shots": total, "seed": 7, "wall_s": 0.4, "attempts": 1,
            "samples": samples,
            "best_sample_energy": best, "mean_energy_sample": sum(c * e for _, c, e in samples) / total,
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


@pytest.mark.parametrize("bad", [float("nan"), {1: "non-string key"}, object()])
def test_verify_seal_turns_uncanonical_body_into_seal_error(job, bad):
    sealed = seal_receipt(not_run_receipt(job, "engine: OSError"), KEY)
    with pytest.raises(SealError):
        verify_seal({**sealed, "extra": bad}, KEY)


def test_not_run_and_refused_shape(job):
    nr = not_run_receipt(job, "engine: OSError")
    assert nr == {"schema": RECEIPT_SCHEMA, "job_digest": job.job_digest, "status": "NOT_RUN",
                  "backend": {"kind": "none"}, "reason": "engine: OSError"}
    rf = refused_receipt(job, "limits: 41 variables exceed service cap 40")
    assert rf["status"] == "REFUSED" and rf["backend"] == {"kind": "none"} and rf["job_digest"] == job.job_digest


@pytest.mark.parametrize("build", [not_run_receipt, refused_receipt])
def test_reason_is_capped_at_200_chars(job, build):
    assert build(job, "x" * 500)["reason"] == "x" * 200
    assert build(job, "engine: OSError")["reason"] == "engine: OSError"


def test_ok_receipt_agree_within_tolerance(job):
    bits = _minimising_bits(job)
    true_min = _energy(job, bits)
    assert abs(true_min - job.classical_min_energy) <= 1e-9     # the fixture's floor is the real minimum
    body = ok_receipt(job, _engine_result(job, ((bits, 600), ("0000", 424))))
    assert body["status"] == "OK" and body["audit"]["agree"] is True
    assert body["audit"]["classical_min_energy"] == job.classical_min_energy
    assert body["audit"]["best_sample_energy"] == true_min
    assert body["audit"]["energy_gap_best"] == pytest.approx(0.0, abs=1e-12)
    assert body["backend"]["kind"] == "statevector" and body["backend"]["chi_max"] is None
    assert body["samples"]["top"][0] == {"bits": bits, "count": 600, "energy": true_min}
    assert body["samples"]["bit_order"] == "x0_first"
    assert body["samples"]["distribution_digest"].startswith("sha256:")
    assert body["variant"] == "ordinary" and body["metrics"]["native_2q"] == 9
    assert "seal" not in body


def test_ok_receipt_disagree_and_truncated(job):
    res = _engine_result(job, (("1100", 600), ("0000", 424)))
    assert res["best_sample_energy"] > job.classical_min_energy + 0.1    # above the floor
    assert ok_receipt(job, res)["backend"]["chi_source"] is None        # statevector results carry none
    res.update(kind="mps", chi_max=8, chi_reached=8, chi_source="peak_log", truncated=True)
    body = ok_receipt(job, res)
    assert body["status"] == "TRUNCATED" and body["audit"]["agree"] is False
    assert body["backend"]["chi_reached"] == 8 and body["backend"]["truncated"] is True
    assert body["backend"]["chi_source"] == "peak_log"


def test_ok_receipt_top_is_capped_at_eight(job):
    res = _engine_result(job, [(format(i, "04b"), 10) for i in range(12)])
    assert len(ok_receipt(job, res)["samples"]["top"]) == 8


def test_zero_classical_min_gives_zero_gap(fixtures_dir):
    zero_job = _job_with_floor(fixtures_dir, 0.0)
    assert _energy(zero_job, "0000") == 0.0 + zero_job.offset == zero_job.classical_min_energy
    body = ok_receipt(zero_job, _engine_result(zero_job, (("0000", 1024),)))
    assert body["audit"]["energy_gap_best"] == 0.0


def test_best_below_floor_is_refused(fixtures_dir):
    job = _job_with_floor(fixtures_dir, -1.0)      # the true minimum (-1.6) undercuts this floor
    res = _engine_result(job, ((_minimising_bits(job), 600), ("0000", 424)))
    assert res["best_sample_energy"] < job.classical_min_energy
    with pytest.raises(ReceiptAuditError):
        ok_receipt(job, res)


def _set(key, value):
    return lambda res: res.__setitem__(key, value)


def _bump_first_energy(res):
    res["samples"][0][2] += 0.1


@pytest.mark.parametrize("mutate", [
    _bump_first_energy,                                       # sample energy disagrees with job.energy
    _set("best_sample_energy", True),
    _set("best_sample_energy", float("nan")),
    _set("best_sample_energy", "low"),
    _set("kind", "gpu"),
    _set("samples", []),
    _set("samples", "1100"),
    _set("samples", [["11", 1, 0.0]]),                        # wrong length
    _set("samples", [["11a0", 1, 0.0]]),                      # wrong alphabet
    _set("samples", [["0000", 0, 0.0]]),                      # zero count
    _set("samples", [["0000", True, 0.0]]),                   # bool count
    _set("samples", [["0000", 1, float("inf")]]),
    _set("samples", [["0000", 1]]),                           # missing energy
    lambda res: res.__setitem__("best_sample_energy", res["best_sample_energy"] + 0.5),   # not the minimum
])
def test_unauditable_engine_result_is_refused(job, mutate):
    res = _engine_result(job)
    mutate(res)
    with pytest.raises(ReceiptAuditError):
        ok_receipt(job, res)
