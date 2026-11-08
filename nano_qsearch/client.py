"""Small authenticated lifecycle client; one HTTP operation per explicit call."""
from __future__ import annotations

import json
import math
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from ._job_state_v1 import is_valid_job_state
from .auth import AUTH_HEADER, MIN_KEY_BYTES, sign_request
from .canonical import canonical_digest, canonical_json, validate_digest
from .lifecycle import JOB_STATE_SCHEMA, NEXT_ACTIONS

_PREFIX = "/qsearch/v2/requests/"
_MAX_RESPONSE_BYTES = 1_048_576
# Wire contract v1 section 2.1: every number exactly representable as an IEEE-754 double
# (magnitude <= 2^53-1) and at most 64 nested containers, the top-level value counting as 1.
_MAX_SAFE_NUMBER = 2**53 - 1
_MAX_JSON_DEPTH = 64
_REMOTE_CODES = frozenset({
    "NOT_FOUND", "NOT_ENABLED", "EMPTY_BODY_REQUIRED", "UNAUTHORIZED", "STORE_UNAVAILABLE",
    "STATE_UNAVAILABLE", "REQUEST_NOT_FOUND", "INVALID_REQUEST", "INVALID_JSON", "JSON", "IDENTITY",
    "REQUEST_ID_CONFLICT", "REQUEST_CONFLICT", "CANCELLATION_REQUESTED", "CANCELLATION_PENDING",
    "CANCEL_ACKNOWLEDGEMENT_UNKNOWN", "LIVE_CONTEXT_REQUIRED", "PROVIDER_SUBMISSION_UNKNOWN",
    "PROVIDER_SUBMISSION_AMBIGUOUS", "PROVIDER_STATUS_UNKNOWN", "PROVIDER_UNAVAILABLE",
    "REPLACEMENT_BLOCKED", "REPLACEMENT_IDENTITY", "REPLACEMENT_TARGET", "QUEUE_EXPIRED",
    "ACCOUNTING_PENDING", "ACCOUNTING_EXPIRED", "LIFECYCLE_BUSY", "INVALID_ACTION", "BODY_TOO_LARGE",
    "REQUEST_BUSY", "REPLACEMENT_CONFLICT", "PROVIDER_OR_STORE_UNAVAILABLE", "LIFECYCLE_UNAVAILABLE",
    "ORIGINAL_ATTEMPT_UNRECONCILED", "NEW_IDENTITY_REQUIRED", "NEW_TARGET_REQUIRED",
    "PROVIDER_IDENTITY_MISMATCH", "CANCELLATION_NOT_AUTHORIZED", "LIFECYCLE_TARGET_UNSUPPORTED",
    "LIFECYCLE_ADAPTER_UNAVAILABLE", "CANCELLATION_FENCE_REQUIRED",
    "INVALID_CONTENT_LENGTH",
})
_REMOTE_FIELDS = frozenset({"body", "request_digest", "request_id", "descriptor", "jobs", "replacement",
                            "descriptor.request_id", "descriptor.target", "descriptor.max_execution_time",
                            "authority", "target", "headers.Content-Length"})


