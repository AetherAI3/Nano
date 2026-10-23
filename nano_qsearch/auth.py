# nano_qsearch/auth.py
"""Request authentication for ``POST /qsearch/v1/submit``.

A caller proves possession of the request key (a file separate from the
receipt-seal key) with ``X-Aether-Qsearch-Auth: v1 ts=<unix> nonce=<hex>
mac=<hex>``; ``mac`` is HMAC-SHA256 over method, path, body digest, timestamp
and nonce joined by newlines. The timestamp must be within ``WINDOW_S`` of the
server clock and the nonce is accepted once (``NonceStore``). Verification
runs the MAC check before touching the nonce store so a forged request cannot
burn a nonce. The receipt seal is a different key and a different concern.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import threading
from typing import Protocol

AUTH_HEADER = "X-Aether-Qsearch-Auth"
WINDOW_S = 120
MIN_KEY_BYTES = 16
_NONCE = re.compile(r"[0-9a-f]{16,64}\Z")
_HEADER = re.compile(r"v1 ts=(?P<ts>-?\d{1,12}) nonce=(?P<nonce>[0-9a-f]{16,64}) mac=(?P<mac>[0-9a-f]{64})\Z")


class AuthError(Exception):
    """Authentication failed; ``code`` ∈ {MALFORMED, STALE, MAC, REPLAY}. Never carries the body."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class NonceStore(Protocol):
    def remember(self, nonce: str, ts: int) -> bool:
        """Record ``nonce``; return False if it was already recorded."""


class MemoryNonceStore:
    """In-process nonce set for tests and development only; it grows without bound and
    forgets everything on restart. ``nano_qsearch.store.JobStore`` is the durable store."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()

    def remember(self, nonce: str, ts: int) -> bool:
        with self._lock:
            if nonce in self._seen:
                return False
            self._seen[nonce] = ts
            return True


def _check_key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise ValueError(f"request key must be at least {MIN_KEY_BYTES} bytes")
    return bytes(key)


def _message(method: str, path: str, body: bytes, ts: int, nonce: str) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return "\n".join((method.upper(), path, digest, str(ts), nonce)).encode("utf-8")


def sign_request(key: bytes, method: str, path: str, body: bytes, *, ts: int, nonce: str) -> str:
    key = _check_key(key)
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise ValueError("ts must be an integer unix time")
    if not isinstance(nonce, str) or not _NONCE.match(nonce):
        raise ValueError("nonce must be 16..64 lowercase hex characters")
    mac = hmac.new(key, _message(method, path, body, ts, nonce), hashlib.sha256).hexdigest()
    return f"v1 ts={ts} nonce={nonce} mac={mac}"


def verify_request(key: bytes, header: str | None, method: str, path: str, body: bytes, *,
                   now: int, nonce_store: NonceStore) -> None:
    key = _check_key(key)
    match = _HEADER.match(header or "")
    if match is None:
        raise AuthError("MALFORMED", "missing or malformed auth header")
    ts, nonce, mac = int(match["ts"]), match["nonce"], match["mac"]
    if abs(now - ts) > WINDOW_S:
        raise AuthError("STALE", "timestamp outside the accepted window")
    expected = hmac.new(key, _message(method, path, body, ts, nonce), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise AuthError("MAC", "request signature does not verify")
    if not nonce_store.remember(nonce, ts):
        raise AuthError("REPLAY", "nonce already used")


__all__ = ["AUTH_HEADER", "AuthError", "MemoryNonceStore", "NonceStore", "WINDOW_S", "sign_request", "verify_request"]
