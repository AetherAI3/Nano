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


if __name__ == "__main__":
    regenerate()
