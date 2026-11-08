"""Issue #101: the q-search lifecycle wire contract v2 (refresh/cancel/replace) and its golden fixtures.

``protocol/qsearch-lifecycle/v2/fixtures/*.json`` are produced from the actual
Python implementation by ``fixtures/regen.py``. These tests prove the committed
bytes are still what Python produces, that every vector satisfies the frozen
CONTRACT.md (checked by an independent reference reader and canonicaliser here,
not by the Python client), and record each place where ``LifecycleClient`` is
laxer than the contract as an ``xfail(strict=True)`` compatibility test with an
explicit entry in ``_DISCREPANCY`` (CONTRACT.md section 6).

Offline only: no network, Qiskit, ATLAS or provider runtime. This source is
ASCII with no backslash escapes; special characters are built with ``chr()``.
"""
from __future__ import annotations

import base64
import decimal
import hashlib
import hmac
import importlib.util
import json
import math
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from nano_qsearch import auth, client, lifecycle, service
from nano_qsearch.canonical import canonical_digest, canonical_json
from nano_qsearch.client import LifecycleClient, LifecycleClientError

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "protocol" / "qsearch-lifecycle" / "v1"
V2 = ROOT / "protocol" / "qsearch-lifecycle" / "v2"
FIXTURES = V2 / "fixtures"
REGEN = FIXTURES / "regen.py"
CONTRACT_ID = "nano.qsearch.lifecycle-wire.v2"
ORIGIN = "http://127.0.0.1:8798"
PREFIX = "/qsearch/v2/requests/"
KEY = b"nano101-synthetic-request-key-1"
OPERATIONS = ("refresh", "cancel", "replace")
BS = chr(92)
NL = chr(10)
CR = chr(13)

# Frozen at the contract-freeze commit 5ec301d.
FROZEN_V2 = {
    "CONTRACT.md": "7fa0524a00e9d5616f9fc06d790ac2809fde2bba70a700ebfd1131a7dd2cfa78",
    "wire.v2.json": "b9d5c32b59c2c943108145ed6cc9ba2fa17e2e7de5a02dc765fb469aa7a19559",
}
# v1 is frozen (CONTRACT.md v2 preamble): the hash of v1/SHA256SUMS pins every v1 file through it.
V1_SHA256SUMS_SHA256 = "9d4048d0f25facd7c13661e5edd240c834ec9b775af2260125f19fd441ae7f8f"


def _load(root: Path, name: str) -> Any:
    return json.loads((root / name).read_text(encoding="utf-8"))


WIRE = _load(V2, "wire.v2.json")
WIRE_V1 = _load(V1, "wire.v1.json")
JOB_SCHEMA = _load(V1, "job-state.v1.schema.json")
ERROR_SCHEMA = _load(V1, "lifecycle-error.v1.schema.json")
JOB_VALIDATOR = Draft202012Validator(JOB_SCHEMA)
ERROR_VALIDATOR = Draft202012Validator(ERROR_SCHEMA)
CANONICAL = _load(FIXTURES, "canonical.v2.json")
SIGNING = _load(FIXTURES, "controls-signing.v2.json")
HISTORIES = _load(FIXTURES, "controls-histories.v2.json")
ERRORS = _load(FIXTURES, "controls-errors.v2.json")
NEGATIVES = _load(FIXTURES, "controls-negative.v2.json")
FIXTURE_FILES = ("canonical.v2.json", "controls-signing.v2.json", "controls-histories.v2.json",
                 "controls-errors.v2.json", "controls-negative.v2.json")
CAP = WIRE_V1["max_response_bytes"]
MAX_SAFE = WIRE_V1["max_safe_integer"]
MAX_DEPTH = WIRE["max_canonical_depth"]

REQUIRED_HISTORIES = {"refresh_queued_to_running", "refresh_throttled_unchanged", "cancel_before_submission",
                      "cancel_dispatched_not_terminal", "cancel_confirmed", "cancel_raced_by_completion",
                      "cancel_on_settled_is_noop", "replace_after_settled"}
REQUIRED_ERROR_CODES = {"ORIGINAL_ATTEMPT_UNRECONCILED", "NEW_IDENTITY_REQUIRED", "NEW_TARGET_REQUIRED",
                        "REPLACEMENT_CONFLICT", "INVALID_REQUEST", "REQUEST_BUSY", "CANCELLATION_NOT_AUTHORIZED",
                        "LIVE_CONTEXT_REQUIRED", "LIFECYCLE_UNAVAILABLE", "PROVIDER_OR_STORE_UNAVAILABLE",
                        "REQUEST_NOT_FOUND", "UNAUTHORIZED"}

# Fixture case -> reason, for every case where the Python client is laxer than CONTRACT.md.
# A new entry must be added here deliberately (test_python_discrepancies_are_exactly_the_recorded_ones).
_DUPLICATES = ("CONTRACT.md section 6: LifecycleClient.replace takes an already-parsed Mapping, so the caller's "
               "json.loads keeps the last duplicate member instead of refusing the text")
