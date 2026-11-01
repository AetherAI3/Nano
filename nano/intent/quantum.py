"""Explicit, offline quantum command profile, independent of market binding.

The closed grammar identifies caller-supplied resources. Neither a catalog name
nor a command grants authority to resolve data, schedule work, or submit a job.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from nano.quantum import QuantumValidationError
from nano.quantum.canonical import canonical_digest, validate_digest
from .tokens import IntentInputError, MAX_INPUT_CHARS, MAX_TOKENS

COMMAND_SCHEMA = "nano.quantum.command.v1"
OPERATION_SCHEMA = "nano.quantum.operations.v1"
CATALOG_VERSION = "quantum-catalog/1"
MAX_CATALOG_RESOURCES = 2048
STATES = ("proposed", "compiled", "queued", "cancelled", "accounting-pending", "verified", "failed")
EFFECTS = {"check": "pure", "compile": "local_compile", "simulate": "local_simulation",
           "inspect_status": "read_supplied_state", "explain_receipt": "read_supplied_state",
           "prepare_submission": "proposal_only"}
_NAME = r"[a-z][a-z0-9._-]{0,63}"
_ALIAS = re.compile(_NAME, re.ASCII)
_FLAG = re.IGNORECASE | re.ASCII
_NEGATION = re.compile(r"(?<![a-z0-9_.-])(?:not|never|don't|don’t|dont|no)(?![a-z0-9_.-])", _FLAG)
_CONJUNCTION = re.compile(r"(?<![a-z0-9_.-])(?:or|and)(?![a-z0-9_.-])", _FLAG)
_UNSUPPORTED_KERNEL = re.compile(r"\b(?:bell|ghz|circuit|circuits)\b", _FLAG)
_QUANTITY = re.compile(r"(?P<number>[+-]?\d+(?:\.\d+)?)\s+(?P<unit>shots?|circuits?|qpu\s+seconds?)\b", _FLAG)
_PATTERNS = (
    ("check", rf"(?:check|validate)\s+(?:quantum\s+)?plan\s+(?P<plan>{_NAME})(?:\s+using\s+evidence\s+bank\s+(?P<bank>{_NAME}))?"),
    ("compile", rf"(?:compile|build)\s+(?:quantum\s+)?plan\s+(?P<plan>{_NAME})\s+on\s+(?P<backend>{_NAME})(?:\s+using\s+evidence\s+bank\s+(?P<bank>{_NAME}))?"),
    ("simulate", rf"simulate\s+(?:quantum\s+)?plan\s+(?P<plan>{_NAME})\s+on\s+(?P<backend>{_NAME})(?P<bounds>\s+with\b.*)?"),
    ("prepare_submission", rf"prepare\s+submission\s+(?:quantum\s+)?plan\s+(?P<plan>{_NAME})\s+on\s+(?P<backend>{_NAME})(?P<bounds>\s+with\b.*)?"),
    ("inspect_status", rf"(?:inspect|show)\s+status\s+job\s+(?P<job>{_NAME})"),
    ("inspect_status", rf"show\s+quantum\s+job\s+(?P<job>{_NAME})\s+status"),
    ("explain_receipt", rf"(?:explain|describe)\s+receipt\s+(?P<receipt>{_NAME})"),
)
_GRAMMAR = tuple((op, re.compile(r"\s*" + pattern + r"\s*", _FLAG)) for op, pattern in _PATTERNS)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _error(code: str, message: str) -> None:
    raise QuantumValidationError(code, message)


def _object(value: Any, required: set[str], label: str, optional: set[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not required <= set(value) or set(value) - required - (optional or set()):
        _error("CATALOG", f"{label} has missing, unknown or incorrectly typed fields")
    return value


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value or value != value.strip() or any(ord(c) < 32 for c in value):
        _error("CATALOG", f"{label} must be nonempty, trimmed, single-line text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _error("CATALOG", f"{label} must be valid UTF-8 text")
    return value


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 1_000_000_000:
        _error("CATALOG", f"{label} must be a positive bounded integer")
    return value


@dataclass(frozen=True)
class QuantumCatalog:
    """Validated immutable catalog data; its contents confer no authority."""
    payload: Mapping[str, Any]


def validate_catalog(value: Any) -> QuantumCatalog:
    if isinstance(value, QuantumCatalog):
        value = value.payload  # Public records are re-admitted at every boundary.
    top = _object(value, {"version", "backends", "plans", "banks", "jobs", "receipts"}, "catalog")
    if top["version"] != CATALOG_VERSION:
        _error("CATALOG", f"catalog.version must be {CATALOG_VERSION}")
    categories = {
        "backends": ({"id", "kind"}, set()),
        "plans": ({"id", "operation_ref", "operation_kind", "admitted_preparation_ref", "max_shots", "max_circuits"}, {"operation_digest"}),
        "banks": ({"id", "digest", "schema_version"}, set()),
        "jobs": ({"id", "plan_id", "state"}, set()),
        "receipts": ({"id", "plan_id", "job_id", "state"}, set()),
    }
    for category, (required, optional) in categories.items():
        table = top[category]
        if not isinstance(table, Mapping) or len(table) > MAX_CATALOG_RESOURCES:
            _error("CATALOG", f"{category} must be a bounded resource table")
        identities: dict[str, Any] = {}
        for alias, raw in table.items():
            if type(alias) is not str or not _ALIAS.fullmatch(alias):
                _error("CATALOG", "resource aliases must be lowercase bounded names")
            row = _object(raw, required, f"{category}.{alias}", optional)
            identity = _text(row["id"], "resource id")
            if identity in identities and row != identities[identity]:
                _error("CATALOG", "aliases for one resource must describe identical records")
            identities[identity] = row
            if category == "backends":
                if row["kind"] not in ("local", "hardware"):
                    _error("CATALOG", "backend.kind must be local or hardware")
            elif category == "plans":
                for name in ("operation_ref", "admitted_preparation_ref"):
                    _text(row[name], name)
                if row["operation_kind"] not in ("prepare_bits", "sample", "optimize_qubo"):
                    _error("CATALOG", "unsupported quantum operation kind")
                for name in ("max_shots", "max_circuits"):
                    _positive_int(row[name], name)
                if "operation_digest" in row:
                    try:
                        validate_digest(row["operation_digest"])
                    except ValueError:
                        _error("CATALOG", "operation_digest must be a sha256 digest")
            elif category == "banks":
                _text(row["schema_version"], "schema_version")
                try:
                    validate_digest(row["digest"])
                except ValueError:
                    _error("CATALOG", "bank.digest must be a sha256 digest")
            else:
                _text(row["plan_id"], "plan_id")
                if type(row["state"]) is not str or row["state"] not in STATES:
                    _error("CATALOG", "unknown supplied observation state")
                if category == "receipts":
                    _text(row["job_id"], "job_id")
    plan_ids = {row["id"] for row in top["plans"].values()}
    jobs_by_id = {row["id"]: row for row in top["jobs"].values()}
    for category in ("jobs", "receipts"):
        for row in top[category].values():
            if row["plan_id"] not in plan_ids:
                _error("CATALOG", "observation references an absent plan")
            if category == "receipts":
                if row["job_id"] not in jobs_by_id:
                    _error("CATALOG", "receipt references an absent job")
                if jobs_by_id[row["job_id"]]["plan_id"] != row["plan_id"]:
                    _error("CATALOG", "receipt and referenced job must identify the same plan")
    return QuantumCatalog(_freeze(top))


@dataclass(frozen=True)
class QuantumQuantity:
    kind: str
    value: int | float
    unit: str
    raw: str
    span: tuple[int, int]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value, "unit": self.unit, "raw": self.raw, "span": list(self.span)}


@dataclass(frozen=True)
class QuantumEntity:
    kind: str
    raw: str
    canonical: str
    span: tuple[int, int]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "raw": self.raw, "canonical": self.canonical, "span": list(self.span)}


@dataclass(frozen=True)
class QuantumDiagnostic:
    code: str
    message: str
    span: tuple[int, int]
    suggestions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "span": list(self.span), "suggestions": list(self.suggestions)}


@dataclass(frozen=True)
class QuantumStep:
    kind: str
    capability: str
    args: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "capability": self.capability, "args": _thaw(self.args)}


@dataclass(frozen=True)
class QuantumCommandIR:
    source: str
    source_hash: str
    plan_digest: str
    operation: str | None
    args: Mapping[str, Any]
    quantities: tuple[QuantumQuantity, ...]
    entities: tuple[QuantumEntity, ...]
    steps: tuple[QuantumStep, ...]
    effects: tuple[str, ...]
    diagnostics: tuple[QuantumDiagnostic, ...]
    catalog_snapshot: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"schema": COMMAND_SCHEMA, "profile": "quantum", "operation_schema": OPERATION_SCHEMA,
                "source": self.source, "source_hash": self.source_hash, "plan_digest": self.plan_digest,
                "operation": self.operation, "args": _thaw(self.args),
                "quantities": [q.to_dict() for q in self.quantities],
                "entities": [e.to_dict() for e in self.entities], "steps": [s.to_dict() for s in self.steps],
                "effects": list(self.effects), "diagnostics": [d.to_dict() for d in self.diagnostics],
                "catalog_snapshot": _thaw(self.catalog_snapshot)}

    def canonical_json(self) -> str:
        """Stable command-document serialization; admission remains explicit."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)

    def canonical_bytes(self) -> bytes:
        return self.canonical_json().encode("ascii")

    @classmethod
    def from_dict(cls, document: Mapping[str, Any], *, catalog: Any = None) -> "QuantumCommandIR":
        return validate_quantum_document(document, catalog=catalog)


