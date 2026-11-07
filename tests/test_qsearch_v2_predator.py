"""Lane HW0 seal parity: Nano's v2 receipts verify under Predator's production verifier (S1) and client (S3).

Runs only when Predator's ``predator.drive.qsearch_receipt_v2`` is importable (its
checkout on ``PYTHONPATH`` or ``NANO_QSEARCH_PREDATOR_PATH``); skipped otherwise.
The ATLAS delegate is a stand-in, so nothing here needs Aer, ATLAS or IBM: what is
tested is that the body Nano builds and seals is exactly what Predator accepts, and
that Predator refuses every altered, reordered or cross-version receipt.
"""
import copy
import json
import secrets
import time
import urllib.request

import pytest

receipt_v2 = pytest.importorskip("predator.drive.qsearch_receipt_v2")
from predator.drive import qsearch_backend  # noqa: E402
from predator.drive.execution import StageRefusal  # noqa: E402
from predator.drive.qsearch_packed_grant import PackedGrantV1, seal_packed_grant  # noqa: E402
from predator_gen3.canonical import canonical_json as predator_canonical_json  # noqa: E402
from predator_gen3.discovery import qsearch_bridge  # noqa: E402

from nano_qsearch.auth import AUTH_HEADER, sign_request  # noqa: E402
from nano_qsearch.canonical import canonical_json  # noqa: E402
from nano_qsearch.request import parse_request  # noqa: E402
from nano_qsearch.runtime_ibm import IbmRuntime  # noqa: E402
from nano_qsearch.service import REQUEST_PATH, QSearchService, ServiceConfig, serve_in_thread  # noqa: E402
from qsearch_v2_support import atlas_request_receipt, request_body  # noqa: E402

pytestmark = pytest.mark.integration
RECEIPT_KEY = secrets.token_bytes(32)
REQUEST_KEY = secrets.token_bytes(32)
GRANT_KEY = b"g" * 48


def _delegate(fail=None):
    def submit(request, compiled, **kw):
        if fail is not None:
            raise fail
        return atlas_request_receipt(request, usage=2.0)
    return submit


def _service(data_dir, submit, environ=None):
    runtime = IbmRuntime(compile_fn=lambda job: None, request_submit_fn=submit, environ=environ or {})
    cfg = ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=data_dir, receipt_key=RECEIPT_KEY,
                        request_key=REQUEST_KEY, v2_enabled=True)
    return QSearchService(cfg, runtime_factory=lambda kind: runtime)


def _sealed(data_dir, body, submit, environ=None):
    svc = _service(data_dir, submit, environ)
    try:
        raw = json.dumps(body, sort_keys=True).encode("utf-8")
        headers = {AUTH_HEADER: sign_request(REQUEST_KEY, "POST", REQUEST_PATH, raw, ts=1000, nonce="ab" * 16)}
        status, receipt = svc.submit_request("POST", REQUEST_PATH, raw, headers, now=1000)
    finally:
        svc.close()
    assert status == 200
    return receipt


def test_descriptor_digest_and_canonical_json_agree_with_predator():
    body = request_body(3)
    expected = receipt_v2.ExpectedRequestV2.from_descriptor(body["descriptor"])
    assert expected.request_digest == parse_request(body).request_digest
    assert predator_canonical_json(body) == canonical_json(body)


@pytest.mark.parametrize("target", ["fake_fez", "ibm_fez"])
def test_ok_receipts_verify_under_predator(tmp_path, target):
    authority = tmp_path / "authority.json"
    authority.write_text("{}", encoding="utf-8")              # Nano's gate needs the file; the delegate is fake
    environ = {"AETHER_AQRC_HW_GO": "1", "AETHER_AQRC_QSEARCH_LIVE": "1", "AETHER_AQRC_QSEARCH_PACKED": "1",
               "AETHER_AQRC_AUTHORITY_PATH": str(authority)}
    body = request_body(3, target=target, max_qpu_s=5, max_execution_time=10)
    receipt = _sealed(tmp_path / "svc", body, _delegate(), environ)
    assert receipt["status"] == "OK", receipt
    expected = receipt_v2.ExpectedRequestV2.from_descriptor(body["descriptor"])
    jobs = [_predator_job(job) for job in body["jobs"]]
    verified = receipt_v2.verify_qsearch_receipt(receipt, key=RECEIPT_KEY, expected=expected, jobs=jobs)
    assert verified.status == "OK" and len(verified.blocks) == 3
    assert [b.job_digest for b in verified.blocks] == [j["job_digest"] for j in body["jobs"]]
    assert all(b.agree for b in verified.blocks)
    assert verified.usage["usage_pending"] is (target == "fake_fez")

    def refused(mutate):
        tampered = copy.deepcopy(receipt)
        mutate(tampered)
        with pytest.raises(StageRefusal):
            receipt_v2.verify_qsearch_receipt(tampered, key=RECEIPT_KEY, expected=expected)
    refused(lambda r: r["blocks"].reverse())
    refused(lambda r: r["blocks"].pop())
    refused(lambda r: r["blocks"].__setitem__(1, r["blocks"][0]))
    refused(lambda r: r["blocks"][0]["counts"].update({"0000": 99}))
    refused(lambda r: r["request"].update(request_digest="sha256:" + "0" * 64))
    refused(lambda r: r.update(seal=r["seal"].replace("hardware-v2", "hardware")))
    other = receipt_v2.ExpectedRequestV2.from_descriptor(
        request_body(3, target=target, max_qpu_s=5, request_id="other")["descriptor"])
    with pytest.raises(StageRefusal):
        receipt_v2.verify_qsearch_receipt(receipt, key=RECEIPT_KEY, expected=other)


