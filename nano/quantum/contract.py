"""Strict, offline proposal records for the quantum language RFC.

This module resolves no resources and performs no circuit or provider calls.
The builders return JSON objects; only ``parse_plan`` admits a complete plan.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .canonical import canonical_digest, validate_digest

SCHEMA = "nano.quantum.operations.v1"
RESULT_SCHEMA = "nano.quantum.fixture-result.v1"
CODECS = ("raw_bits", "evidence_index")


class QuantumValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _fail(code: str, message: str) -> None:
    raise QuantumValidationError(code, message)


def _object(value: Any, keys: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        _fail("FIELDS", f"{where} requires exactly {sorted(keys)}")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\n" in value or "\r" in value:
        _fail("TYPE", f"{where} must be nonempty trimmed single-line text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail("TYPE", f"{where} must be valid UTF-8 text")
    return value


def _integer(value: Any, where: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail("TYPE", f"{where} must be an integer >= {minimum}")
    return value


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        _fail("TYPE", f"{where} must be finite")
    try:
        number = float(value)
    except OverflowError:
        _fail("TYPE", f"{where} must be finite")
    if not math.isfinite(number):
        _fail("TYPE", f"{where} must be finite")
    return number


def _choice(value: Any, values: tuple[Any, ...], where: str) -> Any:
    if not isinstance(value, str) or value not in values:
        _fail("VALUE", f"{where} must be one of {values}")
    return value


def _list(value: Any, where: str, *, nonempty: bool = True) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        _fail("TYPE", f"{where} must be a {'nonempty ' if nonempty else ''}list")
    return value


def _variables(value: Any) -> tuple[str, ...]:
    items = tuple(_text(v, "variable") for v in _list(value, "variables"))
    if len(set(items)) != len(items):
        _fail("VARIABLES", "variables must be unique and ordered")
    return items


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class Packet:
    recipient_id: str
    chunk: int
    slot: int
    evidence_reference: str
    evidence_digest: str
    codec: str
    codec_version: int
    payload_width: int
    authentication_width: int
    bit_significance: str
    expected_records: int


@dataclass(frozen=True)
class PrepareBits:
    id: str
    variables: tuple[str, ...]
    bits: str
    packets: tuple[Packet, ...]
    op: str = "prepare_bits"


@dataclass(frozen=True)
class Sample:
    id: str
    preparation_id: str
    backend: str
    shots: int
    limits: Mapping[str, int]
    seed: int
    result: Mapping[str, Any]
    op: str = "sample"


@dataclass(frozen=True)
class OptimizeQubo:
    id: str
    variables: tuple[str, ...]
    qubo: Mapping[str, Any]
    method: Mapping[str, Any]
    audit: Mapping[str, Any]
    backend: str
    shots: int
    limits: Mapping[str, int]
    seed: int
    result: Mapping[str, Any]
    op: str = "optimize_qubo"


@dataclass(frozen=True)
class QuantumPlan:
    operations: tuple[PrepareBits | Sample | OptimizeQubo, ...]
    plan_digest: str
    payload: Mapping[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return _thaw(self.payload)


@dataclass(frozen=True)
class PubResult:
    operation_id: str
    pub_index: int
    register: str
    bit_order: str
    samples: tuple[str, ...]


@dataclass(frozen=True)
class QuantumResult:
    """Fixture observations, not a receipt or an attestation of packet delivery."""
    plan_digest: str
    pubs: tuple[PubResult, ...]
    optimization_verdict: str
    delivery_verdict: str


def _packet(value: Any, slot: int) -> Packet:
    p = _object(value, {"recipient_id", "chunk", "slot", "evidence_bank", "codec", "payload_width",
                        "authentication_width", "bit_significance", "expected_records"}, "packet")
    bank = _object(p["evidence_bank"], {"reference", "digest"}, "evidence_bank")
    codec = _object(p["codec"], {"name", "version"}, "codec")
    try:
        digest = validate_digest(bank["digest"], field="evidence_bank.digest")
    except ValueError as exc:
        _fail("DIGEST", str(exc))
    if _integer(p["slot"], "slot") != slot:
        _fail("PACKET", "packet slots must be contiguous in list order")
    name = _choice(codec["name"], CODECS, "codec.name")
    if type(codec["version"]) is not int or codec["version"] != 1:
        _fail("CODEC", "only codec version 1 is defined")
    if type(p["expected_records"]) is not int or p["expected_records"] != 1:
        _fail("PACKET", "each v1 packet selects exactly one record")
    return Packet(_text(p["recipient_id"], "recipient_id"), _integer(p["chunk"], "chunk"), slot,
                  _text(bank["reference"], "evidence_bank.reference"), digest, name, 1,
                  _integer(p["payload_width"], "payload_width", 1),
                  _integer(p["authentication_width"], "authentication_width"),
                  _choice(p["bit_significance"], ("lsb_first", "msb_first"), "bit_significance"), 1)


def _execution(value: Mapping[str, Any], width: int, pub_index: int) -> tuple[Any, ...]:
    backend = _text(value["backend"], "backend")
    shots = _integer(value["shots"], "shots", 1)
    seed = _integer(value["seed"], "seed")
    limits = _object(value["limits"], {"max_circuits", "max_wall_s", "max_qpu_s"}, "limits")
    for name in limits:
        _integer(limits[name], f"limits.{name}", 1 if name != "max_qpu_s" else 0)
    if backend == "local_ideal" and limits["max_qpu_s"] != 0:
        _fail("LIMITS", "local_ideal has zero QPU-second allowance")
    result = _object(value["result"], {"pub_index", "register", "width", "bit_order"}, "result")
    if _integer(result["pub_index"], "pub_index") != pub_index:
        _fail("PUB", "PUB indices must be contiguous in execution-operation order")
    if _integer(result["width"], "result.width", 1) != width:
        _fail("WIDTH", "expected result width differs from preparation/objective width")
    _text(result["register"], "register")
    _choice(result["bit_order"], ("msb_first",), "result.bit_order")
    return backend, shots, _freeze(limits), seed, _freeze(result)


def parse_plan(body: Any) -> QuantumPlan:
    body = _object(body, {"schema", "operations"}, "plan")
    if body["schema"] != SCHEMA:
        _fail("SCHEMA", f"schema must be {SCHEMA}")
    raw_ops = _list(body["operations"], "operations")
    ids: set[str] = set()
    recipients: set[tuple[str, int]] = set()
    preparations: dict[str, PrepareBits] = {}
    operations: list[PrepareBits | Sample | OptimizeQubo] = []
    pub_index = 0
    for value in raw_ops:
        if not isinstance(value, Mapping):
            _fail("TYPE", "operation must be an object")
        kind = _choice(value.get("op"), ("prepare_bits", "sample", "optimize_qubo"), "op")
        common = {"id", "op"}
        keys = ({"variables", "bits", "packets"} if kind == "prepare_bits" else
                {"preparation_id", "backend", "shots", "limits", "seed", "result"} if kind == "sample" else
                {"variables", "qubo", "method", "audit", "backend", "shots", "limits", "seed", "result"})
        _object(value, common | keys, "operation")
        operation_id = _text(value["id"], "id")
        if operation_id in ids:
            _fail("ID", "operation IDs must be unique")
        ids.add(operation_id)
        if kind == "prepare_bits":
            variables = _variables(value["variables"])
            bits = value["bits"]
            if not isinstance(bits, str) or set(bits) - {"0", "1"} or len(bits) != len(variables):
                _fail("WIDTH", "bits must have one binary digit per ordered variable")
            packets = tuple(_packet(p, i) for i, p in enumerate(_list(value["packets"], "packets", nonempty=False)))
            if packets and sum(p.payload_width + p.authentication_width for p in packets) != len(bits):
                _fail("WIDTH", "packet payload and authentication widths must cover the preparation exactly")
            for packet in packets:
                key = (packet.recipient_id, packet.chunk)
                if key in recipients:
                    _fail("RECIPIENT", "duplicate recipient/chunk delivery")
                recipients.add(key)
            op = PrepareBits(operation_id, variables, bits, packets)
            preparations[operation_id] = op
        elif kind == "sample":
            ref = _text(value["preparation_id"], "preparation_id")
            if ref not in preparations:
                _fail("REFERENCE", "sample must name an earlier admitted preparation")
            op = Sample(operation_id, ref, *_execution(value, len(preparations[ref].bits), pub_index))
            pub_index += 1
        else:
            variables = _variables(value["variables"])
            qubo = _object(value["qubo"], {"q", "offset", "convention"}, "qubo")
            _choice(qubo["convention"], ("symmetric_half",), "qubo.convention")
            _number(qubo["offset"], "qubo.offset")
            rows = _list(qubo["q"], "qubo.q")
            n = len(variables)
            if len(rows) != n or any(not isinstance(r, list) or len(r) != n for r in rows):
                _fail("QUBO", "QUBO must be square over the variables")
            for i in range(n):
                for j in range(n):
                    _number(rows[i][j], "qubo.q entry")
                    if rows[i][j] != rows[j][i]:
                        _fail("QUBO", "QUBO must be exactly symmetric")
            method = _object(value["method"], {"name", "variant", "p", "gammas", "betas"}, "method")
            _choice(method["name"], ("qaoa",), "method.name")
            _choice(method["variant"], ("auto", "ordinary", "joint5"), "method.variant")
            p = _integer(method["p"], "method.p", 1)
            for angles in (method["gammas"], method["betas"]):
                if len(_list(angles, "angles")) != p:
                    _fail("METHOD", "each angle vector must have p entries")
                for angle in angles:
                    _number(angle, "angle")
            audit = _object(value["audit"], {"classical_min_energy"}, "audit")
            _number(audit["classical_min_energy"], "classical_min_energy")
            op = OptimizeQubo(operation_id, variables, _freeze(qubo), _freeze(method), _freeze(audit),
                              *_execution(value, n, pub_index))
            pub_index += 1
        operations.append(op)
    if any(o.limits["max_circuits"] < pub_index for o in operations if isinstance(o, (Sample, OptimizeQubo))):
        _fail("LIMITS", "max_circuits must bound the complete plan PUB count")
    return QuantumPlan(tuple(operations), canonical_digest(body), _freeze(body))


def build_plan(operations: Sequence[Mapping[str, Any]]) -> QuantumPlan:
    return parse_plan({"schema": SCHEMA, "operations": copy.deepcopy(list(operations))})


def checked_plan(plan: QuantumPlan) -> QuantumPlan:
    """Re-admit a public record before a host adapter or result validator uses it."""
    if not isinstance(plan, QuantumPlan):
        _fail("TYPE", "expected a QuantumPlan")
    admitted = parse_plan(plan.to_payload())
    if admitted.plan_digest != plan.plan_digest or admitted.operations != plan.operations:
        _fail("DIGEST", "typed plan fields or digest differ from its validated payload")
    return admitted


def prepare_bits(bits: str, *, operation_id: str = "prepare", variables: Sequence[str] | None = None,
                 packets: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    return {"id": operation_id, "op": "prepare_bits", "variables": list(variables) if variables is not None else
            [f"x{i}" for i in range(len(bits))], "bits": bits, "packets": copy.deepcopy(list(packets))}


def sample(preparation_id: str, *, expected_width: int, shots: int, max_circuits: int,
           max_wall_s: int, max_qpu_s: int, operation_id: str = "sample", backend: str = "local_ideal",
           seed: int = 0, pub_index: int = 0, register: str = "meas") -> dict[str, Any]:
    return {"id": operation_id, "op": "sample", "preparation_id": preparation_id, "backend": backend,
            "shots": shots, "limits": {"max_circuits": max_circuits, "max_wall_s": max_wall_s, "max_qpu_s": max_qpu_s},
            "seed": seed, "result": {"pub_index": pub_index, "register": register, "width": expected_width,
                                     "bit_order": "msb_first"}}


def optimize_qubo(variables: Sequence[str], q: list[list[float]], *, offset: float, variant: str, p: int,
                  gammas: Sequence[float], betas: Sequence[float], classical_min_energy: float,
                  shots: int, max_circuits: int, max_wall_s: int, max_qpu_s: int,
                  operation_id: str = "optimize", backend: str = "local_ideal", seed: int = 0,
                  pub_index: int = 0, register: str = "meas") -> dict[str, Any]:
    out = sample("unused", expected_width=len(variables), shots=shots, max_circuits=max_circuits,
                 max_wall_s=max_wall_s, max_qpu_s=max_qpu_s, operation_id=operation_id, backend=backend,
                 seed=seed, pub_index=pub_index, register=register)
    del out["preparation_id"]
    return {**out, "op": "optimize_qubo", "variables": list(variables),
            "qubo": {"q": copy.deepcopy(q), "offset": offset, "convention": "symmetric_half"},
            "method": {"name": "qaoa", "variant": variant, "p": p, "gammas": list(gammas), "betas": list(betas)},
            "audit": {"classical_min_energy": classical_min_energy}}


def check_evidence_bank(packet: Packet, catalog: Mapping[str, Any]) -> Any:
    """Check caller-supplied evidence JSON; never dereference a pointer or fetch data."""
    if packet.evidence_reference not in catalog:
        _fail("EVIDENCE", "evidence bank is absent from the caller catalog")
    bank = catalog[packet.evidence_reference]
    if canonical_digest(bank) != packet.evidence_digest:
        _fail("DIGEST", "evidence-bank contents differ from the declared digest")
    return copy.deepcopy(bank)


def parse_result(plan: QuantumPlan, body: Any) -> QuantumResult:
    """Validate fixture observations against the complete expected PUB inventory.

    Verdicts are recorded independent observations, not proven by shape checking.
    A decoder must establish delivery and an optimizer must establish its audit.
    """
    plan = checked_plan(plan)
    body = _object(body, {"schema", "plan_digest", "pubs", "optimization_verdict", "delivery_verdict"}, "result")
    if body["schema"] != RESULT_SCHEMA or body["plan_digest"] != plan.plan_digest:
        _fail("DIGEST", "fixture result schema or plan digest differs")
    expected = [o for o in plan.operations if isinstance(o, (Sample, OptimizeQubo))]
    pubs = _list(body["pubs"], "pubs", nonempty=False)
    if len(pubs) != len(expected):
        _fail("PUB", "result requires exactly the admitted PUB inventory")
    parsed = []
    for raw, operation in zip(pubs, expected):
        raw = _object(raw, {"operation_id", "pub_index", "register", "bit_order", "samples"}, "PUB")
        if (raw["operation_id"] != operation.id or type(raw["pub_index"]) is not int
                or any(raw[k] != operation.result[k] for k in ("pub_index", "register", "bit_order"))):
            _fail("PUB", "PUB identity, register or order differs from the admitted shape")
        samples = _list(raw["samples"], "samples")
        if len(samples) != operation.shots or any(not isinstance(s, str) or len(s) != operation.result["width"]
                                                  or set(s) - {"0", "1"} for s in samples):
            _fail("WIDTH", "PUB sample width or shot count differs from the admitted shape")
        parsed.append(PubResult(operation.id, raw["pub_index"], raw["register"], raw["bit_order"], tuple(samples)))
    verdicts = tuple(_choice(body[k], ("not_applicable", "unverified", "pass", "fail"), k)
                     for k in ("optimization_verdict", "delivery_verdict"))
    return QuantumResult(plan.plan_digest, tuple(parsed), *verdicts)
