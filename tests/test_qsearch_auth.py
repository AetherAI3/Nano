# tests/test_qsearch_auth.py
import pytest

from nano.loop.qsearch_auth import (
    AUTH_HEADER, WINDOW_S, AuthError, MemoryNonceStore, sign_request, verify_request,
)

KEY = b"r" * 32
BODY = b'{"schema":"predator.qsearch.job.v1"}'


def test_sign_then_verify():
    header = sign_request(KEY, "POST", "/qsearch/v1/submit", BODY, ts=1_000_000, nonce="ab" * 16)
    assert header.startswith("v1 ts=1000000 nonce=")
    store = MemoryNonceStore()
    verify_request(KEY, header, "POST", "/qsearch/v1/submit", BODY, now=1_000_010, nonce_store=store)


@pytest.mark.parametrize("tamper", [
    lambda h: h.replace("ts=1000000", "ts=1000001"),
    lambda h: h[:-1] + ("0" if h[-1] != "0" else "1"),
    lambda h: h.replace("nonce=" + "ab" * 16, "nonce=" + "cd" * 16),
    lambda h: "v2 " + h[3:],
    lambda h: h.replace("ts=", "time="),
    lambda h: "",
])
def test_tampered_header_rejected(tamper):
    header = sign_request(KEY, "POST", "/qsearch/v1/submit", BODY, ts=1_000_000, nonce="ab" * 16)
    with pytest.raises(AuthError):
        verify_request(KEY, tamper(header), "POST", "/qsearch/v1/submit", BODY, now=1_000_000, nonce_store=MemoryNonceStore())


def test_body_method_path_and_key_are_bound():
    header = sign_request(KEY, "POST", "/qsearch/v1/submit", BODY, ts=5, nonce="ab" * 16)
    for args in ((KEY, "POST", "/qsearch/v1/submit", BODY + b" "), (KEY, "GET", "/qsearch/v1/submit", BODY),
                 (KEY, "POST", "/qsearch/v1/other", BODY), (b"z" * 32, "POST", "/qsearch/v1/submit", BODY)):
        key, method, path, body = args
        with pytest.raises(AuthError):
            verify_request(key, header, method, path, body, now=5, nonce_store=MemoryNonceStore())


def test_timestamp_window():
    header = sign_request(KEY, "POST", "/qsearch/v1/submit", BODY, ts=10_000, nonce="ab" * 16)
    verify_request(KEY, header, "POST", "/qsearch/v1/submit", BODY, now=10_000 + WINDOW_S, nonce_store=MemoryNonceStore())
    verify_request(KEY, header, "POST", "/qsearch/v1/submit", BODY, now=10_000 - WINDOW_S, nonce_store=MemoryNonceStore())
    for now in (10_000 + WINDOW_S + 1, 10_000 - WINDOW_S - 1):
        with pytest.raises(AuthError) as info:
            verify_request(KEY, header, "POST", "/qsearch/v1/submit", BODY, now=now, nonce_store=MemoryNonceStore())
        assert info.value.code == "STALE"


def test_replay_rejected_only_after_mac_passes():
    store = MemoryNonceStore()
    header = sign_request(KEY, "POST", "/qsearch/v1/submit", BODY, ts=50, nonce="ab" * 16)
    verify_request(KEY, header, "POST", "/qsearch/v1/submit", BODY, now=50, nonce_store=store)
    with pytest.raises(AuthError) as info:
        verify_request(KEY, header, "POST", "/qsearch/v1/submit", BODY, now=51, nonce_store=store)
    assert info.value.code == "REPLAY"
    # a forged mac with a fresh nonce must not consume the nonce
    bad = sign_request(b"q" * 32, "POST", "/qsearch/v1/submit", BODY, ts=50, nonce="cd" * 16)
    with pytest.raises(AuthError):
        verify_request(KEY, bad, "POST", "/qsearch/v1/submit", BODY, now=50, nonce_store=store)
    assert store.remember("cd" * 16, 50) is True


def test_bad_nonce_shape_rejected():
    for nonce in ("", "xyz", "a" * 15, "a" * 65, "AB" * 16):
        with pytest.raises(ValueError):
            sign_request(KEY, "POST", "/qsearch/v1/submit", BODY, ts=1, nonce=nonce)


def test_short_key_rejected():
    with pytest.raises(ValueError):
        sign_request(b"short", "POST", "/qsearch/v1/submit", BODY, ts=1, nonce="ab" * 16)
    assert AUTH_HEADER == "X-Aether-Qsearch-Auth"
