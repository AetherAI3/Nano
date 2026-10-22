import json
import math

import pytest

from nano_qsearch.canonical import (
    CanonicalizationError, canonical_digest, canonical_json, validate_digest,
)


def test_golden_digest_matches_predator(fixtures_dir):
    golden = json.loads((fixtures_dir / "canonical_golden.json").read_text(encoding="utf-8"))
    assert canonical_digest(golden["payload"]) == golden["digest"]


def test_job_digest_of_fixture_matches_predator(fixtures_dir):
    golden = json.loads((fixtures_dir / "canonical_golden.json").read_text(encoding="utf-8"))
    job = json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))
    unsigned = {k: v for k, v in job.items() if k != "job_digest"}
    assert canonical_digest(unsigned) == job["job_digest"] == golden["job_4var_digest"]


def test_sorted_compact_utf8():
    assert canonical_json({"b": 1, "a": [1.5, "é"]}) == '{"a":[1.5,"é"],"b":1}'


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -math.inf])
def test_non_finite_rejected(bad):
    with pytest.raises(CanonicalizationError):
        canonical_json({"x": bad})


def test_non_string_key_rejected():
    with pytest.raises(CanonicalizationError):
        canonical_json({1: "a"})


def test_unsupported_type_rejected():
    with pytest.raises(CanonicalizationError):
        canonical_json({"x": object()})


def test_tuples_become_lists_and_bools_stay_bools():
    assert canonical_json({"t": (1, 2), "b": True}) == '{"b":true,"t":[1,2]}'


def test_validate_digest():
    assert validate_digest("sha256:" + "0" * 64) == "sha256:" + "0" * 64
    for bad in ("sha1:" + "0" * 64, "sha256:" + "0" * 63, "sha256:" + "G" * 64, 5):
        with pytest.raises(ValueError):
            validate_digest(bad)