def _suggest(source: str) -> tuple[str, ...]:
    # One bounded edit, command vocabulary only; suggestions are never executed.
    first = source.strip().split(maxsplit=1)[0].lower() if source.strip() else ""
    if len(first) > 20:
        return ()
    def distance(a: str, b: str) -> int:
        row = list(range(len(b) + 1))
        for i, left in enumerate(a, 1):
            new = [i]
            for j, right in enumerate(b, 1):
                new.append(min(new[-1] + 1, row[j] + 1, row[j - 1] + (left != right)))
            row = new
        return row[-1]
    return tuple(word for word in ("check", "compile", "simulate", "inspect", "explain", "prepare")
                 if first != word and distance(first, word) == 1)[:3]


def _make(source: str, operation: str | None, args: dict[str, Any], quantities: list[QuantumQuantity],
          entities: list[QuantumEntity], snapshot: dict[str, Any], diagnostic: str | None = None) -> QuantumCommandIR:
    diagnostics = () if diagnostic is None else (QuantumDiagnostic(diagnostic, diagnostic.lower().replace("_", " "),
                                                                  (0, len(source)), _suggest(source)),)
    if diagnostics:
        operation, args, quantities, entities = None, {}, [], []
    effects = (EFFECTS[operation],) if operation else ()
    semantic = {"schema": COMMAND_SCHEMA, "operation_schema": OPERATION_SCHEMA, "operation": operation,
                "args": args, "effects": list(effects), "catalog_snapshot": snapshot,
                "diagnostics": [d.code for d in diagnostics]}
    return QuantumCommandIR(source, "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest(),
                            canonical_digest(semantic), operation, _freeze(args), tuple(quantities), tuple(entities),
                            (QuantumStep(effects[0], "quantum." + operation, _freeze(args)),) if operation else (),
                            effects, diagnostics, _freeze(snapshot))