class LifecycleClientError(Exception):
    """Stable public metadata without URL, credentials, response text or provider exceptions."""

    def __init__(self, code: str, *, field: str | None = None, retryable: bool = False,
                 next_action: str = "NONE", http_status: int | None = None) -> None:
        super().__init__(f"q-search lifecycle failed ({code})")
        self.code = code
        self.field = field
        self.retryable = retryable
        self.next_action = next_action
        self.http_status = http_status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(method: str, url: str, body: bytes, headers: Mapping[str, str],
               timeout_s: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body if method == "POST" else None,
                                     headers=dict(headers), method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=timeout_s)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        payload = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise LifecycleClientError("INVALID_RESPONSE", next_action="MANUAL_RECONCILIATION")
        return response.code, payload


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse duplicate member names (wire contract v1 section 2.1) instead of keeping the last."""
    members: dict[str, Any] = {}
    for key, value in pairs:
        if key in members:
            raise ValueError("duplicate JSON member")
        members[key] = value
    return members


def _check_strict_value(value: Any) -> None:
    """Refuse nesting deeper than 64 containers and numbers beyond 2^53-1 (iterative, no recursion)."""
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, Mapping):
            children = item.values()
        elif isinstance(item, (list, tuple)):
            children = item
        else:
            if (not isinstance(item, bool) and isinstance(item, (int, float))
                    and not abs(item) <= _MAX_SAFE_NUMBER):      # also refuses NaN
                raise ValueError("unsafe JSON number")
            continue
        if depth > _MAX_JSON_DEPTH:
            raise ValueError("JSON nesting too deep")
        pending.extend((child, depth + 1) for child in children)


def _parse_strict(raw: Any) -> dict[str, Any]:
    """Strict JSON (wire contract v1 section 2.1) for success AND error bodies.

    Bytes must be UTF-8 (strictly decoded: no surrogates; a BOM is then refused by
    ``json.loads``) so UTF-16/UTF-32 auto-detection never applies. Text or a decoded
    mapping from an injected transport passes the same structural rules.
    """
    if isinstance(raw, Mapping):
        # Canonicalise, then parse like bytes: nested Mappings become dicts, and NaN,
        # non-string keys and cycles (RecursionError) are refused on one code path.
        raw = canonical_json(raw)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")       # the cap counts UTF-8 bytes; lone surrogates are refused
    if not isinstance(raw, bytes) or len(raw) > _MAX_RESPONSE_BYTES:
        raise ValueError("invalid response body")
    payload: Any = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                              object_pairs_hook=_reject_duplicates)
    if not isinstance(payload, dict):
        raise ValueError("top-level JSON value must be an object")
    _check_strict_value(payload)
    canonical_json(payload)
    return payload


class LifecycleClient:
    """Explicit status/cancel/refresh/replace calls with no automatic retry or polling.

    ``transport(method, url, body, headers, timeout_s)`` may be injected for
    offline tests and returns ``(HTTP status, JSON bytes or decoded mapping)``.
    The socket timeout is independent of host queue/accounting deadlines.
    """

    def __init__(self, base_url: str, request_key: bytes, *, timeout_s: float = 30,
                 transport: Callable[..., tuple[int, Any]] | None = None,
                 clock: Callable[[], float] = time.time,
                 nonce_factory: Callable[[], str] | None = None) -> None:
        try:
            parsed = urllib.parse.urlsplit(base_url)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0
                     and parsed.username is None and parsed.password is None
                     and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise LifecycleClientError("CLIENT_CONFIGURATION", field="base_url")
        if not isinstance(request_key, (bytes, bytearray)) or len(request_key) < MIN_KEY_BYTES:
            raise LifecycleClientError("CLIENT_CONFIGURATION", field="request_key")
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or timeout_s <= 0):
            raise LifecycleClientError("CLIENT_CONFIGURATION", field="timeout_s")
        self._base_url = base_url.rstrip("/")
        self._request_key = bytes(request_key)
        self._timeout_s = timeout_s
        self._transport = transport or _transport
        self._clock = clock
        self._nonce_factory = nonce_factory or (lambda: secrets.token_hex(16))

    def status(self, request_digest: str) -> dict[str, Any]:
        return self._call("GET", request_digest)

    def cancel(self, request_digest: str) -> dict[str, Any]:
        return self._call("POST", request_digest, action="cancel")

    def refresh(self, request_digest: str) -> dict[str, Any]:
        return self._call("POST", request_digest, action="refresh")

    def replace(self, request_digest: str, replacement: Mapping[str, Any]) -> dict[str, Any]:
        """Ask the host to admit a new request linked to the original; never retries."""
        try:
            if not isinstance(replacement, Mapping) or not isinstance(replacement.get("descriptor"), Mapping):
                raise ValueError()
            expected_digest = canonical_digest(replacement["descriptor"])
            body = canonical_json(replacement).encode("utf-8")
        except (ValueError, TypeError, RecursionError):
            raise LifecycleClientError("INVALID_REQUEST", field="replacement") from None
        return self._call("POST", request_digest, action="replace", body=body,
                          expected_response_digest=expected_digest)

    def _call(self, method: str, request_digest: str, *, action: str | None = None,
              body: bytes = b"", expected_response_digest: str | None = None) -> dict[str, Any]:
        try:
            return self._call_once(method, request_digest, action=action, body=body,
                                   expected_response_digest=expected_response_digest)
        except LifecycleClientError as exc:
            error = exc
        # Re-raise outside the handler with a fresh traceback: ``raise ... from None`` only hides
        # the chain from display, while ``__context__`` (e.g. a JSONDecodeError whose ``.doc`` is
        # the whole response body) and frame locals (raw bytes, URL, transport exception) would
        # otherwise stay reachable from the public error.
        error.__cause__ = None
        error.__context__ = None
        error.__suppress_context__ = True
        raise error.with_traceback(None)

    def _call_once(self, method: str, request_digest: str, *, action: str | None,
                   body: bytes, expected_response_digest: str | None) -> dict[str, Any]:
        try:
            validate_digest(request_digest, field="request_digest")
        except ValueError:
            raise LifecycleClientError("IDENTITY", field="request_digest") from None
        path = _PREFIX + request_digest + ("/" + action if action else "")
        try:
            now = self._clock()
            if isinstance(now, bool) or not isinstance(now, (float, int)) or not math.isfinite(now) or now < 0:
                raise ValueError()
            ts = int(now)
        except Exception:
            raise LifecycleClientError("CLIENT_CONFIGURATION", field="clock") from None
        try:
            header = sign_request(self._request_key, method, path, body, ts=ts, nonce=self._nonce_factory())
        except Exception:
            raise LifecycleClientError("CLIENT_CONFIGURATION", field="nonce") from None
        headers = {AUTH_HEADER: header, "Accept": "application/json"}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self._transport(method, self._base_url + path, body, headers, self._timeout_s)
        except LifecycleClientError:
            raise
        except Exception:
            raise LifecycleClientError("TRANSPORT_UNAVAILABLE", retryable=True,
                                       next_action="POLL_STATUS" if method == "GET" else "MANUAL_RECONCILIATION") from None
        if type(status) is not int or not 100 <= status <= 599:
            raise LifecycleClientError("INVALID_RESPONSE", next_action="MANUAL_RECONCILIATION")
        if 300 <= status < 400:
            raise LifecycleClientError("UNEXPECTED_REDIRECT", http_status=status)
        try:
            payload = _parse_strict(raw)
        except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
            raise LifecycleClientError("INVALID_RESPONSE", next_action="MANUAL_RECONCILIATION",
                                       http_status=status) from None
        if not 200 <= status < 300:
            code = payload.get("code")
            code = code if isinstance(code, str) and code in _REMOTE_CODES else "REMOTE_ERROR"
            field = payload.get("field")
            field = field if isinstance(field, str) and field in _REMOTE_FIELDS else None
            next_action = payload.get("next_action")
            next_action = next_action if isinstance(next_action, str) and next_action in NEXT_ACTIONS else "MANUAL_RECONCILIATION"
            raise LifecycleClientError(code, field=field, retryable=payload.get("retryable") is True,
                                       next_action=next_action, http_status=status)
        if payload.get("schema") != JOB_STATE_SCHEMA:
            raise LifecycleClientError("INVALID_RESPONSE", next_action="MANUAL_RECONCILIATION", http_status=status)
        request = payload.get("request")
        next_action = payload.get("next_action")
        if (not isinstance(request, Mapping)
                or request.get("request_digest") != (expected_response_digest or request_digest)
                or not isinstance(next_action, str) or next_action not in NEXT_ACTIONS):
            raise LifecycleClientError("INVALID_RESPONSE", next_action="MANUAL_RECONCILIATION", http_status=status)
        if not is_valid_job_state(payload):
            # Closed v1 envelope (wire contract section 2.2): exact members, types, enums and I1-I8.
            raise LifecycleClientError("INVALID_RESPONSE", next_action="MANUAL_RECONCILIATION", http_status=status)
        if action == "replace":
            lineage = payload.get("lineage")
            if not isinstance(lineage, Mapping) or lineage.get("replaces_request_digest") != request_digest:
                raise LifecycleClientError("INVALID_RESPONSE", next_action="MANUAL_RECONCILIATION", http_status=status)
        return payload


__all__ = ["LifecycleClient", "LifecycleClientError"]
