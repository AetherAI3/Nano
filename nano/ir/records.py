"""Immutable records for executable IR 1.1.0 (docs/rfc-0085-immutable-records.md).

This module owns three contracts shared by the compiler, the loader and the VM:

* **Nominal identity.** A record id commits to its defining module, declaration
  name and ordered field schema, so two declarations never alias structurally.
* **Load-time validation.** The `records` table is recomputed and must equal
  the canonical closure of record inputs; a record-typed node may only be read
  by `record.get`, which therefore only ever sees a host-validated value.
* **Host admission.** Every record input is validated strictly, cumulatively
  and within fixed ceilings *before* the VM evaluates any node.

None of this reinterprets the reasoning-signature ``record<Signature>`` type.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .schema import IRValidationError

RECORD_ID_PREFIX = "record:sha256:"
RECORD_SCALARS = ("int", "float", "bool", "string")
RECORD_OPS = frozenset({"record.input", "record.get"})

# Fixed ceilings. Counted cumulatively before allocation; not author-adjustable.
MAX_RECORD_FIELDS = 32
MAX_RECORD_DEPTH = 8
MAX_RECORD_DECLARATIONS = 128
MAX_RECORD_VALUES = 4096
# Equal to the package source/metadata byte ceiling (nano.compiler.package).
MAX_RECORD_STRING_BYTES = 262144
# Exactly representable in binary64, which the arithmetic kernels use.
MAX_RECORD_INT = 2**53 - 1

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_RECORD_ID = re.compile(r"record:sha256:[0-9a-f]{64}\Z")
_ENVELOPE_KEYS = frozenset({"recordType", "fields"})


class RecordIRError(IRValidationError):
    """A 1.1.0 document violates the record contract. Carries a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class RecordValueError(ValueError):
    """A host record value was rejected. Never echoes the rejected value."""

    def __init__(self, code: str, path: str, reason: str) -> None:
        super().__init__(f"{code} at {path}: {reason}")
        self.code = code
        self.path = path


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def record_id(module: str, name: str, fields: Iterable[Tuple[str, str]]) -> str:
    """The nominal id: ordered schema plus defining module and name."""
    payload = {
        "type": "NanoRecord", "identityVersion": 1, "module": module, "name": name,
        "fields": [{"name": field, "type": kind} for field, kind in fields],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return RECORD_ID_PREFIX + hashlib.sha256(canonical.encode("ascii")).hexdigest()


def is_record_id(value: object) -> bool:
    return isinstance(value, str) and _RECORD_ID.match(value) is not None


@dataclass(frozen=True)
class RecordDecl:
    """One nominal record type: `fields` is ordered (name, scalar or record id)."""

    id: str
    module: str
    name: str
    fields: Tuple[Tuple[str, str], ...]

    @staticmethod
    def create(module: str, name: str, fields: Sequence[Tuple[str, str]]) -> "RecordDecl":
        ordered = tuple((field, kind) for field, kind in fields)
        return RecordDecl(record_id(module, name, ordered), module, name, ordered)

    @property
    def label(self) -> str:
        return f"{self.module}.{self.name}"

    def field_type(self, field: str) -> Optional[str]:
        for name, kind in self.fields:
            if name == field:
                return kind
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "module": self.module, "name": self.name,
            "fields": [{"name": name, "type": kind} for name, kind in self.fields],
        }


def canonical_closure(roots: Iterable[str], table: Mapping[str, RecordDecl]) -> Tuple[str, ...]:
    """Post-order closure of `roots`, fields in declaration order, no repeats."""
    ordered: List[str] = []
    seen = set()
    for root in roots:
        pending = [(root, False)]
        while pending:
            current, expanded = pending.pop()
            if current in seen:
                continue
            if expanded:
                seen.add(current)
                ordered.append(current)
                continue
            pending.append((current, True))
            nested = [kind for _, kind in table[current].fields if is_record_id(kind)]
            pending.extend((kind, False) for kind in reversed(nested))
    return tuple(ordered)


