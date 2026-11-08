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


def response(digest=DIGEST, *, replaces=None):
    """A complete, closed ``nano.qsearch.job-state.v1`` envelope (job-state.v1.schema.json)."""
    return {"schema": JOB_STATE_SCHEMA,
            "request": {"request_id": "request-1", "request_digest": digest, "target": "ibm_fez"},
            "lineage": {"replaces_request_digest": replaces, "replacement_request_digest": None},
            "state": "QUEUED", "provider": {"job_id": "job-1", "status": "QUEUED"},
            "execution": {"status": "QUEUED", "outcome": "UNKNOWN", "terminal": False},
            "accounting": {"status": "PENDING"},
            "usage": {"reservation_status": "HELD", "held_quantum_seconds": 30.0, "settled_quantum_seconds": None},
            "cancellation": {"requested": False, "requested_at": None, "confirmed": False, "confirmed_at": None},
            "deadlines": {"queue_at": 4600.0, "queue_expired": False, "accounting_at": None,
                          "accounting_expired": False},
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
        is_replace = path.endswith("/replace")
        payload = response(canonical_digest(replacement["descriptor"]) if is_replace else DIGEST,
                           replaces=DIGEST if is_replace else None)
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
    payload = response(NEW_DIGEST if wrong_digest else expected, replaces=NEW_DIGEST if wrong_parent else DIGEST)
    client = LifecycleClient("https://example.invalid", KEY, transport=lambda *args: (200, payload))
    with pytest.raises(LifecycleClientError) as caught:
        client.replace(DIGEST, replacement)
    assert caught.value.code == "INVALID_RESPONSE"


def test_replacement_requires_descriptor_before_transport():
    client = LifecycleClient("https://example.invalid", KEY, transport=lambda *args: pytest.fail("unexpected I/O"))
    with pytest.raises(LifecycleClientError) as caught:
        client.replace(DIGEST, {})
    assert (caught.value.code, caught.value.field) == ("INVALID_REQUEST", "replacement")


@pytest.mark.parametrize("status,raw", [
    (200, b'{"schema":"nano.qsearch.job-state.v1","request":{"request_digest":"' + DIGEST.encode()
          + b'"},"next_action":"NONE","next_action":"POLL_STATUS"}'),
    (200, b'{"schema":"nano.qsearch.job-state.v1","request":{"request_digest":"' + NEW_DIGEST.encode()
          + b'","request_digest":"' + DIGEST.encode() + b'"},"next_action":"POLL_STATUS"}'),
    (503, b'{"code":"NOT_FOUND","code":"STORE_UNAVAILABLE","retryable":true}'),
])
def test_duplicate_member_names_are_refused_not_last_wins(status, raw):
    # Wire contract v1 section 2.1: a duplicate member name at any depth is INVALID_RESPONSE,
    # in success and error bodies alike, so two clients cannot read different values.
    calls = []
    client = LifecycleClient("https://example.invalid", KEY,
                             transport=lambda *args: calls.append(args) or (status, raw))
    with pytest.raises(LifecycleClientError) as caught:
        client.status(DIGEST)
    assert (caught.value.code, caught.value.next_action, caught.value.http_status) == (
        "INVALID_RESPONSE", "MANUAL_RECONCILIATION", status)
    assert len(calls) == 1


# --- strict JSON (wire contract v1 section 2.1, issue #86 parity) --------------------------------

MAX_SAFE = 2**53 - 1
ERROR_BODY = {"code": "STORE_UNAVAILABLE", "retryable": True, "next_action": "RECONCILE"}


def _nested(depth):
    """``depth`` nested JSON containers, the top-level object counted as depth 1."""
    value = {}
    for index in range(depth - 1):
        value = [value] if index % 2 else {"x": value}
    return value


def _call_status(status, raw):
    calls = []
    client = LifecycleClient("https://example.invalid", KEY,
                             transport=lambda *args: calls.append(args) or (status, raw))
    try:
        return client.status(DIGEST), None
    except LifecycleClientError as exc:
        return None, exc
    finally:
        assert len(calls) == 1


def _assert_invalid(error, status):
    assert error is not None
    assert (error.code, error.field, error.retryable, error.next_action, error.http_status) == (
        "INVALID_RESPONSE", None, False, "MANUAL_RECONCILIATION", status)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16-le", "utf-16-be", "utf-16", "utf-32-le",
                                      "utf-32-be", "utf-32"])
