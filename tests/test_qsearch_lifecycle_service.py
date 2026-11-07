"""Issue #59 lifecycle operations against a durable store and fake host clock.

No provider SDK, live credentials, or quantum execution is used here. Reads
must work even when constructing any runtime would be an error.
"""
import json
import secrets
import sqlite3
import urllib.error
import urllib.request

import pytest

from nano_qsearch.auth import AUTH_HEADER, sign_request
from nano_qsearch.request import parse_request
from nano_qsearch.service import QSearchService, ServiceConfig, serve_in_thread
from qsearch_v2_support import request_body


RECEIPT_KEY = b"lifecycle-receipt-key-" + b"r" * 16
REQUEST_KEY = b"lifecycle-request-key-" + b"a" * 16
PREFIX = "/qsearch/v2/requests/"


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _config(tmp_path, **overrides):
    values = dict(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path,
                  receipt_key=RECEIPT_KEY, request_key=REQUEST_KEY, v2_enabled=True)
    return ServiceConfig(**{**values, **overrides})


def _no_runtime(kind):
    raise AssertionError("a status read constructed a runtime")


def _headers(method, path, raw=b"", *, now=1000, nonce=None):
    return {AUTH_HEADER: sign_request(REQUEST_KEY, method, path, raw, ts=int(now),
                                     nonce=nonce or secrets.token_hex(16))}


def _seed(svc, record=None):
    body = request_body(2, target="ibm_fez")
    request = parse_request(body)
    svc.store.begin_request(request.request_digest, request.request_id, json.dumps(body))
    svc.store.mark_request_attempt(request.request_digest)
    if record is not None:
        svc.store.set_request_record(request.request_digest, json.dumps(record))
    return request, PREFIX + request.request_digest


def _get(svc, path, *, now=1000, headers=None, raw=b""):
    return svc.status_request("GET", path, raw,
                              headers if headers is not None else _headers("GET", path, raw, now=now),
                              now=int(now))


def test_authenticated_status_reads_do_not_construct_runtime_or_change_request(tmp_path):
    clock = Clock()
    svc = QSearchService(_config(tmp_path), runtime_factory=_no_runtime, clock=clock)
    try:
        request, path = _seed(svc, {"provider_job_id": "provider-one", "provider_status": "QUEUED",
                                    "accounting": "pending", "reserved_seconds": 10.0,
                                    "credential": "must-never-leak"})
        before = (svc.store.get_request(request.request_digest), svc.store.request_record(request.request_digest))
        for _ in range(4):
            status, state = _get(svc, path)
            assert status == 200
            assert state["schema"] == "nano.qsearch.job-state.v1"
            assert state["request"]["request_id"] == request.request_id
            assert state["request"]["request_digest"] == request.request_digest
            assert "must-never-leak" not in json.dumps(state)
            assert "success" not in state
        assert (svc.store.get_request(request.request_digest),
                svc.store.request_record(request.request_digest)) == before
        assert svc._runtimes == {}
    finally:
        svc.close()


def test_status_replays_and_wrong_method_or_path_mac_fail_without_provider_access(tmp_path):
    svc = QSearchService(_config(tmp_path), runtime_factory=_no_runtime, clock=Clock())
    try:
        _, path = _seed(svc)
        assert _get(svc, path, headers={})[0] == 401
        headers = _headers("GET", path, nonce="ab" * 16)
        assert _get(svc, path, headers=headers)[0] == 200
        assert _get(svc, path, headers=headers)[0] == 401
        assert _get(svc, path, headers=_headers("POST", path))[0] == 401
        other_path = PREFIX + "sha256:" + "a" * 64
        assert _get(svc, path, headers=_headers("GET", other_path))[0] == 401
        assert svc._runtimes == {}
    finally:
        svc.close()


