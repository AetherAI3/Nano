import json
from dataclasses import replace

import pytest

pytestmark = pytest.mark.engine
pytest.importorskip("qiskit")
pytest.importorskip("aether_atlas")

from nano_qsearch.canonical import canonical_digest        # noqa: E402
from nano_qsearch.job import parse_job                      # noqa: E402
from nano_qsearch.compile import BASIS, compile_program, heavy_hex_map   # noqa: E402


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
    assert plan.nuclei == [[0, 1, 2], [0, 3, 4], [1, 3, 5], [2, 4, 5]] or plan.variant == "ordinary"
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


def test_digest_is_deterministic(four):
    assert compile_program(four).compiled_plan_digest == compile_program(four).compiled_plan_digest
