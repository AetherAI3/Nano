"""Native prepare/sample bridge contract (stdlib only; no SDK, provider or I/O).

These records bind a checked ``prepare_bits``/``sample`` plan to an explicit
local target and compile profile without fabricating a QUBO or QAOA job. They
grant no execution authority: a host admits an exact target-bound identity.
See ``docs/quantum-native-bridge.md`` for the versioned contract.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .. import __version__
from .canonical import canonical_digest, canonical_json, validate_digest
from .contract import (SCHEMA as OPERATIONS_SCHEMA, PrepareBits, QuantumPlan, QuantumValidationError, Sample,
                       checked_plan, parse_plan)

TARGET_SCHEMA = "nano.quantum.native-target.v1"
PROFILE_SCHEMA = "nano.quantum.native-profile.v1"
ARTIFACT_SCHEMA = "nano.quantum.native-artifact.v1"
REQUEST_SCHEMA = "nano.quantum.native-request.v1"
RESULT_SCHEMA = "nano.quantum.native-result.v1"
PUB_MAP_SCHEMA = "nano.quantum.native-pub-map.v1"
CAPABILITIES_SCHEMA = "nano.quantum.native-capabilities.v1"
NATIVE_OPERATIONS = ("prepare_bits", "sample")
LOCAL_TARGET = "local_ideal"
LOCAL_KIND = "local_simulator"
LOCAL_SIMULATOR = "qiskit.primitives.StatevectorSampler"
LOCAL_BASIS = ("measure", "x")
MAX_LOCAL_QUBITS = 20
PINNED_QISKIT = "2.3.1"
COMPILER_NAME = "nano-qsearch"
LAYOUT = "identity"
RESOURCE_KEYS = frozenset({"preparations", "pub_count", "total_shots", "max_shots_per_pub", "max_width",
                           "deadline_s", "qpu_seconds"})
DESCRIPTOR_KEYS = frozenset({"schema", "request_id", "target", "artifact_digest", "plan_digest", "pub_map_digest",
                             "profile_digest", "target_digest", "resources"})
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_TARGET_KEYS = {"schema", "name", "kind", "simulator", "num_qubits", "basis", "qpu_seconds"}
_PROFILE_KEYS = {"schema", "id", "operations", "target_digest", "layout", "optimization_level", "compiler"}
_COMPILER_KEYS = {"name", "version", "qiskit_version", "qiskit_ibm_runtime_version"}


def _fail(code: str, message: str) -> None:
    raise QuantumValidationError(code, message)


def check_schema(value: Any, expected: str, where: str) -> None:
    """Distinguish an unknown document from a known family at another version."""
    if value == expected:
        return
    family = expected.rsplit(".v", 1)[0] + ".v"
    if isinstance(value, str) and value.startswith(family):
        _fail("UNSUPPORTED_VERSION", f"{where} schema version {value!r} is not supported; expected {expected}")
    _fail("SCHEMA", f"{where} schema must be {expected}")


def exact(value: Any, keys: set[str] | frozenset[str], where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        _fail("FIELDS", f"{where} requires exactly {sorted(keys)}")
    return value


def integer(value: Any, where: str, low: int = 0, high: int | None = None) -> int:
    if type(value) is not int or value < low or (high is not None and value > high):
        _fail("TYPE", f"{where} must be an integer in {low}..{high if high is not None else 'max'}")
    return value


def digest(value: Any, where: str) -> str:
    try:
        return validate_digest(value, field=where)
    except ValueError as exc:
        _fail("DIGEST", str(exc))


@dataclass(frozen=True)
class NativeTarget:
    _json: str

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._json)

    @property
    def name(self) -> str:
        return self.to_payload()["name"]

    @property
    def num_qubits(self) -> int:
        return self.to_payload()["num_qubits"]

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_payload())


@dataclass(frozen=True)
class NativeProfile:
    _json: str

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._json)

    @property
    def target_digest(self) -> str:
        return self.to_payload()["target_digest"]

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_payload())


def local_target(num_qubits: int = 16) -> dict[str, Any]:
    return {"schema": TARGET_SCHEMA, "name": LOCAL_TARGET, "kind": LOCAL_KIND, "simulator": LOCAL_SIMULATOR,
            "num_qubits": num_qubits, "basis": list(LOCAL_BASIS), "qpu_seconds": 0}


def parse_native_target(body: Any) -> NativeTarget:
    if isinstance(body, NativeTarget):
        body = body.to_payload()
    if isinstance(body, Mapping):
        check_schema(body.get("schema"), TARGET_SCHEMA, "native target")
    value = exact(body, _TARGET_KEYS, "native target")
    if value["name"] != LOCAL_TARGET:
        _fail("UNSUPPORTED_TARGET", "native v1 admits only the local_ideal simulator target; hardware is not enabled")
    if (value["kind"] != LOCAL_KIND or value["simulator"] != LOCAL_SIMULATOR or value["basis"] != list(LOCAL_BASIS)
            or type(value["qpu_seconds"]) is not int or value["qpu_seconds"] != 0):
        _fail("UNSUPPORTED_TARGET", "native target kind, simulator, basis or QPU allowance is unsupported")
    integer(value["num_qubits"], "native target num_qubits", 1)
    if value["num_qubits"] > MAX_LOCAL_QUBITS:
        _fail("LIMITS", f"native local targets admit at most {MAX_LOCAL_QUBITS} qubits")
    return NativeTarget(canonical_json(value))


def native_profile(target_digest: str, *, profile_id: str = "local-ideal-identity-v1") -> dict[str, Any]:
    return {"schema": PROFILE_SCHEMA, "id": profile_id, "operations": list(NATIVE_OPERATIONS),
            "target_digest": target_digest, "layout": LAYOUT, "optimization_level": 0,
            "compiler": {"name": COMPILER_NAME, "version": __version__, "qiskit_version": PINNED_QISKIT,
                         "qiskit_ibm_runtime_version": None}}


def parse_native_profile(body: Any) -> NativeProfile:
    if isinstance(body, NativeProfile):
        body = body.to_payload()
    if isinstance(body, Mapping):
        check_schema(body.get("schema"), PROFILE_SCHEMA, "native profile")
    value = exact(body, _PROFILE_KEYS, "native profile")
    if (not isinstance(value["id"], str) or not value["id"] or len(value["id"]) > 128
            or value["id"] != value["id"].strip()):
        _fail("TYPE", "native profile id must be trimmed text of at most 128 characters")
    if value["operations"] != list(NATIVE_OPERATIONS):
        _fail("UNSUPPORTED_OPERATION", "native profile v1 covers exactly prepare_bits and sample")
    digest(value["target_digest"], "native profile target_digest")
    if value["layout"] != LAYOUT or type(value["optimization_level"]) is not int or value["optimization_level"] != 0:
        _fail("UNSUPPORTED_CAPABILITY", "native profile v1 requires identity layout and optimization level 0")
    compiler = exact(value["compiler"], _COMPILER_KEYS, "native profile compiler")
    if compiler["name"] != COMPILER_NAME or compiler["version"] != __version__:
        _fail("UNSUPPORTED_COMPILER", "native profile compiler differs from the installed nano-qsearch compiler")
    if compiler["qiskit_version"] != PINNED_QISKIT or compiler["qiskit_ibm_runtime_version"] is not None:
        _fail("UNSUPPORTED_SDK", f"native profile requires qiskit {PINNED_QISKIT} and no IBM runtime")
    return NativeProfile(canonical_json(value))


def native_plan(plan: QuantumPlan) -> QuantumPlan:
    """Re-admit a plan and refuse anything outside the native prepare/sample capability."""
    plan = checked_plan(plan)
    sampled: dict[str, int] = {}
    preparations = {o.id: o for o in plan.operations if isinstance(o, PrepareBits)}
    for operation in plan.operations:
        if operation.op not in NATIVE_OPERATIONS:
            _fail("UNSUPPORTED_OPERATION", f"{operation.op} has no native bridge; use its own adapter")
        if isinstance(operation, Sample):
            if operation.backend != LOCAL_TARGET or operation.limits["max_qpu_s"] != 0:
                _fail("UNSUPPORTED_TARGET", "native v1 samples only local_ideal with zero QPU seconds")
            sampled[operation.preparation_id] = sampled.get(operation.preparation_id, 0) + 1
    if any(count > 1 and preparations[ref].packets for ref, count in sampled.items()):
        _fail("RECIPIENT", "sampling one packet-bearing preparation twice would deliver a recipient/chunk twice")
    return plan


def parse_native_plan(payload: Any) -> QuantumPlan:
    """Parse an operations-v1 payload for the native bridge with stable unsupported codes."""
    if isinstance(payload, Mapping):
        check_schema(payload.get("schema"), OPERATIONS_SCHEMA, "operations plan")
        operations = payload.get("operations")
        if isinstance(operations, list):
            for item in operations:
                if isinstance(item, Mapping) and item.get("op") not in NATIVE_OPERATIONS:
                    _fail("UNSUPPORTED_OPERATION", "native bridge admits only prepare_bits and sample")
    return native_plan(parse_plan(payload))


def pub_map(plan: QuantumPlan) -> list[dict[str, Any]]:
    """The frozen PUB -> register/bit -> recipient/chunk/slot mapping, in PUB order."""
    plan = native_plan(plan)
    preparations = {o.id: o for o in plan.operations if isinstance(o, PrepareBits)}
    out = []
    for operation in plan.operations:
        if not isinstance(operation, Sample):
            continue
        preparation = preparations[operation.preparation_id]
        offset, packets = 0, []
        for packet in preparation.packets:
            packets.append({"recipient_id": packet.recipient_id, "chunk": packet.chunk, "slot": packet.slot,
                            "offset": offset, "payload_width": packet.payload_width,
                            "authentication_width": packet.authentication_width,
                            "bit_significance": packet.bit_significance, "codec": packet.codec,
                            "codec_version": packet.codec_version, "evidence_reference": packet.evidence_reference,
                            "evidence_digest": packet.evidence_digest})
            offset += packet.payload_width + packet.authentication_width
        out.append({"pub_index": operation.result["pub_index"], "operation_id": operation.id,
                    "preparation_id": preparation.id, "register": operation.result["register"],
                    "width": operation.result["width"], "bit_order": operation.result["bit_order"],
                    "shots": operation.shots, "seed": operation.seed, "variables": list(preparation.variables),
                    "packets": packets})
    return out


def pub_map_digest(plan: QuantumPlan) -> str:
    plan = native_plan(plan)
    return canonical_digest({"schema": PUB_MAP_SCHEMA, "plan_digest": plan.plan_digest, "pubs": pub_map(plan)})


def native_resources(plan: QuantumPlan) -> dict[str, Any]:
    """Declared allocation: what a host checks before parsing circuits or admitting a job."""
    plan = native_plan(plan)
    preparations = [o for o in plan.operations if isinstance(o, PrepareBits)]
    samples = [o for o in plan.operations if isinstance(o, Sample)]
    return {"preparations": len(preparations), "pub_count": len(samples),
            "total_shots": sum(o.shots for o in samples),
            "max_shots_per_pub": max((o.shots for o in samples), default=0),
            "max_width": max(len(o.bits) for o in preparations) if preparations else 0,
            "deadline_s": min((o.limits["max_wall_s"] for o in samples), default=None), "qpu_seconds": 0}


def parse_resources(value: Any, where: str = "resources") -> dict[str, Any]:
    value = exact(value, RESOURCE_KEYS, where)
    for key in ("preparations", "pub_count", "total_shots", "max_shots_per_pub", "max_width"):
        integer(value[key], f"{where}.{key}")
    if value["deadline_s"] is not None:
        integer(value["deadline_s"], f"{where}.deadline_s", 1)
    if (value["pub_count"] == 0) != (value["deadline_s"] is None):
        _fail("LIMITS", f"{where}.deadline_s is null exactly when no PUB is requested")
    if type(value["qpu_seconds"]) is not int or value["qpu_seconds"] != 0:
        _fail("UNSUPPORTED_TARGET", "native v1 resources admit zero QPU seconds")
    return dict(value)


def parse_request_descriptor(value: Any) -> dict[str, Any]:
    """Validate the signed request header alone, before any artifact is parsed."""
    if isinstance(value, Mapping):
        check_schema(value.get("schema"), REQUEST_SCHEMA, "native request descriptor")
    value = exact(value, DESCRIPTOR_KEYS, "native request descriptor")
    if not isinstance(value["request_id"], str) or _REQUEST_ID.fullmatch(value["request_id"]) is None:
        _fail("TYPE", "request_id must be 1..80 characters of [A-Za-z0-9_-]")
    if value["target"] != LOCAL_TARGET:
        _fail("UNSUPPORTED_TARGET", "native v1 requests name only the local_ideal target")
    for key in ("artifact_digest", "plan_digest", "pub_map_digest", "profile_digest", "target_digest"):
        digest(value[key], f"descriptor.{key}")
    return {**value, "resources": parse_resources(value["resources"], "descriptor.resources")}


def capability_matrix() -> dict[str, Any]:
    """Which surface supports which operation; frozen by ``native-capabilities.json``."""
    native = {"operations_v1": "supported", "direct_local_adapter": "supported", "legacy_job_adapter": "unsupported",
              "legacy_prepared_artifact": "unsupported", "native_artifact": "supported",
              "native_service": "opt_in_local_only", "hardware": "unsupported"}
    return {"schema": CAPABILITIES_SCHEMA,
            "operations": {"prepare_bits": dict(native), "sample": dict(native),
                           "optimize_qubo": {"operations_v1": "supported", "direct_local_adapter": "unsupported",
                                             "legacy_job_adapter": "supported", "legacy_prepared_artifact": "supported",
                                             "native_artifact": "unsupported", "native_service": "unsupported",
                                             "hardware": "legacy_host_gated"}},
            "native_targets": [LOCAL_TARGET],
            "defaults": {"native_service_enabled": False, "native_enables_hardware": False,
                         "preparation_only_artifact": "admitted_without_execution"}}


__all__ = ["ARTIFACT_SCHEMA", "CAPABILITIES_SCHEMA", "COMPILER_NAME", "DESCRIPTOR_KEYS", "LAYOUT", "LOCAL_BASIS",
           "LOCAL_KIND", "LOCAL_SIMULATOR", "LOCAL_TARGET", "MAX_LOCAL_QUBITS", "NATIVE_OPERATIONS", "PINNED_QISKIT",
           "PROFILE_SCHEMA", "PUB_MAP_SCHEMA", "REQUEST_SCHEMA", "RESOURCE_KEYS", "RESULT_SCHEMA", "TARGET_SCHEMA",
           "NativeProfile", "NativeTarget", "capability_matrix", "check_schema", "local_target", "native_plan",
           "native_profile", "native_resources", "parse_native_plan", "parse_native_profile",
           "parse_native_target", "parse_request_descriptor", "parse_resources", "pub_map", "pub_map_digest"]
