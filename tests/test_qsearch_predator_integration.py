# tests/test_qsearch_predator_integration.py
"""A real Nano q-search service, a real engine child, and Predator's own client.

Predator-M is imported only here, from the checkout named by
``NANO_QSEARCH_PREDATOR_PATH`` (added to ``sys.path`` by ``conftest``); Nano
itself imports nothing from Predator. Each test serves a ``QSearchService`` on
an ephemeral localhost port and submits ``job_pair02.json`` through
``NanoRuntimeQSearchBackend``, so the receipt is checked by Predator's
``QSearchReceiptV1.from_payload``, seal and audit included.

This module is the evidence for gate G-W3, so it skips only when
``NANO_QSEARCH_PREDATOR_PATH`` is unset. Setting it declares an engine run: a
path that is not a Predator checkout fails collection, and a missing Predator
module, ``qiskit_aer`` or ``aether_atlas`` is a collection error, never a skip.
"""
import contextlib
import json
import os
import secrets
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.engine]
_PREDATOR = os.environ.get("NANO_QSEARCH_PREDATOR_PATH")
if not _PREDATOR:
    pytest.skip("NANO_QSEARCH_PREDATOR_PATH not set", allow_module_level=True)
if not (Path(_PREDATOR) / "predator" / "drive" / "qsearch_backend.py").is_file():
    pytest.fail(f"NANO_QSEARCH_PREDATOR_PATH={_PREDATOR!r} is not a Predator-M checkout "
                "(no predator/drive/qsearch_backend.py); unset it to skip the integration test", pytrace=False)

import aether_atlas  # noqa: E402,F401  - the engine child needs it; with the variable set, absence is an error
import qiskit_aer  # noqa: E402,F401
from predator.drive import execution, qsearch_backend  # noqa: E402
from predator_gen3.discovery import qsearch_bridge  # noqa: E402

from nano_qsearch.auth import AUTH_HEADER, sign_request                                  # noqa: E402
from nano_qsearch.service import SUBMIT_PATH, QSearchService, ServiceConfig, serve_in_thread   # noqa: E402

RECEIPT_KEY = secrets.token_bytes(32)
REQUEST_KEY = secrets.token_bytes(32)
TIMEOUT_S = 900


def _job_from_fixture(fixtures_dir):
    body = json.loads((fixtures_dir / "job_pair02.json").read_text(encoding="utf-8"))
    c, b, lim = body["compile"], body["backend"], body["limits"]
    policy = qsearch_bridge.QSearchPolicyV1(
        variant=c["variant"], p=c["p"], gammas=tuple(c["gammas"]), betas=tuple(c["betas"]), opt_level=c["opt_level"],
        seed=c["seed"], target=c["target"], training=c["training"], backend_kind=b["kind"], chi_max=b["chi_max"],
        shots=b["shots"], backend_seed=b["seed"], max_variables=lim["max_variables"], max_wall_s=lim["max_wall_s"],
        max_qpu_s=lim["max_qpu_s"])
    job = qsearch_bridge.QSearchJobV1(compiled_search_digest=body["compiled_search_digest"], variables=tuple(body["variables"]),
                                      qubo=tuple(tuple(r) for r in body["qubo"]["q"]), offset=body["qubo"]["offset"],
                                      policy=policy, classical_min_energy=body["audit"]["classical_min_energy"])
    assert job.job_digest == body["job_digest"]          # Predator and the fixture agree on the wire body
    assert job.to_payload() == body                      # ... field for field, not only by digest
    return job


def _signing_opener(key):
    def opener(url, data, timeout):
        header = sign_request(key, "POST", SUBMIT_PATH, data, ts=int(time.time()), nonce=secrets.token_hex(16))
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", AUTH_HEADER: header})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    return opener


@contextlib.contextmanager
def _serving(tmp_path, backend):
    cfg = ServiceConfig(bind="127.0.0.1:0", backend=backend, data_dir=tmp_path, receipt_key=RECEIPT_KEY,
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


def _backend(endpoint, *, key=RECEIPT_KEY, opener=None, timeout_s=TIMEOUT_S):
    extra = {} if opener is None else {"opener": opener}
    return qsearch_backend.NanoRuntimeQSearchBackend(endpoint, key=key, timeout_s=timeout_s, **extra)


def test_predator_backend_accepts_sealed_statevector_receipt(fixtures_dir, tmp_path):
    job = _job_from_fixture(fixtures_dir)
    with _serving(tmp_path, "auto") as (svc, endpoint):
        backend = _backend(endpoint, opener=_signing_opener(REQUEST_KEY))
        receipt = backend.submit(job)
        assert receipt.status == "OK", receipt.payload
        assert receipt.backend_kind == "statevector"            # n = 6 <= 20 under auto
        assert receipt.agree is True                            # pair02's best sample is the classical minimum
        assert receipt.job_digest == job.job_digest
        assert receipt.payload["variant"] == "joint5"
        assert receipt.payload["seal"].startswith("hmac-sha256:")

        again = backend.submit(job)                             # store hit: same sealed receipt, no recompute
        assert again.receipt_digest == receipt.receipt_digest
        assert svc.store.get(job.job_digest).attempts == 1


def test_predator_default_opener_is_refused_as_transport(fixtures_dir, tmp_path):
    job = _job_from_fixture(fixtures_dir)
    with _serving(tmp_path, "auto") as (svc, endpoint):
        not_run = _backend(endpoint, timeout_s=60).submit(job)  # Predator's own opener sends no auth header
        assert not_run.status == "NOT_RUN" and not_run.reason.startswith("transport:")
        assert svc.store.get(job.job_digest) is None            # a 401 stores nothing


def test_predator_refuses_a_receipt_sealed_with_another_key(fixtures_dir, tmp_path):
    job = _job_from_fixture(fixtures_dir)
    with _serving(tmp_path, "auto") as (_svc, endpoint):
        # positive control: the right receipt key verifies the same service's receipt
        assert _backend(endpoint, opener=_signing_opener(REQUEST_KEY)).submit(job).status == "OK"
        wrong = _backend(endpoint, key=secrets.token_bytes(32), opener=_signing_opener(REQUEST_KEY))
        with pytest.raises(execution.StageRefusal) as refusal:
            wrong.submit(job)
        assert refusal.value.code == "QSEARCH_SEAL_MISMATCH"


def test_predator_backend_accepts_sealed_mps_receipt(fixtures_dir, tmp_path):
    job = _job_from_fixture(fixtures_dir)
    with _serving(tmp_path, "mps") as (svc, endpoint):
        receipt = _backend(endpoint, opener=_signing_opener(REQUEST_KEY)).submit(job)
        assert receipt.status == "OK", receipt.payload
        assert receipt.backend_kind == "mps" and receipt.job_digest == job.job_digest
        assert receipt.payload["backend"]["chi_source"] == "peak_log"
        assert receipt.payload["backend"]["truncated"] is False
        assert receipt.payload["seal"].startswith("hmac-sha256:")
        assert svc.store.get(job.job_digest).state == "DONE"