def _bounds(source: str, match: re.Match[str]) -> tuple[dict[str, Any], list[QuantumQuantity], str | None]:
    tail = match.groupdict().get("bounds")
    if not tail:
        return {}, [], "MISSING_BOUNDS"
    prefix = re.match(r"\s+with\s*", tail, _FLAG)
    assert prefix is not None
    start = match.start("bounds") + prefix.end()
    remainder = source[start:match.end("bounds")]
    values: dict[str, Any] = {}
    quantities = []
    position = 0
    for quantity in _QUANTITY.finditer(remainder):
        if remainder[position:quantity.start()].strip():
            return {}, [], "INVALID_BOUNDS"
        position = quantity.end()
        unit_text = quantity.group("unit").lower()
        kind = "qpu_seconds" if unit_text.startswith("qpu") else "circuits" if unit_text.startswith("circuit") else "shots"
        if kind in values:
            return {}, [], "INVALID_BOUNDS"
        number = Decimal(quantity.group("number"))
        if kind != "qpu_seconds" and (number != number.to_integral_value() or number <= 0):
            return {}, [], "INVALID_BOUNDS"
        if number < 0:
            return {}, [], "INVALID_BOUNDS"
        # Integral spellings share identity, including 0.0 QPU seconds.
        value = int(number) if number == number.to_integral_value() else float(number)
        if isinstance(value, float) and (not math.isfinite(value) or (value == 0 and number != 0)):
            return {}, [], "INVALID_BOUNDS"
        values[kind] = value
        span = (start + quantity.start(), start + quantity.end())
        quantities.append(QuantumQuantity(kind, value, {"shots": "shot", "circuits": "circuit", "qpu_seconds": "qpu_second"}[kind],
                                          source[span[0]:span[1]], span))
    if remainder[position:].strip():
        return {}, [], "INVALID_BOUNDS"
    return values, quantities, None if set(values) == {"shots", "circuits", "qpu_seconds"} else "MISSING_BOUNDS"


