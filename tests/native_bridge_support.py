"""Shared fixtures for the native prepare/sample bridge tests (offline only).

Run ``python tests/native_bridge_support.py`` from the repository root to
regenerate the frozen native fixtures after an intentional contract change.
Artifact fields require the pinned optional Qiskit; contract fields do not.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "nano" / "quantum" / "fixtures"
SCHEMAS = ROOT / "nano" / "quantum" / "schemas"
PLAN_FIXTURES = ("known-bits", "one-record", "packed-records")
SYNTHETIC_RECEIPT_KEY = b"synthetic-test-key-native-receipt-0001"
SYNTHETIC_REQUEST_KEY = b"synthetic-test-key-native-request-0001"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nano.quantum import parse_plan  # noqa: E402
from nano.quantum.conformance import large_mapping_fixture  # noqa: E402
from nano.quantum.native import (capability_matrix, local_target, native_profile, native_resources,  # noqa: E402
                                 parse_native_profile, parse_native_target, pub_map_digest)


def read(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def plan(name: str):
    return large_mapping_fixture() if name == "large-mapping" else parse_plan(read(name))


def target_payload() -> dict:
    return local_target(16)


def profile_payload() -> dict:
    return native_profile(parse_native_target(target_payload()).digest)


def contract_golden(name: str) -> dict:
    value = plan(name)
    return {"plan_digest": value.plan_digest, "pub_map_digest": pub_map_digest(value),
            "resources": native_resources(value)}


def artifact_golden(name: str, artifact) -> dict:
    from nano_qsearch.native_request import build_native_request, request_digest
    request = build_native_request(artifact, request_id=f"native-{name}")
    return {"artifact_digest": artifact.artifact_digest, "request_id": f"native-{name}",
            "request_digest": request_digest(request)}


class Clock:
    """Injected host clock (Unix seconds) for MAC freshness and lifecycle deadlines."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def headers(method: str, path: str, raw: bytes = b"", *, now: float = 1000, nonce: str | None = None,
            key: bytes = SYNTHETIC_REQUEST_KEY) -> dict:
    import secrets
    from nano_qsearch.auth import AUTH_HEADER, sign_request
    return {AUTH_HEADER: sign_request(key, method, path, raw, ts=int(now), nonce=nonce or secrets.token_hex(16))}


def native_policy(**overrides):
    from nano_qsearch.native_policy import NativeHostPolicy
    goldens = read("native-goldens")
    values = dict(admitted_profiles={goldens["profile_digest"]}, admitted_targets={goldens["target_digest"]})
    return NativeHostPolicy(**{**values, **overrides})


def service_config(tmp_path, **overrides):
    from nano_qsearch.service import ServiceConfig
    values = dict(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path, receipt_key=SYNTHETIC_RECEIPT_KEY,
                  request_key=SYNTHETIC_REQUEST_KEY, native_policy=native_policy())
    return ServiceConfig(**{**values, **overrides})


def body_bytes(body) -> bytes:
    from nano.quantum.canonical import canonical_json
    return canonical_json(body).encode("utf-8")


def ideal_observations(artifact) -> dict:
    """What an ideal sampler must observe for a known-bit plan (display order = reversed bits)."""
    operations = {o.id: o for o in artifact.plan.operations}
    pubs = []
    for op in (o for o in artifact.plan.operations if o.op == "sample"):
        display = operations[op.preparation_id].bits[::-1]
        pubs.append({"operation_id": op.id, "pub_index": op.result["pub_index"], "register": op.result["register"],
                     "bit_order": "msb_first", "samples": [display] * op.shots})
    return {"schema": "nano.quantum.fixture-result.v1", "plan_digest": artifact.plan_digest, "pubs": pubs,
            "optimization_verdict": "not_applicable", "delivery_verdict": "unverified"}


class FakeRuntime:
    """A local stand-in for the sampler; ``mode`` scripts its behavior. Never a provider."""

    kind = "fake_local"
    qiskit_version = None

    def __init__(self, mode: str = "ideal", recovered=None) -> None:
        import threading
        self.mode, self.recovered = mode, recovered
        self.runs = self.recovers = self.preflights = 0
        self.started, self.release = threading.Event(), threading.Event()

    def preflight(self, artifact, *, expected_digest):
        self.preflights += 1

    def run(self, artifact, *, expected_digest, record):
        self.runs += 1
        self.started.set()
        if self.mode == "block":
            self.release.wait(10)
        if self.mode == "unknown":
            return None
        if self.mode == "raise":
            raise RuntimeError("synthetic local failure")
        body = ideal_observations(artifact)
        if self.mode == "partial":
            body["pubs"][0]["samples"] = body["pubs"][0]["samples"][:-1]
        if self.mode == "claims_pass":
            body["delivery_verdict"] = "pass"
        return body

    def recover(self, artifact, *, record):
        self.recovers += 1
        return self.recovered(artifact) if callable(self.recovered) else None


def _write(name: str, value) -> None:
    (FIXTURES / f"{name}.json").write_text(json.dumps(value, indent=2, sort_keys=True) + "\n",
                                           encoding="utf-8", newline="\n")


def regenerate() -> None:
    _write("native-target", target_payload())
    _write("native-profile", profile_payload())
    _write("native-capabilities", capability_matrix())
    goldens = {"schema": "nano.quantum.native-goldens.v1",
               "target_digest": parse_native_target(target_payload()).digest,
               "profile_digest": parse_native_profile(profile_payload()).digest, "plans": {}}
    try:
        from nano_qsearch.native_artifact import prepare_native
    except ImportError:
        prepare_native = None
    for name in (*PLAN_FIXTURES, "large-mapping"):
        entry = contract_golden(name)
        if prepare_native is not None:
            artifact = prepare_native(plan(name), profile_payload(), target_payload())
            entry.update(artifact_golden(name, artifact))
            if name == "known-bits":
                _write("native-artifact-known-bits", artifact.to_payload())
        goldens["plans"][name] = entry
    _write("native-goldens", goldens)
    _write("native-result-known-bits", frozen_result())


def frozen_result() -> dict:
    """A sealed local record for the frozen known-bits request (synthetic key; no SDK needed)."""
    from nano.quantum.native import PINNED_QISKIT
    from nano_qsearch.native_artifact import parse_native_artifact
    from nano_qsearch.native_policy import result_record, seal_native_result
    from nano_qsearch.native_request import build_native_request, parse_native_request
    request = parse_native_request(build_native_request(read("native-artifact-known-bits"),
                                                        request_id="native-known-bits"))
    observations = ideal_observations(parse_native_artifact(read("native-artifact-known-bits")))
    unsigned = result_record(request, "OK", runtime_kind="local_ideal", qiskit_version=PINNED_QISKIT,
                             observations=observations)
    return seal_native_result(unsigned, SYNTHETIC_RECEIPT_KEY)



if __name__ == "__main__":
    regenerate()
