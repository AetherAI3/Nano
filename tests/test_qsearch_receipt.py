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


def _job_with_floor(fixtures_dir, floor, offset=None):
    """The 4var job with ``audit.classical_min_energy`` (and optionally ``qubo.offset``) replaced, digest re-signed."""
    body = json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))
    body["audit"]["classical_min_energy"] = floor
    if offset is not None:
        body["qubo"]["offset"] = offset
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
    with pytest.raises(ReceiptAuditError, match="below the classical floor"):
        ok_receipt(job, res)


# Each case below is built so that ONLY the guard it names can fire: wherever a
# mutation would otherwise also break "best is the minimum", best_sample_energy
# is set to the minimum of the mutated samples ("0000" has energy 0.0 in the
# 4var job). ``match`` pins the guard's own message, so deleting that guard
# makes the case fail instead of passing through a neighbouring check.

def _set(key, value):
    return lambda res: res.__setitem__(key, value)


def _samples(rows, best):
    def mutate(res):
        res["samples"] = rows
        res["best_sample_energy"] = best
    return mutate


def _bump_first_energy(res):
    res["samples"][0][2] += 0.1
    res["best_sample_energy"] = min(e for _, _, e in res["samples"])


_NOT_FINITE_BEST = "best_sample_energy is not a finite number"


@pytest.mark.parametrize("mutate, match", [
    (_bump_first_energy, "sample energy does not match the job"),
    (_set("best_sample_energy", float("nan")), _NOT_FINITE_BEST),
    (_set("best_sample_energy", "low"), _NOT_FINITE_BEST),
    (_set("best_sample_energy", 10**400), _NOT_FINITE_BEST),              # overflow branch of _finite_number
    (_set("kind", "gpu"), "engine kind is not statevector or mps"),
    (_set("truncated", "yes"), "truncated is not true, false or null"),
    (_set("truncated", 1), "truncated is not true, false or null"),       # 1 == True but is not True
    (_set("truncated", False), "truncated must be null for statevector"),
    (_samples([], 0.0), "samples must be a non-empty list"),
    (_samples("1100", 0.0), "samples must be a non-empty list"),
    (_samples([["11", 1, 0.0]], 0.0), "sample bits do not match the job"),          # wrong length
    (_samples([["11a0", 1, 0.0]], 0.0), "sample bits do not match the job"),        # wrong alphabet
    (_samples([["0000", 0, 0.0]], 0.0), "sample count is not a positive integer"),  # zero count
    (_samples([["0000", True, 0.0]], 0.0), "sample count is not a positive integer"),  # bool count
    (_samples([["0000", 1, float("inf")]], 0.0), "sample energy is not a finite number"),
    (_samples([["0000", 1, 10**400]], 0.0), "sample energy is not a finite number"),  # overflow branch
    (_samples([["0000", 1]], 0.0), r"sample is not \[bits, count, energy\]"),       # missing energy
    (lambda res: res.__setitem__("best_sample_energy", res["best_sample_energy"] + 0.5), "not the minimum"),
])
def test_unauditable_engine_result_is_refused(job, mutate, match):
    res = _engine_result(job)
    mutate(res)
    with pytest.raises(ReceiptAuditError, match=match):
        ok_receipt(job, res)


def test_bool_best_is_refused_even_when_numerically_consistent(fixtures_dir):
    # offset 1.0 puts "0000" at energy 1.0 == True, so only the bool guard can refuse this.
    job = _job_with_floor(fixtures_dir, -0.6, offset=1.0)
    res = _engine_result(job, (("0000", 1024),))
    assert res["best_sample_energy"] == 1.0
    res["best_sample_energy"] = True
    with pytest.raises(ReceiptAuditError, match=_NOT_FINITE_BEST):
        ok_receipt(job, res)


def test_mps_truncated_false_and_none_are_accepted(job):
    for flag in (False, None):
        res = _engine_result(job)
        res.update(kind="mps", chi_max=8, chi_reached=3, chi_source="final", truncated=flag)
        assert ok_receipt(job, res)["status"] == "OK"


@pytest.mark.parametrize("result", [None, [], "result", 7])
def test_non_mapping_engine_result_is_refused(job, result):
    with pytest.raises(ReceiptAuditError, match="missing or malformed fields"):
        ok_receipt(job, result)


@pytest.mark.parametrize("mutate", [
    lambda res: res.pop("compiled_plan_digest"),
    lambda res: res.pop("backend_id"),
    lambda res: res.pop("mean_energy_ideal"),
    _set("compile", None),
    _set("metrics", 5),
    _set("nuclei", [1, 2]),
])
def test_engine_result_missing_or_ill_typed_fields_is_refused(job, mutate):
    res = _engine_result(job)
    mutate(res)
    with pytest.raises(ReceiptAuditError, match="missing or malformed fields"):
        ok_receipt(job, res)


@pytest.mark.parametrize("payload", [None, ["seal"], "hmac-sha256:" + "0" * 64, 3])
def test_verify_seal_non_mapping_is_seal_error(payload):
    with pytest.raises(SealError):
        verify_seal(payload, KEY)


def test_verify_seal_deeply_nested_body_is_seal_error(job):
    sealed = seal_receipt(not_run_receipt(job, "engine: OSError"), KEY)
    deep = []
    for _ in range(100_000):
        deep = [deep]
    with pytest.raises(SealError):
        verify_seal({**sealed, "extra": deep}, KEY)


@pytest.mark.parametrize("build", [not_run_receipt, refused_receipt])
@pytest.mark.parametrize("reason", [None, 42, b"engine: OSError", OSError("secret path")])
def test_reason_must_be_str(job, build, reason):
    with pytest.raises(TypeError):
        build(job, reason)