_DISCREPANCY: dict[str, str] = {
    "canonical:duplicate_member_top_level": _DUPLICATES,
    "canonical:duplicate_member_nested": _DUPLICATES,
    "canonical:duplicate_member_descriptor": _DUPLICATES,
    "canonical:depth_65": ("CONTRACT.md section 2.1 bounds replacement nesting at 64; LifecycleClient.replace "
                           "canonicalises and sends any depth Python's recursion allows"),
}


@pytest.fixture(scope="module")
def regen():
    """Import the generator without leaving bytecode in protocol/ (SHA256SUMS covers that tree)."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("nano101_lifecycle_wire_v2_regen", REGEN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


# --- independent reference: replacement text -> canonical bytes (CONTRACT.md section 2.1) ---------

class _Refused(ValueError):
    pass


def _depth(value: Any) -> int:
    """Containers only; the top-level value is depth 1 (iterative)."""
    deepest, pending = 0, [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, dict):
            children = list(item.values())
        elif isinstance(item, list):
            children = item
        else:
            continue
        deepest = max(deepest, depth)
        pending.extend((child, depth + 1) for child in children)
    return deepest


def _strings(value: Any):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def reference_parse_replacement(text: str) -> dict[str, Any]:
    """The replacement a portable client must accept, or ``_Refused``."""
    def pairs(items):
        if len({key for key, _ in items}) != len(items):
            raise _Refused("duplicate member")
        return dict(items)

    def as_float(lexeme):
        value = float(lexeme)
        if not math.isfinite(value):
            raise _Refused("float overflows to infinity")
        return value

    def constant(lexeme):
        raise _Refused("non-finite lexeme")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_float=as_float, parse_constant=constant)
    except RecursionError:
        raise _Refused("too deep") from None
    for item in _strings(value):
        try:
            item.encode("utf-8")
        except UnicodeEncodeError:
            raise _Refused("lone surrogate") from None
    if _depth(value) > MAX_DEPTH:
        raise _Refused("too deep")
    if not isinstance(value, dict) or not isinstance(value.get("descriptor"), dict):
        raise _Refused("replacement must be an object with an object descriptor")
    return value


_SHORT = {chr(8): "b", chr(12): "f", chr(10): "n", chr(13): "r", chr(9): "t"}


def _ref_string(text: str) -> str:
    out = ['"']
    for ch in text:
        if ch == '"' or ch == BS:
            out.append(BS + ch)
        elif ch in _SHORT:
            out.append(BS + _SHORT[ch])
        elif ord(ch) < 0x20:
            out.append(BS + "u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def reference_canonical(value: Any) -> str:
    """CONTRACT.md section 2.1 written out directly (not json.dumps)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        assert math.isfinite(value)
        return repr(value)
    if isinstance(value, str):
        return _ref_string(value)
    if isinstance(value, list):
        return "[" + ",".join(reference_canonical(item) for item in value) + "]"
    assert isinstance(value, dict)
    return "{" + ",".join(_ref_string(k) + ":" + reference_canonical(value[k]) for k in sorted(value)) + "}"


_SCIENTIFIC = re.compile("-?[0-9]([.][0-9]+)?e[+-][0-9]{2,}")
_POSITIONAL = re.compile("-?[0-9]+[.][0-9]+")


def _float_form_follows_contract(value: float) -> bool:
    text = repr(value)
    exponent = 0 if value == 0 else decimal.Decimal(text).adjusted()
    return bool((_POSITIONAL if -4 <= exponent < 16 else _SCIENTIFIC).fullmatch(text))


def _floats(value: Any):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float):
            yield item
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


# --- independent reference: control responses (CONTRACT.md section 3 over v1 section 2) ----------

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
    if _depth(value) > WIRE_V1["max_json_depth"]:
        raise _Refused("too deep")
    return value


def _err(code: str, status: int | None, *, field: str | None = None, retryable: bool = False,
         next_action: str = "MANUAL_RECONCILIATION") -> dict[str, Any]:
    return {"code": code, "field": field, "retryable": retryable, "next_action": next_action,
            "http_status": status}


def _replacement_text(case: dict[str, Any]) -> str | None:
    raw = case.get("replacement_b64")
    return None if raw is None else base64.b64decode(raw, validate=True).decode("utf-8")


def contract_outcome(case: dict[str, Any], status: Any, raw: bytes) -> dict[str, Any]:
    """The envelope, or the five-field client error, CONTRACT.md mandates for one control response."""
    if type(status) is not int or not 100 <= status <= 599:
        return _err("INVALID_RESPONSE", None)
    if 300 <= status < 400:
        return _err("UNEXPECTED_REDIRECT", status, next_action="NONE")
    try:
        payload = _strict_loads(raw)
    except (ValueError, RecursionError):
        return _err("INVALID_RESPONSE", status)
    if not isinstance(payload, dict):
        return _err("INVALID_RESPONSE", status)
    if not 200 <= status < 300:
        code, field, action = payload.get("code"), payload.get("field"), payload.get("next_action")
        return _err(code if isinstance(code, str) and code in WIRE_V1["remote_codes"] else "REMOTE_ERROR", status,
                    field=field if isinstance(field, str) and field in WIRE_V1["remote_fields"] else None,
                    retryable=payload.get("retryable") is True,
                    next_action=(action if isinstance(action, str) and action in WIRE_V1["next_actions"]
                                 else "MANUAL_RECONCILIATION"))
    if not JOB_VALIDATOR.is_valid(payload):
        return _err("INVALID_RESPONSE", status)
    digest = case["request_digest"]
    if case["operation"] == "replace":
        replacement = reference_parse_replacement(_replacement_text(case))
        bound = (payload["request"]["request_digest"] == canonical_digest(replacement["descriptor"])
                 and payload["lineage"]["replaces_request_digest"] == digest)
    else:
        bound = payload["request"]["request_digest"] == digest
    return payload if bound else _err("INVALID_RESPONSE", status)


