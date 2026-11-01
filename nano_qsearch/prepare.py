"""Offline, explicit quantum compilation and approval artifacts.

The module imports no quantum SDK at import time. Parsing/checking is stdlib
only; preparing/loading uses the pinned optional Qiskit compiler, never an IBM
service. An artifact is an inspection/approval value, not execution authority.
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nano import __version__

from .canonical import canonical_digest, canonical_json, validate_digest
from .compile_manifest import MAX_JSON_INTEGER, MAX_RECIPIENTS, MAX_SHOTS, MAX_WIDTH
from .request import MAX_BLOCKS_LIMIT, QSearchRequest, parse_request

PROFILE_SCHEMA = "nano.qsearch.compile-profile.v1"
SNAPSHOT_SCHEMA = "nano.qsearch.target-snapshot.v1"
ARTIFACT_SCHEMA = "nano.qsearch.prepared-artifact.v1"
CHECK_SCHEMA = "nano.qsearch.check.v1"
PINNED_QISKIT = "2.3.1"
COMPILER_NAME = "nano-qsearch"
OPERATIONS_SCHEMA = "nano.quantum.operations.v1"
SUPPORTED_OPERATION = "optimize_qubo"
_COMPILER_KEYS = {"name", "version", "qiskit_version", "qiskit_ibm_runtime_version"}
_DEVICE_NAMES = {"ibm_fez", "ibm_kingston", "ibm_marrakesh"}
_GATES = {"id": 1, "x": 1, "y": 1, "z": 1, "h": 1, "s": 1, "sdg": 1,
          "t": 1, "tdg": 1, "sx": 1, "sxdg": 1, "rx": 1, "ry": 1, "rz": 1,
          "cx": 2, "cy": 2, "cz": 2, "ecr": 2, "swap": 2, "iswap": 2,
          "rxx": 2, "ryy": 2, "rzz": 2, "measure": 1, "reset": 1}
MAX_QPY_BYTES = 64 * 1024 * 1024


class PreparationError(ValueError):
    """Stable diagnostic code plus a bounded, public explanation."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _fail(message: str, code: str = "invalid_plan") -> None:
    raise PreparationError(code, message)


def _obj(value: Any, keys: set[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail(f"{field}: missing or unexpected fields")
    return value


def _int(value: Any, field: str, low: int = 0, high: int = MAX_JSON_INTEGER) -> int:
    if type(value) is not int or not low <= value <= high:
        _fail(f"{field}: invalid integer")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        _fail(f"{field}: expected a nonempty string of at most 256 characters")
    return value


def _number(value: Any, field: str, *, nullable: bool = True, high: float | None = None) -> Any:
    if value is None and nullable:
        return None
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value) and value >= 0 and (high is None or value <= high))
    except OverflowError:
        valid = False
    if not valid:
        _fail(f"{field}: expected a finite nonnegative number")
    return value


def _digest(value: Any, field: str) -> str:
    try:
        return validate_digest(value, field=field)
    except ValueError:
        _fail(f"{field}: invalid sha256 digest")


def _copy(value: Any) -> Any:
    try:
        return json.loads(canonical_json(value))
    except (ValueError, TypeError, RecursionError):
        _fail("document is not canonical JSON")


@dataclass(frozen=True)
class CompileProfile:
    _json: str

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._json)

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_payload())

    @property
    def reference(self) -> dict[str, Any]:
        value = self.to_payload()
        return {"schema": PROFILE_SCHEMA, "id": value["id"], "digest": self.digest}


def parse_compile_profile(payload: Any) -> CompileProfile:
    if isinstance(payload, CompileProfile):
        payload = payload.to_payload()
    value = _obj(payload, {"schema", "id", "operation", "basis_prep", "layout", "seed",
                           "optimization_level", "compiler", "atlas_source_digest"}, "profile")
    if value["schema"] != PROFILE_SCHEMA:
        _fail("profile: unsupported schema version")
    _text(value["id"], "profile.id")
    operation = _obj(value["operation"], {"schema", "op"}, "profile.operation")
    if operation["schema"] != OPERATIONS_SCHEMA or operation["op"] != SUPPORTED_OPERATION:
        _fail("profile: unsupported operation", "unsupported_capability")
    if type(value["basis_prep"]) is not bool:
        _fail("profile.basis_prep: expected boolean")
    if value["layout"] not in ("none", "target"):
        _fail("profile: supported layouts are none and target", "unsupported_capability")
    _int(value["seed"], "profile.seed")
    _int(value["optimization_level"], "profile.optimization_level", high=3)
    compiler = _obj(value["compiler"], _COMPILER_KEYS, "profile.compiler")
    for key, item in compiler.items():
        if item is not None or key != "qiskit_ibm_runtime_version":
            _text(item, "profile.compiler." + key)
    if (compiler["name"] != COMPILER_NAME or compiler["version"] != __version__
            or compiler["qiskit_version"] != PINNED_QISKIT
            or compiler["qiskit_ibm_runtime_version"] is not None):
        _fail("profile: unsupported pinned compiler or SDK", "unsupported_capability")
    if value["atlas_source_digest"] is not None:
        _digest(value["atlas_source_digest"], "profile.atlas_source_digest")
    return CompileProfile(canonical_json(_copy(value)))


