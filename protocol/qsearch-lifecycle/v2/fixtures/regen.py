#!/usr/bin/env python3
"""Regenerate (or ``--check``) the ``nano.qsearch.lifecycle-wire.v2`` golden fixtures.

Every byte here comes from the actual Python implementation, never from hand
written expectations:

* ``canonical.v2.json``: replacement JSON text, ``nano_qsearch.canonical``
  bytes of ``json.loads(text)`` and the descriptor digest. ``refusals`` are
  texts CONTRACT.md section 2.1 refuses; ``python_client`` records whether
  ``LifecycleClient.replace(digest, json.loads(text))`` refuses them locally
  (``INVALID_REQUEST``, field ``replacement``, zero transport calls:
  ``"agrees"``) or sends them (``"accepts"``).
* ``controls-signing.v2.json``: ``LifecycleClient.refresh/cancel/replace`` run
  against a capturing fake transport; the exact URL, body and headers it sent
  are recorded and the MAC is re-derived from the CONTRACT.md framing.
* ``controls-histories.v2.json``: ``QSearchService.control_request`` on a
  temporary durable store with an injected clock and a fake, nonblocking
  provider adapter, reached through ``LifecycleClient`` and serialised exactly
  like ``_Handler._send``.
* ``controls-errors.v2.json``: real service error responses to controls;
  ``expected_error`` is what ``LifecycleClient`` raised for those bytes.
* ``controls-negative.v2.json``: control responses CONTRACT.md section 3
  refuses. ``expected_error`` is the contract outcome; ``python_client`` is
  ``"agrees"`` when the Python client raises exactly that and ``"accepts"``
  when it returns a payload instead. Any other Python outcome aborts generation.
* ``../SHA256SUMS``: ``sha256sum`` lines for every file in ``v2/`` but itself.

Stdlib plus ``nano_qsearch`` only: no network, Qiskit, ATLAS or test helpers.
Keys are synthetic. Output is ASCII JSON (``indent=1, sort_keys=True``), LF,
with a trailing newline. ``--check`` exits 1 when a committed file differs.
This source is ASCII with no backslash escapes: special characters are built
with ``chr()``.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import json
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent
V2 = FIXTURES.parent
REPO = V2.parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from nano_qsearch.auth import AUTH_HEADER, MemoryNonceStore, verify_request  # noqa: E402
from nano_qsearch.canonical import canonical_digest, canonical_json  # noqa: E402
from nano_qsearch.client import _MAX_RESPONSE_BYTES, LifecycleClient, LifecycleClientError  # noqa: E402
from nano_qsearch.receipt_v2 import request_receipt, seal_receipt_v2  # noqa: E402
from nano_qsearch.request import parse_request  # noqa: E402
from nano_qsearch.runtime_ibm import LifecycleAdapterError  # noqa: E402
from nano_qsearch.service import QSearchService, ServiceConfig  # noqa: E402

CONTRACT = "nano.qsearch.lifecycle-wire.v2"
ORIGIN = "http://127.0.0.1:8798"
PREFIX = "/qsearch/v2/requests/"
RECEIPT_KEY = b"nano101-synthetic-receipt-key-1"
REQUEST_KEY = b"nano101-synthetic-request-key-1"
WRONG_REQUEST_KEY = b"nano101-synthetic-wrong-key-001"
MANIFEST = "sha256:" + "1" * 64
BATCH = "sha256:" + "2" * 64
MINIMUM_DISPLAY = "1101"
NL = chr(10)
BS = chr(92)
OPERATIONS = ("refresh", "cancel", "replace")
TERMINAL = ("DONE", "CANCELLED", "ERROR")

# The tests/qsearch_fixtures/job_4var.json job, embedded so this script needs no test helpers.
JOB_4VAR: dict[str, Any] = {
    "schema": "predator.qsearch.job.v1",
    "compiled_search_digest": "sha256:a8f06cba3ed022dd2fd4efc1085510502517152067653b417fcf5bd50b1bc26d",
    "variables": ["c0", "c1", "c2", "c3"],
    "qubo": {"q": [[-1.0, 0.45, 0.35, 0.0], [0.45, -0.8, 0.25, 0.0], [0.35, 0.25, -0.6, -0.15],
                   [0.0, 0.0, -0.15, -0.4]], "offset": 0.0, "convention": "symmetric_half"},
    "compile": {"variant": "auto", "p": 1, "gammas": [0.4], "betas": [0.4], "opt_level": 3, "seed": 11,
                "target": "heron_heavy_hex", "training": "none"},
    "backend": {"kind": "auto", "chi_max": 64, "shots": 1024, "seed": 7},
    "limits": {"max_variables": 40, "max_wall_s": 120, "max_qpu_s": 0},
    "audit": {"classical_min_energy": -1.6},
}


# --- request bodies and fake ATLAS receipts (as protocol/qsearch-lifecycle/v1/fixtures/regen.py) ----

def _job(target: str, seed: int) -> dict[str, Any]:
    job = copy.deepcopy(JOB_4VAR)
    job["compile"]["target"] = target
    job["backend"].update(kind="ibm", shots=8, seed=seed)
    job["limits"]["max_qpu_s"] = 5
    return {**job, "job_digest": canonical_digest(job)}


def request_body(request_id: str, *, target: str = "ibm_fez") -> dict[str, Any]:
    jobs = [_job(target, 7)]
    blocks = [{"pub_index": i, "job_digest": job["job_digest"],
               "recipients": [{"agent_id": "agent-007", "chunk": i, "slot": 0}],
               "n": len(job["variables"]), "shots": job["backend"]["shots"], "seed": job["backend"]["seed"],
               "classical_min_energy": job["audit"]["classical_min_energy"]} for i, job in enumerate(jobs)]
    descriptor = {"schema": "predator.qsearch.request.v2", "request_id": request_id, "target": target,
                  "purpose": "qcomms", "max_execution_time": 5, "manifest_digest": MANIFEST, "batch_index": 0,
                  "batch_digest": BATCH, "blocks": blocks}
    return {"schema": "predator.qsearch.request.v2", "descriptor": descriptor, "jobs": jobs}


def replacement_text(request_id: str, target: str) -> str:
    """A valid replacement as a caller would send it: insertion order, indented, not canonical."""
    return json.dumps(request_body(request_id, target=target), indent=2) + NL


def atlas_receipt(request: Any, *, usage: Any, job_id: str) -> dict[str, Any]:
    samples = [[MINIMUM_DISPLAY] * (job.shots - 1) + ["0000"] for job in request.jobs]
    children = ["sha256:" + format(i + 10, "x").rjust(64, "0") for i in range(request.block_count)]
    envelope = {"schema": "AqrcQSearchRequestV1", "request_id": request.request_id, "purpose": request.purpose,
                "target": request.target, "children": children, "shots": [job.shots for job in request.jobs],
                "pub_count": request.block_count, "max_execution_time": request.max_execution_time,
                "recovery_tag": f"atlas-qsearch-req-{request.request_id}"}
    receipt = {"schema": "AqrcQSearchRequestReceiptV1", "request": {"envelope": envelope, "request_sha256": "x"},
               "purpose": request.purpose, "target": request.target, "backend_name": request.target,
               "dry_run": False, "job_id": job_id, "status": "DONE", "pub_count": request.block_count,
               "max_execution_time": request.max_execution_time, "job_tags": [envelope["recovery_tag"]],
               "pubs": [{"pub_index": i, "register": "meas", "bit_order": "msb_first", "shots": len(raw),
                         "raw_samples": list(raw), "child_sha256": children[i]} for i, raw in enumerate(samples)]}
    receipt.update(provider_final_usage_seconds=usage, usage_pending=not (usage and usage > 0),
                   provider_metrics={"usage": {"quantum_seconds": usage}})
    return receipt


def settled_receipt(*, usage: float, job_id: str):
    """A sealed OK receipt with final positive usage: the request is SETTLED."""
    return lambda request: seal_receipt_v2(request_receipt(request, atlas_receipt(request, usage=usage,
                                                                                    job_id=job_id),
                                                           purpose=request.purpose), RECEIPT_KEY)


# --- service plumbing --------------------------------------------------------------------------

class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Nonces:
    def __init__(self, start: int) -> None:
        self.next = start

    def __call__(self) -> str:
        self.next += 1
        return f"{self.next:032x}"


class Provider:
    """Nonblocking fake lifecycle adapter (the shape of tests/test_qsearch_lifecycle_controls.py).

    ``status`` is what the provider reports; ``cancel_status`` is what it reports after the
    cancel RPC. ``fail`` maps a hook (``observe``, ``cancel_preflight``, ``cancel_ack``,
    ``run``) to an exception factory. ``calls`` records every adapter entry and every RPC.
    """

    def __init__(self, *, status: str = "QUEUED", cancel_status: str = "QUEUED", job_id: str = "nano101-job",
                 fail: dict[str, Any] | None = None) -> None:
        self.status = status
        self.cancel_status = cancel_status
        self.job_id = job_id
        self.fail = fail or {}
        self.calls: list[str] = []

    def _hook(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]()

    def _record(self, record: dict[str, Any]) -> None:
        record.update(provider_status=self.status, provider_job_id=self.job_id, held_qpu_s=10.0)

    def run_request(self, request: Any, *, max_pubs: int, record: dict[str, Any]) -> None:
        self.calls.append("run_request")
        self._hook("run")
        self._record(record)
        return None                       # submitted; outcome not known yet

    def observe_request(self, request: Any, *, record: dict[str, Any]) -> None:
        self.calls.append("observe_request")
        self._hook("observe")
        self._record(record)

    def cancel_request(self, request: Any, *, record: dict[str, Any], before_cancel: Any) -> None:
        self.calls.append("cancel_request")
        self._hook("cancel_preflight")
        if self.status in TERMINAL:       # already finished: report it, never dispatch a cancel RPC
            self._record(record)
            return
        before_cancel()
        self.calls.append("cancel_rpc")
        self.status = self.cancel_status
        self._record(record)
        self._hook("cancel_ack")

    def recover_request(self, request: Any, *, record: dict[str, Any]) -> None:
        self.calls.append("recover_request")
        self._record(record)
        if self.status in TERMINAL:
            record.update(accounting="pending", result="OK" if self.status == "DONE" else "FAILED")
        return None                       # usage never final here: accounting stays pending


class Host:
    """One fresh service (own data directory) with a fake provider and an injected clock."""

    def __init__(self, root: Path, name: str, provider: Provider | None = None, **config: Any) -> None:
        self.clock = Clock()
        self.provider = provider or Provider()
        values = dict(bind="127.0.0.1:0", backend="auto", data_dir=root / name, receipt_key=RECEIPT_KEY,
                      request_key=REQUEST_KEY, v2_enabled=True)
        self.service = QSearchService(ServiceConfig(**{**values, **config}), clock=self.clock,
                                      runtime_factory=lambda kind: self.provider)

    def __enter__(self) -> "Host":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.service.close()

    def seed(self, request_id: str, *, target: str = "ibm_fez", attempts: int = 1,
             record: dict[str, Any] | None = None, receipt: Any = None) -> Any:
        body = request_body(request_id, target=target)
        request = parse_request(body)
        store = self.service.store
        store.begin_request(request.request_digest, request.request_id, json.dumps(body))
        for _ in range(attempts):
            store.mark_request_attempt(request.request_digest)
        if receipt is not None:
            store.complete_request(request.request_digest, json.dumps(receipt(request)))
        if record is not None:
            store.set_request_record(request.request_digest, json.dumps(record))
        return request


def send_bytes(payload: dict[str, Any]) -> bytes:
    """Exactly ``nano_qsearch.service._Handler._send``."""
    return json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")


class ServiceTransport:
    """``LifecycleClient`` transport answering through ``QSearchService.control_request``."""

    def __init__(self, service: QSearchService) -> None:
        self.service = service
        self.calls: list[tuple[str, str, bytes]] = []
        self.last: tuple[int, bytes] | None = None

    def __call__(self, method: str, url: str, body: bytes, headers: dict[str, str], timeout_s: float):
        if not url.startswith(ORIGIN + PREFIX) or method != "POST":
            raise AssertionError("client contacted an unexpected URL or method")
        self.calls.append((method, url, body))
        status, payload = self.service.control_request(method, url[len(ORIGIN):], body,
                                                       {AUTH_HEADER: headers.get(AUTH_HEADER)})
        self.last = (status, send_bytes(payload))
        return self.last


def replay(status: int, raw: bytes, calls: list[Any] | None = None):
    def transport(method, url, body, headers, timeout_s):
        if calls is not None:
            calls.append((method, url, body))
        return status, raw
    return transport


def invoke(transport: Any, operation: str, digest: str, replacement: str | None = None, *,
           key: bytes = REQUEST_KEY, clock: Any = None,
           nonces: Any = None) -> tuple[dict[str, Any] | None, LifecycleClientError | None]:
    """One explicit control through the real Python client."""
    client = LifecycleClient(ORIGIN, key, transport=transport, clock=clock or (lambda: 1000),
                             nonce_factory=nonces or Nonces(0x5000))
    try:
        if operation == "replace":
            return client.replace(digest, json.loads(replacement)), None
        return getattr(client, operation)(digest), None
    except LifecycleClientError as exc:
        return None, exc


def error_fields(exc: LifecycleClientError) -> dict[str, Any]:
    return {"code": exc.code, "field": exc.field, "retryable": exc.retryable,
            "next_action": exc.next_action, "http_status": exc.http_status}


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _through(host: Host, operation: str, digest: str, replacement: str | None = None, *,
             key: bytes = REQUEST_KEY, nonces: Nonces) -> tuple[int, bytes, dict[str, Any] | None,
                                                                 LifecycleClientError | None]:
    transport = ServiceTransport(host.service)
    payload, error = invoke(transport, operation, digest, replacement, key=key, clock=host.clock, nonces=nonces)
    if len(transport.calls) != 1:
        raise RuntimeError(f"{operation} made {len(transport.calls)} transport calls")
    status, raw = transport.last
    return status, raw, payload, error


def _case(name: str, operation: str, digest: str, replacement: str | None) -> dict[str, Any]:
    out = {"name": name, "operation": operation, "request_digest": digest}
    if replacement is not None:
        out["replacement_b64"] = b64(replacement.encode("utf-8"))
    return out


# --- canonical replacement bytes (CONTRACT.md section 2.1) -----------------------------------------

def _u(hex4: str) -> str:
    """The six-character JSON escape ``<backslash>u<hex4>``."""
    return BS + "u" + hex4


def _desc(inner: str, extra: str = "") -> str:
    return '{"descriptor":' + inner + extra + "}"


def _nest(depth: int) -> str:
    """A replacement whose deepest container sits at ``depth`` (the top-level object is depth 1)."""
    arrays = depth - 2
    return '{"descriptor":{"d":' + "[" * arrays + "0" + "]" * arrays + "}}"


def canonical_specs() -> list[tuple[str, str]]:
    floats_low = ["0.0001", "0.00001", "1e-4", "1E-5", "0.000123", "1.5e-05", "0.00015", "1e-7"]
    floats_high = ["1e15", "1e16", "999999999999999.9", "9999999999999998.0", "1234567890123456.7",
                   "12345678901234567.0", "1E+16", "1e+015"]
    zeros = ["-0.0", "0.0", "-0e0", "0e-10", "-0.0E5", "0.000"]
    extremes = ["5e-324", "4.9e-324", "2.2250738585072014e-308", "1.7976931348623157e308",
                "-1.7976931348623157E+308", "2.5e-320"]
    lexemes = ["1E5", "1e5", "1E+5", "1e+05", "2.5E-3", "-3E2", "7E0", "1.25E1"]
    kinds = ["1", "1.0", "100", "100.0", "1e0", "1E2", "0", "-1", "-1.0", "10.50", "0.1", "123e-2"]
    integers = ["-0", "9007199254740993", "18446744073709551616", "-123456789012345678901234567890",
                "1" + "0" * 100, "-1", "-9007199254740993"]
    # Code point order (Python str) differs from UTF-16 code unit order above U+FFFF.
    keys = ('{"descriptor":{"' + chr(0xE000) + '":1,"' + _u("d800") + _u("dc00") + '":2,"'
            + chr(0xFFFD) + '":3,"' + chr(0x1F600) + '":4,"' + _u("fb01") + '":5,"a":6,"B":7,"~":8,"'
            + chr(0x7F) + '":9,"' + chr(0xE9) + '":10,"' + _u("ffff") + '":11},"'
            + _u("dbff") + _u("dfff") + '":0,"' + chr(0xF900) + '":1}')
    escapes = ('{"descriptor":{"s":"' + _u("0000") + _u("0001") + BS + "b" + BS + "f" + BS + "n" + BS + "r"
               + BS + "t" + _u("000b") + _u("001F") + _u("001e") + BS + '"' + BS + BS + BS + "/" + "/"
               + _u("0022") + _u("005c") + _u("0041") + '","' + BS + "n" + _u("0009") + 'key":"'
               + _u("000A") + '"}}')
    raws = ('{"descriptor":{"r":["' + chr(0x7F) + '","' + _u("007f") + '","' + chr(0x2028) + '","'
            + _u("2029") + '","' + chr(0xE9) + '","' + _u("00E9") + '","' + chr(0x4E2D) + chr(0x6587) + '","'
            + chr(0x1F600) + '","' + _u("d83d") + _u("de00") + '","' + chr(0xFEFF) + '","'
            + chr(0x10FFFF) + '"]}}')
    ws = [" ", chr(9), chr(13) + NL, NL + chr(9) + " "]
    whitespace = (ws[3] + "{" + ws[0] + '"descriptor"' + ws[1] + ":" + ws[2] + "{" + ws[0] + '"a"' + ws[0] + ":"
                  + ws[3] + "[" + ws[1] + "1" + ws[0] + "," + ws[2] + "2.50" + ws[0] + "," + ws[0] + '"x y"'
                  + ws[3] + "]" + ws[1] + "," + ws[0] + '"e"' + ":" + ws[2] + "{" + ws[1] + "}" + ws[0] + "}"
                  + ws[2] + "," + ws[3] + '"z"' + ws[0] + ":" + ws[1] + "null" + ws[0] + "}" + ws[2] + ws[3])
    nested = ('{"jobs":[{"b":[true,false,null],"a":{}}],"descriptor":{"z":{"y":{"x":[[],[{}],'
              '[{"k":[1,2.0,"3"]}]]}},"m":{"b":{"a":1},"a":{"b":2}},"a":[{"z":0,"a":-0.5}],'
              '"empty_s":"","empty_a":[],"empty_o":{}},"a":{"a":{"a":"a"}}}')
    literals = '{"descriptor":{},"x":[],"y":{},"t":true,"f":false,"n":null}'
    return [
        ("float_exponent_minus4_minus5", _desc('{"f":[' + ",".join(floats_low) + "]}")),
        ("float_exponent_15_16", _desc('{"f":[' + ",".join(floats_high) + "]}")),
        ("float_signed_zero", _desc('{"f":[' + ",".join(zeros) + "]}", ',"z":-0.0')),
        ("float_subnormal_and_max_double", _desc('{"f":[' + ",".join(extremes) + "]}")),
        ("float_uppercase_and_signed_exponent_lexemes", _desc('{"f":[' + ",".join(lexemes) + "]}")),
        ("number_kind_follows_lexeme", _desc('{"n":[' + ",".join(kinds) + '],"one":1,"one_float":1.0}')),
        ("integer_huge_negative_and_minus_zero", _desc('{"i":[' + ",".join(integers) + '],"z":-0}')),
        ("key_order_code_point_not_utf16", keys),
        ("string_escapes_control_and_solidus", escapes),
        ("string_raw_del_separators_non_ascii", raws),
        ("whitespace_heavy", whitespace),
        ("nested_descriptor_content", nested),
        ("empty_descriptor_and_literals", literals),
        ("depth_64_boundary", _nest(64)),
        ("realistic_request_v2", replacement_text("nano101-canonical-realistic", "ibm_kingston")),
    ]


def refusal_specs() -> list[tuple[str, str]]:
    return [
        ("float_overflow_positive", _desc('{"f":1e400}')),
        ("float_overflow_negative_outside_descriptor", _desc("{}", ',"f":-1e400')),
        ("float_overflow_just_above_max_double", _desc('{"f":1.8e308}')),
        ("lexeme_nan", _desc('{"f":NaN}')),
        ("lexeme_infinity", _desc('{"f":Infinity}')),
        ("lexeme_negative_infinity", _desc("{}", ',"f":-Infinity')),
        ("lone_high_surrogate_value", _desc('{"s":"' + _u("d800") + '"}')),
        ("lone_low_surrogate_key", _desc('{"' + _u("dc00") + '":1}')),
        ("reversed_surrogate_pair", _desc("{}", ',"s":"' + _u("de00") + _u("d83d") + '"')),
        ("duplicate_member_top_level", _desc('{"a":1}', ',"x":1,"x":2')),
        ("duplicate_member_nested", _desc('{"a":{"b":1,"b":1}}')),
        ("duplicate_member_descriptor", '{"descriptor":{"a":1},"descriptor":{"a":1}}'),
        ("top_level_array", '[{"descriptor":{}}]'),
        ("top_level_string", '"descriptor"'),
        ("top_level_number", "1"),
        ("top_level_null", "null"),
        ("descriptor_missing", '{"schema":"predator.qsearch.request.v2","jobs":[]}'),
        ("descriptor_array", '{"descriptor":[]}'),
        ("descriptor_null", '{"descriptor":null}'),
        ("descriptor_string", '{"descriptor":"x"}'),
        ("depth_65", _nest(65)),
    ]


def _replace_outcome(name: str, text: str) -> str:
    """``"agrees"``: refused locally as INVALID_REQUEST/replacement before I/O; ``"accepts"``: sent."""
    calls: list[Any] = []
    _, error = invoke(replay(503, b'{"error": "capture only"}', calls), "replace", "sha256:" + "0" * 64, text)
    if calls:
        return "accepts"
    if error is not None and (error.code, error.field) == ("INVALID_REQUEST", "replacement"):
        return "agrees"
    raise RuntimeError(f"canonical refusal {name}: unexpected Python outcome")


def build_canonical() -> dict[str, Any]:
    vectors = []
    for name, text in canonical_specs():
        value = json.loads(text)
        canonical = canonical_json(value).encode("utf-8")
        if not isinstance(value, dict) or not isinstance(value.get("descriptor"), dict):
            raise RuntimeError(f"canonical vector {name} has no descriptor object")
        if _replace_outcome(name, text) != "accepts":
            raise RuntimeError(f"canonical vector {name} is refused by the Python client")
        vectors.append({"name": name, "input_b64": b64(text.encode("utf-8")), "canonical_b64": b64(canonical),
                        "descriptor_canonical_b64": b64(canonical_json(value["descriptor"]).encode("utf-8")),
                        "descriptor_digest": canonical_digest(value["descriptor"])})
    refusals = [{"name": name, "input_b64": b64(text.encode("utf-8")), "python_client": _replace_outcome(name, text)}
                for name, text in refusal_specs()]
    return {"contract": CONTRACT, "vectors": vectors, "refusals": refusals}


# --- signing -----------------------------------------------------------------------------------

SIGNING_TEXT_UNICODE = ('{"schema":"x","descriptor":{"note":"caf' + chr(0xE9) + " " + _u("d83d") + _u("de00")
                        + '","y":-0.0,"x":1E5,"big":18446744073709551616},"a":[1.0,0.00001]}')

# (name, operation, base url, key, clock, nonce, replacement text or None)
SIGNING_SPECS: list[tuple[str, str, str, bytes, Any, str, str | None]] = [
    ("refresh_key16_fractional_clock", "refresh", "http://127.0.0.1:8798", b"nano101-synth-16",
     1700000000.9, "0123456789abcdef" * 2, None),
    ("refresh_key32_trailing_slash_nonce64", "refresh", "https://qsearch.example.test/",
     b"nano101-synthetic-request-key-32", 1000, "fedcba9876543210" * 4, None),
    ("cancel_key31_localhost_slash_nonce16", "cancel", "http://localhost:8798/", REQUEST_KEY, 1730000000,
     "a1b2c3d4e5f60718", None),
    ("cancel_key64_ipv6_origin_sub_second_clock", "cancel", "https://[::1]:8443", bytes(range(64)), 0.5,
     "00" * 16, None),
    ("replace_key32_realistic_body", "replace", "https://qsearch.example.test",
     b"nano101-synthetic-request-key-32", 1762560000.75, "9" * 40,
     replacement_text("nano101-signing-replacement", "ibm_kingston")),
    ("replace_key16_unicode_floats_body", "replace", "http://127.0.0.1:8798/", b"\xff" * 16, 4102444800.25,
     "abcdef0123456789" * 3, SIGNING_TEXT_UNICODE),
]


def build_signing() -> list[dict[str, Any]]:
    vectors = []
    for name, operation, base_url, key, clock, nonce, text in SIGNING_SPECS:
        digest = "sha256:" + hashlib.sha256(name.encode()).hexdigest()
        sent: list[tuple[str, str, bytes, dict[str, str]]] = []

        def transport(method, url, body, headers, timeout_s, sent=sent):
            sent.append((method, url, body, dict(headers)))
            return 503, b'{"error": "capture only"}'

        client = LifecycleClient(base_url, key, transport=transport, clock=lambda clock=clock: clock,
                                 nonce_factory=lambda nonce=nonce: nonce)
        try:
            if operation == "replace":
                client.replace(digest, json.loads(text))
            else:
                getattr(client, operation)(digest)
        except LifecycleClientError:
            pass
        if len(sent) != 1:
            raise RuntimeError(f"signing vector {name}: expected exactly one transport call")
        method, url, body, headers = sent[0]
        path = PREFIX + digest + "/" + operation
        ts = int(clock)
        expected_body = canonical_json(json.loads(text)).encode("utf-8") if text is not None else b""
        body_sha256 = hashlib.sha256(expected_body).hexdigest()
        message = NL.join(("POST", path, body_sha256, str(ts), nonce)).encode("utf-8")
        mac = hmac.new(key, message, hashlib.sha256).hexdigest()
        header = f"v1 ts={ts} nonce={nonce} mac={mac}"
        if (method != "POST" or body != expected_body or headers.get(AUTH_HEADER) != header
                or url != base_url.rstrip("/") + path):
            raise RuntimeError(f"signing vector {name} does not match the client")
        verify_request(key, header, "POST", path, body, now=ts, nonce_store=MemoryNonceStore())
        vector = {"name": name, "operation": operation, "base_url": base_url, "request_digest": digest,
                  "clock": clock, "key_hex": key.hex(), "nonce": nonce, "ts": ts, "method": method, "path": path,
                  "url": url, "body_b64": b64(body), "body_sha256": body_sha256, "message_b64": b64(message),
                  "mac_hex": mac, "header": header, "headers": headers}
        if text is not None:
            vector["replacement_b64"] = b64(text.encode("utf-8"))
        vectors.append(vector)
    return vectors


# --- control histories -------------------------------------------------------------------------

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _history(host: Host, name: str, operation: str, digest: str, replacement: str | None,
             nonces: Nonces) -> dict[str, Any]:
    status, raw, payload, error = _through(host, operation, digest, replacement, nonces=nonces)
    _require(error is None and status == 200, f"history {name} did not answer as a 200 envelope")
    expected = json.loads(raw)
    _require(payload == expected, f"history {name}: client result differs from the service body")
    return {**_case(name, operation, digest, replacement), "http_status": status, "body_b64": b64(raw),
            "expected": expected}


def _queued_record(job_id: str, **extra: Any) -> dict[str, Any]:
    return {"provider_status": "QUEUED", "provider_job_id": job_id, "held_qpu_s": 10.0,
            "queue_started_at": 1000.0, **extra}


def history_refresh_queued_to_running(root: Path, nonces: Nonces) -> dict[str, Any]:
    with Host(root, "h-refresh-run", Provider(status="RUNNING", job_id="nano101-job-q2r")) as host:
        request = host.seed("nano101-refresh-run", record=_queued_record("nano101-job-q2r"))
        host.clock.now = 1100.0
        out = _history(host, "refresh_queued_to_running", "refresh", request.request_digest, None, nonces)
        _require(host.provider.calls == ["observe_request"], "refresh must observe exactly once")
        _require(out["expected"]["execution"]["status"] == "RUNNING", "refresh did not reach RUNNING")
        return out


def history_refresh_throttled_unchanged(root: Path, nonces: Nonces) -> dict[str, Any]:
    with Host(root, "h-refresh-throttled", Provider(status="RUNNING", job_id="nano101-job-thr")) as host:
        request = host.seed("nano101-refresh-throttled",
                            record=_queued_record("nano101-job-thr", last_provider_poll_at=1090.0))
        host.clock.now = 1100.0
        out = _history(host, "refresh_throttled_unchanged", "refresh", request.request_digest, None, nonces)
        _require(host.provider.calls == [] and host.service._runtimes == {}, "throttled refresh polled")
        _require(out["expected"]["execution"]["status"] == "QUEUED", "throttled refresh changed state")
        return out


def history_cancel_before_submission(root: Path, nonces: Nonces) -> dict[str, Any]:
    with Host(root, "h-cancel-unsubmitted") as host:
        request = host.seed("nano101-cancel-unsubmitted", attempts=0)
        host.clock.now = 1050.0
        out = _history(host, "cancel_before_submission", "cancel", request.request_digest, None, nonces)
        _require(host.provider.calls == [], "cancel before submission reached the provider")
        _require(out["expected"]["execution"]["status"] == "NOT_RUN", "unsubmitted cancel is not NOT_RUN")
        return out


def history_cancel_dispatched_not_terminal(root: Path, nonces: Nonces) -> dict[str, Any]:
    provider = Provider(status="QUEUED", cancel_status="RUNNING", job_id="nano101-job-dispatched")
    with Host(root, "h-cancel-dispatched", provider) as host:
        request = host.seed("nano101-cancel-dispatched", record=_queued_record("nano101-job-dispatched"))
        host.clock.now = 1200.0
        out = _history(host, "cancel_dispatched_not_terminal", "cancel", request.request_digest, None, nonces)
        _require(provider.calls == ["cancel_request", "cancel_rpc"], "cancel RPC not dispatched exactly once")
        _require(out["expected"]["state"] == "CANCELLATION_REQUESTED",
                 "dispatched cancel is not CANCELLATION_REQUESTED")
        return out


def history_cancel_confirmed(root: Path, nonces: Nonces) -> dict[str, Any]:
    provider = Provider(status="RUNNING", cancel_status="CANCELLED", job_id="nano101-job-confirmed")
    with Host(root, "h-cancel-confirmed", provider) as host:
        request = host.seed("nano101-cancel-confirmed",
                            record={"provider_status": "RUNNING", "provider_job_id": "nano101-job-confirmed",
                                    "held_qpu_s": 10.0, "queue_started_at": 1000.0})
        host.clock.now = 1300.0
        out = _history(host, "cancel_confirmed", "cancel", request.request_digest, None, nonces)
        _require(provider.calls == ["cancel_request", "cancel_rpc", "recover_request"], "cancel confirm calls")
        _require(out["expected"]["execution"]["status"] == "CANCELLED"
                 and out["expected"]["cancellation"]["confirmed"] is True, "provider-confirmed cancel not CANCELLED")
        return out


def history_cancel_raced_by_completion(root: Path, nonces: Nonces) -> dict[str, Any]:
    provider = Provider(status="DONE", cancel_status="CANCELLED", job_id="nano101-job-raced")
    with Host(root, "h-cancel-raced", provider) as host:
        request = host.seed("nano101-cancel-raced",
                            record={"provider_status": "RUNNING", "provider_job_id": "nano101-job-raced",
                                    "held_qpu_s": 10.0, "queue_started_at": 1000.0})
        host.clock.now = 1400.0
        out = _history(host, "cancel_raced_by_completion", "cancel", request.request_digest, None, nonces)
        _require("cancel_rpc" not in provider.calls, "a finished job was sent a cancel RPC")
        _require(out["expected"]["execution"]["status"] != "CANCELLED"
                 and out["expected"]["provider"]["status"] == "DONE", "completed job reported as cancelled")
        return out


def history_cancel_on_settled_is_noop(root: Path, nonces: Nonces) -> dict[str, Any]:
    with Host(root, "h-cancel-settled") as host:
        request = host.seed("nano101-cancel-settled", receipt=settled_receipt(usage=2.5, job_id="nano101-job-set"))
        host.clock.now = 1500.0
        out = _history(host, "cancel_on_settled_is_noop", "cancel", request.request_digest, None, nonces)
        _require(host.provider.calls == [] and host.service._runtimes == {}, "settled cancel reached the provider")
        _require(out["expected"]["state"] == "SETTLED", "settled cancel changed state")
        return out


def _settled_original(host: Host, request_id: str) -> Any:
    return host.seed(request_id, receipt=settled_receipt(usage=2.5, job_id="nano101-job-" + request_id[-8:]))


def history_replace_after_settled(root: Path, nonces: Nonces) -> dict[str, Any]:
    provider = Provider(status="QUEUED", job_id="nano101-job-successor")
    with Host(root, "h-replace", provider) as host:
        original = _settled_original(host, "nano101-replace-original")
        text = replacement_text("nano101-replace-successor", "ibm_kingston")
        host.clock.now = 1600.0
        out = _history(host, "replace_after_settled", "replace", original.request_digest, text, nonces)
        expected = out["expected"]
        _require(provider.calls == ["run_request"], "replacement must submit the new identity exactly once")
        _require(expected["request"]["request_digest"] == canonical_digest(json.loads(text)["descriptor"])
                 != original.request_digest, "replacement digest is not the descriptor digest")
        _require(expected["lineage"]["replaces_request_digest"] == original.request_digest, "lineage missing")
        return out


HISTORY_BUILDERS = (history_refresh_queued_to_running, history_refresh_throttled_unchanged,
                    history_cancel_before_submission, history_cancel_dispatched_not_terminal,
                    history_cancel_confirmed, history_cancel_raced_by_completion, history_cancel_on_settled_is_noop,
                    history_replace_after_settled)


def build_histories(root: Path) -> list[dict[str, Any]]:
    nonces = Nonces(0x101000)
    return [build(root, nonces) for build in HISTORY_BUILDERS]


# --- control errors ----------------------------------------------------------------------------

def _error_case(name: str, operation: str, digest: str, replacement: str | None, status: int, raw: bytes,
                error: LifecycleClientError | None) -> dict[str, Any]:
    _require(error is not None, f"error case {name} did not raise")
    expected = error_fields(error)
    _require(expected["http_status"] == status, f"error case {name}: http_status mismatch")
    return {**_case(name, operation, digest, replacement), "http_status": status, "body_b64": b64(raw),
            "expected_error": expected}


def _error(host: Host, name: str, operation: str, digest: str, replacement: str | None, nonces: Nonces, *,
           key: bytes = REQUEST_KEY) -> dict[str, Any]:
    status, raw, _, error = _through(host, operation, digest, replacement, key=key, nonces=nonces)
    return _error_case(name, operation, digest, replacement, status, raw, error)


def _sign(key: bytes, method: str, path: str, body: bytes, *, ts: int, nonce: str) -> str:
    message = NL.join((method, path, hashlib.sha256(body).hexdigest(), str(ts), nonce)).encode("utf-8")
    return f"v1 ts={ts} nonce={nonce} mac={hmac.new(key, message, hashlib.sha256).hexdigest()}"


def _direct(host: Host, name: str, operation: str, digest: str, body: bytes, nonces: Nonces) -> dict[str, Any]:
    """A request the Python client never sends, answered by the real service and mapped by the client."""
    path = PREFIX + digest + "/" + operation
    headers = {AUTH_HEADER: _sign(REQUEST_KEY, "POST", path, body, ts=int(host.clock()), nonce=nonces())}
    status, payload = host.service.control_request("POST", path, body, headers)
    raw = send_bytes(payload)
    _, error = invoke(replay(status, raw), operation, digest)
    return _error_case(name, operation, digest, None, status, raw, error)


def _replacement_errors(root: Path, nonces: Nonces) -> list[dict[str, Any]]:
    cases = []
    with Host(root, "e-replace") as host:
        running = host.seed("nano101-e-running",
                            record={"provider_status": "RUNNING", "provider_job_id": "nano101-job-e-running"})
        host.clock.now = 2000.0
        cases.append(_error(host, "original_attempt_unreconciled", "replace", running.request_digest,
                            replacement_text("nano101-e-successor-0", "ibm_kingston"), nonces))
        original = _settled_original(host, "nano101-e-original")
        host.clock.now = 2000.0
        cases.append(_error(host, "new_identity_required", "replace", original.request_digest,
                            replacement_text("nano101-e-original", "ibm_kingston"), nonces))
        cases.append(_error(host, "new_target_required", "replace", original.request_digest,
                            replacement_text("nano101-e-successor-1", "ibm_fez"), nonces))
        invalid = json.dumps({"schema": "predator.qsearch.request.v2",
                              "descriptor": {"schema": "predator.qsearch.request.v2",
                                             "request_id": "nano101-e-invalid"}, "jobs": []})
        cases.append(_error(host, "invalid_replacement_body", "replace", original.request_digest, invalid, nonces))
        _require(host.provider.calls == [], "a refused replacement reached the provider")
        status, _, _, _ = _through(host, "replace", original.request_digest,
                                   replacement_text("nano101-e-successor-2", "ibm_kingston"), nonces=nonces)
        _require(status == 200 and host.provider.calls == ["run_request"], "first replacement was not admitted")
        cases.append(_error(host, "replacement_conflict", "replace", original.request_digest,
                            replacement_text("nano101-e-successor-3", "ibm_marrakesh"), nonces))
        _require(host.provider.calls == ["run_request"], "a conflicting replacement reached the provider")

        missing = "sha256:" + hashlib.sha256(b"nano101-missing-request").hexdigest()
        cases.append(_error(host, "request_not_found", "refresh", missing, None, nonces))
        cases.append(_error(host, "unauthorized", "cancel", running.request_digest, None, nonces,
                            key=WRONG_REQUEST_KEY))

        flight = "request:" + running.request_digest
        host.service._inflight[flight] = threading.Event()        # another control holds this request
        try:
            cases.append(_error(host, "request_busy", "refresh", running.request_digest, None, nonces))
        finally:
            del host.service._inflight[flight]

        def store_down(_digest: str) -> Any:
            raise sqlite3.OperationalError("synthetic store failure")

        host.service.store.get_request = store_down
        try:
            cases.append(_error(host, "provider_or_store_unavailable", "refresh", running.request_digest, None,
                                nonces))
        finally:
            del host.service.store.get_request
        cases.append(_direct(host, "empty_body_required", "refresh", running.request_digest, b"{}", nonces))
    return cases


def _adapter_errors(root: Path, nonces: Nonces) -> list[dict[str, Any]]:
    adapter = LifecycleAdapterError
    failures = [
        ("cancellation_not_authorized", "cancel", {"cancel_preflight": lambda: adapter(
            "CANCELLATION_NOT_AUTHORIZED", field="authority")}),
        ("live_context_required", "refresh", {"observe": lambda: adapter(
            "LIVE_CONTEXT_REQUIRED", next_action="MANUAL_RECONCILIATION")}),
        ("provider_status_unknown", "refresh", {"observe": lambda: adapter(
            "PROVIDER_STATUS_UNKNOWN", retryable=True, next_action="RECONCILE")}),
        ("lifecycle_unavailable", "refresh", {"observe": lambda: RuntimeError("synthetic adapter defect")}),
        ("cancel_acknowledgement_lost", "cancel", {"cancel_ack": lambda: TimeoutError("synthetic-provider-token")}),
    ]
    cases = []
    for name, operation, fail in failures:
        provider = Provider(status="QUEUED", cancel_status="QUEUED", job_id="nano101-job-" + name[:20], fail=fail)
        with Host(root, "e-" + name, provider) as host:
            request = host.seed("nano101-e-" + name[:24], record=_queued_record(provider.job_id))
            host.clock.now = 2100.0
            cases.append(_error(host, name, operation, request.request_digest, None, nonces))
    return cases


def build_errors(root: Path) -> list[dict[str, Any]]:
    nonces = Nonces(0x102000)
    cases = _replacement_errors(root, nonces) + _adapter_errors(root, nonces)
    with Host(root, "e-disabled", v2_enabled=False) as host:
        digest = "sha256:" + hashlib.sha256(b"nano101-disabled").hexdigest()
        cases.append(_error(host, "not_enabled", "cancel", digest, None, nonces))
    with Host(root, "e-small-body", max_body_bytes=1024) as host:
        original = _settled_original(host, "nano101-e-small-orig")
        cases.append(_error(host, "body_too_large", "replace", original.request_digest,
                            replacement_text("nano101-e-small-next", "ibm_kingston"), nonces))
        _require(host.provider.calls == [], "an oversized replacement reached the provider")
    return cases


# --- control negatives (CONTRACT.md section 3) ---------------------------------------------------

SENTINEL = "__NANO101_DUPLICATE__"


def _get(value: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        value = value[key]
    return value


def _mutate(base: dict[str, Any], path: tuple[str, ...], value: Any = None, *, delete: bool = False) -> bytes:
    out = copy.deepcopy(base)
    target = _get(out, path[:-1])
    if delete:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return send_bytes(out)


def _duplicate(base: dict[str, Any], path: tuple[str, ...], first: Any) -> bytes:
    """Insert ``"<key>": first`` before the existing member so the LAST duplicate is the valid one."""
    raw = _mutate(base, path, SENTINEL)
    marker = (json.dumps(path[-1]) + ": " + json.dumps(SENTINEL)).encode()
    _require(raw.count(marker) == 1, "duplicate substitution is ambiguous")
    duplicate = json.dumps(path[-1]) + ": " + json.dumps(first)
    original = json.dumps(path[-1]) + ": " + json.dumps(_get(base, path))
    return raw.replace(marker, (duplicate + ", " + original).encode())


def _invalid(status: int) -> dict[str, Any]:
    # ``field`` is deliberately unspecified for INVALID_RESPONSE (v1 convention, CONTRACT v1 2.4):
    # clients may report null or a static schema path, never response content.
    return {"code": "INVALID_RESPONSE", "retryable": False, "next_action": "MANUAL_RECONCILIATION",
            "http_status": status}


def _redirect(status: int) -> dict[str, Any]:
    return {"code": "UNEXPECTED_REDIRECT", "field": None, "retryable": False, "next_action": "NONE",
            "http_status": status}


def negative_specs(h: dict[str, dict[str, Any]]) -> list[tuple[str, str, int, bytes, int, dict[str, Any]]]:
    """(name, base history, http status, body, trailing pad, contract-expected error)."""
    refresh, cancel, replace = (h[n]["expected"] for n in
                                ("refresh_queued_to_running", "cancel_dispatched_not_terminal",
                                 "replace_after_settled"))
    original = h["replace_after_settled"]["request_digest"]
    other = base64.b64decode(h["cancel_confirmed"]["body_b64"])
    oversized = send_bytes(refresh)
    error_dup = send_bytes({"code": "STORE_UNAVAILABLE", "error": "x", "field": None, "next_action": "NONE",
                            "retryable": True}).replace(b'"code": "STORE_UNAVAILABLE"',
                                                        b'"code": "UNAUTHORIZED", "code": "STORE_UNAVAILABLE"')
    return [
        ("refresh_returns_other_request", "refresh_queued_to_running", 200, other, 0, _invalid(200)),
        ("refresh_request_digest_mutated", "refresh_queued_to_running", 200,
         _mutate(refresh, ("request", "request_digest"), "sha256:" + "f" * 64), 0, _invalid(200)),
        ("cancel_returns_other_request", "cancel_dispatched_not_terminal", 200, other, 0, _invalid(200)),
        ("cancel_returns_replacement_digest", "cancel_dispatched_not_terminal", 200,
         _mutate(cancel, ("request", "request_digest"), replace["request"]["request_digest"]), 0, _invalid(200)),
        ("replace_returns_original_digest", "replace_after_settled", 200,
         _mutate(replace, ("request", "request_digest"), original), 0, _invalid(200)),
        ("replace_lineage_missing", "replace_after_settled", 200, _mutate(replace, ("lineage",), delete=True), 0,
         _invalid(200)),
        ("replace_lineage_null", "replace_after_settled", 200,
         _mutate(replace, ("lineage", "replaces_request_digest"), None), 0, _invalid(200)),
        ("replace_lineage_wrong", "replace_after_settled", 200,
         _mutate(replace, ("lineage", "replaces_request_digest"), "sha256:" + "e" * 64), 0, _invalid(200)),
        ("replace_lineage_points_at_itself", "replace_after_settled", 200,
         _mutate(replace, ("lineage", "replaces_request_digest"), replace["request"]["request_digest"]), 0,
         _invalid(200)),
        ("replace_invalid_envelope_extra_member", "replace_after_settled", 200,
         _mutate(replace, ("success",), True), 0, _invalid(200)),
        ("replace_invalid_envelope_unknown_state", "replace_after_settled", 200,
         _mutate(replace, ("state",), "REPLACED"), 0, _invalid(200)),
        ("refresh_redirect_302", "refresh_queued_to_running", 302, send_bytes(refresh), 0, _redirect(302)),
        ("cancel_redirect_307_empty_body", "cancel_dispatched_not_terminal", 307, b"", 0, _redirect(307)),
        ("replace_redirect_308", "replace_after_settled", 308, send_bytes(replace), 0, _redirect(308)),
        ("refresh_oversized_body", "refresh_queued_to_running", 200, oversized,
         _MAX_RESPONSE_BYTES + 1 - len(oversized), _invalid(200)),
        ("cancel_duplicate_key_top_level", "cancel_dispatched_not_terminal", 200,
         _duplicate(cancel, ("state",), "CANCELLED"), 0, _invalid(200)),
        ("replace_duplicate_key_lineage", "replace_after_settled", 200,
         _duplicate(replace, ("lineage", "replaces_request_digest"), "sha256:" + "e" * 64), 0, _invalid(200)),
        ("cancel_error_body_duplicate_key", "cancel_dispatched_not_terminal", 503, error_dup, 0, _invalid(503)),
    ]


def python_outcome(name: str, case: dict[str, Any], status: int, raw: bytes, expected: dict[str, Any]) -> str:
    replacement = base64.b64decode(case["replacement_b64"]).decode("utf-8") if "replacement_b64" in case else None
    _, error = invoke(replay(status, raw), case["operation"], case["request_digest"], replacement)
    if error is None:
        return "accepts"
    if {key: error_fields(error)[key] for key in expected} == expected:
        return "agrees"
    raise RuntimeError(f"negative {name}: unexpected Python outcome {error_fields(error)}")


def build_negatives(histories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {h["name"]: h for h in histories}
    cases = []
    for name, base, status, raw, pad, expected in negative_specs(by_name):
        source = by_name[base]
        replacement = (base64.b64decode(source["replacement_b64"]).decode("utf-8")
                       if "replacement_b64" in source else None)
        case = {**_case(name, source["operation"], source["request_digest"], replacement), "http_status": status,
                "body_b64": b64(raw), "pad_trailing_spaces": pad, "expected_error": expected}
        case["python_client"] = python_outcome(name, case, status, raw + b" " * pad, expected)
        cases.append(case)
    return cases


# --- output ------------------------------------------------------------------------------------

def render(value: Any) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True, ensure_ascii=True, allow_nan=False) + NL).encode("ascii")


def build_fixtures() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="nano101-regen-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        histories = build_histories(root)
        errors = build_errors(root)
    return {
        "fixtures/canonical.v2.json": render(build_canonical()),
        "fixtures/controls-signing.v2.json": render({"contract": CONTRACT, "vectors": build_signing()}),
        "fixtures/controls-histories.v2.json": render({"contract": CONTRACT, "cases": histories}),
        "fixtures/controls-errors.v2.json": render({"contract": CONTRACT, "cases": errors}),
        "fixtures/controls-negative.v2.json": render({"contract": CONTRACT, "cases": build_negatives(histories)}),
    }


def hashed_files(generated: dict[str, bytes]) -> dict[str, bytes]:
    """Every file under v2/ except SHA256SUMS, bytecode and dot-paths, with ``generated`` overriding disk."""
    files = {}
    for path in sorted(V2.rglob("*")):
        rel = path.relative_to(V2).as_posix()
        parts = path.relative_to(V2).parts
        if (path.is_file() and rel != "SHA256SUMS" and path.suffix != ".pyc"
                and not any(part == "__pycache__" or part.startswith(".") for part in parts)):
            files[rel] = path.read_bytes()
    files.update(generated)
    return files


def sha256sums(files: dict[str, bytes]) -> bytes:
    return "".join(f"{hashlib.sha256(files[rel]).hexdigest()}  {rel}{NL}" for rel in sorted(files)).encode("ascii")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate the q-search lifecycle wire v2 fixtures.")
    parser.add_argument("--check", action="store_true", help="exit 1 if a committed file differs")
    args = parser.parse_args(argv)
    outputs = build_fixtures()
    outputs["SHA256SUMS"] = sha256sums(hashed_files(outputs))
    stale = [rel for rel, data in sorted(outputs.items())
             if not (V2 / rel).is_file() or (V2 / rel).read_bytes() != data]
    if args.check:
        for rel in stale:
            print(f"stale: {rel}", file=sys.stderr)
        return 1 if stale else 0
    for rel in stale:
        (V2 / rel).parent.mkdir(parents=True, exist_ok=True)
        (V2 / rel).write_bytes(outputs[rel])
        print(f"wrote {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