def compile_quantum(source: str, *, catalog: Any = None) -> QuantumCommandIR:
    """Compile the closed command grammar without any ambient I/O or model call."""
    if type(source) is not str:
        raise IntentInputError("quantum command source must be text")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise IntentInputError("quantum source contains invalid Unicode") from exc
    if len(source) > MAX_INPUT_CHARS:
        raise IntentInputError("quantum source exceeds character limit")
    if len(source.split()) > MAX_TOKENS:
        raise IntentInputError("quantum source exceeds token limit")
    snapshot: dict[str, Any] = {"version": CATALOG_VERSION, "resources": {}}
    reject = lambda code: _make(source, None, {}, [], [], snapshot, code)
    if catalog is None:
        return reject("MISSING_CATALOG")
    data = validate_catalog(catalog).payload
    if _NEGATION.search(source):
        return reject("NEGATED_COMMAND")
    if _CONJUNCTION.search(source):
        return reject("AMBIGUOUS_COMMAND")
    selected = next(((op, result) for op, grammar in _GRAMMAR if (result := grammar.fullmatch(source))), None)
    if selected is None:
        if _UNSUPPORTED_KERNEL.search(source):
            return reject("UNSUPPORTED_COMMAND")
        if re.fullmatch(r"\s*(?:(?:compile|build|check|validate|simulate)\s+quantum(?:\s+(?:search|plan))?|(?:show\s+quantum\s+job\s+status)|(?:explain|describe)\s+receipt)\s*", source, _FLAG):
            return reject("MISSING_REFERENCE")
        if re.match(r"\s*(?:compile|build|simulate|prepare\s+submission)\s+(?:quantum\s+)?plan\b", source, _FLAG) and not re.search(r"\bon\s+" + _NAME, source, _FLAG):
            return reject("MISSING_REFERENCE")
        return reject("UNSUPPORTED_COMMAND")
    operation, match = selected
    args: dict[str, Any] = {}
    entities: list[QuantumEntity] = []
    roles = {"plan": ("plans", "plan_id"), "backend": ("backends", "backend"),
             "bank": ("banks", "evidence_bank_ref"), "job": ("jobs", "job_id"),
             "receipt": ("receipts", "receipt_id")}
    for role, (category, argument) in roles.items():
        alias = match.groupdict().get(role)
        if alias is None:
            continue
        resource = data[category].get(alias.lower())
        if resource is None:
            return reject("UNKNOWN_REFERENCE")
        args[argument] = resource["id"]
        snapshot["resources"][role] = _thaw(resource)
        entities.append(QuantumEntity("evidence_bank" if role == "bank" else role, alias, resource["id"], match.span(role)))
    if operation in ("inspect_status", "explain_receipt"):
        observation = snapshot["resources"]["job" if operation == "inspect_status" else "receipt"]
        snapshot["resources"]["plan"] = _thaw(next(row for row in data["plans"].values() if row["id"] == observation["plan_id"]))
        if operation == "explain_receipt":
            snapshot["resources"]["job"] = _thaw(next(row for row in data["jobs"].values() if row["id"] == observation["job_id"]))
    quantities: list[QuantumQuantity] = []
    if operation in ("simulate", "prepare_submission"):
        quantities_args, quantities, diagnostic = _bounds(source, match)
        if diagnostic:
            return reject(diagnostic)
        backend = snapshot["resources"]["backend"]
        plan = snapshot["resources"]["plan"]
        if operation == "simulate" and backend["kind"] != "local":
            return reject("UNSUPPORTED_COMMAND")
        if operation == "prepare_submission" and backend["kind"] != "hardware":
            return reject("UNSUPPORTED_COMMAND")
        if ((backend["kind"] == "local" and quantities_args["qpu_seconds"] != 0)
                or (backend["kind"] == "hardware" and quantities_args["qpu_seconds"] <= 0)
                or quantities_args["shots"] > plan["max_shots"]
                or quantities_args["circuits"] > plan["max_circuits"]):
            return reject("INVALID_BOUNDS")
        args.update(quantities_args)
    return _make(source, operation, args, quantities, entities, snapshot)