@dataclass(frozen=True)
class TargetSnapshot:
    _json: str

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._json)

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_payload())

    @property
    def calibration_digest(self) -> str:
        value = self.to_payload()
        return canonical_digest({"instructions": value["instructions"], "qubit_properties": value["qubit_properties"]})

    @property
    def structural_digest(self) -> str:
        value = self.to_payload()
        operations = {name: (None if rows[0]["qargs"] is None else sorted(row["qargs"] for row in rows))
                      for name, rows in value["instructions"].items()}
        return canonical_digest({"schema": "nano.qsearch.device-target.v1", "num_qubits": value["num_qubits"],
                                 "operations": operations})

    def to_backend(self) -> Any:
        """Construct a local Target; no runtime package, credentials or service."""
        value = self.to_payload()
        unknown = set(value["instructions"]) - set(_GATES)
        if unknown:
            _fail("target contains instructions unsupported by the offline adapter", "unsupported_capability")
        _qiskit()
        from qiskit.transpiler import InstructionProperties, QubitProperties, Target
        gates = _standard_gates()
        props = [QubitProperties(**row) for row in value["qubit_properties"]]
        target = Target(num_qubits=value["num_qubits"], qubit_properties=props, **value["timing_properties"])
        for name, rows in value["instructions"].items():
            properties = {None if row["qargs"] is None else tuple(row["qargs"]):
                          InstructionProperties(error=row["error"], duration=row["duration"]) for row in rows}
            target.add_instruction(gates[name], properties)
        return SimpleNamespace(name=value["name"], target=target)


def parse_target_snapshot(payload: Any) -> TargetSnapshot:
    if isinstance(payload, TargetSnapshot):
        payload = payload.to_payload()
    if payload is None:
        _fail("a local pinned target snapshot is required", "missing_target")
    value = _obj(payload, {"schema", "name", "num_qubits", "instructions", "qubit_properties", "timing_properties"}, "target snapshot")
    if value["schema"] != SNAPSHOT_SCHEMA:
        _fail("target snapshot: unsupported schema version")
    _text(value["name"], "target.name")
    if value["name"] not in _DEVICE_NAMES:
        _fail("offline preparation requires an explicit supported device snapshot", "unsupported_capability")
    width = _int(value["num_qubits"], "target.num_qubits", low=1, high=MAX_WIDTH)
    instructions = value["instructions"]
    if not isinstance(instructions, Mapping) or not instructions:
        _fail("target.instructions: expected a nonempty object")
    for name, rows in instructions.items():
        _text(name, "instruction name")
        if not isinstance(rows, list) or not rows:
            _fail("target instruction: expected a nonempty ordered list")
        previous = None
        seen = set()
        for row in rows:
            row = _obj(row, {"qargs", "error", "duration"}, "target instruction")
            qargs = row["qargs"]
            if qargs is None:
                if len(rows) != 1:
                    _fail("global target instruction must be the sole entry")
            elif (not isinstance(qargs, list) or not qargs or any(type(q) is not int or not 0 <= q < width for q in qargs)
                    or len(set(qargs)) != len(qargs) or (name in _GATES and len(qargs) != _GATES[name])):
                _fail("target instruction qargs are malformed")
            marker = None if qargs is None else tuple(qargs)
            if marker in seen or (previous is not None and marker is not None and marker <= previous):
                _fail("target instruction qargs must be unique and sorted")
            seen.add(marker)
            previous = marker
            _number(row["error"], "instruction error", high=1)
            _number(row["duration"], "instruction duration")
    props = value["qubit_properties"]
    if not isinstance(props, list) or len(props) != width:
        _fail("target.qubit_properties: expected one entry per qubit")
    for row in props:
        row = _obj(row, {"t1", "t2", "frequency"}, "qubit properties")
        for name, item in row.items():
            _number(item, "qubit " + name)
    timing = _obj(value["timing_properties"], {"dt", "granularity", "min_length", "pulse_alignment", "acquire_alignment"}, "target timing properties")
    _number(timing["dt"], "target dt")
    if timing["dt"] == 0:
        _fail("target dt must be positive when present")
    for field in ("granularity", "min_length", "pulse_alignment", "acquire_alignment"):
        _int(timing[field], field, low=1)
    return TargetSnapshot(canonical_json(_copy(value)))


