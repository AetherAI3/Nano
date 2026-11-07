"""Nominal immutable record types for package programs (RFC 0085).

``RecordType`` is distinct from the reasoning-signature ``record<Signature>``
type in ``kinds.py``: its name is a nominal record id, it is never produced by
``infer``, and the checker accepts it only through the mixin below, which only
the package compiler uses. Single-source compilation never sees it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields as dataclass_fields, is_dataclass
from typing import Dict, Mapping, Optional, Tuple

from ..compiler.ast import Binary, Call, Expr, Member, RecordLit, Unary
from ..compiler.errors import NanoTypeError
from ..ir.records import RecordDecl, is_record_id
from .kinds import SCALAR_TYPES, Type, parse_type


@dataclass(frozen=True)
class RecordType(Type):
    """A nominal record. ``name`` is the record id; ``label`` is Module.Name."""

    label: str = ""


def record_type(decl: RecordDecl) -> RecordType:
    return RecordType(name=decl.id, label=decl.label)


def describe(kind: Type) -> str:
    """Readable spelling for diagnostics (ids are for IR, labels for people)."""
    return kind.label if isinstance(kind, RecordType) else str(kind)


class RecordTypeError(NanoTypeError):
    """A positioned record diagnostic with a stable ``RECORD_*`` code."""

    def __init__(self, code: str, message: str, line: int, column: int, *, category: str = "type") -> None:
        super().__init__(message, line, column)
        self.code = code
        self.category = category


class RecordChecking:
    """Checker mixin: records visible in one module, plus visit memoization.

    Memoization makes each AST object's type a single computation. That matters
    because substitution shares argument subtrees, and because record checks
    must inspect operand types before delegating to the base checker.
    """

    record_scope: Mapping[str, RecordDecl] = {}
    record_table: Mapping[str, RecordDecl] = {}
    helper_names: frozenset = frozenset()

    def _memo(self) -> Dict[int, Tuple[Type, int]]:
        memo = self.__dict__.get("_record_memo")
        if memo is None:
            memo = self.__dict__["_record_memo"] = {}
        return memo

    def _visit(self, expr: Expr) -> Tuple[Type, int]:
        memo = self._memo()
        if id(expr) not in memo:
            memo[id(expr)] = super()._visit(expr)
        return memo[id(expr)]

    # -- annotations -------------------------------------------------------

    def field_type(self, kind: str) -> Type:
        if is_record_id(kind):
            return record_type(self.record_table[kind])
        parsed = parse_type(kind)
        if parsed is None:  # pragma: no cover - declarations were validated
            raise ValueError(f"unknown record field type {kind!r}")
        return parsed

    def _resolve_annotation(self, text: str, line: int, column: int) -> Type:
        decl = self.record_scope.get(text)
        if decl is not None:
            return record_type(decl)
        if text.startswith("series<") and text[7:-1] in self.record_scope:
            raise RecordTypeError("RECORD_SERIES", f"{text} is not a type: records are per-bar values, not series", line, column)
        return super()._resolve_annotation(text, line, column)

    def _declare_signatures(self) -> None:
        for signature in self.strategy.signatures:
            for item in (*signature.inputs, *signature.outputs):
                if item.declared_type in self.record_scope:
                    raise RecordTypeError(
                        "RECORD_TYPE", "records cannot cross the reasoning boundary in this milestone",
                        item.line, item.column,
                    )
        super()._declare_signatures()

    # -- expressions -------------------------------------------------------

    def _visit_uncached(self, expr: Expr) -> Tuple[Type, int]:
        if isinstance(expr, RecordLit):
            return self._visit_record_lit(expr)
        return super()._visit_uncached(expr)

    def _visit_member(self, expr: Member) -> Tuple[Type, int]:
        target_type, lookback = self._visit(expr.target)
        if not isinstance(target_type, RecordType):
            return super()._visit_member(expr)
        decl = self.record_table[target_type.name]
        kind = decl.field_type(expr.field_name)
        if kind is None:
            raise RecordTypeError(
                "RECORD_FIELD_UNKNOWN", f"record {decl.label} declares no field {expr.field_name!r}",
                expr.line, expr.column,
            )
        return self.field_type(kind), lookback

    def _visit_unary(self, expr: Unary) -> Tuple[Type, int]:
        self._reject_record_operands(expr, (expr.operand,))
        return super()._visit_unary(expr)

    def _visit_binary(self, expr: Binary) -> Tuple[Type, int]:
        self._reject_record_operands(expr, (expr.left, expr.right))
        return super()._visit_binary(expr)

    def _reject_record_operands(self, expr: Expr, operands: Tuple[Expr, ...]) -> None:
        for operand in operands:
            kind, _ = self._visit(operand)
            if isinstance(kind, RecordType):
                raise RecordTypeError(
                    "RECORD_OPERATOR", f"record {kind.label} supports only field access, not {expr.op!r}",
                    expr.line, expr.column,
                )

    def _visit_record_lit(self, expr: RecordLit) -> Tuple[Type, int]:
        decl = self.record_scope.get(expr.record)
        if decl is None:
            raise RecordTypeError("RECORD_TYPE", f"unknown record type {expr.record!r}", expr.line, expr.column)
        declared = dict(decl.fields)
        seen = set()
        lookback = 0
        for init in expr.fields:
            if init.name in seen:
                raise RecordTypeError("RECORD_DUPLICATE", f"field {init.name!r} is given twice", init.line, init.column)
            seen.add(init.name)
            if init.name not in declared:
                raise RecordTypeError(
                    "RECORD_FIELD_UNKNOWN", f"record {decl.label} declares no field {init.name!r}",
                    init.line, init.column,
                )
            self._reject_effects(init.value)
            value_type, value_lookback = self._visit(init.value)
            expected = self.field_type(declared[init.name])
            if value_type.is_series:
                raise RecordTypeError(
                    "RECORD_SERIES", f"{decl.label}.{init.name} expects {describe(expected)}, got {value_type}",
                    init.value.line, init.value.column,
                )
            if value_type != expected:
                raise RecordTypeError(
                    "RECORD_TYPE", f"{decl.label}.{init.name} expects {describe(expected)}, got {describe(value_type)}",
                    init.value.line, init.value.column,
                )
            lookback = max(lookback, value_lookback)
        missing = [name for name, _ in decl.fields if name not in seen]
        if missing:
            raise RecordTypeError(
                "RECORD_FIELD_MISSING", f"record {decl.label} is missing field(s): {', '.join(missing)}",
                expr.line, expr.column,
            )
        return record_type(decl), lookback

    def _reject_effects(self, value: object) -> None:
        """Construction fields may not contain host, indicator or inference calls."""
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, Call) and item.callee not in self.helper_names:
                raise RecordTypeError(
                    "RECORD_EFFECT", f"record fields cannot call {item.callee!r}; bind the value with a scalar let first",
                    item.line, item.column,
                )
            if is_dataclass(item):
                pending.extend(getattr(item, f.name) for f in dataclass_fields(item))
            elif isinstance(item, tuple):
                pending.extend(item)


def resolve_signature_type(text: str, scope: Mapping[str, RecordDecl]) -> Optional[Type]:
    """A helper parameter/result spelling: a scalar name or a visible record."""
    decl = scope.get(text)
    if decl is not None:
        return record_type(decl)
    return SCALAR_TYPES.get(text)