def test_not_run_receipts_verify_under_predator(tmp_path):
    body = request_body(3)
    receipt = _sealed(tmp_path, body, _delegate(fail=MemoryError()))
    expected = receipt_v2.ExpectedRequestV2.from_descriptor(body["descriptor"])
    verified = receipt_v2.verify_qsearch_receipt(receipt, key=RECEIPT_KEY, expected=expected)
    assert verified.status == "NOT_RUN" and verified.blocks == () and verified.reason == "ibm: MemoryError"


def test_v1_and_v2_are_never_confused(tmp_path):
    body = request_body(2)
    receipt = _sealed(tmp_path, body, _delegate())
    with pytest.raises(StageRefusal) as refusal:
        receipt_v2.verify_qsearch_receipt(receipt, key=RECEIPT_KEY,
                                          expected=receipt_v2.ExpectedJobV1(job_digest=body["jobs"][0]["job_digest"]))
    assert refusal.value.code == "QSEARCH_RECEIPT_VERSION"


# --- S3: Predator's own client over loopback -----------------------------------------------------


def _predator_job(body):
    c, b, lim = body["compile"], body["backend"], body["limits"]
    policy = qsearch_bridge.QSearchPolicyV1(
        variant=c["variant"], p=c["p"], gammas=tuple(c["gammas"]), betas=tuple(c["betas"]), opt_level=c["opt_level"],
        seed=c["seed"], target=c["target"], training=c["training"], backend_kind=b["kind"], chi_max=b["chi_max"],
        shots=b["shots"], backend_seed=b["seed"], max_variables=lim["max_variables"], max_wall_s=lim["max_wall_s"],
        max_qpu_s=lim["max_qpu_s"])
    job = qsearch_bridge.QSearchJobV1(compiled_search_digest=body["compiled_search_digest"],
                                      variables=tuple(body["variables"]), qubo=tuple(tuple(r) for r in body["qubo"]["q"]),
                                      offset=body["qubo"]["offset"], policy=policy,
                                      classical_min_energy=body["audit"]["classical_min_energy"])
    assert job.to_payload() == body
    return job


def _signing_opener(key):
    def opener(url, data, timeout):
        path = "/" + url.split("/", 3)[3]
        header = sign_request(key, "POST", path, data, ts=int(time.time()), nonce=secrets.token_hex(16))
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", AUTH_HEADER: header})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    return opener


def _stamp(offset):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))


def test_predator_submit_request_round_trips_through_nano(tmp_path):
    body = request_body(3)
    jobs = [_predator_job(job) for job in body["jobs"]]
    expected = receipt_v2.ExpectedRequestV2.from_descriptor(body["descriptor"])
    grant = PackedGrantV1.from_payload(seal_packed_grant(
        {"schema": "predator.qsearch.packed-grant.v1", "grant_id": "hw0-nano", "issued_at": _stamp(-60),
         "expires_at": _stamp(3600), "request_digest": expected.request_digest, "target": "fake_fez",
         "pub_count": 3, "max_execution_time": 10, "max_qpu_s": 0, "issuer": "nano-test"}, GRANT_KEY), key=GRANT_KEY)
    svc = _service(tmp_path, _delegate())
    server, thread = serve_in_thread(svc)
    try:
        client = qsearch_backend.NanoRuntimeQSearchBackend(f"http://127.0.0.1:{server.server_port}",
                                                          key=RECEIPT_KEY, opener=_signing_opener(REQUEST_KEY))
        with pytest.raises(StageRefusal):                    # PREDATOR_QSEARCH_PACKED missing: nothing sent
            client.submit_request(expected, jobs, grant=grant, environ={})
        assert svc.store.get_request(expected.request_digest) is None
        verified = client.submit_request(expected, jobs, grant=grant, environ={"PREDATOR_QSEARCH_PACKED": "1"})
        assert verified.status == "OK", verified.reason
        assert [b.block_index for b in verified.blocks] == [0, 1, 2]
        assert svc.store.get_request(expected.request_digest).state == "DONE"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        svc.close()