def slot_counts(table: Mapping[str, RecordDecl], order: Sequence[str]) -> Dict[str, int]:
    """Materialized field slots per value of each record, nested slots included."""
    counts: Dict[str, int] = {}
    for rid in order:
        counts[rid] = sum(1 + counts.get(kind, 0) for _, kind in table[rid].fields)
    return counts


# ---------------------------------------------------------------------------
# load-time validation
# ---------------------------------------------------------------------------


def _load_entry(entry: Any, index: int, known: Dict[str, RecordDecl], depths: Dict[str, int]) -> RecordDecl:
    where = f"records[{index}]"
    if not isinstance(entry, Mapping) or set(entry) != {"id", "module", "name", "fields"}:
        raise RecordIRError("RECORD_IR_SCHEMA", f"{where} needs exactly id, module, name and fields")
    module, name, raw_fields = entry["module"], entry["name"], entry["fields"]
    if not all(isinstance(v, str) and _IDENT.match(v) for v in (module, name)):
        raise RecordIRError("RECORD_IR_SCHEMA", f"{where} module and name must be identifiers")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise RecordIRError("RECORD_IR_SCHEMA", f"{where} needs at least one field")
    if len(raw_fields) > MAX_RECORD_FIELDS:
        raise RecordIRError("RECORD_IR_LIMIT", f"{where} exceeds {MAX_RECORD_FIELDS} fields")
    fields: List[Tuple[str, str]] = []
    for spec in raw_fields:
        if not isinstance(spec, Mapping) or set(spec) != {"name", "type"}:
            raise RecordIRError("RECORD_IR_SCHEMA", f"{where} fields need exactly name and type")
        field, kind = spec["name"], spec["type"]
        if not isinstance(field, str) or not _IDENT.match(field) or field in dict(fields):
            raise RecordIRError("RECORD_IR_SCHEMA", f"{where} has an invalid or duplicate field name")
        if kind not in RECORD_SCALARS and kind not in known:
            raise RecordIRError("RECORD_IR_SCHEMA", f"{where} field type must be a scalar or an earlier record")
        fields.append((field, kind))
    decl = RecordDecl.create(module, name, fields)
    if entry["id"] != decl.id:
        raise RecordIRError("RECORD_IR_IDENTITY", f"{where} id does not match its module, name and schema")
    if decl.id in known:
        raise RecordIRError("RECORD_IR_SCHEMA", f"{where} duplicates an earlier record")
    depth = 1 + max((depths[kind] for _, kind in fields if kind in depths), default=0)
    if depth > MAX_RECORD_DEPTH:
        raise RecordIRError("RECORD_IR_LIMIT", f"{where} exceeds record depth {MAX_RECORD_DEPTH}")
    depths[decl.id] = depth
    return decl


def load_record_table(raw: Any) -> Tuple[RecordDecl, ...]:
    """Validate a `records` table: identities, limits and backward references."""
    if not isinstance(raw, list) or not raw:
        raise RecordIRError("RECORD_IR_SCHEMA", "'records' must be a non-empty list")
    known: Dict[str, RecordDecl] = {}
    depths: Dict[str, int] = {}
    for index, entry in enumerate(raw):
        decl = _load_entry(entry, index, known, depths)
        known[decl.id] = decl
    return tuple(known.values())


def _check_version(version: str, nodes: Sequence[Any], inputs: Sequence[Any], records: Sequence[RecordDecl]) -> None:
    uses_records = any(node.op in RECORD_OPS for node in nodes) or any(
        isinstance(item.type, str) and item.type.startswith("record:") for item in inputs
    )
    if version == "1.1.0":
        if not records:
            raise RecordIRError("RECORD_IR_VERSION", "1.1.0 documents declare at least one record")
        return
    if records or uses_records:
        raise RecordIRError("RECORD_IR_VERSION", "record declarations, inputs and opcodes require nanoIrVersion 1.1.0")