def _standard_gates() -> dict[str, Any]:
    _qiskit()
    from qiskit.circuit import Measure, Parameter, Reset
    from qiskit.circuit.library import (CXGate, CYGate, CZGate, ECRGate, HGate, IGate, iSwapGate,
                                       RXGate, RXXGate, RYGate, RYYGate, RZGate, RZZGate, SGate,
                                       SdgGate, SXGate, SXdgGate, SwapGate, TGate, TdgGate,
                                       XGate, YGate, ZGate)
    theta = Parameter("theta")
    return {"id": IGate(), "x": XGate(), "y": YGate(), "z": ZGate(), "h": HGate(),
            "s": SGate(), "sdg": SdgGate(), "t": TGate(), "tdg": TdgGate(), "sx": SXGate(),
            "sxdg": SXdgGate(), "rx": RXGate(theta), "ry": RYGate(theta), "rz": RZGate(theta),
            "cx": CXGate(), "cy": CYGate(), "cz": CZGate(), "ecr": ECRGate(), "swap": SwapGate(),
            "iswap": iSwapGate(), "rxx": RXXGate(theta), "ryy": RYYGate(theta), "rzz": RZZGate(theta),
            "measure": Measure(), "reset": Reset()}


def _validate_target_definitions(target: Any) -> None:
    """Snapshot names mean standard gates, never a broader implicit capability."""
    gates = _standard_gates()
    from qiskit.circuit import Parameter
    if not callable(getattr(target, "operation_from_name", None)):
        _fail("target does not expose public operation definitions", "unsupported_capability")
    for name in target.operation_names:
        if name not in gates:
            _fail("target contains an unsupported operation definition", "unsupported_capability")
        actual, expected = target.operation_from_name(name), gates[name]
        params = getattr(actual, "params", None)
        if (getattr(actual, "base_class", None) is not expected.base_class
                or getattr(actual, "num_qubits", None) != expected.num_qubits
                or getattr(actual, "num_clbits", None) != expected.num_clbits
                or not isinstance(params, list) or len(params) != len(expected.params)
                or any(not isinstance(param, Parameter) for param in params)):
            _fail("target operation differs from the supported standard parameterized gate", "unsupported_capability")


def snapshot_from_backend(backend: Any) -> TargetSnapshot:
    """Capture an already-resolved backend's public Target, with calibration.

    Reading this object does not resolve a backend or construct a service.
    Callers must obtain the permitted backend through their host admission gate.
    """
    target = backend.target
    _validate_target_definitions(target)
    instructions = {}
    for name in sorted(target.operation_names):
        qargs = target.qargs_for_operation_name(name)
        keys = [None] if qargs is None else sorted(qargs)
        rows = []
        for key in keys:
            prop = target[name].get(key)
            rows.append({"qargs": None if key is None else list(key), "error": getattr(prop, "error", None),
                         "duration": getattr(prop, "duration", None)})
        instructions[name] = rows
    raw_properties = getattr(target, "qubit_properties", None)
    properties = [{name: getattr(raw_properties[i], name, None) if raw_properties is not None else None
                   for name in ("t1", "t2", "frequency")} for i in range(target.num_qubits)]
    name = backend.name() if callable(backend.name) else backend.name
    timing = {field: getattr(target, field, None if field == "dt" else 1)
              for field in ("dt", "granularity", "min_length", "pulse_alignment", "acquire_alignment")}
    return parse_target_snapshot({"schema": SNAPSHOT_SCHEMA, "name": name, "num_qubits": int(target.num_qubits),
                                  "instructions": instructions, "qubit_properties": properties, "timing_properties": timing})


def _qiskit() -> Any:
    try:
        module = importlib.import_module("qiskit")
    except ImportError:
        _fail("the pinned optional qiskit dependency is unavailable", "unavailable_dependency")
    if getattr(module, "__version__", None) != PINNED_QISKIT:
        _fail("installed qiskit differs from the declared pinned compiler", "unsupported_capability")
    return module


