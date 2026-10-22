import json
import sys
from dataclasses import replace

import pytest

pytestmark = pytest.mark.engine
pytest.importorskip("qiskit")
pytest.importorskip("aether_atlas")

from nano_qsearch.canonical import canonical_digest        # noqa: E402
from nano_qsearch.job import parse_job                      # noqa: E402
from nano_qsearch.compile import BASIS, TargetUnavailable, compile_program, heavy_hex_map   # noqa: E402


@pytest.fixture
def pair02(fixtures_dir):
    return parse_job(json.loads((fixtures_dir / "job_pair02.json").read_text(encoding="utf-8")))


@pytest.fixture
def four(fixtures_dir):
    return parse_job(json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8")))


def test_heavy_hex_map_sizes():
    assert heavy_hex_map(4).size() == 19 and heavy_hex_map(19).size() == 19
    assert heavy_hex_map(20).size() == 57 and heavy_hex_map(58).size() == 115


def test_auto_compiles_both_and_keeps_smaller(pair02):
    plan = compile_program(pair02)
    cands = plan.candidates
    assert set(cands) == {"ordinary", "joint5"} and cands["ordinary"]["logical_2q"] == 30
    assert plan.variant == "joint5" and plan.nuclei == [[0, 1, 2], [0, 3, 4], [1, 3, 5], [2, 4, 5]]
    chosen = min(cands, key=lambda k: (cands[k]["native_2q"], k != "ordinary"))
    assert plan.variant == chosen and plan.metrics == cands[chosen]
    assert plan.compiled_plan_digest.startswith("sha256:") and plan.coupling_map == "heavy_hex_d3"
    assert set(plan.isa.count_ops()) <= set(BASIS) | {"measure", "barrier"}
    assert plan.logical.num_qubits == 6 and sorted(plan.mapping) == list(range(6))


def test_explicit_ordinary_skips_joint5(pair02):
    plan = compile_program(replace(pair02, variant="ordinary"))
    assert plan.variant == "ordinary" and plan.candidates["joint5"] is None and plan.nuclei == []


def test_joint5_without_nuclei_is_an_error(fixtures_dir):
    body = json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))
    n = len(body["variables"])
    for i in range(n):
        for j in range(n):
            if i != j:
                body["qubo"]["q"][i][j] = 0.0        # strip all couplings → no triangle
    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    job = parse_job({**unsigned, "job_digest": canonical_digest(unsigned)})
    with pytest.raises(ValueError):
        compile_program(replace(job, variant="joint5"))
    plan = compile_program(job)              # auto → ordinary, no 2q gates at all
    assert plan.variant == "ordinary" and plan.metrics["native_2q"] == 0


@pytest.mark.parametrize("name", ["job_4var.json", "job_pair02.json"])
def test_digest_is_deterministic(fixtures_dir, name):
    job = parse_job(json.loads((fixtures_dir / name).read_text(encoding="utf-8")))
    a, b = compile_program(job), compile_program(job)
    assert a.compiled_plan_digest == b.compiled_plan_digest and a.inspection == b.inspection


def test_heavy_hex_map_refuses_oversized_jobs():
    with pytest.raises(ValueError):
        heavy_hex_map(10_000)


@pytest.mark.parametrize("name", ["job_4var.json", "job_pair02.json"])
def test_inspection_reads_every_logical_bit_once(fixtures_dir, name):
    job = parse_job(json.loads((fixtures_dir / name).read_text(encoding="utf-8")))
    plan = compile_program(job)
    mmap = plan.inspection["measurement_map"]
    assert len(mmap) == job.n and sorted(clbit for _, clbit in mmap) == list(range(job.n))
    assert len(plan.inspection["initial_layout"]) == job.n
    assert plan.compile_record["basis"] == BASIS


def test_fake_fez_unavailable_raises_target_unavailable(four, monkeypatch):
    monkeypatch.setitem(sys.modules, "qiskit_ibm_runtime.fake_provider", None)
    with pytest.raises(TargetUnavailable):
        compile_program(replace(four, target="fake_fez"))


def test_fake_fez_records_the_isa_basis(four):
    pytest.importorskip("qiskit_ibm_runtime")
    plan = compile_program(replace(four, target="fake_fez"))
    ops = set(plan.isa.count_ops()) - {"measure", "barrier"}
    assert plan.coupling_map == "fake_fez" and plan.compile_record["basis"] == sorted(ops)