def validate_record_flow(version: str, nodes: Sequence[Any], inputs: Sequence[Any], records: Sequence[RecordDecl]) -> None:
    """Check version gating, the canonical table, and that records flow only into `record.get`."""
    _check_version(version, nodes, inputs, records)
    if not records:
        return
    table = {decl.id: decl for decl in records}
    declared = {item.name: item.type for item in inputs}
    roots = []
    for item in inputs:
        if isinstance(item.type, str) and item.type.startswith("record:"):
            if item.type not in table:
                raise RecordIRError("RECORD_IR_SCHEMA", f"input {item.name!r} names an undeclared record")
            roots.append(item.type)
    if canonical_closure(roots, table) != tuple(table):
        raise RecordIRError("RECORD_IR_SCHEMA", "'records' must be exactly the canonical closure of record inputs")
    typed: Dict[str, str] = {}
    for node in nodes:
        if node.op != "record.get" and any(ref in typed for ref in node.inputs):
            raise RecordIRError("RECORD_IR_FLOW", f"node {node.id!r} ({node.op}) cannot consume a record value")
        if node.op == "record.input":
            name, rid = node.attrs.get("name"), node.attrs.get("record")
            if not isinstance(name, str) or not isinstance(rid, str) or declared.get(name) != rid:
                raise RecordIRError("RECORD_IR_FLOW", f"node {node.id!r} must read a declared input of its record type")
            typed[node.id] = rid
        elif node.op == "record.get":
            rid, field = node.attrs.get("record"), node.attrs.get("field")
            if not isinstance(rid, str) or typed.get(node.inputs[0]) != rid:
                raise RecordIRError("RECORD_IR_FLOW", f"node {node.id!r} must read a validated value of its record type")
            kind = table[rid].field_type(field) if isinstance(field, str) else None
            if kind is None:
                raise RecordIRError("RECORD_IR_FLOW", f"node {node.id!r} reads an undeclared field")
            if kind in table:
                typed[node.id] = kind


# ---------------------------------------------------------------------------
# host admission
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordValue:
    """An admitted, immutable record value. Holds no reference to host objects."""

    record_id: str
    fields: Tuple[Tuple[str, Any], ...]

    def get(self, field: str) -> Any:
        for name, value in self.fields:
            if name == field:
                return value
        raise KeyError(field)


def _check_scalar(kind: str, value: Any, path: str, budget: List[int]) -> None:
    if kind == "bool":
        if type(value) is not bool:
            raise RecordValueError("RECORD_INPUT_TYPE", path, "expected bool")
    elif kind == "int":
        if type(value) is not int:
            raise RecordValueError("RECORD_INPUT_TYPE", path, "expected int")
        if abs(value) > MAX_RECORD_INT:
            raise RecordValueError("RECORD_INPUT_RANGE", path, "integer outside +/-(2^53 - 1)")
    elif kind == "float":
        if type(value) is not float:
            raise RecordValueError("RECORD_INPUT_TYPE", path, "expected float")
        if not math.isfinite(value):
            raise RecordValueError("RECORD_INPUT_NONFINITE", path, "expected a finite float")
    else:
        if type(value) is not str:
            raise RecordValueError("RECORD_INPUT_TYPE", path, "expected string")
        if budget[0] + len(value) > MAX_RECORD_STRING_BYTES:
            raise RecordValueError("RECORD_INPUT_BUDGET", path, "string byte budget exceeded")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise RecordValueError("RECORD_INPUT_TYPE", path, "string is not valid Unicode") from None
        budget[0] += size
        if budget[0] > MAX_RECORD_STRING_BYTES:
            raise RecordValueError("RECORD_INPUT_BUDGET", path, "string byte budget exceeded")