def atlas_source_digest() -> str:
    """Identify the exact local ATLAS source used for logical construction."""
    try:
        modules = [importlib.import_module("aether_atlas.qcompiler." + name)
                   for name in ("nuclei", "nucleus_program")]
        contents = {module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for module in modules}
    except (ImportError, AttributeError, OSError):
        _fail("the pinned optional ATLAS source is unavailable", "unavailable_dependency")
    return canonical_digest(contents)


def _request(body: Any) -> QSearchRequest:
    try:
        return parse_request(body, max_blocks=MAX_BLOCKS_LIMIT)
    except (ValueError, TypeError, OverflowError, RecursionError):
        _fail("request does not satisfy the local q-search schema")


def _inputs(body: Any, profile: Any, snapshot: Any) -> tuple[QSearchRequest, CompileProfile, TargetSnapshot]:
    request, profile, snapshot = _request(body), parse_compile_profile(profile), parse_target_snapshot(snapshot)
    declared = profile.to_payload()
    if snapshot.to_payload()["name"] != request.target:
        _fail("target snapshot differs from the requested target")
    if any(job.compile_seed != declared["seed"] or job.opt_level != declared["optimization_level"] for job in request.jobs):
        _fail("job compile seed or optimization level differs from the explicit profile")
    _int(request.descriptor["batch_index"], "request batch_index")
    for job, block in zip(request.jobs, request.blocks):
        _int(job.n, "PUB width", low=1, high=MAX_WIDTH)
        _int(job.shots, "PUB shots", low=1, high=MAX_SHOTS)
        _int(job.backend_seed, "PUB sample seed")
        for field in ("max_variables", "max_wall_s", "max_qpu_s", "chi_max"):
            _int(getattr(job, field), "job " + field)
        if len(block.recipients) > MAX_RECIPIENTS:
            _fail("PUB recipients exceed the packed provenance limit")
        for agent, chunk, slot in block.recipients:
            _text(agent, "recipient agent_id")
            _int(chunk, "recipient chunk")
            _int(slot, "recipient slot")
    if max(job.n for job in request.jobs) > snapshot.to_payload()["num_qubits"]:
        _fail("request width exceeds the target snapshot")
    if set(snapshot.to_payload()["instructions"]) - set(_GATES):
        _fail("target contains instructions unsupported by the offline adapter", "unsupported_capability")
    return request, profile, snapshot


def _logical_identity(request: QSearchRequest) -> str:
    return canonical_digest([{"variables": list(job.variables), "q": job.q, "offset": job.offset,
                              "variant": job.variant, "p": job.p, "gammas": job.gammas, "betas": job.betas,
                              "recipients": block.recipients_payload()} for job, block in zip(request.jobs, request.blocks)])


def _resources(request: QSearchRequest, pubs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"pub_count": request.block_count, "shots": [job.shots for job in request.jobs],
            "total_shots": sum(job.shots for job in request.jobs), "widths": [job.n for job in request.jobs],
            "gate_counts": None if pubs is None else [pub["inspection"]["ops"] for pub in pubs],
            "gate_counts_status": "requires_preparation" if pubs is None else "compiled",
            "resource_limits": {"max_execution_time": request.max_execution_time,
                                "jobs": [_copy(job.payload["limits"]) for job in request.jobs]},
            "usage": {"estimated": {"qpu_seconds_upper_bound": request.max_execution_time,
                                    "method": "declared execution cap; not a provider quote"},
                      "observed": None, "settled": None}}


def check_request(request_body: Any, profile_payload: Any, target_snapshot_payload: Any = None) -> dict[str, Any]:
    """Check local documents without importing an optional SDK or reading credentials."""
    report: dict[str, Any] = {"schema": CHECK_SCHEMA, "ok": False, "diagnostics": [],
                            "validation_scope": "local_request_profile_target", "isa_validation": "not_prepared"}
    try:
        request = _request(request_body)
        report.update(request_digest=request.request_digest, logical_identity=_logical_identity(request),
                      resources=_resources(request))
        request, profile, snapshot = _inputs(request_body, profile_payload, target_snapshot_payload)
        report.update(ok=True, profile_digest=profile.digest, target_snapshot_digest=snapshot.digest,
                      calibration_digest=snapshot.calibration_digest)
    except PreparationError as exc:
        report["diagnostics"] = [{"code": exc.code, "message": str(exc)}]
    return report


