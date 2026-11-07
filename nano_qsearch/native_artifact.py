"""Offline prepared artifacts for native ``prepare_bits``/``sample`` plans.

Parsing is stdlib only. Preparing and loading use the pinned optional Qiskit
compiler and never import an IBM runtime, ATLAS or a network client. An
artifact is an inspection/approval value: it carries no samples, verdicts or
execution claim and never authorizes execution by itself.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import importlib
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from nano.quantum import PrepareBits, QuantumPlan, QuantumValidationError, Sample
from nano.quantum.canonical import canonical_digest, canonical_json
from nano.quantum.native import (ARTIFACT_SCHEMA, PINNED_QISKIT, NativeProfile, NativeTarget, check_schema,
                                 digest, exact, native_plan, native_resources, parse_native_plan,
                                 parse_native_profile, parse_native_target, pub_map_digest)

QPY_NAME = "nano.native.prepare_bits.v1"
METADATA_KEY = "nano.native.v1"
MAX_PUB_QPY_BYTES = 64 * 1024
_ARTIFACT_KEYS = {"schema", "plan", "plan_digest", "pub_map_digest", "profile", "profile_digest", "target",
                  "target_digest", "preparations", "pubs", "resources", "effect", "artifact_digest"}
_PUB_KEYS = {"pub_index", "operation_id", "preparation_id", "register", "width", "shots", "seed", "layout", "ops",
             "isa_digest", "qpy_base64"}


def _fail(code: str, message: str) -> None:
    raise QuantumValidationError(code, message)


@dataclass(frozen=True)
class NativeArtifact:
    _json: str
    plan: QuantumPlan

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._json)

    def _field(self, key: str) -> Any:
        return self.to_payload()[key]

    @property
    def artifact_digest(self) -> str:
        return self._field("artifact_digest")

    @property
    def plan_digest(self) -> str:
        return self._field("plan_digest")

    @property
    def pub_map_digest(self) -> str:
        return self._field("pub_map_digest")

    @property
    def profile_digest(self) -> str:
        return self._field("profile_digest")

    @property
    def target_digest(self) -> str:
        return self._field("target_digest")

    @property
    def target_name(self) -> str:
        return self._field("target")["name"]

    @property
    def resources(self) -> dict[str, Any]:
        return self._field("resources")

    @property
    def pubs(self) -> list[dict[str, Any]]:
        return self._field("pubs")


def _identity(plan: QuantumPlan, profile: NativeProfile, target: NativeTarget, sample: Sample) -> dict[str, Any]:
    return {"plan_digest": plan.plan_digest, "profile_digest": profile.digest, "target_digest": target.digest,
            "operation_id": sample.id, "preparation_id": sample.preparation_id,
            "pub_index": sample.result["pub_index"]}


def _preparations(plan: QuantumPlan) -> list[dict[str, Any]]:
    return [{"preparation_id": o.id, "width": len(o.bits), "x_qubits": [i for i, b in enumerate(o.bits) if b == "1"]}
            for o in plan.operations if isinstance(o, PrepareBits)]


def _expected_pub(preparations: Mapping[str, PrepareBits], sample: Sample) -> dict[str, Any]:
    preparation = preparations[sample.preparation_id]
    width = len(preparation.bits)
    ops = {"measure": width}
    if "1" in preparation.bits:
        ops["x"] = preparation.bits.count("1")
    return {"pub_index": sample.result["pub_index"], "operation_id": sample.id,
            "preparation_id": sample.preparation_id, "register": sample.result["register"], "width": width,
            "shots": sample.shots, "seed": sample.seed, "layout": list(range(width)), "ops": ops}


def _bind(plan: QuantumPlan, profile: NativeProfile, target: NativeTarget) -> None:
    if profile.target_digest != target.digest:
        _fail("IDENTITY", "native profile is bound to a different target")
    if native_resources(plan)["max_width"] > target.num_qubits:
        _fail("WIDTH", "a preparation is wider than the native target")


def _qiskit() -> Any:
    try:
        module = importlib.import_module("qiskit")
    except ImportError:
        _fail("UNAVAILABLE_DEPENDENCY", "the optional pinned qiskit dependency is unavailable")
    if getattr(module, "__version__", None) != PINNED_QISKIT:
        _fail("UNSUPPORTED_SDK", f"native artifacts require the pinned qiskit {PINNED_QISKIT}")
    return module


def build_circuit(qiskit: Any, preparation: PrepareBits, register: str, identity: Mapping[str, Any]) -> Any:
    """The pinned compiler's circuit: X per declared 1-bit, then measure q[i] -> register[i]."""
    width = len(preparation.bits)
    q = qiskit.QuantumRegister(width, "q")
    c = qiskit.ClassicalRegister(width, register)
    circuit = qiskit.QuantumCircuit(q, c, name=QPY_NAME, metadata={METADATA_KEY: dict(identity)})
    for index, bit in enumerate(preparation.bits):
        if bit == "1":
            circuit.x(q[index])
    circuit.measure(q, c)
    return circuit


