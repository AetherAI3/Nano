import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nano_qsearch.job import parse_job
from nano_qsearch.receipt import HARDWARE_SEAL_PREFIX, SEAL_PREFIX, seal_receipt, verify_seal
from nano_qsearch.runtime_ibm import IbmRuntime
from nano_qsearch.runtime_mps import build_runtime

KEY = b"k" * 32


@pytest.fixture
def job(fixtures_dir):
    """A 4-variable job on ibm_fez with QPU seconds granted: the full gate applies."""
    j = parse_job(json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8")))
    return replace(j, target="ibm_fez", max_qpu_s=5)


@pytest.fixture
def dry_job(job):
    return replace(job, target="fake_fez", max_qpu_s=0, shots=6)


def _compiled(job):
    return SimpleNamespace(isa=None, compiled_plan_digest="sha256:" + "c" * 64, variant="ordinary", nuclei=[],
                           compile_record={"target": job.target, "coupling_map": job.target}, metrics={"native_2q": 0})


def _atlas_receipt(job, samples, *, target=None):
    return {"schema": "AqrcQSearchJobReceiptV1", "target": target or job.target, "backend_name": target or job.target,
            "job_id": "job-0001", "dry_run": job.target == "fake_fez", "status": "DONE",
            "pubs": [{"pub_index": 0, "register": "meas", "bit_order": "msb_first", "shots": len(samples),
                      "raw_samples": list(samples)}]}


@pytest.fixture
def spy():
    calls = []

    def compile_fn(job):
        calls.append(("compile", job.job_digest))
        return _compiled(job)

    def submit_fn(job, compiled, *, purpose):
        calls.append(("submit", compiled.compiled_plan_digest, purpose))
        # x = (1,0,1,1), display order "1101", is the fixture's exact minimum (-1.6).
        return _atlas_receipt(job, ["1101"] * (job.shots - 1) + ["0000"])
    return calls, compile_fn, submit_fn


def _open_gates(monkeypatch, tmp_path):
    monkeypatch.setenv("AETHER_AQRC_HW_GO", "1")
    auth = tmp_path / "authority.json"
    auth.write_text("{}")
    monkeypatch.setenv("AETHER_AQRC_AUTHORITY_PATH", str(auth))


# --- ibm_fez keeps its gates exactly -----------------------------------------------------------


def test_refuses_without_go_flag(job, spy, monkeypatch, tmp_path):
    calls, c, s = spy
    monkeypatch.delenv("AETHER_AQRC_HW_GO", raising=False)
    auth = tmp_path / "authority.json"
    auth.write_text("{}")
    monkeypatch.setenv("AETHER_AQRC_AUTHORITY_PATH", str(auth))
    body = IbmRuntime(compile_fn=c, submit_fn=s).run(job)
    assert body["status"] == "REFUSED" and "AETHER_AQRC_HW_GO" in body["reason"] and body["backend"] == {"kind": "none"}
    assert calls == []
    assert seal_receipt(body, KEY)["seal"].startswith(SEAL_PREFIX)      # refusals carry the plain seal


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
    _open_gates(monkeypatch, tmp_path)
    assert "max_qpu_s" in IbmRuntime(compile_fn=c, submit_fn=s).run(replace(job, max_qpu_s=0))["reason"]
    assert "target" in IbmRuntime(compile_fn=c, submit_fn=s).run(replace(job, target="heron_heavy_hex"))["reason"]
    assert calls == []


def test_gates_open_then_delegates_with_purpose(job, spy, monkeypatch, tmp_path):
    calls, c, s = spy
    _open_gates(monkeypatch, tmp_path)
    body = IbmRuntime(compile_fn=c, submit_fn=s, purpose="qcomms").run(job)
    assert body["status"] == "OK" and body["backend"]["kind"] == "ibm" and body["hardware"]["purpose"] == "qcomms"
    assert calls == [("compile", job.job_digest), ("submit", "sha256:" + "c" * 64, "qcomms")]


def test_delegate_exception_becomes_refused(job, monkeypatch, tmp_path):
    _open_gates(monkeypatch, tmp_path)

    def boom(job):
        raise RuntimeError("secret detail")
    body = IbmRuntime(compile_fn=boom, submit_fn=lambda j, c, *, purpose: None).run(job)
    assert body["status"] == "REFUSED" and body["reason"] == "ibm: RuntimeError" and "secret" not in json.dumps(body)


def test_build_runtime_ibm_refuses_by_default(job, monkeypatch):
    monkeypatch.delenv("AETHER_AQRC_HW_GO", raising=False)
    rt = build_runtime("ibm")
    assert isinstance(rt, IbmRuntime) and rt.run(job)["status"] == "REFUSED"
    assert build_runtime("ibm", purpose="qcomms").purpose == "qcomms"


def test_purpose_comes_from_env_and_is_validated(job, spy, monkeypatch, tmp_path):
    calls, c, s = spy
    _open_gates(monkeypatch, tmp_path)
    monkeypatch.setenv("NANO_QSEARCH_PURPOSE", "qcomms")
    assert IbmRuntime(compile_fn=c, submit_fn=s).purpose == "qcomms"
    body = IbmRuntime(compile_fn=c, submit_fn=s, purpose="research").run(job)
    assert body["status"] == "REFUSED" and "purpose" in body["reason"] and calls == []


# --- fake_fez is a dry run: no spend gates, same code path, hardware seal ------------------------


def test_fakefez_dry_run_needs_no_go_and_yields_a_hardware_sealed_ordered_receipt(dry_job, spy, monkeypatch):
    calls, c, s = spy
    monkeypatch.delenv("AETHER_AQRC_HW_GO", raising=False)
    monkeypatch.delenv("AETHER_AQRC_AUTHORITY_PATH", raising=False)
    body = IbmRuntime(compile_fn=c, submit_fn=s).run(dry_job)
    assert body["status"] == "OK" and body["backend"]["kind"] == "ibm" and body["backend"]["id"] == "fake_fez"
    hw = body["hardware"]
    assert hw == {"backend_name": "fake_fez", "target": "fake_fez", "purpose": "qsearch", "job_id": "job-0001",
                  "pub_index": 0, "register": "meas", "bit_order": "msb_first", "shots": 6,
                  "raw_samples": ["1101"] * 5 + ["0000"], "dry_run": True,
                  "atlas_receipt_sha256": hw["atlas_receipt_sha256"]}
    assert body["samples"]["bit_order"] == "x0_first" and body["samples"]["top"][0]["bits"] == "1011"
    assert body["audit"]["agree"] is True and body["audit"]["best_sample_energy"] == pytest.approx(-1.6)
    sealed = seal_receipt(body, KEY)
    assert sealed["seal"].startswith(HARDWARE_SEAL_PREFIX)
    verify_seal(sealed, KEY)
    assert calls == [("compile", dry_job.job_digest), ("submit", "sha256:" + "c" * 64, "qsearch")]


@pytest.mark.parametrize("mutate", [
    lambda r, j: r["pubs"][0]["raw_samples"].pop(),                       # fewer samples than shots
    lambda r, j: r["pubs"][0].update(bit_order="x0_first"),
    lambda r, j: r["pubs"][0].update(raw_samples=["00111"] * j.shots),    # wrong width
    lambda r, j: r.update(target="ibm_fez"),
    lambda r, j: r.update(job_id=""),
    lambda r, j: r.update(pubs=r["pubs"] * 2),
    lambda r, j: r.update(schema="CampaignResultV1"),
])
def test_malformed_delegate_receipt_is_refused_not_sealed_as_ok(dry_job, mutate):
    def submit_fn(job, compiled, *, purpose):
        receipt = _atlas_receipt(job, ["1101"] * job.shots)
        mutate(receipt, job)
        return receipt
    body = IbmRuntime(compile_fn=_compiled, submit_fn=submit_fn).run(dry_job)
    assert body["status"] == "REFUSED" and body["reason"] == "ibm: ReceiptAuditError"


def test_sample_below_classical_floor_is_refused(dry_job):
    low = replace(dry_job, classical_min_energy=0.0)        # floor above the sampled energy

    def submit_fn(job, compiled, *, purpose):
        return _atlas_receipt(job, ["1101"] * job.shots)
    body = IbmRuntime(compile_fn=_compiled, submit_fn=submit_fn).run(low)
    assert body["status"] == "REFUSED" and body["reason"] == "ibm: ReceiptAuditError"


def test_default_submit_blocks_ibm_fez_without_a_campaign_stage(job):
    pytest.importorskip("aether_atlas")
    qiskit = pytest.importorskip("qiskit")
    from nano_qsearch.runtime_ibm import atlas_campaign_submit
    qc = qiskit.QuantumCircuit(job.n)
    qc.measure_all()
    compiled = SimpleNamespace(isa=qc, compiled_plan_digest="sha256:" + "c" * 64)
    with pytest.raises(RuntimeError, match="BLOCKED"):
        atlas_campaign_submit(job, compiled, purpose="qsearch")