def _qpy_bytes(circuit: Any) -> bytes:
    """Match compile._qpy_sha256: stable circuit name and no transient layout."""
    from qiskit import QuantumCircuit, qpy
    from .compile import QPY_CIRCUIT_NAME
    bare = QuantumCircuit(*circuit.qregs, *circuit.cregs, name=QPY_CIRCUIT_NAME,
                          global_phase=circuit.global_phase, metadata=dict(circuit.metadata or {}))
    for inst in circuit.data:
        bare.append(inst)
    buffer = io.BytesIO()
    qpy.dump(bare, buffer)
    return buffer.getvalue()


def _plan_digest(pub: Mapping[str, Any]) -> str:
    record = pub["compile_record"]
    return canonical_digest({"schema": "nano.qsearch.compiled-plan.v1",
        **{key: record[key] for key in ("target", "coupling_map", "physical_qubits", "basis", "opt_level", "seed")},
        "variant": pub["variant"], "nuclei": pub["nuclei"], "qpy_sha256": pub["isa_digest"][7:],
        "inspection": pub["inspection"], "backend_name": record["backend_name"], "target_digest": record["target_digest"],
        **({"rewrites": record["rewrites"]} if "rewrites" in record else {})})


@dataclass(frozen=True)
class PreparedArtifact:
    _json: str

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._json)

    @property
    def artifact_digest(self) -> str:
        return self.to_payload()["artifact_digest"]

    @property
    def request_body(self) -> dict[str, Any]:
        return self.to_payload()["request"]

    @property
    def request(self) -> QSearchRequest:
        return _request(self.request_body)

    @property
    def profile_reference(self) -> dict[str, Any]:
        return parse_compile_profile(self.to_payload()["profile"]).reference

    @property
    def compiler(self) -> dict[str, Any]:
        return self.to_payload()["profile"]["compiler"]

    @property
    def target_snapshot(self) -> TargetSnapshot:
        return parse_target_snapshot(self.to_payload()["target_snapshot"])

    @property
    def snapshot_digest(self) -> str:
        return self.target_snapshot.digest

    @property
    def calibration_digest(self) -> str:
        return self.target_snapshot.calibration_digest

    @property
    def compiled_plan_evidence(self) -> list[dict[str, Any]]:
        return [{key: pub[key] for key in ("compiled_plan_digest", "isa_digest", "layout")} for pub in self.to_payload()["pubs"]]


def prepare_request(request_body: Any, profile_payload: Any, target_snapshot_payload: Any) -> PreparedArtifact:
    request, profile, snapshot = _inputs(request_body, profile_payload, target_snapshot_payload)
    declared = profile.to_payload()
    _qiskit()
    from .compile import CompileOptions, compile_jobs, diagonal_ground
    needs_atlas = any(not declared["basis_prep"] or job.variant == "joint5" or diagonal_ground(job.q) is None
                      for job in request.jobs)
    if needs_atlas and declared["atlas_source_digest"] is None:
        _fail("profile must pin ATLAS source for this logical construction", "unsupported_capability")
    if declared["atlas_source_digest"] is not None and atlas_source_digest() != declared["atlas_source_digest"]:
        _fail("local ATLAS source differs from the declared profile", "unsupported_capability")
    options = CompileOptions(basis_prep=declared["basis_prep"], layout=None if declared["layout"] == "none" else "target")
    try:
        plans = compile_jobs(request.jobs, backend=snapshot.to_backend(), options=options)
    except ImportError:
        _fail("an optional compiler dependency is unavailable", "unavailable_dependency")
    except (ValueError, RuntimeError):
        _fail("request cannot compile against the declared snapshot", "unsupported_capability")
    pubs = []
    for index, (job, plan) in enumerate(zip(request.jobs, plans)):
        binary = _qpy_bytes(plan.isa)
        layout = plan.inspection["initial_layout"]
        pubs.append({"pub_index": index, "job_digest": job.job_digest, "compiled_plan_digest": plan.compiled_plan_digest,
                     "isa_digest": "sha256:" + hashlib.sha256(binary).hexdigest(), "layout": layout,
                     "qpy_base64": base64.b64encode(binary).decode("ascii"), "variant": plan.variant,
                     "nuclei": plan.nuclei, "mapping": plan.mapping, "metrics": plan.metrics,
                     "compile_record": plan.compile_record, "inspection": plan.inspection})
    body = {"schema": ARTIFACT_SCHEMA, "request": _copy(request_body), "profile": declared,
            "target_snapshot": snapshot.to_payload(), "logical_identity": _logical_identity(request),
            "target_snapshot_digest": snapshot.digest, "profile_digest": profile.digest,
            "pubs": pubs, "resources": _resources(request, pubs)}
    return parse_prepared_artifact({**body, "artifact_digest": canonical_digest(body)})


