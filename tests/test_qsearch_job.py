# tests/test_qsearch_job.py
import copy
import json

import pytest

from nano.loop.qsearch_canonical import canonical_digest
from nano.loop.qsearch_job import JobValidationError, QSearchJob, parse_job


@pytest.fixture
def body(fixtures_dir):
    return json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))


def _resigned(body):
    """Return a copy whose job_digest matches its (possibly edited) content."""
    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    return {**unsigned, "job_digest": canonical_digest(unsigned)}


def test_parses_fixture(body):
    job = parse_job(body)
    assert isinstance(job, QSearchJob)
    assert job.n == 4 and job.variables == ("c0", "c1", "c2", "c3")
    assert job.q[0][1] == job.q[1][0] and job.job_digest == body["job_digest"]
    assert job.variant == "auto" and job.backend_kind == "auto" and job.target == "heron_heavy_hex"
    assert job.payload["schema"] == "predator.qsearch.job.v1"
    with pytest.raises(TypeError):
        job.payload["schema"] = "x"          # frozen view


def test_energy_matches_quadratic_form(body):
    job = parse_job(body)
    x = (1, 0, 1, 1)
    expected = sum(job.q[i][j] * x[i] * x[j] for i in range(4) for j in range(4)) + job.offset
    assert abs(job.energy(x) - expected) <= 1e-12
    with pytest.raises(ValueError):
        job.energy((1, 0, 1))


def _bad(body, mutate):
    b = copy.deepcopy(body)
    mutate(b)
    return _resigned(b)


@pytest.mark.parametrize("mutate, code", [
    (lambda b: b.__setitem__("schema", "nope"), "SCHEMA"),
    (lambda b: b.__setitem__("variables", ["a", "a", "b", "c"]), "VARIABLES"),
    (lambda b: b.__setitem__("variables", []), "VARIABLES"),
    (lambda b: b["qubo"]["q"].pop(), "QUBO"),
    (lambda b: b["qubo"]["q"][0].__setitem__(1, 99.0), "QUBO"),            # asymmetric
    (lambda b: b["qubo"]["q"][0].__setitem__(0, True), "QUBO"),            # bool is not a number
    (lambda b: b["qubo"].__setitem__("convention", "upper"), "QUBO"),
    (lambda b: b["qubo"].__setitem__("offset", "0"), "QUBO"),
    (lambda b: b["compile"].__setitem__("variant", "qaoa"), "COMPILE"),
    (lambda b: b["compile"].__setitem__("gammas", [0.1, 0.2]), "COMPILE"),  # p=1 but two angles
    (lambda b: b["compile"].__setitem__("opt_level", 4), "COMPILE"),
    (lambda b: b["compile"].__setitem__("training", "cobyla"), "COMPILE"),
    (lambda b: b["compile"].__setitem__("target", "rigetti"), "COMPILE"),
    (lambda b: b["backend"].__setitem__("kind", "gpu"), "BACKEND"),
    (lambda b: b["backend"].__setitem__("shots", 0), "BACKEND"),
    (lambda b: b["backend"].__setitem__("chi_max", 1.5), "BACKEND"),
    (lambda b: b["limits"].__setitem__("max_variables", 3), "LIMITS"),     # 4 variables > 3
    (lambda b: b["limits"].__setitem__("max_wall_s", -1), "LIMITS"),
    (lambda b: b["audit"].__setitem__("classical_min_energy", "low"), "AUDIT"),
    (lambda b: b.__setitem__("compiled_search_digest", "sha256:short"), "DIGEST"),
    (lambda b: b.__setitem__("extra", 1), "SCHEMA"),                        # unknown top-level key
])
def test_rejections(body, mutate, code):
    with pytest.raises(JobValidationError) as info:
        parse_job(_bad(body, mutate))
    assert info.value.code == code


def test_digest_mismatch_rejected(body):
    body = copy.deepcopy(body)
    body["job_digest"] = "sha256:" + "0" * 64
    with pytest.raises(JobValidationError) as info:
        parse_job(body)
    assert info.value.code == "DIGEST"


def test_non_finite_entries_rejected_before_digest(body):
    body = copy.deepcopy(body)
    body["qubo"]["q"][0][0] = float("nan")
    with pytest.raises(JobValidationError) as info:
        parse_job(body)
    assert info.value.code == "QUBO"


def test_non_mapping_rejected():
    with pytest.raises(JobValidationError):
        parse_job(["not", "a", "job"])
