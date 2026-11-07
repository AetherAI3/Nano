"""Lifecycle client contract against offline transports and the local HTTP server."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nano_qsearch.auth import AUTH_HEADER, MemoryNonceStore, verify_request
from nano_qsearch.canonical import canonical_digest, canonical_json
from nano_qsearch.client import LifecycleClient, LifecycleClientError
from nano_qsearch.lifecycle import JOB_STATE_SCHEMA

KEY = b"separate-test-request-key-1234"
DIGEST = "sha256:" + "b" * 64
NEW_DIGEST = "sha256:" + "c" * 64


def response(digest=DIGEST):
    return {"schema": JOB_STATE_SCHEMA, "request": {"request_digest": digest}, "state": "QUEUED",
            "next_action": "POLL_STATUS"}


def test_all_operations_bind_exact_method_path_body_and_fresh_nonce():
    calls = []
    nonce_store = MemoryNonceStore()
    nonces = iter(f"{i:032x}" for i in range(4))
    replacement = {"schema": "predator.qsearch.request.v2", "descriptor": {"request_id": "new"}, "jobs": []}

    def transport(method, url, body, headers, timeout_s):
        path = url.removeprefix("http://127.0.0.1:8798")
        verify_request(KEY, headers[AUTH_HEADER], method, path, body, now=1234, nonce_store=nonce_store)
        calls.append((method, path, body, timeout_s))
        payload = response(canonical_digest(replacement["descriptor"]) if path.endswith("/replace") else DIGEST)
        if path.endswith("/replace"):
            payload["lineage"] = {"replaces_request_digest": DIGEST}
        return 200, json.dumps(payload).encode()

    client = LifecycleClient("http://127.0.0.1:8798/", KEY, transport=transport,
                             clock=lambda: 1234, nonce_factory=lambda: next(nonces), timeout_s=3)
    client.status(DIGEST)
    client.cancel(DIGEST)
    client.refresh(DIGEST)
    client.replace(DIGEST, replacement)
    prefix = "/qsearch/v2/requests/" + DIGEST
    assert calls == [("GET", prefix, b"", 3), ("POST", prefix + "/cancel", b"", 3),
                     ("POST", prefix + "/refresh", b"", 3),
                     ("POST", prefix + "/replace", canonical_json(replacement).encode(), 3)]


@pytest.mark.parametrize("operation", ["status", "cancel", "refresh", "replace"])
def test_lost_acknowledgement_does_not_retry_or_cancel(operation):
    calls = []

    def transport(*args):
        calls.append(args)
        raise socket.timeout("url contained token=secret")

    client = LifecycleClient("https://example.invalid", KEY, transport=transport)
    with pytest.raises(LifecycleClientError) as caught:
        getattr(client, operation)(DIGEST, {"descriptor": {}}) if operation == "replace" else getattr(client, operation)(DIGEST)
    error = caught.value
    assert error.code == "TRANSPORT_UNAVAILABLE"
    assert error.retryable
    assert error.next_action == ("POLL_STATUS" if operation == "status" else "MANUAL_RECONCILIATION")
    assert len(calls) == 1
    assert "secret" not in str(error)
    assert error.__cause__ is None


def test_known_server_error_metadata_propagates_without_free_text():
    payload = {"code": "PROVIDER_UNAVAILABLE", "field": "request_digest", "retryable": True,
               "next_action": "RECONCILE", "error": "credential-secret", "traceback": "credential-secret"}
    client = LifecycleClient("https://example.invalid", KEY, transport=lambda *args: (503, payload))
    with pytest.raises(LifecycleClientError) as caught:
        client.refresh(DIGEST)
    error = caught.value
    assert (error.code, error.field, error.retryable, error.next_action, error.http_status) == (
        "PROVIDER_UNAVAILABLE", "request_digest", True, "RECONCILE", 503)
    assert "credential-secret" not in str(error)
    assert "credential-secret" not in repr(error.__dict__)


def test_unknown_remote_error_fields_are_sanitized():
    payload = {"code": "token=credential-secret", "field": "credential-secret", "retryable": "yes",
               "next_action": "SUBMIT_REPLACEMENT"}
    client = LifecycleClient("https://example.invalid", KEY, transport=lambda *args: (400, payload))
    with pytest.raises(LifecycleClientError) as caught:
        client.cancel(DIGEST)
    error = caught.value
    assert (error.code, error.field, error.retryable, error.next_action) == (
        "REMOTE_ERROR", None, False, "MANUAL_RECONCILIATION")


@pytest.mark.parametrize("status,payload,code", [(200, b"broken JSON credential-secret", "INVALID_RESPONSE"),
                                               (200, b"[]", "INVALID_RESPONSE"),
                                               (200, b'{"value":NaN}', "INVALID_RESPONSE"),
                                               (302, b"token=credential-secret", "UNEXPECTED_REDIRECT"),
                                               (200, {"schema": "wrong"}, "INVALID_RESPONSE"),
                                               (200, response(NEW_DIGEST), "INVALID_RESPONSE"),
                                               (200, {**response(), "next_action": []}, "INVALID_RESPONSE")])
def test_protocol_failures_are_stable_and_sanitized(status, payload, code):
    client = LifecycleClient("https://example.invalid", KEY, transport=lambda *args: (status, payload))
    with pytest.raises(LifecycleClientError) as caught:
        client.status(DIGEST)
    assert caught.value.code == code
    assert "credential-secret" not in str(caught.value)


@pytest.mark.parametrize("digest", ["sha256:" + "B" * 64, "bad", DIGEST + "/cancel", None])
def test_identity_error_prevents_transport(digest):
    client = LifecycleClient("https://example.invalid", KEY, transport=lambda *args: pytest.fail("unexpected I/O"))
    with pytest.raises(LifecycleClientError) as caught:
        client.status(digest)
    assert (caught.value.code, caught.value.field) == ("IDENTITY", "request_digest")


@pytest.mark.parametrize("url", ["https://user:secret@example.invalid", "https://example.invalid/path",
                                "file:///tmp/value", "https://example.invalid?token=secret", "http://[invalid"])
def test_base_url_rejects_credentials_and_non_origin_urls(url):
    with pytest.raises(LifecycleClientError) as caught:
        LifecycleClient(url, KEY)
    assert (caught.value.code, caught.value.field) == ("CLIENT_CONFIGURATION", "base_url")
    assert "secret" not in str(caught.value)


def test_invalid_clock_or_nonce_is_configuration_error_before_transport():
    for kwargs, field in [({"clock": lambda: float("nan")}, "clock"),
                           ({"nonce_factory": lambda: "invalid"}, "nonce")]:
        client = LifecycleClient("https://example.invalid", KEY,
                                 transport=lambda *args: pytest.fail("unexpected I/O"), **kwargs)
        with pytest.raises(LifecycleClientError) as caught:
            client.status(DIGEST)
        assert (caught.value.code, caught.value.field) == ("CLIENT_CONFIGURATION", field)


def test_default_http_transport_does_not_follow_redirect_or_leak_provider_error_text():
    paths = []

    class Handler(BaseHTTPRequestHandler):
        mode = "success"

        def do_GET(self):
            paths.append(self.path)
            verify_request(KEY, self.headers.get(AUTH_HEADER), "GET", self.path, b"", now=1234,
                           nonce_store=MemoryNonceStore())
            if self.mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "/credential-leak-target")
                self.end_headers()
                return
            payload = (response() if self.mode == "success" else
                       {"code": "UNAUTHORIZED", "error": "provider credential-secret", "next_action": "NONE"})
            raw = json.dumps(payload).encode()
            self.send_response(200 if self.mode == "success" else 401)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = LifecycleClient(f"http://127.0.0.1:{server.server_port}", KEY, clock=lambda: 1234)
        assert client.status(DIGEST)["state"] == "QUEUED"
        Handler.mode = "error"
        with pytest.raises(LifecycleClientError) as caught:
            client.status(DIGEST)
        assert caught.value.code == "UNAUTHORIZED"
        assert "credential-secret" not in str(caught.value)
        Handler.mode = "redirect"
        with pytest.raises(LifecycleClientError) as caught:
            client.status(DIGEST)
        assert caught.value.code == "UNEXPECTED_REDIRECT"
        assert paths == ["/qsearch/v2/requests/" + DIGEST] * 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_client_real_service_http_status_cancel_and_refresh_keep_one_execution(tmp_path):
    from nano_qsearch.service import serve_in_thread
    from test_qsearch_lifecycle_controls import Provider, _service
    from test_qsearch_lifecycle_service import Clock, REQUEST_KEY, _seed

    provider, clock = Provider(), Clock()
    service = _service(tmp_path, provider, clock)
    request, _ = _seed(service, {"provider_status": "QUEUED", "provider_job_id": "one-job"})
    server, thread = serve_in_thread(service)
    try:
        client = LifecycleClient(f"http://127.0.0.1:{server.server_port}", REQUEST_KEY, clock=clock)
        assert client.status(request.request_digest)["state"] == "QUEUED"
        assert provider.observes == provider.recovers == provider.cancels == 0
        state = client.cancel(request.request_digest)
        assert state["state"] == "CANCELLATION_REQUESTED"
        assert not state["execution"]["terminal"]
        assert provider.cancels == 1
        provider.status = "CANCELLED"
        clock.now += 31
        state = client.refresh(request.request_digest)
        assert state["execution"]["outcome"] == "CANCELLED"
        assert state["state"] == "ACCOUNTING_PENDING"
        assert state["usage"]["reservation_status"] == "HELD"
        client.cancel(request.request_digest)
        assert provider.cancels == 1
        assert provider.submits == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        service.close()


@pytest.mark.parametrize("wrong_digest,wrong_parent", [(True, False), (False, True)])
def test_replacement_response_must_bind_new_descriptor_and_original_parent(wrong_digest, wrong_parent):
    replacement = {"descriptor": {"request_id": "replacement"}, "jobs": []}
    expected = canonical_digest(replacement["descriptor"])
    payload = response(NEW_DIGEST if wrong_digest else expected)
    payload["lineage"] = {"replaces_request_digest": NEW_DIGEST if wrong_parent else DIGEST}
    client = LifecycleClient("https://example.invalid", KEY, transport=lambda *args: (200, payload))
    with pytest.raises(LifecycleClientError) as caught:
        client.replace(DIGEST, replacement)
    assert caught.value.code == "INVALID_RESPONSE"


def test_replacement_requires_descriptor_before_transport():
    client = LifecycleClient("https://example.invalid", KEY, transport=lambda *args: pytest.fail("unexpected I/O"))
    with pytest.raises(LifecycleClientError) as caught:
        client.replace(DIGEST, {})
    assert (caught.value.code, caught.value.field) == ("INVALID_REQUEST", "replacement")