# --- helpers -------------------------------------------------------------------------------------

def _body(case: dict[str, Any]) -> bytes:
    return base64.b64decode(case["body_b64"], validate=True) + b" " * case.get("pad_trailing_spaces", 0)


def _call(case: dict[str, Any], status: Any, raw: bytes, *, transport: Any = None,
          client_: LifecycleClient | None = None):
    """One Python control over a replay transport; returns (result, error, transport calls)."""
    calls = []

    def replay(method, url, body, headers, timeout_s):
        calls.append((method, url, body, dict(headers)))
        return status, raw

    python_client = client_ or LifecycleClient(ORIGIN, KEY, transport=transport or replay, clock=lambda: 1000,
                                               nonce_factory=lambda: "ab" * 16)
    try:
        if case["operation"] == "replace":
            result = python_client.replace(case["request_digest"], json.loads(_replacement_text(case)))
        else:
            result = getattr(python_client, case["operation"])(case["request_digest"])
        return result, None, calls
    except LifecycleClientError as exc:
        return None, exc, calls


def _error_fields(exc: LifecycleClientError) -> dict[str, Any]:
    return {"code": exc.code, "field": exc.field, "retryable": exc.retryable, "next_action": exc.next_action,
            "http_status": exc.http_status}


def _same_json(a: Any, b: Any) -> bool:
    """Equality that also distinguishes booleans from numbers and ints from floats."""
    if isinstance(a, (bool, int, float)) or isinstance(b, (bool, int, float)):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same_json(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same_json(x, y) for x, y in zip(a, b))
    return a == b


def _expected_path(case: dict[str, Any]) -> str:
    return PREFIX + case["request_digest"] + "/" + case["operation"]


# --- file-level stability -----------------------------------------------------------------------

def test_regen_check_reproduces_committed_fixtures_byte_for_byte():
    result = subprocess.run([sys.executable, "-B", str(REGEN), "--check"], cwd=ROOT, capture_output=True,
                            text=True, timeout=600)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_fixture_files_are_ascii_lf_canonical_render(name, regen):
    raw = (FIXTURES / name).read_bytes()
    assert raw.isascii() and CR.encode() not in raw and raw.endswith(NL.encode())
    document = json.loads(raw)
    assert raw == regen.render(document)
    assert document["contract"] == CONTRACT_ID == WIRE["contract"]


def test_regen_source_is_ascii_lf():
    raw = REGEN.read_bytes()
    assert raw.isascii() and CR.encode() not in raw and raw.endswith(NL.encode())


def test_sha256sums_cover_every_v2_file_and_verify(regen):
    lines = (V2 / "SHA256SUMS").read_bytes().decode("ascii").splitlines()
    entries = [re.fullmatch("([0-9a-f]{64})  ([^ ]+)", line) for line in lines]
    assert all(entries)
    paths = [m[2] for m in entries]
    assert paths == sorted(paths)
    assert set(paths) == set(regen.hashed_files({}))
    assert set(paths) >= {"CONTRACT.md", "wire.v2.json", "fixtures/regen.py",
                          *(f"fixtures/{n}" for n in FIXTURE_FILES)}
    for m in entries:
        assert hashlib.sha256((V2 / m[2]).read_bytes()).hexdigest() == m[1], m[2]


def test_frozen_v2_contract_files_are_unchanged():
    for name, digest in FROZEN_V2.items():
        assert hashlib.sha256((V2 / name).read_bytes()).hexdigest() == digest, name


