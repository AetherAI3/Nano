"""Lane HW0 N4: one real packed FakeFez dry run through ``/qsearch/v2/submit_request`` (Aer, no IBM).

The production runtime path end to end: Nano compiles three jobs for FakeFez, freezes
them as ATLAS children, prepares one request and runs ``run_qsearch_request_dry``
(one ``SamplerV2.run`` with three PUBs on Aer), then seals the v2 receipt. Skips unless
``NANO_QSEARCH_ATLAS_PATH`` names an ATLAS checkout with the HW0 request functions, and only
when ``NANO_QSEARCH_FAKEFEZ_DRY_RUN=1``: it is meant to run on its own (inside a loaded full
suite on this laptop Aer ran out of memory and the receipt was, correctly, NOT_RUN). Small on
purpose (4 variables, 16 shots per block); run with ``OMP_NUM_THREADS=1`` (Aer access-violated
once here without it, as ``runtime_mps`` notes for MPS).
"""
import json
import os

import pytest

pytestmark = pytest.mark.engine
if os.environ.get("NANO_QSEARCH_FAKEFEZ_DRY_RUN") != "1":
    pytest.skip("set NANO_QSEARCH_FAKEFEZ_DRY_RUN=1 to run the real FakeFez dry run (alone)", allow_module_level=True)
pytest.importorskip("qiskit_aer")
pytest.importorskip("qiskit_ibm_runtime")
campaign_runtime = pytest.importorskip("aether_atlas.aqrc.campaign_runtime")
if not hasattr(campaign_runtime, "run_qsearch_request_dry"):
    pytest.skip("ATLAS checkout predates the HW0 request functions", allow_module_level=True)

from nano_qsearch.auth import AUTH_HEADER, sign_request  # noqa: E402
from nano_qsearch.receipt_v2 import HARDWARE_SEAL_PREFIX_V2, verify_seal_v2  # noqa: E402
from nano_qsearch.request import parse_request  # noqa: E402
from nano_qsearch.service import REQUEST_PATH, QSearchService, ServiceConfig  # noqa: E402
from qsearch_v2_support import request_body  # noqa: E402

RECEIPT_KEY = b"receipt-key-" + b"0" * 20
REQUEST_KEY = b"request-key-" + b"1" * 20


def test_packed_fakefez_dry_run_returns_every_block_in_pub_order(tmp_path, monkeypatch):
    for name in ("AETHER_AQRC_HW_GO", "AETHER_AQRC_QSEARCH_LIVE", "AETHER_AQRC_QSEARCH_PACKED"):
        monkeypatch.delenv(name, raising=False)                    # a dry run needs none of them
    body = request_body(3, shots=16, max_qpu_s=0, max_execution_time=10)
    cfg = ServiceConfig(bind="127.0.0.1:0", backend="ibm", data_dir=tmp_path, receipt_key=RECEIPT_KEY,
                        request_key=REQUEST_KEY, v2_enabled=True)
    svc = QSearchService(cfg)                                      # the production runtime factory
    try:
        raw = json.dumps(body, sort_keys=True).encode("utf-8")
        headers = {AUTH_HEADER: sign_request(REQUEST_KEY, "POST", REQUEST_PATH, raw, ts=1000, nonce="ab" * 16)}
        status, receipt = svc.submit_request("POST", REQUEST_PATH, raw, headers, now=1000)
    finally:
        svc.close()
    assert status == 200 and receipt["status"] == "OK", receipt
    assert receipt["seal"].startswith(HARDWARE_SEAL_PREFIX_V2)
    verify_seal_v2(receipt, RECEIPT_KEY)
    assert receipt["request"] == {"request_digest": parse_request(body).request_digest, "block_count": 3}
    assert [b["job_digest"] for b in receipt["blocks"]] == [j["job_digest"] for j in body["jobs"]]
    assert all(sum(b["counts"].values()) == 16 and all(len(k) == 4 for k in b["counts"]) for b in receipt["blocks"])
    hw = receipt["hardware"]
    assert hw["backend_name"] == "fake_fez" and hw["dry_run"] is True and hw["provider_job_id"]
    assert hw["job_tags"] == ["atlas-qsearch-req-req-hw0"] and hw["max_execution_time"] == 10
    assert receipt["usage"]["usage_pending"] is True and receipt["usage"]["quantum_seconds"] is None
