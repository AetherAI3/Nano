# tests/test_qsearch_ibm_fakefez.py
"""Gate G-W4 evidence: a real FakeFez dry run through the Nano service, checked by Predator.

The job is built by Predator's own bridge from a 4-candidate compiled problem,
served by a real ``QSearchService`` on ``backend=ibm``, compiled by
``nano_qsearch.compile`` and run through ATLAS ``campaign_runtime.run_qsearch_dry``
(``SamplerV2`` local testing mode on ``FakeFez``). Predator's
``NanoRuntimeQSearchBackend`` verifies the hardware seal and
``CfcUnitConvergenceV1._select_hardware`` decodes the ordered raw samples and
selects only under a verifying grant. Skips only when
``NANO_QSEARCH_PREDATOR_PATH`` is unset (``conftest`` adds it and
``NANO_QSEARCH_ATLAS_PATH`` to ``sys.path``); with it set, a missing Predator,
ATLAS or qiskit-ibm-runtime module is an error, never a skip.
"""
import contextlib
import json
import os
import secrets
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.engine]
_PREDATOR = os.environ.get("NANO_QSEARCH_PREDATOR_PATH")
if not _PREDATOR:
    pytest.skip("NANO_QSEARCH_PREDATOR_PATH not set", allow_module_level=True)
if not (Path(_PREDATOR) / "predator" / "drive" / "qsearch_hw_grant.py").is_file():
    pytest.fail(f"NANO_QSEARCH_PREDATOR_PATH={_PREDATOR!r} is not a lane-L4 Predator-M checkout "
                "(no predator/drive/qsearch_hw_grant.py)", pytrace=False)

import aether_atlas  # noqa: E402,F401
import qiskit_ibm_runtime  # noqa: E402,F401
from predator.drive import execution, qsearch_backend  # noqa: E402
from predator.drive.cfc_convergence_v1 import CfcUnitConvergenceV1  # noqa: E402
from predator.drive.qsearch_hw_grant import HardwareGrantV1, seal_grant  # noqa: E402
from predator_gen3.discovery.compiler import CompiledSearchProblemV1  # noqa: E402
from predator_gen3.discovery.qsearch_bridge import QSearchPolicyV1, build_qsearch_job  # noqa: E402

from nano_qsearch.auth import AUTH_HEADER, sign_request                                  # noqa: E402
from nano_qsearch.receipt import HARDWARE_SEAL_PREFIX                                     # noqa: E402
from nano_qsearch.service import SUBMIT_PATH, QSearchService, ServiceConfig, serve_in_thread   # noqa: E402

RECEIPT_KEY = secrets.token_bytes(32)
REQUEST_KEY = secrets.token_bytes(32)
GRANT_KEY = b"g" * 48
SHOTS = 256
TIMEOUT_S = 900


def _compiled():
    return CompiledSearchProblemV1(
        proposal_digest="sha256:" + "a" * 64, candidate_ids=("c0", "c1", "c2", "c3"),
        linear=(-1.0, -0.8, -0.6, -0.4), quadratic=((0, 1, 0.9), (0, 2, 0.7), (1, 2, 0.5), (2, 3, -0.3)),
        penalty=4.5, lineage=tuple({"candidate_id": f"c{i}", "prerequisites": [], "exclusions": []} for i in range(4)))


def _job(compiled):
    policy = QSearchPolicyV1(backend_kind="ibm", target="fake_fez", shots=SHOTS, max_qpu_s=0)
    return build_qsearch_job(compiled, classical_min_energy=compiled.classical_minimum()[1], policy=policy)


def _grant(**overrides):
    stamp = lambda offset: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))
    body = {"schema": "predator.qsearch.hw-grant.v1", "grant_id": "l4-fakefez-e2e", "issued_at": stamp(-60),
            "expires_at": stamp(3600), "max_qpu_s": 0, "targets": ["fake_fez"], "purpose": "qsearch",
            "issuer": "l4-e2e", **overrides}
    return HardwareGrantV1.from_payload(seal_grant(body, GRANT_KEY), key=GRANT_KEY)


def _signing_opener(key):
    def opener(url, data, timeout):
        header = sign_request(key, "POST", SUBMIT_PATH, data, ts=int(time.time()), nonce=secrets.token_hex(16))
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", AUTH_HEADER: header})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    return opener


