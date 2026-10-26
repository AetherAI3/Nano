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

    def submit_fn(job, compiled, *, purpose, environ):
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
    body = IbmRuntime(compile_fn=boom, submit_fn=lambda j, c, *, purpose, environ: None).run(job)
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
    lambda r, j: r.update(dry_run=False),                                 # fake_fez claiming a real run
])
def test_malformed_delegate_receipt_is_refused_not_sealed_as_ok(dry_job, mutate):
    def submit_fn(job, compiled, *, purpose, environ):
        receipt = _atlas_receipt(job, ["1101"] * job.shots)
        mutate(receipt, job)
        return receipt
    body = IbmRuntime(compile_fn=_compiled, submit_fn=submit_fn).run(dry_job)
    assert body["status"] == "REFUSED" and body["reason"] == "ibm: ReceiptAuditError"


def test_ibm_fez_receipt_claiming_a_dry_run_is_refused(job, monkeypatch, tmp_path):
    _open_gates(monkeypatch, tmp_path)

    def submit_fn(job, compiled, *, purpose, environ):
        return {**_atlas_receipt(job, ["1101"] * job.shots), "dry_run": True}
    body = IbmRuntime(compile_fn=_compiled, submit_fn=submit_fn).run(job)
    assert body["status"] == "REFUSED" and body["reason"] == "ibm: ReceiptAuditError"


def test_sample_below_classical_floor_is_refused(dry_job):
    low = replace(dry_job, classical_min_energy=0.0)        # floor above the sampled energy

    def submit_fn(job, compiled, *, purpose, environ):
        return _atlas_receipt(job, ["1101"] * job.shots)
    body = IbmRuntime(compile_fn=_compiled, submit_fn=submit_fn).run(low)
    assert body["status"] == "REFUSED" and body["reason"] == "ibm: ReceiptAuditError"


def test_ibm_body_without_hardware_block_never_gets_a_seal(dry_job):
    from nano_qsearch.receipt import ReceiptAuditError, SealError
    body = {"schema": "predator.qsearch.receipt.v1", "job_digest": dry_job.job_digest, "status": "OK",
            "backend": {"kind": "ibm", "id": "fake_fez"}, "audit": {"agree": True}}
    with pytest.raises(ReceiptAuditError, match="hardware block"):
        seal_receipt(body, KEY)
    import hashlib
    import hmac
    from nano_qsearch.canonical import canonical_json
    plain = {**body, "seal": SEAL_PREFIX + hmac.new(KEY, canonical_json(body).encode(), hashlib.sha256).hexdigest()}
    with pytest.raises(SealError, match="hardware block"):
        verify_seal(plain, KEY)


def _ibm_fez_fixture(job, tmp_path):
    """A real campaign ledger and a plan-scoped authority token for this exact ibm_fez child."""
    import time
    qiskit = pytest.importorskip("qiskit")
    from aether_atlas.aqrc import campaign_runtime
    from aether_atlas.aqrc.campaign import CampaignLedger, authorize_execution, protocol_document
    qc = qiskit.QuantumCircuit(job.n)
    qc.measure_all()
    compiled = SimpleNamespace(isa=qc, compiled_plan_digest="sha256:" + "c" * 64)
    child = campaign_runtime.prepare_qsearch_child(
        qc, purpose="qsearch", target=job.target, shots=job.shots, job_digest=job.job_digest,
        compiled_plan_digest=compiled.compiled_plan_digest, request_id=job.job_digest[len("sha256:"):][:40])
    protocol = protocol_document(campaign_id="l4", instance="test-instance", backend="ibm_fez",
                                 capability_mode="standard", cohort_sha256="a" * 64, scorer_sha256="b" * 64)
    ledger = CampaignLedger.create(tmp_path / "ledger.sqlite", protocol, confirmed_start_seconds=600)
    key = b"offline-test-host-key-never-used-for-hardware"
    token = authorize_execution(key=key, ledger=ledger, scope="plan", subject_sha256=child["child_sha256"],
                                max_seconds=60, expires_at=int(time.time()) + 60, operator="offline-test")
    (tmp_path / "authority.json").write_text(json.dumps(token), encoding="utf-8")
    (tmp_path / "authority.key").write_bytes(key)
    environ = {"AETHER_AQRC_HW_GO": "1", "AETHER_AQRC_AUTHORITY_PATH": str(tmp_path / "authority.json"),
               "AETHER_AQRC_AUTHORITY_KEY_PATH": str(tmp_path / "authority.key"),
               "AETHER_AQRC_LEDGER_PATH": str(tmp_path / "ledger.sqlite")}
    return compiled, ledger, token, environ


def test_default_submit_routes_ibm_fez_through_go_authority_and_cap_then_blocks(job, tmp_path, monkeypatch):
    pytest.importorskip("aether_atlas")
    from nano_qsearch.runtime_ibm import atlas_campaign_submit
    compiled, ledger, token, environ = _ibm_fez_fixture(job, tmp_path)
    monkeypatch.setenv("AETHER_AQRC_HW_GO", "1")                       # ATLAS reads the GO from os.environ
    with pytest.raises(RuntimeError, match="no reservable q-search stage"):
        atlas_campaign_submit(job, compiled, purpose="qsearch", environ=environ)
    assert ledger.totals()["jobs"] == 0
    monkeypatch.delenv("AETHER_AQRC_HW_GO")
    with pytest.raises(RuntimeError, match="AETHER_AQRC_HW_GO"):
        atlas_campaign_submit(job, compiled, purpose="qsearch", environ=environ)
    monkeypatch.setenv("AETHER_AQRC_HW_GO", "1")
    (tmp_path / "authority.json").write_text(json.dumps({**token, "hmac_sha256": "0" * 64}), encoding="utf-8")
    with pytest.raises(ValueError, match="signature differs"):
        atlas_campaign_submit(job, compiled, purpose="qsearch", environ=environ)
    with pytest.raises(RuntimeError, match="AETHER_AQRC_LEDGER_PATH"):
        atlas_campaign_submit(job, compiled, purpose="qsearch", environ={**environ, "AETHER_AQRC_LEDGER_PATH": ""})
    # Through the runtime every one of these is a REFUSED receipt naming the class only.
    body = IbmRuntime(compile_fn=lambda j: compiled, environ={**environ, "AETHER_AQRC_AUTHORITY_KEY_PATH": ""}).run(job)
    assert body["status"] == "REFUSED" and body["reason"] == "ibm: RuntimeError" and ledger.totals()["jobs"] == 0