@pytest.mark.parametrize("status", [200, 503])
def test_non_utf8_or_bom_body_is_refused(encoding, status):
    payload = response() if status == 200 else ERROR_BODY
    _, error = _call_status(status, json.dumps(payload).encode(encoding))
    _assert_invalid(error, status)


@pytest.mark.parametrize("raw", [b'{"code":"NOT_FOUND","x":"\xff"}', b'{"code":"\xc3("}',
                                 b'{"code":"\xed\xa0\x80"}'])
def test_invalid_utf8_error_body_is_refused(raw):
    _, error = _call_status(503, raw)
    _assert_invalid(error, 503)


def test_text_from_injected_transport_is_still_parsed():
    result, error = _call_status(200, json.dumps(response()))
    assert error is None and result["state"] == "QUEUED"
    _, error = _call_status(503, json.dumps(ERROR_BODY))
    assert (error.code, error.http_status) == ("STORE_UNAVAILABLE", 503)
    _, error = _call_status(503, "\ufeff" + json.dumps(ERROR_BODY))
    _assert_invalid(error, 503)


@pytest.mark.parametrize("lexeme", [str(MAX_SAFE + 1), str(-MAX_SAFE - 1), "9007199254740993", "1e16",
                                    "9007199254740993.0", "-1.0e300", "18446744073709551616"])
@pytest.mark.parametrize("status", [200, 503])
def test_number_beyond_2_53_minus_1_is_refused_in_success_and_error_bodies(lexeme, status):
    body = response() if status == 200 else ERROR_BODY
    raw = json.dumps({**body, "extra": "SENTINEL"}).replace('"SENTINEL"', lexeme).encode()
    _, error = _call_status(status, raw)
    _assert_invalid(error, status)


@pytest.mark.parametrize("lexeme", [str(MAX_SAFE), str(-MAX_SAFE), "9007199254740991.0", "0", "-0", "1e15", "0.5"])
def test_number_within_2_53_minus_1_is_kept(lexeme):
    raw = json.dumps({**ERROR_BODY, "retry_after": "SENTINEL"}).replace('"SENTINEL"', lexeme).encode()
    _, error = _call_status(503, raw)
    assert (error.code, error.retryable, error.next_action, error.http_status) == (
        "STORE_UNAVAILABLE", True, "RECONCILE", 503)


@pytest.mark.parametrize("status", [404, 503])
def test_depth_64_is_accepted_and_65_is_refused(status):
    # Only an (open) error body can carry 64 levels; a closed job-state.v1 envelope is 2 deep.
    deepest = json.dumps({**ERROR_BODY, "detail": _nested(63)}).encode()   # top-level object + 63 = 64
    _, error = _call_status(status, deepest)
    assert (error.code, error.http_status) == ("STORE_UNAVAILABLE", status)
    _, error = _call_status(status, json.dumps({**ERROR_BODY, "detail": _nested(64)}).encode())
    _assert_invalid(error, status)


def test_success_body_deeper_than_64_is_refused_by_the_parser():
    _, error = _call_status(200, json.dumps({**response(), "detail": _nested(64)}).encode())
    _assert_invalid(error, 200)


def test_depth_far_beyond_recursion_limit_is_refused_not_crashing():
    raw = b'{"code":"NOT_FOUND","d":' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    _, error = _call_status(503, raw)
    _assert_invalid(error, 503)