def _validate_pub(pub: Any, index: int, request: QSearchRequest, profile: CompileProfile, snapshot: TargetSnapshot) -> None:
    pub = _obj(pub, {"pub_index", "job_digest", "compiled_plan_digest", "isa_digest", "layout", "qpy_base64",
                     "variant", "nuclei", "mapping", "metrics", "compile_record", "inspection"}, "prepared PUB")
    job = request.jobs[index]
    if type(pub["pub_index"]) is not int or pub["pub_index"] != index or pub["job_digest"] != job.job_digest:
        _fail("prepared PUB order or requested job differs")
    for field in ("compiled_plan_digest", "isa_digest"):
        _digest(pub[field], field)
    if not isinstance(pub["qpy_base64"], str) or len(pub["qpy_base64"]) > MAX_QPY_BYTES * 4 // 3 + 4:
        _fail("prepared QPY bytes are absent or too large")
    try:
        binary = base64.b64decode(pub["qpy_base64"], validate=True)
    except (ValueError, TypeError):
        _fail("prepared QPY encoding is malformed")
    if not binary or len(binary) > MAX_QPY_BYTES or "sha256:" + hashlib.sha256(binary).hexdigest() != pub["isa_digest"]:
        _fail("prepared ISA digest differs from QPY bytes")
    width = snapshot.to_payload()["num_qubits"]
    for field in ("layout", "mapping"):
        values = pub[field]
        limit = job.n if field == "mapping" else width
        if (not isinstance(values, list) or len(values) != job.n or any(type(x) is not int or not 0 <= x < limit for x in values)
                or len(set(values)) != len(values)):
            _fail("prepared logical/physical layout is malformed")
    if pub["variant"] not in ("ordinary", "joint5"):
        _fail("prepared variant is unsupported")
    if not isinstance(pub["nuclei"], list) or any(not isinstance(tri, list) or len(tri) != 3
            or any(type(q) is not int or not 0 <= q < job.n for q in tri) or len(set(tri)) != 3 for tri in pub["nuclei"]):
        _fail("prepared nuclei are malformed")
    metrics = _obj(pub["metrics"], {"logical_2q", "native_2q", "native_depth"}, "prepared metrics")
    for field, item in metrics.items():
        _int(item, field)
    record_keys = {"target", "coupling_map", "physical_qubits", "basis", "opt_level", "seed", "candidates",
                   "backend_name", "target_digest"}
    record = pub["compile_record"]
    declared = profile.to_payload()
    enabled = declared["basis_prep"] or declared["layout"] != "none"
    record = _obj(record, record_keys | ({"rewrites"} if enabled else set()), "compile record")
    if (record["target"] != request.target or record["backend_name"] != request.target
            or record["target_digest"] != snapshot.structural_digest
            or record["coupling_map"] != "target:" + request.target
            or type(record["physical_qubits"]) is not int or record["physical_qubits"] != width
            or type(record["seed"]) is not int or record["seed"] != declared["seed"]
            or type(record["opt_level"]) is not int or record["opt_level"] != declared["optimization_level"]):
        _fail("prepared compile record differs from declared target/profile")
    if (not isinstance(record["basis"], list) or any(not isinstance(name, str) for name in record["basis"])
            or record["basis"] != sorted(set(record["basis"]))):
        _fail("prepared basis is malformed")
    candidates = _obj(record["candidates"], {"ordinary", "joint5"}, "candidate metrics")
    for name, candidate in candidates.items():
        if candidate is not None:
            candidate = _obj(candidate, {"logical_2q", "native_2q", "native_depth"}, "candidate metrics")
            for field, item in candidate.items():
                _int(item, field)
    if canonical_json(candidates[pub["variant"]]) != canonical_json(metrics):
        _fail("prepared chosen candidate differs from metrics")
    if enabled:
        rewrites = _obj(record["rewrites"], {"basis_prep", "layout"}, "compile rewrites")
        if rewrites["basis_prep"] not in (("applied", "not_applicable") if declared["basis_prep"] else ("off",)):
            _fail("prepared basis rewrite differs from the explicit profile")
        placement = rewrites["layout"]
        if declared["layout"] == "none":
            _obj(placement, {"mode"}, "layout rewrite")
            if placement["mode"] != "off":
                _fail("prepared layout rewrite differs from the explicit profile")
        else:
            if not isinstance(placement, Mapping) or placement.get("mode") != "target" or type(placement.get("applied")) is not bool:
                _fail("prepared target layout rewrite is malformed")
            if placement["applied"]:
                _obj(placement, {"mode", "applied", "errors_digest", "physical"}, "layout rewrite")
                _digest(placement["errors_digest"], "layout errors_digest")
                if canonical_json(placement["physical"]) != canonical_json(pub["layout"]):
                    _fail("prepared target layout differs from its inspected placement")
            else:
                _obj(placement, {"mode", "applied", "reason"}, "layout rewrite")
                if placement["reason"] != "circuit has two-qubit gates":
                    _fail("prepared target layout refusal is unsupported")
    inspection = _obj(pub["inspection"], {"ops", "logical_2q", "native_2q", "native_depth", "measurement_map", "initial_layout"}, "inspection")
    if (canonical_json(inspection["initial_layout"]) != canonical_json(pub["layout"])
            or any(type(inspection[key]) is not int or inspection[key] != metrics[key] for key in metrics)):
        _fail("prepared inspection differs from layout or metrics")
    if not isinstance(inspection["ops"], Mapping) or any(not isinstance(k, str) or type(v) is not int or v < 1 for k, v in inspection["ops"].items()):
        _fail("prepared operation counts are malformed")
    measurement = inspection["measurement_map"]
    if (not isinstance(measurement, list) or len(measurement) != job.n
            or any(not isinstance(row, list) or len(row) != 2 or type(row[0]) is not int or not 0 <= row[0] < width
                   or type(row[1]) is not int or not 0 <= row[1] < job.n for row in measurement)
            or len({row[0] for row in measurement}) != job.n or sorted(row[1] for row in measurement) != list(range(job.n))):
        _fail("prepared measurement mapping is malformed")
    if _plan_digest(pub) != pub["compiled_plan_digest"]:
        _fail("prepared compile-plan digest differs from its inspected contents")