def test_v1_files_and_sums_stay_byte_identical():
    sums = (V1 / "SHA256SUMS").read_bytes()
    assert hashlib.sha256(sums).hexdigest() == V1_SHA256SUMS_SHA256
    for line in sums.decode("ascii").splitlines():
        digest, _, rel = line.partition("  ")
        assert hashlib.sha256((V1 / rel).read_bytes()).hexdigest() == digest, rel
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout: the pinned hash chain above is the byte-identity proof")
    result = subprocess.run([git, "diff", "--exit-code", "--stat", "HEAD", "--", "protocol/qsearch-lifecycle/v1"],
                            cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


# --- constants -----------------------------------------------------------------------------------

def _captured_headers(operation: str) -> tuple[str, str, dict[str, str]]:
    sent = []

    def transport(method, url, body, headers, timeout_s):
        sent.append((method, url, dict(headers)))
        raise ConnectionError("capture only")

    python_client = LifecycleClient(ORIGIN, KEY, transport=transport, clock=lambda: 1000,
                                    nonce_factory=lambda: "ab" * 16)
    digest = "sha256:" + "0" * 64
    with pytest.raises(LifecycleClientError):
        if operation == "replace":
            python_client.replace(digest, {"descriptor": {}})
        else:
            getattr(python_client, operation)(digest)
    assert len(sent) == 1
    method, url, headers = sent[0]
    return method, url[len(ORIGIN):], headers


def test_wire_v2_constants_equal_python_constants():
    assert WIRE["contract"] == CONTRACT_ID and WIRE["extends"] == WIRE_V1["contract"]
    assert WIRE["auth_header"] == auth.AUTH_HEADER == WIRE_V1["auth"]["header"]
    assert WIRE["empty_body_sha256"] == hashlib.sha256(b"").hexdigest() == WIRE_V1["auth"]["empty_body_sha256"]
    assert WIRE["job_state_schema"] == lifecycle.JOB_STATE_SCHEMA == JOB_SCHEMA["title"]
    assert WIRE["max_canonical_depth"] == WIRE_V1["max_json_depth"] == client._MAX_JSON_DEPTH
    assert WIRE["capabilities"] == sorted(("status", *OPERATIONS)) == sorted(WIRE["operations"])
    assert WIRE["default_capabilities"] == ["status"]
    assert WIRE["client_error_codes"] == sorted({*WIRE_V1["client_error_codes"], "INVALID_REQUEST"})
    assert WIRE["local_refusals"]["capability"] == {"code": "UNSUPPORTED_CAPABILITY", "field": "operation"}
    digest = "sha256:" + "0" * 64
    for name, operation in WIRE["operations"].items():
        path = operation["path_template"].format(request_digest=digest)
        method, sent_path, headers = _captured_headers(name)
        assert method == operation["method"] and sent_path == path, name
        assert {k: v for k, v in headers.items() if k != auth.AUTH_HEADER} == WIRE["request_headers"][method]
        if name == "status":
            assert service._REQUEST_STATUS.fullmatch(path) and operation["body"] == "empty"
        else:
            assert service._REQUEST_CONTROL.fullmatch(path)
            assert path == client._PREFIX + digest + "/" + name
    assert WIRE["operations"]["refresh"]["body"] == WIRE["operations"]["cancel"]["body"] == "empty"
    assert WIRE["operations"]["replace"]["body"] == "canonical_json(replacement)"
    assert WIRE["request_headers"]["GET"] == WIRE_V1["request_headers"]


@pytest.mark.parametrize("operation", ("status", *OPERATIONS))
def test_wire_transport_failure_matches_python(operation):
    method = WIRE["operations"][operation]["method"]
    calls = []

    def transport(*args):
        calls.append(args)
        raise ConnectionError("synthetic")

    python_client = LifecycleClient(ORIGIN, KEY, transport=transport, clock=lambda: 1000)
    digest = "sha256:" + "0" * 64
    with pytest.raises(LifecycleClientError) as caught:
        if operation == "replace":
            python_client.replace(digest, {"descriptor": {}})
        else:
            getattr(python_client, operation)(digest)
    assert len(calls) == 1
    expected = WIRE["transport_failure"][method]
    assert (caught.value.code, caught.value.retryable, caught.value.next_action) == (
        expected["code"], expected["retryable"], expected["next_action"])


def test_wire_local_replacement_refusal_matches_python():
    calls = []
    python_client = LifecycleClient(ORIGIN, KEY, transport=lambda *a: calls.append(a), clock=lambda: 1000)
    with pytest.raises(LifecycleClientError) as caught:
        python_client.replace("sha256:" + "0" * 64, {"descriptor": []})
    assert calls == []
    assert {"code": caught.value.code, "field": caught.value.field} == WIRE["local_refusals"]["replacement"]


# --- canonical replacement bytes -----------------------------------------------------------------

def _text(entry: dict[str, Any]) -> str:
    return base64.b64decode(entry["input_b64"], validate=True).decode("utf-8")


def test_canonical_vectors_cover_required_edges():
    names = {v["name"] for v in CANONICAL["vectors"]}
    assert len(CANONICAL["vectors"]) >= 12 and len(names) == len(CANONICAL["vectors"])
    assert names >= {"float_exponent_minus4_minus5", "float_exponent_15_16", "float_signed_zero",
                     "float_subnormal_and_max_double", "float_uppercase_and_signed_exponent_lexemes",
                     "number_kind_follows_lexeme", "integer_huge_negative_and_minus_zero",
                     "key_order_code_point_not_utf16", "string_escapes_control_and_solidus",
                     "string_raw_del_separators_non_ascii", "whitespace_heavy", "nested_descriptor_content",
                     "depth_64_boundary", "realistic_request_v2"}
    texts = "".join(_text(v) for v in CANONICAL["vectors"])
    for lexeme in ("0.0001", "0.00001", "1e15", "1e16", "-0.0", "5e-324", "1.7976931348623157e308", "1E5",
                   "1.0", "-0", "18446744073709551616", BS + "/", chr(0x7F), chr(0x2028), chr(0xE9)):
        assert lexeme in texts, lexeme
    by = {v["name"]: v for v in CANONICAL["vectors"]}
    keys = list(json.loads(_text(by["key_order_code_point_not_utf16"]))["descriptor"])
    assert sorted(keys) != sorted(keys, key=lambda k: k.encode("utf-16-be"))
    assert _depth(json.loads(_text(by["depth_64_boundary"]))) == MAX_DEPTH
    realistic = json.loads(_text(by["realistic_request_v2"]))
    assert realistic["schema"] == realistic["descriptor"]["schema"] == "predator.qsearch.request.v2"


def test_canonical_refusals_cover_required_classes():
    names = {r["name"] for r in CANONICAL["refusals"]}
    assert names >= {"float_overflow_positive", "lexeme_nan", "lone_high_surrogate_value",
                     "duplicate_member_top_level", "duplicate_member_nested", "top_level_array",
                     "descriptor_missing", "descriptor_array", "descriptor_null", "depth_65"}
    by = {r["name"]: r for r in CANONICAL["refusals"]}
    assert _depth(json.loads(_text(by["depth_65"]))) == MAX_DEPTH + 1
    assert {r["python_client"] for r in CANONICAL["refusals"]} <= {"agrees", "accepts"}


@pytest.mark.parametrize("vector", CANONICAL["vectors"], ids=lambda v: v["name"])
def test_canonical_vector_reproduces_via_nano_qsearch_canonical(vector):
    value = json.loads(_text(vector))
    canonical = base64.b64decode(vector["canonical_b64"], validate=True)
    assert canonical_json(value).encode("utf-8") == canonical
    descriptor = base64.b64decode(vector["descriptor_canonical_b64"], validate=True)
    assert canonical_json(value["descriptor"]).encode("utf-8") == descriptor
    assert vector["descriptor_digest"] == canonical_digest(value["descriptor"])
    assert vector["descriptor_digest"] == "sha256:" + hashlib.sha256(descriptor).hexdigest()
    # Canonical bytes are a fixed point.
    assert canonical_json(json.loads(canonical.decode("utf-8"))).encode("utf-8") == canonical


@pytest.mark.parametrize("vector", CANONICAL["vectors"], ids=lambda v: v["name"])
def test_canonical_vector_matches_the_independent_reference(vector):
    value = reference_parse_replacement(_text(vector))
    canonical = base64.b64decode(vector["canonical_b64"], validate=True).decode("utf-8")
    assert reference_canonical(value) == canonical
    assert all(_float_form_follows_contract(f) for f in _floats(value))


@pytest.mark.parametrize("refusal", CANONICAL["refusals"], ids=lambda r: r["name"])
def test_canonical_refusal_is_refused_by_the_independent_reference(refusal):
    with pytest.raises(_Refused):
        reference_parse_replacement(_text(refusal))


def _refusal_params():
    for refusal in CANONICAL["refusals"]:
        marks = ([pytest.mark.xfail(strict=True, reason=_DISCREPANCY.get("canonical:" + refusal["name"],
                                                                           "unrecorded discrepancy"))]
                 if refusal["python_client"] == "accepts" else [])
        yield pytest.param(refusal, id=refusal["name"], marks=marks)


@pytest.mark.parametrize("refusal", list(_refusal_params()))
def test_python_client_refuses_replacement_locally_before_io(refusal):
    calls = []
    python_client = LifecycleClient(ORIGIN, KEY, transport=lambda *a: calls.append(a) or (503, b"{}"),
                                    clock=lambda: 1000)
    with pytest.raises(LifecycleClientError) as caught:
        python_client.replace("sha256:" + "0" * 64, json.loads(_text(refusal)))
    assert calls == []
    assert _error_fields(caught.value) == {"code": "INVALID_REQUEST", "field": "replacement", "retryable": False,
                                           "next_action": "NONE", "http_status": None}


# --- signing -------------------------------------------------------------------------------------

def test_signing_vectors_cover_every_control_twice():
    vectors = SIGNING["vectors"]
    for operation in OPERATIONS:
        assert sum(v["operation"] == operation for v in vectors) >= 2, operation
    assert {len(bytes.fromhex(v["key_hex"])) for v in vectors} >= {16, 32, 64}
    assert any(v["base_url"].endswith("/") for v in vectors)
    assert any(not v["base_url"].endswith("/") for v in vectors)
    assert any(isinstance(v["clock"], float) and v["clock"] != int(v["clock"]) for v in vectors)
    replace_bodies = [base64.b64decode(v["body_b64"]) for v in vectors if v["operation"] == "replace"]
    assert any(not body.isascii() for body in replace_bodies)


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
        if vector["operation"] == "replace":
            python_client.replace(vector["request_digest"], json.loads(_replacement_text(vector)))
        else:
            getattr(python_client, vector["operation"])(vector["request_digest"])
    assert caught.value.code == "TRANSPORT_UNAVAILABLE"
    body = base64.b64decode(vector["body_b64"], validate=True)
    assert sent == [("POST", vector["url"], body, vector["headers"])]
    assert vector["method"] == "POST" and vector["path"] == _expected_path(vector)
    assert vector["url"] == vector["base_url"].rstrip("/") + vector["path"]
    if vector["operation"] == "replace":
        assert body == canonical_json(json.loads(_replacement_text(vector))).encode("utf-8")
        assert body.decode("utf-8") == reference_canonical(reference_parse_replacement(_replacement_text(vector)))
    else:
        assert body == b"" and "replacement_b64" not in vector
    assert vector["body_sha256"] == hashlib.sha256(body).hexdigest()
    ts = math.floor(vector["clock"])
    assert vector["ts"] == ts
    message = NL.join(("POST", vector["path"], vector["body_sha256"], str(ts), vector["nonce"])).encode("utf-8")
    assert base64.b64decode(vector["message_b64"]) == message
    assert vector["mac_hex"] == hmac.new(key, message, hashlib.sha256).hexdigest()
    assert vector["header"] == f"v1 ts={ts} nonce={vector['nonce']} mac={vector['mac_hex']}"
    assert vector["header"] == auth.sign_request(key, "POST", vector["path"], body, ts=ts, nonce=vector["nonce"])
    assert vector["headers"][auth.AUTH_HEADER] == vector["header"]
    assert {k: v for k, v in vector["headers"].items() if k != auth.AUTH_HEADER} == WIRE["request_headers"]["POST"]
    auth.verify_request(key, vector["header"], "POST", vector["path"], body, now=ts,
                        nonce_store=auth.MemoryNonceStore())


# --- control histories ---------------------------------------------------------------------------

def test_histories_are_complete():
    names = [h["name"] for h in HISTORIES["cases"]]
    assert len(names) == len(set(names)) and REQUIRED_HISTORIES <= set(names)
    assert {h["operation"] for h in HISTORIES["cases"]} == set(OPERATIONS)


@pytest.mark.parametrize("history", HISTORIES["cases"], ids=lambda h: h["name"])
def test_history_validates_and_python_client_binds_it(history):
    raw = _body(history)
    expected = history["expected"]
    assert history["http_status"] == 200
    JOB_VALIDATOR.validate(expected)
    assert _same_json(json.loads(raw), expected)
    assert _same_json(contract_outcome(history, 200, raw), expected)
    result, error, calls = _call(history, 200, raw)
    assert error is None and _same_json(result, expected)
    assert len(calls) == 1 and calls[0][1] == ORIGIN + _expected_path(history)
    if history["operation"] == "replace":
        replacement = json.loads(_replacement_text(history))
        descriptor_digest = canonical_digest(replacement["descriptor"])
        assert result["request"]["request_digest"] == descriptor_digest != history["request_digest"]
        assert result["lineage"]["replaces_request_digest"] == history["request_digest"]
        assert calls[0][2] == canonical_json(replacement).encode("utf-8")
    else:
        assert result["request"]["request_digest"] == history["request_digest"]
        assert calls[0][2] == b"" and "replacement_b64" not in history


def test_history_semantics_that_bind_every_client():
    by = {h["name"]: h["expected"] for h in HISTORIES["cases"]}
    assert by["refresh_queued_to_running"]["execution"]["status"] == "RUNNING"
    assert by["refresh_throttled_unchanged"]["execution"]["status"] == "QUEUED"
    assert by["cancel_before_submission"]["execution"]["status"] == "NOT_RUN"
    assert by["cancel_before_submission"]["accounting"]["status"] == "NOT_APPLICABLE"
    assert by["cancel_dispatched_not_terminal"]["state"] == "CANCELLATION_REQUESTED"
    assert by["cancel_dispatched_not_terminal"]["execution"]["terminal"] is False
    assert by["cancel_confirmed"]["execution"]["status"] == "CANCELLED"
    assert by["cancel_confirmed"]["cancellation"]["confirmed"] is True
    assert by["cancel_confirmed"]["usage"]["settled_quantum_seconds"] is None
    assert by["cancel_raced_by_completion"]["provider"]["status"] == "DONE"
    assert by["cancel_raced_by_completion"]["cancellation"]["requested"] is True
    assert by["cancel_on_settled_is_noop"]["state"] == "SETTLED"
    assert by["replace_after_settled"]["lineage"]["replaces_request_digest"] is not None


def test_cancel_response_is_cancelled_only_when_the_provider_confirmed_it():
    cancels = [h["expected"] for h in HISTORIES["cases"] if h["operation"] == "cancel"]
    assert len(cancels) >= 5
    for state in cancels:
        cancelled = state["execution"]["status"] == "CANCELLED"
        assert cancelled is (state["cancellation"]["confirmed"] is True)
        assert cancelled is (state["provider"]["status"] in ("CANCELLED", "CANCELED"))
    assert sum(state["execution"]["status"] == "CANCELLED" for state in cancels) == 1


@pytest.mark.parametrize("after_rpc", ["QUEUED", "RUNNING"])
def test_repeated_cancel_without_provider_confirmation_never_reports_cancelled(tmp_path, regen, after_rpc):
    provider = regen.Provider(status="QUEUED", cancel_status=after_rpc, job_id="nano101-job-live")
    with regen.Host(tmp_path, "live", provider) as host:
        request = host.seed("nano101-live-cancel", record=regen._queued_record("nano101-job-live"))
        nonces = regen.Nonces(0x7000)
        for step in range(4):
            host.clock.now = 1200.0 + 31 * step
            status, _, payload, error = regen._through(host, "cancel", request.request_digest, nonces=nonces)
            assert status == 200 and error is None
            assert payload["execution"]["status"] != "CANCELLED" and payload["cancellation"]["confirmed"] is False
        assert provider.calls.count("cancel_rpc") == 1
        provider.status = "CANCELLED"
        host.clock.now += 31
        _, _, payload, error = regen._through(host, "cancel", request.request_digest, nonces=nonces)
        assert error is None and payload["execution"]["status"] == "CANCELLED"
        assert payload["cancellation"]["confirmed"] is True and provider.calls.count("cancel_rpc") == 1


# --- errors --------------------------------------------------------------------------------------

def test_error_cases_cover_required_codes():
    names = [c["name"] for c in ERRORS["cases"]]
    assert len(names) == len(set(names))
    assert REQUIRED_ERROR_CODES <= {c["expected_error"]["code"] for c in ERRORS["cases"]}
    by = {c["name"]: c for c in ERRORS["cases"]}
    assert by["invalid_replacement_body"]["expected_error"]["code"] == "INVALID_REQUEST"
    assert by["cancel_acknowledgement_lost"]["expected_error"]["retryable"] is True


@pytest.mark.parametrize("case", ERRORS["cases"], ids=lambda c: c["name"])
def test_error_body_maps_to_expected_metadata(case):
    raw = _body(case)
    body = json.loads(raw)
    assert not 200 <= case["http_status"] < 400
    if "code" in body and "next_action" in body:
        ERROR_VALIDATOR.validate(body)
    assert contract_outcome(case, case["http_status"], raw) == case["expected_error"]
    result, error, calls = _call(case, case["http_status"], raw)
    assert result is None and _error_fields(error) == case["expected_error"]
    assert len(calls) == 1 and calls[0][1] == ORIGIN + _expected_path(case)
    assert str(error) == f"q-search lifecycle failed ({error.code})"
    assert b"synthetic-provider-token" not in raw and b"synthetic store failure" not in raw


# --- negatives -----------------------------------------------------------------------------------

def _negative_params():
    for case in NEGATIVES["cases"]:
        marks = ([pytest.mark.xfail(strict=True, reason=_DISCREPANCY.get("negative:" + case["name"],
                                                                           "unrecorded discrepancy"))]
                 if case["python_client"] == "accepts" else [])
        yield pytest.param(case, id=case["name"], marks=marks)


def test_python_discrepancies_are_exactly_the_recorded_ones():
    accepts = {"canonical:" + r["name"] for r in CANONICAL["refusals"] if r["python_client"] == "accepts"}
    accepts |= {"negative:" + c["name"] for c in NEGATIVES["cases"] if c["python_client"] == "accepts"}
    assert accepts == set(_DISCREPANCY)


def test_negative_cases_cover_the_required_classes():
    names = {c["name"] for c in NEGATIVES["cases"]}
    assert names >= {"refresh_returns_other_request", "cancel_returns_other_request",
                     "replace_returns_original_digest", "replace_lineage_missing", "replace_lineage_null",
                     "replace_lineage_wrong", "replace_invalid_envelope_extra_member", "refresh_redirect_302",
                     "cancel_redirect_307_empty_body", "replace_redirect_308", "refresh_oversized_body",
                     "cancel_duplicate_key_top_level", "replace_duplicate_key_lineage"}
    assert {c["python_client"] for c in NEGATIVES["cases"]} <= {"agrees", "accepts"}
    oversized = next(c for c in NEGATIVES["cases"] if c["name"] == "refresh_oversized_body")
    assert len(_body(oversized)) == CAP + 1 and len(oversized["body_b64"]) < 8192
    by = {c["name"]: c for c in NEGATIVES["cases"]}
    # Schema-valid bodies that fail only the v2 binding.
    for name in ("replace_returns_original_digest", "replace_lineage_null", "replace_lineage_wrong",
                 "cancel_returns_other_request"):
        assert JOB_VALIDATOR.is_valid(json.loads(_body(by[name]))), name


@pytest.mark.parametrize("case", NEGATIVES["cases"], ids=lambda c: c["name"])
def test_negative_expected_error_is_what_the_contract_mandates(case):
    # Negatives specify only the keys the contract mandates (``field`` is unspecified for INVALID_RESPONSE).
    outcome = contract_outcome(case, case["http_status"], _body(case))
    assert {key: outcome[key] for key in case["expected_error"]} == case["expected_error"]


@pytest.mark.parametrize("case", list(_negative_params()))
def test_python_client_refuses_negative_as_the_contract_requires(case):
    result, error, calls = _call(case, case["http_status"], _body(case))
    assert len(calls) == 1
    assert result is None and error is not None
    actual = _error_fields(error)
    assert {key: actual[key] for key in case["expected_error"]} == case["expected_error"]


# --- side effects (CONTRACT.md section 4) ----------------------------------------------------------

@pytest.mark.parametrize("history", HISTORIES["cases"], ids=lambda h: h["name"])
def test_each_control_is_exactly_one_transport_call_even_on_error(history):
    for status, raw in ((200, _body(history)), (503, b'{"code": "LIFECYCLE_BUSY", "retryable": true}'),
                        (302, b""), (200, b"{")):
        _, _, calls = _call(history, status, raw)
        assert len(calls) == 1, status


def _local_refusal(operation: str, **overrides: Any) -> tuple[LifecycleClientError, list[Any]]:
    calls = []
    options = {"transport": lambda *a: calls.append(a) or (200, b"{}"), "clock": lambda: 1000,
               "nonce_factory": lambda: "ab" * 16}
    digest = overrides.pop("digest", "sha256:" + "0" * 64)
    replacement = overrides.pop("replacement", {"descriptor": {}})
    python_client = LifecycleClient(ORIGIN, KEY, **{**options, **overrides})
    with pytest.raises(LifecycleClientError) as caught:
        if operation == "replace":
            python_client.replace(digest, replacement)
        else:
            getattr(python_client, operation)(digest)
    return caught.value, calls


@pytest.mark.parametrize("operation", OPERATIONS)
def test_local_refusals_make_zero_transport_calls(operation):
    for digest in ("sha256:" + "A" * 64, "sha256:" + "0" * 63, "0" * 64, None):
        error, calls = _local_refusal(operation, digest=digest)
        assert (error.code, calls) == ("IDENTITY", [])
    for clock in (lambda: math.nan, lambda: -1, lambda: True, lambda: "1000"):
        error, calls = _local_refusal(operation, clock=clock)
        assert (error.code, error.field, calls) == ("CLIENT_CONFIGURATION", "clock", [])
    error, calls = _local_refusal(operation, nonce_factory=lambda: "XYZ")
    assert (error.code, error.field, calls) == ("CLIENT_CONFIGURATION", "nonce", [])
    if operation == "replace":
        for replacement in ([], {"descriptor": None}, {"descriptor": {"f": math.inf}}, {"x": 1}):
            error, calls = _local_refusal(operation, replacement=replacement)
            assert (error.code, error.field, calls) == ("INVALID_REQUEST", "replacement", [])


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("failure", [ConnectionError, TimeoutError, socket.timeout, OSError, RuntimeError],
                         ids=lambda f: f.__name__)
def test_transport_failure_on_a_control_is_manual_reconciliation_and_never_a_state(operation, failure):
    case = next(h for h in HISTORIES["cases"] if h["operation"] == operation)
    calls = []

    def transport(method, url, body, headers, timeout_s):
        calls.append(url)
        raise failure("secret-transport-detail")

    result, error, _ = _call(case, None, b"", transport=transport)
    assert result is None and len(calls) == 1
    assert _error_fields(error) == {"code": "TRANSPORT_UNAVAILABLE", "field": None, "retryable": True,
                                    "next_action": "MANUAL_RECONCILIATION", "http_status": None}
    assert "secret-transport-detail" not in str(error) and error.__context__ is None


def test_a_caller_retry_uses_a_fresh_nonce_and_the_same_identity():
    case = next(h for h in HISTORIES["cases"] if h["operation"] == "replace")
    sent = []

    def transport(method, url, body, headers, timeout_s):
        sent.append((url, body, headers[auth.AUTH_HEADER]))
        raise TimeoutError()

    python_client = LifecycleClient(ORIGIN, KEY, transport=transport, clock=lambda: 1000)
    for _ in range(2):
        _call(case, None, b"", client_=python_client)
    (url_a, body_a, header_a), (url_b, body_b, header_b) = sent
    assert (url_a, body_a) == (url_b, body_b)
    nonce = re.compile("v1 ts=1000 nonce=([0-9a-f]+) mac=[0-9a-f]{64}")
    assert nonce.fullmatch(header_a)[1] != nonce.fullmatch(header_b)[1]


@pytest.mark.xfail(strict=True, reason="CONTRACT.md section 6: the Python client has no capability set; every "
                                       "operation is always available instead of failing UNSUPPORTED_CAPABILITY")
@pytest.mark.parametrize("operation", OPERATIONS)
def test_default_capability_set_refuses_controls_before_io(operation):
    calls = []
    python_client = LifecycleClient(ORIGIN, KEY, transport=lambda *a: calls.append(a) or (503, b"{}"),
                                    clock=lambda: 1000)
    with pytest.raises(LifecycleClientError) as caught:
        if operation == "replace":
            python_client.replace("sha256:" + "0" * 64, {"descriptor": {}})
        else:
            getattr(python_client, operation)("sha256:" + "0" * 64)
    assert calls == []
    assert (caught.value.code, caught.value.field) == ("UNSUPPORTED_CAPABILITY", "operation")