@pytest.mark.parametrize("payload", [{**ERROR_BODY, "n": MAX_SAFE + 1}, {**ERROR_BODY, "n": -float(2**60)},
                                     {**ERROR_BODY, "detail": _nested(65)}])
def test_decoded_mapping_from_injected_transport_obeys_the_same_rules(payload):
    _, error = _call_status(503, payload)
    _assert_invalid(error, 503)


# --- closed job-state.v1 envelope on 2xx (wire contract v1 section 2.2) ---------------------------

_DELETE = object()


def _mutated(changes, digest=DIGEST):
    """``response()`` with ``{dotted.path: value}`` applied; ``_DELETE`` removes the member."""
    payload = json.loads(json.dumps(response(digest)))
    for path, value in changes.items():
        *parents, leaf = path.split(".")
        target = payload
        for name in parents:
            target = target[name]
        if value is _DELETE:
            del target[leaf]
        else:
            target[leaf] = value
    return payload


_SETTLED = {"state": "SETTLED", "next_action": "NONE", "accounting.status": "SETTLED",
            "usage.reservation_status": "SETTLED", "usage.held_quantum_seconds": 0, "usage.settled_quantum_seconds": 12,
            "execution.status": "FAILED", "execution.outcome": "FAILED", "execution.terminal": True}
_CANCELLED = {**_SETTLED, "execution.status": "CANCELLED", "execution.outcome": "CANCELLED",
              "cancellation.confirmed": True, "cancellation.confirmed_at": 1500.5}


@pytest.mark.parametrize("changes", [
    {},
    {"usage.held_quantum_seconds": 30, "deadlines.queue_at": MAX_SAFE},
    {"usage.held_quantum_seconds": None, "request.target": None, "provider.job_id": None},
    {"usage.held_quantum_seconds": 0.0, "deadlines.queue_at": -0.0},
    {"lineage.replaces_request_digest": NEW_DIGEST, "lineage.replacement_request_digest": NEW_DIGEST},
    {"request.request_id": "a" * 80, "request.target": "ibm.fez:1_x-" + "z" * 188},
    {"cancellation.requested": True, "cancellation.requested_at": 1200, "state": "CANCELLATION_REQUESTED",
     "next_action": "RECONCILE"},
    _SETTLED,
    {**_SETTLED, "usage.held_quantum_seconds": 0.0, "usage.settled_quantum_seconds": 0},
    _CANCELLED,
    {**_CANCELLED, "cancellation.confirmed_at": None},
    {"accounting.status": "NOT_APPLICABLE", "next_action": "NONE", "usage.reservation_status": "NOT_RESERVED",
     "usage.held_quantum_seconds": None, "state": "NOT_RUN", "execution.status": "NOT_RUN",
     "execution.outcome": "NOT_RUN", "execution.terminal": True},
    {"execution.status": "COMPLETED", "execution.outcome": "UNKNOWN", "execution.terminal": True,
     "state": "ACCOUNTING_PENDING", "next_action": "RECONCILE", "provider.status": "DONE"},
], ids=lambda changes: ",".join(changes) or "base")
def test_valid_closed_envelopes_are_accepted(changes):
    payload = _mutated(changes)
    result, error = _call_status(200, json.dumps(payload, sort_keys=True, allow_nan=False).encode())
    assert error is None and result == payload


