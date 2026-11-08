"""Issue #86: the portable q-search lifecycle wire contract v1 and its golden fixtures.

``protocol/qsearch-lifecycle/v1/fixtures/*.json`` are produced from the actual
Python implementation by ``fixtures/regen.py``. These tests prove that the
committed bytes are still what Python produces, that every vector satisfies the
frozen contract (checked by an independent strict reference reader here, not by
the Python client), and record each place where ``LifecycleClient`` is laxer
than the contract as an ``xfail(strict=True)`` compatibility test.

Offline only: no network, Qiskit, ATLAS or provider runtime.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import inspect
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from nano_qsearch import auth, client, lifecycle, receipt_v2
from nano_qsearch.canonical import validate_digest
from nano_qsearch.client import LifecycleClient, LifecycleClientError
from nano_qsearch.request import parse_request
from nano_qsearch.service import REQUEST_STATUS_PREFIX

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "protocol" / "qsearch-lifecycle" / "v1"
FIXTURES = V1 / "fixtures"
REGEN = FIXTURES / "regen.py"
CONTRACT_ID = "nano.qsearch.lifecycle-wire.v1"
ORIGIN = "http://127.0.0.1:8798"
PREFIX = "/qsearch/v2/requests/"
KEY = b"nano86-synthetic-request-key-01"


def _load(name: str) -> Any:
    return json.loads((V1 / name).read_text(encoding="utf-8"))


WIRE = _load("wire.v1.json")
JOB_SCHEMA = _load("job-state.v1.schema.json")
ERROR_SCHEMA = _load("lifecycle-error.v1.schema.json")
JOB_VALIDATOR = Draft202012Validator(JOB_SCHEMA)
ERROR_VALIDATOR = Draft202012Validator(ERROR_SCHEMA)
SIGNING = _load("fixtures/signing.v1.json")
HISTORIES = _load("fixtures/status-histories.v1.json")
ERRORS = _load("fixtures/errors.v1.json")
NEGATIVES = _load("fixtures/negative.v1.json")
FIXTURE_FILES = ("signing.v1.json", "status-histories.v1.json", "errors.v1.json", "negative.v1.json")
CAP = WIRE["max_response_bytes"]
MAX_SAFE = WIRE["max_safe_integer"]

REQUIRED_HISTORIES = {
    "queued", "running", "completed_unknown_outcome", "failed", "cancelled_confirmed", "accounting_pending",
    "accounting_expired", "settled_positive", "confirmed_zero", "unknown_zero_held", "queue_expired",
    "cancellation_requested_not_terminal", "not_run", "dry_run", "replacement_lineage",
}
REQUIRED_ERRORS = {"not_enabled", "unauthorized", "request_not_found", "store_unavailable", "state_unavailable",
                   "synthetic_unknown_code", "synthetic_unknown_field", "synthetic_unknown_next_action",
                   "synthetic_retryable_string"}
# Error bodies are mapped, never trusted (CONTRACT.md 2.3): this one is outside the error schema on purpose.
SCHEMA_INVALID_ERROR_BODIES = {"synthetic_retryable_string"}


@pytest.fixture(scope="module")
def regen():
    """Import the generator without leaving bytecode in protocol/ (SHA256SUMS covers that tree)."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("nano86_lifecycle_wire_regen", REGEN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


# --- independent strict reference reader (CONTRACT.md section 2) ---------------------------------

class _Refused(ValueError):
    pass


def _depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(v) for v in value), default=0)
    return 0


def _strict_loads(raw: bytes) -> Any:
    if len(raw) > CAP:
        raise _Refused("over cap")

    def pairs(items):
        if len({key for key, _ in items}) != len(items):
            raise _Refused("duplicate member")
        return dict(items)

    def as_int(text):
        value = int(text)
        if abs(value) > MAX_SAFE:
            raise _Refused("unsafe integer")
        return value

    def as_float(text):
        value = float(text)
        if not math.isfinite(value) or abs(value) > MAX_SAFE:
            raise _Refused("unsafe number")
        return value

    def constant(text):
        raise _Refused("non-finite lexeme")

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_int=as_int, parse_float=as_float,
                       parse_constant=constant)
    if _depth(value) > WIRE["max_json_depth"]:
        raise _Refused("too deep")
    return value


def _invalid(status: int | None) -> dict[str, Any]:
    out = {"code": "INVALID_RESPONSE", "retryable": False, "next_action": "MANUAL_RECONCILIATION"}
    return out if status is None else {**out, "http_status": status}