@contextlib.contextmanager
def _serving(tmp_path):
    cfg = ServiceConfig(bind="127.0.0.1:0", backend="ibm", data_dir=tmp_path, receipt_key=RECEIPT_KEY,
                        request_key=REQUEST_KEY)
    svc = QSearchService(cfg)
    server, thread = serve_in_thread(svc)
    try:
        yield svc, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        svc.close()


def _select(receipt, compiled, grant):
    """Run the unbound hardware selector with just the grant it reads from ``self``."""
    selected_exact, _ = compiled.classical_minimum()
    return CfcUnitConvergenceV1._select_hardware(SimpleNamespace(_hardware_grant=grant), compiled,
                                                 selected_exact, receipt)


def test_fakefez_dry_run_receipt_is_hardware_sealed_and_selects_under_a_grant(tmp_path, monkeypatch):
    monkeypatch.delenv("AETHER_AQRC_HW_GO", raising=False)          # a dry run needs no GO
    monkeypatch.delenv("AETHER_AQRC_AUTHORITY_PATH", raising=False)
    compiled, grant = _compiled(), _grant()
    job = _job(compiled)
    with _serving(tmp_path) as (svc, endpoint):
        backend = qsearch_backend.NanoRuntimeQSearchBackend(endpoint, key=RECEIPT_KEY, timeout_s=TIMEOUT_S,
                                                           opener=_signing_opener(REQUEST_KEY))
        receipt = backend.submit(job)
        assert receipt.status == "OK", receipt.payload
        assert receipt.backend_kind == "ibm" and receipt.payload["seal"].startswith(HARDWARE_SEAL_PREFIX)
        hw = receipt.payload["hardware"]
        assert hw["backend_name"] == hw["target"] == "fake_fez" and hw["dry_run"] is True and hw["job_id"]
        assert hw["purpose"] == "qsearch" and hw["pub_index"] == 0 and hw["register"] == "meas"
        assert hw["bit_order"] == "msb_first" and hw["shots"] == SHOTS == len(hw["raw_samples"])
        assert all(len(bits) == 4 and set(bits) <= {"0", "1"} for bits in hw["raw_samples"])
        assert receipt.payload["compile"]["target"] == "fake_fez" and receipt.payload["metrics"]["native_2q"] > 0
        assert svc.store.get(job.job_digest).state == "DONE"

        # The ordered raw samples decode to the exact minimum under hardware_select.
        selected_exact, exact_energy = compiled.classical_minimum()
        assert receipt.agree is True and receipt.payload["audit"]["best_sample_energy"] == pytest.approx(exact_energy)
        assert _select(receipt, compiled, grant) == selected_exact

        # The same receipt never selects without a covering grant.
        with pytest.raises(execution.StageRefusal, match="does not cover purpose"):
            _select(receipt, compiled, _grant(purpose="qcomms"))
        with pytest.raises(execution.StageRefusal) as refusal:
            _select(receipt, compiled, None)
        assert refusal.value.code == "HARDWARE_GRANT_REQUIRED"


def test_plain_sealed_or_forged_mps_hardware_receipt_is_refused_by_predator(tmp_path):
    compiled = _compiled()
    job = _job(compiled)
    body = json.dumps(job.to_payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    with _serving(tmp_path) as (_svc, endpoint):
        payload = json.loads(_signing_opener(REQUEST_KEY)(endpoint + SUBMIT_PATH, body, TIMEOUT_S))
    assert payload["seal"].startswith(HARDWARE_SEAL_PREFIX)
    plain = qsearch_backend.seal_receipt_payload(payload, RECEIPT_KEY)        # re-sealed with the plain seal
    with pytest.raises(execution.StageRefusal, match="must carry the hardware seal"):
        qsearch_backend.QSearchReceiptV1.from_payload(plain, key=RECEIPT_KEY, expected_job_digest=job.job_digest)
    forged = {**payload, "backend": {**payload["backend"], "kind": "mps"}}     # hardware seal, MPS label
    with pytest.raises(execution.StageRefusal) as refusal:
        qsearch_backend.QSearchReceiptV1.from_payload(forged, key=RECEIPT_KEY, expected_job_digest=job.job_digest)
    assert refusal.value.code == "QSEARCH_SEAL_MISMATCH"
    unsealed = {k: v for k, v in payload.items() if k != "seal"}
    with pytest.raises(execution.StageRefusal) as refusal:
        qsearch_backend.QSearchReceiptV1.from_payload(unsealed, key=RECEIPT_KEY, expected_job_digest=job.job_digest)
    assert refusal.value.code == "QSEARCH_SEAL_MISMATCH"