@pytest.mark.parametrize("changes", [
    {"cancellation": _DELETE}, {"usage.settled_quantum_seconds": _DELETE}, {"request.target": _DELETE},
    {"extra": 1}, {"provider.extra": None}, {"accounting.reason": "x"},
    {"state": 3}, {"execution.terminal": "false"}, {"execution.terminal": 0}, {"cancellation.requested": None},
    {"usage.held_quantum_seconds": True}, {"deadlines.queue_expired": 0}, {"deadlines.queue_at": None},
    {"usage.held_quantum_seconds": "30"}, {"usage.held_quantum_seconds": -0.5}, {"deadlines.queue_at": -1},
    {"state": "DONE"}, {"provider.status": "done"}, {"execution.outcome": "OK"}, {"accounting.status": None},
    {"usage.reservation_status": "RELEASED"},
    {"request.request_id": ""}, {"request.request_id": "a" * 81}, {"request.request_id": "request.1"},
    {"request.request_id": "request-1\n"}, {"request.target": "ibm_fez\n"}, {"request.target": "x" * 201},
    {"request.target": "ibm fez"}, {"provider.job_id": "job-1\n"}, {"provider.job_id": ""},
    {"lineage.replaces_request_digest": NEW_DIGEST + "\n"}, {"lineage.replaces_request_digest": "sha256:" + "C" * 64},
    {"lineage.replacement_request_digest": "c" * 64}, {"request.request_id": 1},
    {"request": [DIGEST]}, {"usage": None}, {"deadlines": []},
    {"usage.held_quantum_seconds": 2**53}, {"deadlines.accounting_at": float(2**53)},
    # I1..I8
    {**_SETTLED, "state": "ACCOUNTING_PENDING"}, {**_SETTLED, "next_action": "RECONCILE"},
    {**_SETTLED, "usage.reservation_status": "HELD"}, {**_SETTLED, "usage.held_quantum_seconds": 1},
    {**_SETTLED, "usage.held_quantum_seconds": None}, {**_SETTLED, "usage.settled_quantum_seconds": None},
    {"accounting.status": "NOT_APPLICABLE", "next_action": "NONE", "usage.reservation_status": "NOT_RESERVED"},
    {"accounting.status": "NOT_APPLICABLE", "next_action": "NONE", "usage.reservation_status": "HELD",
     "usage.held_quantum_seconds": None},
    {"usage.settled_quantum_seconds": 1}, {"usage.reservation_status": "SETTLED"}, {"state": "SETTLED"},
    {"state": "FAILED"},
    {"execution.outcome": "SUCCEEDED"}, {"execution.status": "COMPLETED"},
    {"execution.terminal": True},
    {"execution.status": "SUCCEEDED", "execution.outcome": "SUCCEEDED"},
    {"execution.status": "CANCELLED", "execution.outcome": "FAILED", "execution.terminal": True},
    {"execution.status": "NOT_RUN", "execution.outcome": "UNKNOWN", "execution.terminal": True},
    {"execution.status": "COMPLETED", "execution.outcome": "SUCCEEDED", "execution.terminal": True},
    {**_CANCELLED, "cancellation.confirmed": False},
    {"cancellation.confirmed": True}, {"cancellation.confirmed_at": 1500},
    {"cancellation.requested": True}, {"cancellation.requested_at": 1200},
], ids=lambda changes: ",".join(f"{k}={v!r}"[:40] for k, v in changes.items()))
def test_open_partial_or_inconsistent_envelopes_are_refused(changes):
    _, error = _call_status(200, json.dumps(_mutated(changes)).encode())
    _assert_invalid(error, 200)


@pytest.mark.parametrize("operation", ["cancel", "refresh", "replace"])
def test_write_operation_responses_are_validated_as_closed_envelopes(operation):
    replacement = {"descriptor": {"request_id": "replacement"}, "jobs": []}
    digest = canonical_digest(replacement["descriptor"]) if operation == "replace" else DIGEST
    payload = response(digest, replaces=DIGEST if operation == "replace" else None)
    for body, accepted in ((payload, True), ({**payload, "extra": None}, False)):
        client = LifecycleClient("https://example.invalid", KEY, transport=lambda *args, b=body: (200, b))
        call = getattr(client, operation)
        try:
            result = call(DIGEST, replacement) if operation == "replace" else call(DIGEST)
        except LifecycleClientError as exc:
            assert not accepted
            _assert_invalid(exc, 200)
        else:
            assert accepted and result == payload