def validate_quantum_document(document: Any, *, catalog: Any = None) -> QuantumCommandIR:
    """Admit a proposed document through the same source, catalog and policy checks.

    Recompilation verifies every field, including absence of executable steps on
    diagnostics. A future model-produced proposal receives no alternate path.
    """
    if not isinstance(document, Mapping) or type(document.get("source")) is not str:
        _error("COMMAND", "quantum command document requires a source string")
    rebuilt = compile_quantum(document["source"], catalog=catalog)
    def same_json(actual: Any, expected: Any) -> bool:
        if isinstance(expected, dict):
            return isinstance(actual, Mapping) and set(actual) == set(expected) and all(same_json(actual[k], v) for k, v in expected.items())
        if isinstance(expected, list):
            return type(actual) is list and len(actual) == len(expected) and all(same_json(a, e) for a, e in zip(actual, expected))
        return type(actual) is type(expected) and actual == expected
    # Native types matter: True must not compare as 1, nor tuple as JSON array.
    if not same_json(document, rebuilt.to_dict()):
        _error("COMMAND", "proposed command differs from source/catalog admission")
    return rebuilt


def structured_reply(ir: QuantumCommandIR, *, catalog: Any = None) -> dict[str, Any]:
    """Render only proposal state or observations explicitly supplied by the caller."""
    if not isinstance(ir, QuantumCommandIR):
        _error("COMMAND", "reply requires a checked quantum command")
    ir = validate_quantum_document(ir.to_dict(), catalog=catalog)
    if ir.diagnostics:
        return {"state": "clarification", "plan_digest": ir.plan_digest,
                "diagnostics": [d.to_dict() for d in ir.diagnostics]}
    references = {key: value for key, value in ir.args.items() if key.endswith("_id") or key == "evidence_bank_ref"}
    if ir.operation in ("inspect_status", "explain_receipt"):
        role = "job" if ir.operation == "inspect_status" else "receipt"
        record = ir.catalog_snapshot["resources"][role]
        references.update({key: record[key] for key in ("plan_id", "job_id") if key in record})
        return {"state": record["state"], "references": references, "plan_digest": ir.plan_digest,
                "observed_from": "caller_catalog", "attestation_verified": False}
    return {"state": "proposed", "operation": ir.operation, "references": references,
            "plan_digest": ir.plan_digest, "effects": list(ir.effects)}


__all__ = ["COMMAND_SCHEMA", "OPERATION_SCHEMA", "QuantumCatalog", "QuantumCommandIR", "QuantumDiagnostic",
           "QuantumEntity", "QuantumQuantity", "QuantumStep", "compile_quantum", "structured_reply",
           "validate_catalog", "validate_quantum_document"]