def contract_outcome(digest: str, status: Any, raw: bytes) -> dict[str, Any]:
    """The envelope, or the client-error metadata, that CONTRACT.md mandates for one response."""
    if type(status) is not int or not 100 <= status <= 599:
        return _invalid(None)
    if 300 <= status < 400:
        return {"code": "UNEXPECTED_REDIRECT", "retryable": False, "next_action": "NONE", "http_status": status}
    try:
        payload = _strict_loads(raw)
    except (ValueError, RecursionError):
        return _invalid(status)
    if not isinstance(payload, dict):
        return _invalid(status)
    if not 200 <= status < 300:
        code, field, action = payload.get("code"), payload.get("field"), payload.get("next_action")
        return {"code": code if isinstance(code, str) and code in WIRE["remote_codes"] else "REMOTE_ERROR",
                "field": field if isinstance(field, str) and field in WIRE["remote_fields"] else None,
                "retryable": payload.get("retryable") is True,
                "next_action": (action if isinstance(action, str) and action in WIRE["next_actions"]
                                else "MANUAL_RECONCILIATION"),
                "http_status": status}
    if not JOB_VALIDATOR.is_valid(payload) or payload["request"]["request_digest"] != digest:
        return _invalid(status)
    return payload


# --- helpers -------------------------------------------------------------------------------------

def _body(case: dict[str, Any]) -> bytes:
    return base64.b64decode(case["body_b64"], validate=True) + b" " * case.get("pad_trailing_spaces", 0)


def _python(digest: str, status: int, raw: bytes) -> tuple[dict[str, Any] | None, LifecycleClientError | None]:
    """One Python ``status`` call over a replay transport; asserts it made exactly one transport call."""
    calls = []

    def transport(method, url, body, headers, timeout_s):
        calls.append(url)
        return status, raw

    python_client = LifecycleClient(ORIGIN, KEY, transport=transport, clock=lambda: 1000,
                                    nonce_factory=lambda: "ab" * 16)
    try:
        result, error = python_client.status(digest), None
    except LifecycleClientError as exc:
        result, error = None, exc
    assert calls == [ORIGIN + PREFIX + digest]
    return result, error


def _error_fields(exc: LifecycleClientError, *, compact: bool = False) -> dict[str, Any]:
    out = {"code": exc.code, "field": exc.field, "retryable": exc.retryable, "next_action": exc.next_action,
           "http_status": exc.http_status}
    return {k: v for k, v in out.items() if not (compact and k in ("field", "http_status") and v is None)}