def qpy_bytes(circuit: Any) -> bytes:
    from qiskit import qpy
    buffer = io.BytesIO()
    qpy.dump(circuit, buffer)
    return buffer.getvalue()


def prepare_native(plan: QuantumPlan, profile: Any, target: Any) -> NativeArtifact:
    """Admit a checked plan and build its pinned local circuits; no execution or provider."""
    plan = native_plan(plan)
    target, profile = parse_native_target(target), parse_native_profile(profile)
    _bind(plan, profile, target)
    qiskit = _qiskit()
    preparations = {o.id: o for o in plan.operations if isinstance(o, PrepareBits)}
    pubs = []
    for sample in (o for o in plan.operations if isinstance(o, Sample)):
        circuit = build_circuit(qiskit, preparations[sample.preparation_id], sample.result["register"],
                                _identity(plan, profile, target, sample))
        binary = qpy_bytes(circuit)
        pubs.append({**_expected_pub(preparations, sample), "isa_digest": "sha256:" + hashlib.sha256(binary).hexdigest(),
                     "qpy_base64": base64.b64encode(binary).decode("ascii")})
    body = {"schema": ARTIFACT_SCHEMA, "plan": plan.to_payload(), "plan_digest": plan.plan_digest,
            "pub_map_digest": pub_map_digest(plan), "profile": profile.to_payload(), "profile_digest": profile.digest,
            "target": target.to_payload(), "target_digest": target.digest, "preparations": _preparations(plan),
            "pubs": pubs, "resources": native_resources(plan), "effect": "local_compile"}
    return parse_native_artifact({**body, "artifact_digest": canonical_digest(body)})


def _check_pub(raw: Any, expected: Mapping[str, Any], seen: set[str]) -> None:
    pub = exact(raw, _PUB_KEYS, "native artifact PUB")
    if type(pub["pub_index"]) is not int or pub["pub_index"] != expected["pub_index"] \
            or pub["operation_id"] != expected["operation_id"]:
        _fail("PUB", "native artifact PUB is missing, duplicated or reordered")
    if pub["width"] != expected["width"] or pub["layout"] != expected["layout"] or pub["register"] != expected["register"]:
        _fail("WIDTH", "native artifact PUB width, layout or register differs from its preparation")
    if any(canonical_json(pub[k]) != canonical_json(expected[k]) for k in expected):
        _fail("IDENTITY", "native artifact PUB fields differ from the checked plan")
    isa = digest(pub["isa_digest"], "isa_digest")
    if isa in seen:
        _fail("PUB", "native artifact repeats a PUB circuit")
    seen.add(isa)
    encoded = pub["qpy_base64"]
    if not isinstance(encoded, str) or len(encoded) > MAX_PUB_QPY_BYTES * 4 // 3 + 4:
        _fail("QPY", "native PUB QPY is absent or too large")
    try:
        binary = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        _fail("QPY", "native PUB QPY encoding is malformed")
    if not binary or len(binary) > MAX_PUB_QPY_BYTES or "sha256:" + hashlib.sha256(binary).hexdigest() != isa:
        _fail("QPY", "native PUB isa_digest differs from its QPY bytes")