def test_status_disabled_before_auth_and_unknown_request_has_structured_error(tmp_path):
    svc = QSearchService(_config(tmp_path, v2_enabled=False), runtime_factory=_no_runtime, clock=Clock())
    path = PREFIX + "sha256:" + "b" * 64
    try:
        headers = _headers("GET", path, nonce="cd" * 16)
        status, error = _get(svc, path, headers=headers)
        assert status == 404 and error["code"] == "NOT_ENABLED"
        assert svc.store.remember("cd" * 16, 1000) is True
    finally:
        svc.close()
    svc = QSearchService(_config(tmp_path), runtime_factory=_no_runtime, clock=Clock())
    try:
        status, error = _get(svc, path)
        assert status == 404 and isinstance(error["code"], str)
        assert type(error["retryable"]) is bool and isinstance(error["next_action"], str)
        assert svc._runtimes == {}
    finally:
        svc.close()


def test_status_survives_restart_and_keeps_used_attempt(tmp_path):
    clock = Clock()
    svc = QSearchService(_config(tmp_path), runtime_factory=_no_runtime, clock=clock)
    request, path = _seed(svc, {"provider_job_id": "provider-one", "provider_status": "QUEUED"})
    expected = _get(svc, path)
    svc.close()
    svc = QSearchService(_config(tmp_path), runtime_factory=_no_runtime, clock=clock)
    try:
        assert _get(svc, path) == expected
        assert svc.store.get_request(request.request_digest).attempts == 1
        assert svc._runtimes == {}
    finally:
        svc.close()


def test_status_rejects_bodies_and_invalid_digest_with_stable_metadata(tmp_path):
    svc = QSearchService(_config(tmp_path), runtime_factory=_no_runtime, clock=Clock())
    try:
        _, path = _seed(svc)
        status, error = _get(svc, path, raw=b"{}")
        assert status == 400 and error["code"] == "EMPTY_BODY_REQUIRED"
        assert error["field"] == "body" and error["retryable"] is False
        for invalid in (PREFIX + "not-a-digest", path + "/", path + "?refresh=1"):
            status, error = _get(svc, invalid)
            assert status == 404
            assert {"code", "field", "retryable", "next_action"} <= error.keys()
        assert svc._runtimes == {}
    finally:
        svc.close()


def test_status_store_failure_has_sanitized_retryable_metadata(tmp_path, monkeypatch):
    svc = QSearchService(_config(tmp_path), runtime_factory=_no_runtime, clock=Clock())
    try:
        _, path = _seed(svc)

        def fail(_digest):
            raise sqlite3.OperationalError("credential=secret-provider-token")

        monkeypatch.setattr(svc.store, "get_request", fail)
        status, error = _get(svc, path)
        assert status == 503 and error["retryable"] is True
        assert {"code", "field", "next_action"} <= error.keys()
        assert "secret-provider-token" not in json.dumps(error)
        assert svc._runtimes == {}
    finally:
        svc.close()


def test_status_corrupted_receipt_returns_structured_error_without_provider_access(tmp_path):
    svc = QSearchService(_config(tmp_path), runtime_factory=_no_runtime, clock=Clock())
    try:
        request, path = _seed(svc)
        svc.store.complete_request(request.request_digest, "{credential=secret-provider-token")
        status, error = _get(svc, path)
        assert status == 503
        assert {"code", "field", "retryable", "next_action"} <= error.keys()
        assert "secret-provider-token" not in json.dumps(error)
        assert svc._runtimes == {}
    finally:
        svc.close()


def test_http_get_route_authenticates_full_digest_path(tmp_path):
    svc = QSearchService(_config(tmp_path), runtime_factory=_no_runtime, clock=Clock())
    _, path = _seed(svc, {"provider_status": "QUEUED"})
    server, thread = serve_in_thread(svc)
    try:
        # The host clock is injected for MAC freshness as well as lifecycle deadlines.
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", method="GET",
                                     headers=_headers("GET", path))
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read())["schema"] == "nano.qsearch.job-state.v1"
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", method="GET")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 401
        assert svc._runtimes == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        svc.close()