def _parse_prepared_artifact(payload: Any, *, expected_digest: str | None = None) -> PreparedArtifact:
    if isinstance(payload, PreparedArtifact):
        payload = payload.to_payload()
    value = _obj(payload, {"schema", "request", "profile", "target_snapshot", "logical_identity", "target_snapshot_digest",
                           "profile_digest", "pubs", "resources", "artifact_digest"}, "prepared artifact")
    if value["schema"] != ARTIFACT_SCHEMA:
        _fail("prepared artifact: unsupported schema version")
    _digest(value["artifact_digest"], "artifact_digest")
    if expected_digest is not None and _digest(expected_digest, "expected_digest") != value["artifact_digest"]:
        _fail("prepared artifact differs from the independently expected digest")
    if canonical_digest({key: item for key, item in value.items() if key != "artifact_digest"}) != value["artifact_digest"]:
        _fail("prepared artifact digest differs from its contents")
    request, profile, snapshot = _inputs(value["request"], value["profile"], value["target_snapshot"])
    if (value["logical_identity"] != _logical_identity(request) or value["target_snapshot_digest"] != snapshot.digest
            or value["profile_digest"] != profile.digest):
        _fail("prepared source/profile/target identity differs from its contents")
    pubs = value["pubs"]
    if not isinstance(pubs, list) or len(pubs) != request.block_count:
        _fail("prepared artifact must contain every requested PUB")
    for index, pub in enumerate(pubs):
        _validate_pub(pub, index, request, profile, snapshot)
    if canonical_json(value["resources"]) != canonical_json(_resources(request, pubs)):
        _fail("prepared resource report differs from the requested/compiled plan")
    return PreparedArtifact(canonical_json(_copy(value)))


def parse_prepared_artifact(payload: Any, *, expected_digest: str | None = None) -> PreparedArtifact:
    """Validate all fields/digests without SDK imports; reject malformed input uniformly."""
    try:
        return _parse_prepared_artifact(payload, expected_digest=expected_digest)
    except PreparationError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        _fail("prepared artifact contains malformed fields")


