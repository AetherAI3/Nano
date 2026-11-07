"""Package-only record syntax, declarations and lowering (RFC 0085).

The single-source parser is untouched. The package compiler builds its parser
from ``record_parser(base)``, which adds three things only: `record`
declarations inside modules, named-field construction `R(f: e, ...)`, and a
positioned `RECORD_MUTATION` for any attempt to assign a field.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Dict, Mapping, Tuple

from ..ir.records import (
    MAX_RECORD_DECLARATIONS, MAX_RECORD_DEPTH, MAX_RECORD_FIELDS, RECORD_SCALARS,
    RecordDecl, canonical_closure, is_record_id,
)
from ..ir.schema import IRValidationError
from ..types.env import KIND_INPUT
from ..types.kinds import SCALAR_TYPES
from ..types.records import RecordType, RecordTypeError
from .ast import Member, RecordFieldInit, RecordLit

_MUTATION = "record fields are immutable; construct a new record instead"


@dataclass(frozen=True)
class RecordFieldAst:
    name: str
    declared_type: str
    line: int
    column: int


@dataclass(frozen=True)
class RecordAst:
    name: str
    fields: Tuple[RecordFieldAst, ...]
    line: int
    column: int


def parse_record(parser) -> RecordAst:
    """`record Name { field: type ... }` inside a package module."""
    parser._expect_keyword("record")
    name = parser._expect("IDENT", "record name")
    parser._expect("LBRACE", "'{'")
    fields = []
    while parser._peek().type != "RBRACE":
        field = parser._expect("IDENT", "field name")
        parser._expect("COLON", "':'")
        fields.append(RecordFieldAst(field.value, parser._parse_type(), field.line, field.column))
    parser._advance()
    return RecordAst(name.value, tuple(fields), name.line, name.column)


class RecordSyntax:
    """Parser mixin for package sources. Depth and nesting limits still apply."""

    def _token_at(self, offset: int):
        return self._tokens[min(self._pos + offset, len(self._tokens) - 1)]

    def _expr(self, expr):
        if not isinstance(expr, RecordLit):
            return super()._expr(expr)
        depth = 1 + max((self._expr_depths.get(id(f.value), 0) for f in expr.fields), default=0)
        if depth > self._limits.max_nesting:
            raise self._limit_error(expr.line, expr.column, "expression depth")
        self._expr_depths[id(expr)] = depth
        return expr

    def _parse_primary(self):
        token = self._peek()
        if (
            self._pending is None and token.type == "IDENT" and token.value not in ("true", "false")
            and self._token_at(1).type == "LPAREN" and self._token_at(2).type == "IDENT"
            and self._token_at(3).type == "COLON"
        ):
            return self._parse_construction()
        return super()._parse_primary()

    def _parse_construction(self):
        name = self._advance()
        opening = self._advance()
        inits = []
        with self._nested(opening):
            while True:
                field = self._expect("IDENT", "field name")
                self._expect("COLON", "':'")
                inits.append(RecordFieldInit(field.line, field.column, field.value, self._parse_expr()))
                if self._peek().type != "COMMA":
                    break
                self._advance()
            self._expect("RPAREN", "')'")
        return self._expr(RecordLit(line=name.line, column=name.column, record=name.value, fields=tuple(inits)))

    def _parse_postfix(self):
        expr = super()._parse_postfix()
        token = self._peek()
        if isinstance(expr, Member) and token.type == "ASSIGN":
            raise RecordTypeError("RECORD_MUTATION", _MUTATION, token.line, token.column, category="syntax")
        return expr

    def _parse_statement(self):
        if self._pending is None and self._peek().type == "IDENT":
            offset = 1
            while self._token_at(offset).type == "DOT" and self._token_at(offset + 1).type == "IDENT":
                offset += 2
            token = self._token_at(offset)
            if offset > 1 and token.type == "ASSIGN":
                raise RecordTypeError("RECORD_MUTATION", _MUTATION, token.line, token.column, category="syntax")
        return super()._parse_statement()


@lru_cache(maxsize=8)
def record_parser(base):
    """A parser class with record syntax layered over ``base``."""
    return type("RecordParser", (RecordSyntax, base), {})


# ---------------------------------------------------------------------------
# declarations -> nominal registry
# ---------------------------------------------------------------------------

Fail = Callable[..., None]


@dataclass
class RecordRegistry:
    own: Dict[str, Dict[str, RecordDecl]]
    scopes: Dict[str, Dict[str, RecordDecl]]
    table: Dict[str, RecordDecl]


def _reaches(start: str, target: str, pending: Mapping[str, RecordAst]) -> bool:
    stack, seen = [start], set()
    while stack:
        name = stack.pop()
        if name == target:
            return True
        if name in seen or name not in pending:
            continue
        seen.add(name)
        stack.extend(f.declared_type for f in pending[name].fields)
    return False


class _RegistryBuilder:
    def __init__(self, units, modules, fail: Fail, limit: Fail, reserved: Callable[[str], bool]) -> None:
        self.units, self.modules = units, modules
        self.names = {path: name for name, path in modules.items()}
        self.fail, self.limit, self.reserved = fail, limit, reserved
        self.registry = RecordRegistry({}, {}, {})
        self.depths: Dict[str, int] = {}

    def visit(self, path: str) -> None:
        if path in self.registry.scopes:
            return
        imports, functions, _, records = self.units[path]
        visible: Dict[str, RecordDecl] = {}
        for site in imports:
            dep = self.modules[site.value]
            self.visit(dep)
            for name, decl in self.registry.own[dep].items():
                if name in visible:
                    self.fail("RECORD_DUPLICATE", f"duplicate symbol {name!r}", path, site)
                visible[name] = decl
        mine: Dict[str, RecordDecl] = {}
        pending = {record.name: record for record in records}
        function_names = {fn.name for fn in functions}
        for record in records:
            if record.name in mine or record.name in visible or record.name in function_names:
                self.fail("RECORD_DUPLICATE", f"duplicate symbol {record.name!r}", path, record)
            if self.reserved(record.name):
                self.fail("RECORD_DECLARATION", f"record shadows builtin or type {record.name!r}", path, record)
            decl = self._declare(path, record, mine, visible, pending)
            mine[record.name] = visible[record.name] = decl
        self.registry.own[path] = mine
        self.registry.scopes[path] = visible

    def _declare(self, path, record, mine, visible, pending) -> RecordDecl:
        if not record.fields:
            self.fail("RECORD_DECLARATION", f"record {record.name!r} needs at least one field", path, record)
        if len(record.fields) > MAX_RECORD_FIELDS:
            self.limit(f"record field budget exceeded ({MAX_RECORD_FIELDS})", path, record)
        fields, seen = [], set()
        for field in record.fields:
            if field.name in seen:
                self.fail("RECORD_DUPLICATE", f"duplicate field {field.name!r}", path, field)
            seen.add(field.name)
            fields.append((field.name, self._field_kind(path, record, field, mine, visible, pending)))
        decl = RecordDecl.create(self.names[path], record.name, fields)
        depth = 1 + max((self.depths[kind] for _, kind in fields if is_record_id(kind)), default=0)
        if depth > MAX_RECORD_DEPTH:
            self.limit(f"record depth budget exceeded ({MAX_RECORD_DEPTH})", path, record)
        if len(self.registry.table) >= MAX_RECORD_DECLARATIONS:
            self.limit(f"record declaration budget exceeded ({MAX_RECORD_DECLARATIONS})", path, record)
        self.depths[decl.id] = depth
        self.registry.table[decl.id] = decl
        return decl

    def _field_kind(self, path, record, field, mine, visible, pending) -> str:
        kind = field.declared_type
        if kind in RECORD_SCALARS:
            return kind
        if kind.startswith("series<"):
            self.fail("RECORD_SERIES", f"field {field.name!r} cannot be a series", path, field)
        if kind in SCALAR_TYPES or "<" in kind:
            self.fail("RECORD_DECLARATION", f"unsupported field type {kind!r}", path, field)
        if kind in mine:
            return mine[kind].id
        if kind in pending:
            if _reaches(kind, record.name, pending):
                self.fail("RECORD_RECURSIVE", f"record {record.name!r} refers back to itself through {kind!r}", path, field)
            self.fail("RECORD_DECLARATION", f"record {kind!r} is used before its declaration", path, field)
        if kind in visible:
            return visible[kind].id
        self.fail("RECORD_DECLARATION", f"unknown field type {kind!r}", path, field)
        raise AssertionError("unreachable")  # pragma: no cover - fail raises


def build_registry(units, modules, fail: Fail, limit: Fail, reserved: Callable[[str], bool]) -> RecordRegistry:
    """Resolve every declaration (used or not) into nominal ids, in import order."""
    builder = _RegistryBuilder(units, modules, fail, limit, reserved)
    for path in sorted(units):
        builder.visit(path)
    return builder.registry


def module_records(inputs, table: Mapping[str, RecordDecl]) -> Tuple[RecordDecl, ...]:
    """The canonical 1.1.0 `records` table for a lowered module's inputs."""
    roots = [item.type for item in inputs if is_record_id(item.type)]
    return tuple(table[rid] for rid in canonical_closure(roots, table))


class RecordLowering:
    """Lowerer mixin: record inputs and field reads become 1.1.0 opcodes."""

    def _lower_expr(self, expr):
        if isinstance(expr, Member):
            target_type = self.program.type_of(expr.target)
            if isinstance(target_type, RecordType):
                return self._emit(
                    "record.get", inputs=(self._lower_expr(expr.target),),
                    attrs={"record": target_type.name, "field": expr.field_name},
                    type_=self._type_of(expr),
                )
        if isinstance(expr, RecordLit):  # pragma: no cover - projection removes every construction
            raise IRValidationError("record construction survived projection")
        return super()._lower_expr(expr)

    def _lower_name(self, expr):
        symbol = self.program.symbols.get(expr.name)
        if symbol is None or symbol.kind != KIND_INPUT or not isinstance(symbol.type, RecordType):
            return super()._lower_name(expr)
        key = f"record:{expr.name}"
        if key not in self._named:
            self._named[key] = self._emit(
                "record.input", attrs={"name": expr.name, "record": symbol.type.name}, type_=symbol.type.name,
            )
        return self._named[key]