def _parse(payload: Any, expected_digest: str | None) -> NativeArtifact:
    if isinstance(payload, NativeArtifact):
        payload = payload.to_payload()
    if isinstance(payload, Mapping):
        check_schema(payload.get("schema"), ARTIFACT_SCHEMA, "native artifact")
    value = exact(payload, _ARTIFACT_KEYS, "native artifact")
    stated = digest(value["artifact_digest"], "artifact_digest")
    if expected_digest is not None and digest(expected_digest, "expected_digest") != stated:
        _fail("IDENTITY", "native artifact differs from the caller-retained digest")
    if canonical_digest({k: v for k, v in value.items() if k != "artifact_digest"}) != stated:
        _fail("DIGEST", "native artifact digest differs from its contents")
    target, profile = parse_native_target(value["target"]), parse_native_profile(value["profile"])
    plan = parse_native_plan(value["plan"])
    if (value["target_digest"] != target.digest or value["profile_digest"] != profile.digest
            or value["plan_digest"] != plan.plan_digest or value["pub_map_digest"] != pub_map_digest(plan)):
        _fail("IDENTITY", "native artifact plan, PUB map, profile or target identity differs from its contents")
    _bind(plan, profile, target)
    if value["effect"] != "local_compile":
        _fail("IDENTITY", "a native artifact records only its local compilation effect")
    if canonical_json(value["preparations"]) != canonical_json(_preparations(plan)):
        _fail("IDENTITY", "native artifact preparations differ from the checked plan")
    samples = [o for o in plan.operations if isinstance(o, Sample)]
    pubs = value["pubs"]
    if not isinstance(pubs, list) or len(pubs) != len(samples):
        _fail("PUB", "native artifact must contain exactly one PUB per admitted sample")
    seen: set[str] = set()
    preparations = {o.id: o for o in plan.operations if isinstance(o, PrepareBits)}
    for raw, sample in zip(pubs, samples):
        _check_pub(raw, _expected_pub(preparations, sample), seen)
    if canonical_json(value["resources"]) != canonical_json(native_resources(plan)):
        _fail("LIMITS", "native artifact resources differ from the checked plan")
    return NativeArtifact(canonical_json(value), plan)


def parse_native_artifact(payload: Any, *, expected_digest: str | None = None) -> NativeArtifact:
    """Validate every field and digest without SDK imports; malformed input fails uniformly."""
    try:
        return _parse(payload, expected_digest)
    except QuantumValidationError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError, StopIteration):
        _fail("FIELDS", "native artifact contains malformed fields")


def _check_loaded(circuit: Any, pub: Mapping[str, Any], preparation: PrepareBits, identity: Mapping[str, Any]) -> None:
    if circuit.name != QPY_NAME or (circuit.metadata or {}) != {METADATA_KEY: dict(identity)}:
        _fail("METADATA", "loaded circuit name or identity metadata differs from the artifact")
    width = pub["width"]
    if (circuit.num_qubits != width or circuit.num_clbits != width or len(circuit.qregs) != 1
            or len(circuit.cregs) != 1 or circuit.cregs[0].name != pub["register"] or circuit.parameters):
        _fail("WIDTH", "loaded circuit width or register differs from the artifact")
    expected = [("x", (i,), ()) for i, bit in enumerate(preparation.bits) if bit == "1"]
    expected += [("measure", (i,), (i,)) for i in range(width)]
    actual = [(inst.operation.name, tuple(circuit.find_bit(q).index for q in inst.qubits),
               tuple(circuit.find_bit(c).index for c in inst.clbits)) for inst in circuit.data]
    if actual != expected:
        _fail("ISA", "loaded circuit instructions differ from the admitted preparation and layout")


def load_native_circuits(artifact: Any, *, expected_digest: str) -> tuple[NativeArtifact, list[Any]]:
    """Load approved QPY with the pinned SDK, re-verify each circuit and never recompile it."""
    artifact = parse_native_artifact(artifact, expected_digest=expected_digest)
    qiskit = _qiskit()
    from qiskit import qpy
    payload = artifact.to_payload()
    target, profile = parse_native_target(payload["target"]), parse_native_profile(payload["profile"])
    preparations = {o.id: o for o in artifact.plan.operations if isinstance(o, PrepareBits)}
    samples = [o for o in artifact.plan.operations if isinstance(o, Sample)]
    circuits = []
    for pub, sample in zip(payload["pubs"], samples):
        raw = base64.b64decode(pub["qpy_base64"], validate=True)
        try:
            loaded = qpy.load(io.BytesIO(raw))
        except Exception:  # noqa: BLE001 - any QPY reader failure is one stable refusal
            _fail("QPY", "the pinned SDK cannot read the prepared QPY")
        if len(loaded) != 1:
            _fail("QPY", "prepared QPY must contain exactly one circuit per PUB")
        identity = _identity(artifact.plan, profile, target, sample)
        preparation = preparations[sample.preparation_id]
        _check_loaded(loaded[0], pub, preparation, identity)
        if qpy_bytes(build_circuit(qiskit, preparation, pub["register"], identity)) != raw:
            _fail("DRIFT", "stored QPY differs from the pinned compiler's rebuild")
        circuits.append(loaded[0])
    return artifact, circuits


__all__ = ["MAX_PUB_QPY_BYTES", "METADATA_KEY", "QPY_NAME", "NativeArtifact", "build_circuit",
           "load_native_circuits", "parse_native_artifact", "prepare_native", "qpy_bytes"]