def load_prepared_plans(artifact: PreparedArtifact | Mapping[str, Any], *, current_snapshot: TargetSnapshot | Mapping[str, Any],
                        expected_digest: str) -> list[Any]:
    """Load approved ISA bytes after target drift checks; never recompile."""
    artifact = parse_prepared_artifact(artifact, expected_digest=expected_digest)
    current = parse_target_snapshot(current_snapshot)
    if current.digest != artifact.snapshot_digest:
        _fail("current permitted target drifted; explicit re-preparation is required", "target_drift")
    _qiskit()
    from qiskit import qpy
    from .compile import TARGET_METADATA_KEY, check_isa
    backend = current.to_backend()
    request = artifact.request
    target_width = current.to_payload()["num_qubits"]
    plans = []
    for pub, job in zip(artifact.to_payload()["pubs"], request.jobs):
        try:
            circuits = qpy.load(io.BytesIO(base64.b64decode(pub["qpy_base64"], validate=True)))
        except Exception:
            _fail("prepared QPY cannot be read by the declared pinned SDK")
        if len(circuits) != 1:
            _fail("prepared QPY must contain exactly one circuit per PUB")
        isa = circuits[0]
        if "sha256:" + hashlib.sha256(_qpy_bytes(isa)).hexdigest() != pub["isa_digest"]:
            _fail("loaded ISA differs from approved QPY identity")
        binding = (isa.metadata or {}).get(TARGET_METADATA_KEY)
        if binding != {"backend_name": request.target, "target_digest": current.structural_digest}:
            _fail("loaded ISA target metadata differs from the approved snapshot")
        if isa.num_qubits != target_width or isa.num_clbits != job.n or len(isa.cregs) != 1 or isa.cregs[0].name != "meas":
            _fail("loaded ISA width or measurement register differs")
        measurement = [[isa.find_bit(inst.qubits[0]).index, isa.find_bit(inst.clbits[0]).index]
                       for inst in isa.data if inst.operation.name == "measure"]
        operations = {name: int(count) for name, count in isa.count_ops().items()}
        native_2q = sum(len(inst.qubits) == 2 and inst.operation.name != "barrier" for inst in isa.data)
        native_depth = int(isa.depth(lambda inst: len(inst.qubits) == 2 and inst.operation.name != "barrier"))
        if (measurement != pub["inspection"]["measurement_map"] or operations != pub["inspection"]["ops"]
                or native_2q != pub["metrics"]["native_2q"] or native_depth != pub["metrics"]["native_depth"]
                or isa.parameters):
            _fail("loaded ISA inspection differs from the approved artifact")
        try:
            check_isa(isa, backend.target)
        except ValueError:
            _fail("loaded ISA is unsupported by the current target", "target_drift")
        plans.append(SimpleNamespace(isa=isa, logical=None, compiled_plan_digest=pub["compiled_plan_digest"],
                                     variant=pub["variant"], nuclei=pub["nuclei"], mapping=pub["mapping"],
                                     metrics=pub["metrics"], candidates=pub["compile_record"]["candidates"],
                                     compile_record=pub["compile_record"], inspection=pub["inspection"],
                                     physical_qubits=pub["compile_record"]["physical_qubits"],
                                     coupling_map=pub["compile_record"]["coupling_map"]))
    return plans


def samples_from_pub_result(pub: Any, *, register: str, width: int, shots: int) -> list[str]:
    """Read public Sampler PUB result shapes with explicit display-bit ordering.

    Both attribute DataBin and mapping DataBin adapters return get_bitstrings()
    display strings unchanged: x[i] is string[-1-i]. No implicit reversal or
    count expansion is permitted, and a partial PUB is rejected.
    """
    _text(register, "register")
    _int(width, "width", low=1)
    _int(shots, "shots", low=1)
    data = pub.get("data") if isinstance(pub, Mapping) else getattr(pub, "data", None)
    bits = data.get(register) if isinstance(data, Mapping) else getattr(data, register, None)
    getter = getattr(bits, "get_bitstrings", None)
    if not callable(getter):
        _fail("PUB result does not expose a supported public bitstring interface", "unsupported_capability")
    values = getter()
    if (not isinstance(values, list) or len(values) != shots
            or any(not isinstance(value, str) or len(value) != width or set(value) - {"0", "1"} for value in values)):
        _fail("PUB result shots, width or bit alphabet differs from the requested PUB")
    return list(values)


__all__ = ["PROFILE_SCHEMA", "SNAPSHOT_SCHEMA", "ARTIFACT_SCHEMA", "CHECK_SCHEMA", "PINNED_QISKIT",
           "OPERATIONS_SCHEMA", "SUPPORTED_OPERATION",
           "PreparationError", "CompileProfile", "TargetSnapshot", "PreparedArtifact", "parse_compile_profile",
           "parse_target_snapshot", "snapshot_from_backend", "atlas_source_digest", "check_request", "prepare_request",
           "parse_prepared_artifact", "load_prepared_plans", "samples_from_pub_result"]