def _open_envelope(value: Any, rid: str, path: str, ancestors: frozenset) -> Mapping:
    if id(value) in ancestors:
        raise RecordValueError("RECORD_INPUT_RECURSIVE", path, "record value graph is cyclic")
    if not isinstance(value, Mapping):
        raise RecordValueError("RECORD_INPUT_SCHEMA", path, "expected a record envelope")
    try:
        keys = set(value.keys())
    except Exception:
        raise RecordValueError("RECORD_INPUT_SCHEMA", path, "unreadable record envelope") from None
    if keys != _ENVELOPE_KEYS:
        raise RecordValueError("RECORD_INPUT_SCHEMA", path, "envelope needs exactly recordType and fields")
    if type(value["recordType"]) is not str or value["recordType"] != rid:
        raise RecordValueError("RECORD_INPUT_STALE_ID", path, "recordType is not the compiled nominal id")
    fields = value["fields"]
    if id(fields) in ancestors or fields is value:
        raise RecordValueError("RECORD_INPUT_RECURSIVE", path, "record value graph is cyclic")
    if not isinstance(fields, Mapping):
        raise RecordValueError("RECORD_INPUT_SCHEMA", path, "fields must be a mapping")
    return fields


def _validate(root: Any, rid: str, path: str, table: Mapping[str, RecordDecl], budget: List[int]) -> None:
    """Iterative strict walk; depth is bounded by the (already bounded) schema."""
    pending = [(root, rid, path, 1, frozenset())]
    while pending:
        value, current, where, depth, ancestors = pending.pop()
        if depth > MAX_RECORD_DEPTH:
            raise RecordValueError("RECORD_INPUT_BUDGET", where, "record depth budget exceeded")
        fields = _open_envelope(value, current, where, ancestors)
        decl = table[current]
        try:
            names = list(fields.keys())
        except Exception:
            raise RecordValueError("RECORD_INPUT_SCHEMA", where, "unreadable fields") from None
        declared = {name for name, _ in decl.fields}
        hidden = [name for name in names if not isinstance(name, str) or name not in declared]
        if hidden:
            raise RecordValueError("RECORD_INPUT_UNKNOWN_FIELD", where, f"{len(hidden)} undeclared field(s)")
        inner = ancestors | {id(value), id(fields)}
        nested = []
        for name, kind in decl.fields:
            if name not in fields:
                raise RecordValueError("RECORD_INPUT_MISSING_FIELD", f"{where}/{name}", "declared field is absent")
            if kind in table:
                nested.append((fields[name], kind, f"{where}/{name}", depth + 1, inner))
            else:
                _check_scalar(kind, fields[name], f"{where}/{name}", budget)
        pending.extend(reversed(nested))


def _build(value: Mapping, rid: str, table: Mapping[str, RecordDecl]) -> RecordValue:
    fields = value["fields"]
    built = tuple(
        (name, _build(fields[name], kind, table) if kind in table else fields[name])
        for name, kind in table[rid].fields
    )
    return RecordValue(rid, built)


def admit_record_inputs(
    records: Sequence[RecordDecl], inputs: Sequence[Any], signals: Mapping[str, Sequence[Any]]
) -> Dict[str, Tuple[Optional[RecordValue], ...]]:
    """Validate every declared record input, then build immutable values.

    Materialized slots are counted from the schema for every present bar
    before any value is walked; string bytes are counted while walking and
    before any `RecordValue` is allocated.
    """
    table = {decl.id: decl for decl in records}
    slots = slot_counts(table, tuple(table))
    selected = [(item.name, item.type) for item in inputs if item.type in table]
    total = 0
    for name, rid in selected:
        if name not in signals:
            raise RecordValueError("RECORD_INPUT_MISSING", name, "declared record input is absent")
        total += slots[rid] * sum(1 for cell in signals[name] if cell is not None)
        if total > MAX_RECORD_VALUES:
            raise RecordValueError("RECORD_INPUT_BUDGET", name, "materialized field value budget exceeded")
    budget = [0]
    for name, rid in selected:
        for bar, cell in enumerate(signals[name]):
            if cell is not None:
                _validate(cell, rid, f"{name}/{bar}", table, budget)
    return {
        name: tuple(None if cell is None else _build(cell, rid, table) for cell in signals[name])
        for name, rid in selected
    }
