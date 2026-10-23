import json
from dataclasses import replace

import pytest

from nano_qsearch.job import parse_job
from nano_qsearch.runtime_ibm import IbmRuntime
from nano_qsearch.runtime_mps import build_runtime


@pytest.fixture
def job(fixtures_dir):
    j = parse_job(json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8")))
    return replace(j, target="fake_fez", max_qpu_s=5)


@pytest.fixture
def spy():
    calls = []

    def compile_fn(job):
        calls.append(("compile", job.job_digest))
        return "compiled"

    def submit_fn(job, compiled):
        calls.append(("submit", compiled))
        return {"schema": "x", "status": "OK"}
    return calls, compile_fn, submit_fn


def test_refuses_without_go_flag(job, spy, monkeypatch, tmp_path):
    calls, c, s = spy
    monkeypatch.delenv("AETHER_AQRC_HW_GO", raising=False)
    auth = tmp_path / "authority.json"
    auth.write_text("{}")
    monkeypatch.setenv("AETHER_AQRC_AUTHORITY_PATH", str(auth))
    body = IbmRuntime(compile_fn=c, submit_fn=s).run(job)
    assert body["status"] == "REFUSED" and "AETHER_AQRC_HW_GO" in body["reason"] and body["backend"] == {"kind": "none"}
    assert calls == []


def test_refuses_without_authority_file(job, spy, monkeypatch, tmp_path):
    calls, c, s = spy
    monkeypatch.setenv("AETHER_AQRC_HW_GO", "1")
    monkeypatch.setenv("AETHER_AQRC_AUTHORITY_PATH", str(tmp_path / "missing.json"))
    assert "AUTHORITY_PATH" in IbmRuntime(compile_fn=c, submit_fn=s).run(job)["reason"]
    monkeypatch.delenv("AETHER_AQRC_AUTHORITY_PATH")
    assert "AUTHORITY_PATH" in IbmRuntime(compile_fn=c, submit_fn=s).run(job)["reason"]
    assert calls == []


def test_refuses_zero_qpu_budget_and_wrong_target(job, spy, monkeypatch, tmp_path):
    calls, c, s = spy
    monkeypatch.setenv("AETHER_AQRC_HW_GO", "1")
    auth = tmp_path / "authority.json"
    auth.write_text("{}")
    monkeypatch.setenv("AETHER_AQRC_AUTHORITY_PATH", str(auth))
    assert "max_qpu_s" in IbmRuntime(compile_fn=c, submit_fn=s).run(replace(job, max_qpu_s=0))["reason"]
    assert "target" in IbmRuntime(compile_fn=c, submit_fn=s).run(replace(job, target="heron_heavy_hex"))["reason"]
    assert calls == []


def test_gates_open_then_delegates(job, spy, monkeypatch, tmp_path):
    calls, c, s = spy
    monkeypatch.setenv("AETHER_AQRC_HW_GO", "1")
    auth = tmp_path / "authority.json"
    auth.write_text("{}")
    monkeypatch.setenv("AETHER_AQRC_AUTHORITY_PATH", str(auth))
    assert IbmRuntime(compile_fn=c, submit_fn=s).run(job) == {"schema": "x", "status": "OK"}
    assert calls == [("compile", job.job_digest), ("submit", "compiled")]


def test_delegate_exception_becomes_refused(job, monkeypatch, tmp_path):
    monkeypatch.setenv("AETHER_AQRC_HW_GO", "1")
    auth = tmp_path / "authority.json"
    auth.write_text("{}")
    monkeypatch.setenv("AETHER_AQRC_AUTHORITY_PATH", str(auth))

    def boom(job):
        raise RuntimeError("secret detail")
    body = IbmRuntime(compile_fn=boom, submit_fn=lambda j, c: None).run(job)
    assert body["status"] == "REFUSED" and body["reason"] == "ibm: RuntimeError" and "secret" not in json.dumps(body)


def test_build_runtime_ibm_refuses_by_default(job, monkeypatch):
    monkeypatch.delenv("AETHER_AQRC_HW_GO", raising=False)
    rt = build_runtime("ibm")
    assert isinstance(rt, IbmRuntime) and rt.run(job)["status"] == "REFUSED"


def test_default_submit_refuses_unqualified_purpose(job):
    pytest.importorskip("aether_atlas")
    from nano_qsearch.runtime_ibm import atlas_campaign_submit
    body = atlas_campaign_submit(job, None)
    assert body["status"] == "REFUSED" and "campaign_runtime" in body["reason"]