# --- R3 H1: FAILED parity ------------------------------------------------------------------------

_HAS_FAILED = "FAILED" in getattr(receipt_v2, "_STATUSES", ())


def _failed_atlas_receipt(request):
    receipt = atlas_request_receipt(request, usage=0.7)
    receipt.update(status="FAILED", pubs=[], provider_status="ERROR", reason="provider job ended ERROR")
    return receipt


@pytest.mark.skipif(not _HAS_FAILED, reason="Predator checkout predates the R3 FAILED status")
def test_failed_receipts_verify_under_predator(tmp_path):
    authority = tmp_path / "authority.json"
    authority.write_text("{}", encoding="utf-8")
    environ = {"AETHER_AQRC_HW_GO": "1", "AETHER_AQRC_QSEARCH_LIVE": "1", "AETHER_AQRC_QSEARCH_PACKED": "1",
               "AETHER_AQRC_AUTHORITY_PATH": str(authority)}
    body = request_body(3, target="ibm_fez", max_qpu_s=5, max_execution_time=10)
    receipt = _sealed(tmp_path / "svc", body, lambda request, compiled, **kw: _failed_atlas_receipt(request), environ)
    assert receipt["status"] == "FAILED"
    expected = receipt_v2.ExpectedRequestV2.from_descriptor(body["descriptor"])
    verified = receipt_v2.verify_qsearch_receipt(receipt, key=RECEIPT_KEY, expected=expected)
    assert verified.status == "FAILED" and verified.blocks == () and verified.reason == "provider: ERROR"
    assert verified.hardware["provider_job_id"] == "fake-job-1" and verified.usage["quantum_seconds"] == 0.7
    tampered = copy.deepcopy(receipt)
    tampered["status"] = "NOT_RUN"                                     # a submitted job never reads NOT_RUN
    with pytest.raises(StageRefusal):
        receipt_v2.verify_qsearch_receipt(tampered, key=RECEIPT_KEY, expected=expected)


@pytest.mark.skipif(not _HAS_FAILED, reason="Predator checkout predates the R3 FAILED status")
def test_predator_client_sees_pending_then_failed_through_nano(tmp_path):
    authority = tmp_path / "authority.json"
    authority.write_text("{}", encoding="utf-8")
    body = request_body(3, target="ibm_fez", max_qpu_s=5, max_execution_time=10)
    jobs = [_predator_job(job) for job in body["jobs"]]
    expected = receipt_v2.ExpectedRequestV2.from_descriptor(body["descriptor"])
    grant = PackedGrantV1.from_payload(seal_packed_grant(
        {"schema": "predator.qsearch.packed-grant.v1", "grant_id": "hw0-nano-r3", "issued_at": _stamp(-60),
         "expires_at": _stamp(3600), "request_digest": expected.request_digest, "target": "ibm_fez",
         "pub_count": 3, "max_execution_time": 10, "max_qpu_s": 15, "issuer": "nano-test"}, GRANT_KEY), key=GRANT_KEY)
    done = []

    class QSearchSubmittedPending(Exception):
        pass

    def submit(request, compiled, **kw):
        raise QSearchSubmittedPending("req-hw0", "fake-job-1")
    runtime = IbmRuntime(compile_fn=lambda job: None, request_submit_fn=submit,
                         request_recover_fn=lambda request, **kw: _failed_atlas_receipt(request) if done else None,
                         environ={"AETHER_AQRC_HW_GO": "1", "AETHER_AQRC_QSEARCH_LIVE": "1",
                                  "AETHER_AQRC_QSEARCH_PACKED": "1", "AETHER_AQRC_AUTHORITY_PATH": str(authority)})
    cfg = ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path / "svc", receipt_key=RECEIPT_KEY,
                        request_key=REQUEST_KEY, v2_enabled=True)
    svc = QSearchService(cfg, runtime_factory=lambda kind: runtime)
    server, thread = serve_in_thread(svc)
    try:
        client = qsearch_backend.NanoRuntimeQSearchBackend(f"http://127.0.0.1:{server.server_port}",
                                                          key=RECEIPT_KEY, opener=_signing_opener(REQUEST_KEY))
        packed = {"PREDATOR_QSEARCH_PACKED": "1"}
        with pytest.raises(StageRefusal) as pending:
            client.submit_request(expected, jobs, grant=grant, environ=packed)
        assert pending.value.code == "QSEARCH_PENDING"                 # 503 is pending, never an outcome
        assert svc.store.get_request(expected.request_digest).state == "ACCEPTED"
        done.append(True)
        verified = client.submit_request(expected, jobs, grant=grant, environ=packed)
        assert verified.status == "FAILED" and verified.blocks == () and verified.reason == "provider: ERROR"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        svc.close()