def _same_json(a: Any, b: Any) -> bool:
    """Equality that also distinguishes booleans from numbers (``True == 1`` in Python)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same_json(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same_json(x, y) for x, y in zip(a, b))
    return a == b


# --- file-level stability -----------------------------------------------------------------------

def test_regen_check_reproduces_committed_fixtures_byte_for_byte():
    result = subprocess.run([sys.executable, "-B", str(REGEN), "--check"], cwd=ROOT, capture_output=True,
                            text=True, timeout=600)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_fixture_files_are_ascii_lf_canonical_render(name, regen):
    raw = (FIXTURES / name).read_bytes()
    assert raw.isascii() and b"\r" not in raw and raw.endswith(b"\n")
    document = json.loads(raw)
    assert raw == regen.render(document)
    assert document["contract"] == CONTRACT_ID == WIRE["contract"]


def test_sha256sums_cover_every_v1_file_and_verify(regen):
    lines = (V1 / "SHA256SUMS").read_bytes().decode("ascii").splitlines()
    entries = [re.fullmatch(r"([0-9a-f]{64})  (\S+)", line) for line in lines]
    assert all(entries)
    paths = [m[2] for m in entries]
    assert paths == sorted(paths)
    assert set(paths) == set(regen.hashed_files({}))
    assert set(paths) >= {"CONTRACT.md", "job-state.v1.schema.json", "lifecycle-error.v1.schema.json",
                          "wire.v1.json", "fixtures/regen.py", *(f"fixtures/{n}" for n in FIXTURE_FILES)}
    for m in entries:
        assert hashlib.sha256((V1 / m[2]).read_bytes()).hexdigest() == m[1], m[2]


# --- constants -----------------------------------------------------------------------------------

def test_wire_constants_equal_python_constants():
    assert WIRE["remote_codes"] == sorted(client._REMOTE_CODES)
    assert WIRE["remote_fields"] == sorted(client._REMOTE_FIELDS)
    assert WIRE["max_response_bytes"] == client._MAX_RESPONSE_BYTES
    assert WIRE["next_actions"] == sorted(lifecycle.NEXT_ACTIONS)
    # job_state() maps any status outside _PROVIDER_STATUSES to UNKNOWN, so the wire enum adds it.
    assert WIRE["provider_statuses"] == sorted(lifecycle._PROVIDER_STATUSES | {"UNKNOWN"})
    assert WIRE["job_state_schema"] == lifecycle.JOB_STATE_SCHEMA == JOB_SCHEMA["title"]
    assert WIRE["auth"]["header"] == auth.AUTH_HEADER
    assert WIRE["auth"]["window_s"] == auth.WINDOW_S
    assert WIRE["auth"]["min_key_bytes"] == auth.MIN_KEY_BYTES
    assert WIRE["auth"]["empty_body_sha256"] == hashlib.sha256(b"").hexdigest()
    assert WIRE["auth"]["nonce_pattern"] == "^" + auth._NONCE.pattern.removesuffix(r"\Z") + "$"
    status = WIRE["operations"]["status"]
    assert status["method"] == "GET" and status["body"] == ""
    assert status["path_template"] == client._PREFIX + "{request_digest}"
    assert client._PREFIX == REQUEST_STATUS_PREFIX
    assert WIRE["default_timeout_s"] == inspect.signature(LifecycleClient).parameters["timeout_s"].default
    assert WIRE["max_safe_integer"] == 2**53 - 1
    for vector in SIGNING["vectors"]:
        assert {k: v for k, v in vector["headers"].items() if k != auth.AUTH_HEADER} == WIRE["request_headers"]
    props = JOB_SCHEMA["properties"]
    assert sorted(props["next_action"]["enum"]) == WIRE["next_actions"]
    assert sorted(props["provider"]["properties"]["status"]["enum"]) == WIRE["provider_statuses"]
    assert validate_digest("sha256:" + "0" * 64)
    assert re.fullmatch(WIRE["request_digest_pattern"], "sha256:" + "0" * 64)


def test_schemas_are_valid_draft_2020_12():
    Draft202012Validator.check_schema(JOB_SCHEMA)
    Draft202012Validator.check_schema(ERROR_SCHEMA)


# --- signing -------------------------------------------------------------------------------------

def test_signing_vectors_cover_required_shapes():
    vectors = SIGNING["vectors"]
    assert len(vectors) >= 3
    assert {len(bytes.fromhex(v["key_hex"])) for v in vectors} >= {16, 32}
    assert any(len(v["nonce"]) == 64 for v in vectors)
    assert any(v["base_url"].endswith("/") for v in vectors)
    assert any(not v["base_url"].endswith("/") for v in vectors)
    assert any(isinstance(v["clock"], float) and v["clock"] != int(v["clock"]) for v in vectors)


@pytest.mark.parametrize("vector", SIGNING["vectors"], ids=lambda v: v["name"])
def test_signing_vector_reproduces_python_client_bytes_and_verifies(vector):
    key = bytes.fromhex(vector["key_hex"])
    sent = []

    def transport(method, url, body, headers, timeout_s):
        sent.append((method, url, body, dict(headers)))
        raise ConnectionError("capture only")

    python_client = LifecycleClient(vector["base_url"], key, transport=transport, clock=lambda: vector["clock"],
                                    nonce_factory=lambda: vector["nonce"])
    with pytest.raises(LifecycleClientError) as caught:
        python_client.status(vector["request_digest"])
    assert caught.value.code == "TRANSPORT_UNAVAILABLE"
    assert sent == [("GET", vector["url"], b"", vector["headers"])]
    assert vector["ts"] == math.floor(vector["clock"]) and vector["path"] == PREFIX + vector["request_digest"]
    assert vector["url"] == vector["base_url"].rstrip("/") + vector["path"]
    message = "\n".join(("GET", vector["path"], vector["body_sha256"], str(vector["ts"]), vector["nonce"]))
    assert base64.b64decode(vector["message_b64"]) == message.encode("utf-8")
    assert vector["body_sha256"] == WIRE["auth"]["empty_body_sha256"]
    assert vector["mac_hex"] == hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()
    assert vector["header"] == f"v1 ts={vector['ts']} nonce={vector['nonce']} mac={vector['mac_hex']}"
    assert vector["header"] == auth.sign_request(key, "GET", vector["path"], b"", ts=vector["ts"],
                                                 nonce=vector["nonce"])
    assert vector["headers"][auth.AUTH_HEADER] == vector["header"]
    auth.verify_request(key, vector["header"], "GET", vector["path"], b"", now=vector["ts"],
                        nonce_store=auth.MemoryNonceStore())


# --- status histories ----------------------------------------------------------------------------

def test_histories_are_complete_and_pairwise_distinct(regen):
    names = [h["name"] for h in HISTORIES["histories"]]
    assert len(names) == len(set(names)) and REQUIRED_HISTORIES <= set(names)
    keys = [regen.history_key(h["expected"]) for h in HISTORIES["histories"]]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("history", HISTORIES["histories"], ids=lambda h: h["name"])
def test_history_validates_and_python_client_parses_it_field_by_field(history):
    raw = _body(history)
    assert history["http_status"] == 200
    assert hashlib.sha256(raw).hexdigest() == history["body_sha256"]
    expected = history["expected"]
    JOB_VALIDATOR.validate(expected)
    assert _same_json(json.loads(raw), expected)
    assert _same_json(contract_outcome(history["request_digest"], 200, raw), expected)
    result, error = _python(history["request_digest"], 200, raw)
    assert error is None
    assert result.keys() == expected.keys()
    assert result["request"]["request_digest"] == history["request_digest"]
    for member in ("schema", "request", "lineage", "state", "provider", "execution", "accounting", "usage",
                   "cancellation", "deadlines", "next_action"):
        assert _same_json(result[member], expected[member]), member


def test_history_semantics_that_bind_every_client():
    by = {h["name"]: h["expected"] for h in HISTORIES["histories"]}
    assert by["confirmed_zero"]["usage"]["settled_quantum_seconds"] == 0
    assert by["confirmed_zero"]["execution"]["outcome"] == "CANCELLED"
    assert by["confirmed_zero"]["state"] == "SETTLED"
    assert by["unknown_zero_held"]["usage"]["settled_quantum_seconds"] is None
    assert by["unknown_zero_held"]["usage"]["reservation_status"] == "HELD"
    assert by["cancellation_requested_not_terminal"]["cancellation"]["requested"] is True
    assert by["cancellation_requested_not_terminal"]["execution"]["terminal"] is False
    assert by["cancelled_confirmed"]["cancellation"]["confirmed"] is True
    assert by["running"]["usage"]["held_quantum_seconds"] is None
    assert by["replacement_lineage"]["lineage"]["replaces_request_digest"] is not None
    assert by["accounting_expired"]["deadlines"]["accounting_expired"] is True
    assert by["queue_expired"]["deadlines"]["queue_expired"] is True


def test_confirmed_zero_history_comes_from_real_seal_and_predicate(tmp_path, regen):
    clock = regen.Clock()
    service = regen.make_service(tmp_path, clock)
    try:
        reads = {name: request for name, request, _ in regen.seed_histories(service, clock)}
        row = service.store.get_request(reads["confirmed_zero"].request_digest)
        receipt = json.loads(row.receipt_json)
        receipt_v2.verify_seal_v2(receipt, regen.RECEIPT_KEY)
        assert receipt["seal"].startswith(receipt_v2.HARDWARE_SEAL_PREFIX_V2)
        request = parse_request(json.loads(row.request_json))
        assert receipt_v2.confirmed_zero_receipt(request, receipt) is True
        assert receipt_v2.confirmed_zero_receipt.__module__ == "nano_qsearch.receipt_v2"
    finally:
        service.close()


def _snapshot(service) -> tuple[list[tuple], ...]:
    db = service.store._db
    return tuple(db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                 for table in ("requests", "request_events", "request_links", "jobs"))


def test_repeated_status_reads_write_nothing_but_nonces_and_reproduce_fixtures(tmp_path, regen):
    clock = regen.Clock()
    service = regen.make_service(tmp_path, clock)
    try:
        reads = regen.seed_histories(service, clock)
        before = _snapshot(service)
        issued: set[str] = set()
        for round_index in range(3):
            nonces = regen.Nonces(0x900000 * (round_index + 1))
            start = nonces.next
            histories = regen.read_histories(service, clock, reads, nonces)
            issued |= {f"{n:032x}" for n in range(start + 1, nonces.next + 1)}
            assert [h["body_b64"] for h in histories] == [h["body_b64"] for h in HISTORIES["histories"]]
        assert _snapshot(service) == before
        stored = {row[0] for row in service.store._db.execute("SELECT nonce FROM nonces")}
        assert stored and stored <= issued
        assert service._runtimes == {}
    finally:
        service.close()


def test_status_is_exactly_one_transport_call_or_zero_before_io():
    history = HISTORIES["histories"][0]
    _python(history["request_digest"], 200, _body(history))      # asserts exactly one call
    calls = []
    python_client = LifecycleClient(ORIGIN, KEY, transport=lambda *a: calls.append(a), clock=lambda: 1000)
    with pytest.raises(LifecycleClientError) as caught:
        python_client.status("sha256:" + "A" * 64)
    assert caught.value.code == "IDENTITY" and calls == []


def test_body_of_exactly_the_cap_is_accepted_and_one_more_byte_is_not():
    history = HISTORIES["histories"][0]
    raw = base64.b64decode(history["body_b64"])
    exact = raw + b" " * (CAP - len(raw))
    assert _same_json(contract_outcome(history["request_digest"], 200, exact), history["expected"])
    result, error = _python(history["request_digest"], 200, exact)
    assert error is None and _same_json(result, history["expected"])
    _, error = _python(history["request_digest"], 200, exact + b" ")
    assert error is not None and error.code == "INVALID_RESPONSE"


# --- errors --------------------------------------------------------------------------------------

def test_error_cases_are_complete():
    names = [c["name"] for c in ERRORS["cases"]]
    assert len(names) == len(set(names)) and REQUIRED_ERRORS <= set(names)


@pytest.mark.parametrize("case", ERRORS["cases"], ids=lambda c: c["name"])
def test_error_body_maps_to_expected_metadata(case):
    raw = _body(case)
    body = json.loads(raw)
    assert ERROR_VALIDATOR.is_valid(body) is (case["name"] not in SCHEMA_INVALID_ERROR_BODIES)
    assert not 200 <= case["http_status"] < 400
    assert contract_outcome(case["request_digest"], case["http_status"], raw) == case["expected_error"]
    result, error = _python(case["request_digest"], case["http_status"], raw)
    assert result is None and _error_fields(error) == case["expected_error"]
    assert str(error) == f"q-search lifecycle failed ({error.code})"
    for text in (body.get("error"), "must-be-discarded"):
        assert not text or text not in str(error)


# --- negatives -----------------------------------------------------------------------------------

_DISCREPANCY = {
    "error_body": "LifecycleClient maps error bodies without the strict-JSON rules (2^53-1, depth 64)",
    "encoding": "CONTRACT.md section 4: json.loads auto-detects a UTF-8 BOM, UTF-16 or UTF-32 body",
    "unsafe_integer": "LifecycleClient accepts integers beyond 2^53-1 (CONTRACT.md 2.1 portability cap)",
    "envelope": "CONTRACT.md section 4: LifecycleClient checks only schema, request_digest and next_action",
}


def _reason(name: str) -> str:
    if name.startswith("error_body"):
        return _DISCREPANCY["error_body"]
    if name.startswith("encoding_"):
        return _DISCREPANCY["encoding"]
    if name == "unsafe_integer":
        return _DISCREPANCY["unsafe_integer"]
    return _DISCREPANCY["envelope"]


def _negative_params():
    for case in NEGATIVES["cases"]:
        marks = ([pytest.mark.xfail(strict=True, reason=_reason(case["name"]))]
                 if case["python_client"] == "accepts" else [])
        yield pytest.param(case, id=case["name"], marks=marks)


def test_negative_cases_cover_the_required_classes():
    names = {c["name"] for c in NEGATIVES["cases"]}
    for required in ("wrong_request_digest", "wrong_schema_version", "bool_as_number", "number_as_bool",
                     "lexeme_nan", "lexeme_infinity", "lexeme_negative_infinity", "unsafe_integer",
                     "missing_member_cancellation", "extra_member_top_level", "duplicate_key_top_level",
                     "malformed_json", "top_level_array", "oversized_body", "redirect_302", "status_99",
                     "status_600"):
        assert required in names
    assert sum(name.startswith("invariant_i") for name in names) >= 4
    assert {c["python_client"] for c in NEGATIVES["cases"]} <= {"agrees", "accepts"}
    oversized = next(c for c in NEGATIVES["cases"] if c["name"] == "oversized_body")
    assert len(_body(oversized)) == CAP + 1 and len(oversized["body_b64"]) < 8192


@pytest.mark.parametrize("case", NEGATIVES["cases"], ids=lambda c: c["name"])
def test_negative_expected_error_is_what_the_contract_mandates(case):
    assert "field" not in case["expected_error"]
    assert contract_outcome(case["request_digest"], case["http_status"], _body(case)) == case["expected_error"]


@pytest.mark.parametrize("case", list(_negative_params()))
def test_python_client_refuses_negative_as_the_contract_requires(case):
    result, error = _python(case["request_digest"], case["http_status"], _body(case))
    assert result is None and error is not None
    assert _error_fields(error, compact=True) == case["expected_error"]
