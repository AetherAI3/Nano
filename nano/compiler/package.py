"""Explicit local packages, format 0.1.0; executable output remains Nano IR v1.

Only this opt-in entry point reads files. The source compiler stays a pure
function. Imports select manifest names, never paths or ambient search roots.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path, PurePosixPath

from .ast import (
    Binary, BoolLit, Call, Expr, InputAst, LetAst, Name, NumberLit,
    StrategyAst, StringLit, Unary,
)
from .codegen import _Lowerer, source_hash
from .errors import NanoCompileError
from .lexer import tokenize
from .parser import _Parser
from ..ir.module import COMPILER_VERSION, NanoModule
from ..ir.schema import KNOWN_EFFECTS
from ..types.checker import _BUILTINS, _Checker, check
from ..types.kinds import parse_type
from ..indicators import lookup

PACKAGE_VERSION = "0.1.0"
# Fixed admission ceilings, deliberately not author-adjustable.
MAX_FILES = 32
MAX_BYTES = 262144
MAX_TOKENS = 16384
MAX_FUNCTIONS = 128
MAX_DEPTH = 32
MAX_EXPANSION = 8192
MAX_IR_NODES = 8192
SCALARS = frozenset({"int", "float", "bool", "string"})


class PackageError(NanoCompileError):
    """A positioned package diagnostic, including the dependency's relative path."""

    code = "PACKAGE_ERROR"

    def __init__(self, message, path, line=1, column=1):
        super().__init__(f"{path}: {message}", line, column)
        self.path = path


class PackageResourceError(PackageError):
    """Admission failed before compilation exceeded a fixed resource ceiling."""

    code = "RESOURCE_LIMIT"


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _location(path, node):
    return {"path": path, "line": node.line, "column": node.column}


@dataclass(frozen=True)
class _Function:
    name: str
    params: tuple
    result: str
    body: Expr
    path: str
    line: int
    column: int


@dataclass(frozen=True)
class CompiledPackage:
    """Versioned build evidence alongside an independently versioned executable."""

    module: NanoModule
    manifest: dict
    lock: dict
    source_map: dict

    def to_dict(self):
        payload = {
            "type": "NanoPackage", "nanoPackageVersion": PACKAGE_VERSION,
            "compiler": {"name": "nnc-package", "version": PACKAGE_VERSION,
                         "irCompilerVersion": COMPILER_VERSION},
            "manifest": self.manifest, "lock": self.lock,
            "module": self.module.to_dict(), "sourceMap": self.source_map,
        }
        return {**payload, "packageHash": _digest(_canonical(payload))}


class _SignatureChecker(_Checker):
    def __init__(self, strategy, functions):
        super().__init__(strategy)
        self.functions = functions

    def _visit_call(self, expr):
        fn = self.functions.get(expr.callee)
        if fn is None:
            return super()._visit_call(expr)
        if len(expr.args) != len(fn.params):
            raise self._fail(f"{fn.name} expects {len(fn.params)} arguments, got {len(expr.args)}",
                             expr.line, expr.column)
        for arg, param in zip(expr.args, fn.params):
            self._check_argument(arg)
            actual, _ = self._visit(arg)
            if str(actual) != param.declared_type:
                raise self._fail(f"{fn.name}.{param.name} expects {param.declared_type}, got {actual}",
                                 expr.line, expr.column)
        return parse_type(fn.result), 0

    def _check_argument(self, value):
        if isinstance(value, Call) and value.callee not in self.functions:
            raise self._fail("helper arguments cannot call host functions or indicators; bind the result first",
                             value.line, value.column)
        if is_dataclass(value):
            for f in fields(value):
                self._check_argument(getattr(value, f.name))
        elif isinstance(value, tuple):
            for item in value:
                self._check_argument(item)


class _MappedLowerer(_Lowerer):
    def __init__(self, program, origins, default):
        super().__init__(program)
        self.origins = origins
        self.context = [default]
        self.source_map = {}

    def _emit(self, *args, **kwargs):
        if len(self.nodes) >= MAX_IR_NODES:
            site = self.context[-1]
            raise PackageResourceError("IR node budget exceeded", site["path"], site["line"], site["column"])
        node_id = super()._emit(*args, **kwargs)
        self.source_map[node_id] = list(self.context)
        return node_id

    def _lower_expr(self, expr):
        previous = self.context
        self.context = self.origins[id(expr)]
        try:
            return super()._lower_expr(expr)
        finally:
            self.context = previous


class _Build:
    def __init__(self, manifest_path, lock_path):
        self.manifest_path = Path(manifest_path).resolve()
        self.lock_path = Path(lock_path).resolve()
        self.root = self.manifest_path.parent
        self.manifest = self._json(self.manifest_path)
        self.lock = self._json(self.lock_path)
        self.sources = {}
        self.units = {}
        self.origins = {}
        self.costs = {}
        self.expansion = 0
        self._validate_config()

    @staticmethod
    def _json(path):
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise PackageResourceError("metadata byte budget exceeded", path.name)
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise PackageError(f"duplicate metadata key {key!r}", path.name)
                    result[key] = value
                return result
            return json.loads(raw, object_pairs_hook=unique)
        except RecursionError as exc:
            raise PackageResourceError("metadata depth budget exceeded", path.name) from exc
        except (OSError, ValueError) as exc:
            if isinstance(exc, PackageError):
                raise
            line, column = getattr(exc, "lineno", 1), getattr(exc, "colno", 1)
            raise PackageError(f"cannot read JSON: {exc}", path.name, line, column) from exc

    def fail(self, message, path, node=None):
        raise PackageError(message, path, getattr(node, "line", 1), getattr(node, "column", 1))

    def limit(self, message, path, node=None):
        raise PackageResourceError(message, path, getattr(node, "line", 1), getattr(node, "column", 1))

    def _bound_tree(self, value, path):
        pending = [(value, 0)]
        while pending:
            value, depth = pending.pop()
            if isinstance(value, Expr):
                depth += 1
                if depth > MAX_DEPTH:
                    if id(value) in self.origins:
                        site = self.origins[id(value)][-1]
                        raise PackageResourceError("expression depth budget exceeded", site["path"], site["line"], site["column"])
                    self.limit("expression depth budget exceeded", path, value)
            if is_dataclass(value):
                pending.extend((getattr(value, f.name), depth) for f in fields(value))
            elif isinstance(value, tuple):
                pending.extend((v, depth) for v in value)

    def _validate_config(self):
        m, lock = self.manifest, self.lock
        if not isinstance(m, dict) or set(m) != {"nanoPackageVersion", "entry", "modules", "effects"}:
            self.fail("expected version, entry, modules and effects", self.manifest_path.name)
        if m["nanoPackageVersion"] != PACKAGE_VERSION or not isinstance(m["modules"], dict):
            self.fail("unsupported manifest version or modules", self.manifest_path.name)
        if not isinstance(lock, dict) or set(lock) != {"nanoLockVersion", "dependencies"} or lock["nanoLockVersion"] != PACKAGE_VERSION:
            self.fail("unsupported lock format", self.lock_path.name)
        paths = [m["entry"], *m["modules"].values()]
        if len(paths) > MAX_FILES:
            self.limit("file budget exceeded", self.manifest_path.name)
        if not all(isinstance(p, str) for p in paths):
            self.fail("invalid file list", self.manifest_path.name)
        for name in m["modules"]:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                self.fail("invalid module name", self.manifest_path.name)
        for path in paths:
            parts = PurePosixPath(path)
            if (not path or "\\" in path or ":" in path or parts.is_absolute()
                    or any(p in {"", ".", ".."} for p in path.split("/"))
                    or parts.suffix != ".nano"):
                self.fail("expected canonical relative .nano path", self.manifest_path.name)
        if len(set(p.casefold() for p in paths)) != len(paths):
            self.fail("duplicate source paths", self.manifest_path.name)
        deps = lock["dependencies"]
        if not isinstance(deps, dict) or set(deps) != set(paths) or not all(
            isinstance(v, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", v) for v in deps.values()
        ):
            self.fail("lock must pin exactly every manifest source", self.lock_path.name)
        effects = m["effects"]
        if not isinstance(effects, list) or not all(isinstance(e, str) for e in effects) or len(set(effects)) != len(effects):
            self.fail("effects must be distinct names", self.manifest_path.name)
        if set(effects) - KNOWN_EFFECTS:
            self.fail("unknown effect grant", self.manifest_path.name)

    def _read(self, path, site_path=None, site=None):
        if path in self.sources:
            return
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            self.fail("source escapes package root", site_path or path, site)
        try:
            with resolved.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
        except OSError as exc:
            self.fail(f"cannot read pinned source: {exc.strerror}", site_path or path, site)
        if sum(len(s.encode("utf-8")) for s in self.sources.values()) + len(raw) > MAX_BYTES:
            self.limit("source byte budget exceeded", site_path or path, site)
        if _digest(raw) != self.lock["dependencies"][path]:
            self.fail(f"stale dependency hash for {path}", site_path or path, site)
        try:
            self.sources[path] = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.fail("source must be UTF-8", path)

    def _parse(self, path, module_name=None):
        try:
            tokens = tokenize(self.sources[path])
            if len(tokens) > MAX_TOKENS:
                self.limit("token budget exceeded", path)
            # Bound parser recursion before recursive descent, including long
            # unary chains and unparenthesized expression trees.
            depth = 0
            for token in tokens:
                if token.type in {"LPAREN", "LBRACE", "LBRACKET"}:
                    depth += 1
                    if depth > MAX_DEPTH:
                        self.limit("syntax depth budget exceeded", path, token)
                elif token.type in {"RPAREN", "RBRACE", "RBRACKET"}:
                    depth -= 1
            parser = _Parser(tokens)
            imports = []
            while parser._at_keyword("import"):
                parser._advance()
                imports.append(parser._expect("IDENT", "manifest module name"))
            functions = []
            if module_name is not None:
                parser._expect_keyword("module")
                name = parser._expect("IDENT", "module name")
                if name.value != module_name:
                    self.fail("module name differs from manifest", path, name)
                parser._expect("LBRACE", "'{'")
                while parser._peek().type != "RBRACE":
                    parser._expect_keyword("pure")
                    parser._expect_keyword("fn")
                    name = parser._expect("IDENT", "function name")
                    if name.value in {fn.name for fn in functions}:
                        self.fail(f"duplicate symbol {name.value!r}", path, name)
                    if name.value == "infer" or name.value in dict(_BUILTINS) or lookup(name.value) is not None:
                        self.fail(f"helper shadows builtin {name.value!r}", path, name)
                    parser._expect("LPAREN", "'('")
                    params = []
                    while parser._peek().type != "RPAREN":
                        param = parser._expect("IDENT", "parameter name")
                        parser._expect("COLON", "':'")
                        type_ = parser._parse_type()
                        if type_ not in SCALARS:
                            self.fail("pure parameters require scalar types", path, param)
                        if param.value in {p.name for p in params}:
                            self.fail("duplicate parameter", path, param)
                        params.append(InputAst(param.value, type_, param.line, param.column))
                        if parser._peek().type != "COMMA":
                            break
                        parser._advance()
                    parser._expect("RPAREN", "')'")
                    parser._expect("COLON", "':'")
                    result = parser._parse_type()
                    if result not in SCALARS:
                        self.fail("pure result requires scalar type", path, name)
                    parser._expect("LBRACE", "'{'")
                    body = parser._parse_expr()
                    self._bound_tree(body, path)
                    parser._expect("RBRACE", "'}'")
                    functions.append(_Function(name.value, tuple(params), result, body, path, name.line, name.column))
                parser._advance()
                parser._expect("EOF", "end of module")
                strategy = None
            else:
                strategy = parser.parse_program()
                self._bound_tree(strategy, path)
            self.units[path] = (imports, functions, strategy)
        except RecursionError:
            self.limit("syntax depth budget exceeded", path)
        except NanoCompileError as exc:
            if isinstance(exc, PackageError):
                raise
            raise PackageError(exc.message, path, exc.line, exc.column) from exc

    def _load(self, path, stack=()):
        if len(stack) > MAX_DEPTH:
            self.limit("import depth budget exceeded", path)
        imports = self.units[path][0]
        seen = set()
        for site in imports:
            if site.value in seen:
                self.fail("duplicate import", path, site)
            seen.add(site.value)
            dep = self.manifest["modules"].get(site.value)
            if dep is None:
                self.fail(f"module {site.value!r} is not explicitly pinned", path, site)
            if dep in (*stack, path):
                self.fail("import cycle: " + " -> ".join((*stack, path, dep)), path, site)
            self._read(dep, path, site)
            if dep not in self.units:
                self._parse(dep, site.value)
                self._load(dep, (*stack, path))

    def _scope(self, path):
        imports, functions, strategy = self.units[path]
        scope = {}
        for site in imports:
            for fn in self.units[self.manifest["modules"][site.value]][1]:
                if fn.name in scope:
                    self.fail(f"duplicate symbol {fn.name!r}", path, site)
                scope[fn.name] = fn
        for fn in functions:
            if fn.name in scope:
                self.fail(f"duplicate symbol {fn.name!r}", path, fn)
            scope[fn.name] = fn
        if strategy:
            for group in (strategy.params, strategy.inputs, strategy.lets, strategy.signatures, strategy.agents, strategy.routes):
                for item in group:
                    if item.name in scope:
                        self.fail(f"duplicate symbol {item.name!r}", path, item)
        return scope

    def _pure(self, expr, fn, scope, stack, depth=0):
        height = 0
        if depth > MAX_DEPTH:
            self.limit("pure expression depth budget exceeded", fn.path, expr)
        if isinstance(expr, Name):
            if expr.name not in {p.name for p in fn.params}:
                self.fail(f"pure helper cannot capture {expr.name!r}", fn.path, expr)
        elif isinstance(expr, Call):
            target = scope.get(expr.callee)
            if target is None:
                self.fail(f"pure helper cannot call {expr.callee!r}", fn.path, expr)
            key = (target.path, target.name)
            if key in stack:
                self.fail("recursive helper call", fn.path, expr)
            if len(stack) >= MAX_DEPTH:
                self.limit("call depth budget exceeded", fn.path, expr)
            if key not in self.checked:
                self.checked[key] = self._pure(target.body, target, self.scopes[target.path], (*stack, key))
            height = 1 + self.checked[key]
            if height >= MAX_DEPTH:
                self.limit("call depth budget exceeded", fn.path, expr)
            for arg in expr.args:
                height = max(height, self._pure(arg, fn, scope, stack, depth + 1))
        elif isinstance(expr, Unary):
            height = self._pure(expr.operand, fn, scope, stack, depth + 1)
        elif isinstance(expr, Binary):
            height = max(self._pure(expr.left, fn, scope, stack, depth + 1),
                         self._pure(expr.right, fn, scope, stack, depth + 1))
        elif not isinstance(expr, (NumberLit, BoolLit, StringLit)):
            self.fail("unsupported pure expression", fn.path, expr)
        return height

    def _expand(self, value, path, env=None, chain=(), depth=0):
        if depth > MAX_DEPTH * 4:
            self.limit("expansion depth budget exceeded", path, value)
        if isinstance(value, tuple):
            return tuple(self._expand(v, path, env, chain, depth + 1) for v in value)
        if not is_dataclass(value):
            return value
        self.expansion += 1
        if self.expansion > MAX_EXPANSION:
            self.limit("expansion budget exceeded", path, value)
        if isinstance(value, Name) and env is not None:
            # Substitute only formal parameters; arguments were expanded in
            # the caller's lexical scope, so helpers cannot capture its names.
            argument = env[value.name]
            result = replace(argument)
            self.costs[id(result)] = self.costs[id(argument)]
            self.origins[id(result)] = (*chain, _location(path, value), *self.origins[id(argument)])
            return result
        if isinstance(value, Call) and value.callee in self.scopes[path]:
            fn = self.scopes[path][value.callee]
            args = tuple(self._expand(a, path, env, chain, depth + 1) for a in value.args)
            return self._expand(fn.body, fn.path, dict(zip((p.name for p in fn.params), args)),
                                (*chain, _location(path, value)), depth + 1)
        result = replace(value, **{
            f.name: self._expand(getattr(value, f.name), path, env, chain, depth + 1)
            for f in fields(value)
        })
        def cost(item):
            if is_dataclass(item):
                return self.costs[id(item)]
            if isinstance(item, tuple):
                return sum(cost(v) for v in item)
            return 0
        # Count occurrences, not unique objects: substitution can share a DAG
        # whose tree traversal would otherwise explode in the type checker.
        size = 1 + sum(cost(getattr(result, f.name)) for f in fields(result))
        if size > MAX_EXPANSION:
            self.limit("expanded tree budget exceeded", path, value)
        self.costs[id(result)] = size
        if isinstance(result, Expr):
            self.origins[id(result)] = (*chain, _location(path, value))
        return result

    def run(self):
        entry = self.manifest["entry"]
        self._read(entry)
        self._parse(entry)
        self._load(entry)
        # Every manifest member is part of the closed package, including
        # unused code. Reject stale pins and invalid declarations there too.
        for name, path in sorted(self.manifest["modules"].items()):
            self._read(path)
            if path not in self.units:
                self._parse(path, name)
                self._load(path)
        count = sum(len(unit[1]) for unit in self.units.values())
        if count > MAX_FUNCTIONS:
            self.limit("function budget exceeded", entry)
        self.scopes = {p: self._scope(p) for p in self.units}
        self.checked = {}
        for path, (_, functions, _) in sorted(self.units.items()):
            for fn in functions:
                self._pure(fn.body, fn, self.scopes[path], ((path, fn.name),))
                # Check definitions even when unused, with no implicit feeds.
                result_name = "__result"
                while result_name in {p.name for p in fn.params}:
                    result_name += "_"
                strategy = StrategyAst(name="Pure", inputs=fn.params,
                    lets=(LetAst(result_name, None, fn.body, fn.line, fn.column),))
                try:
                    typed = _SignatureChecker(strategy, self.scopes[path]).run()
                    actual = typed.type_of(fn.body)
                    if str(actual) != fn.result:
                        self.fail(f"{fn.name} returns {actual}, expected {fn.result}", path, fn)
                except NanoCompileError as exc:
                    if isinstance(exc, PackageError):
                        raise
                    raise PackageError(exc.message, path, exc.line, exc.column) from exc
        strategy = self.units[entry][2]
        try:
            _SignatureChecker(strategy, self.scopes[entry]).run()
            expanded = self._expand(strategy, entry)
            self._bound_tree(expanded, entry)
            typed = check(expanded)
            lowerer = _MappedLowerer(typed, self.origins, _location(entry, strategy))
            module = lowerer.run(source_hash=source_hash(self.sources[entry]))
        except RecursionError:
            self.limit("expansion depth budget exceeded", entry)
        except NanoCompileError as exc:
            if isinstance(exc, PackageError):
                raise
            raise PackageError(exc.message, entry, exc.line, exc.column) from exc
        if len(module.nodes) > MAX_IR_NODES:
            self.limit("IR node budget exceeded", entry)
        undeclared = set(module.effects) - set(self.manifest["effects"])
        if undeclared:
            self.fail("undeclared effects: " + ", ".join(sorted(undeclared)), entry)
        module = NanoModule.from_dict(module.to_dict())
        return CompiledPackage(module, self.manifest, self.lock, lowerer.source_map)


def compile_package(manifest_path, *, lock_path):
    """Compile only explicitly named, hash-pinned local UTF-8 sources.

    Both metadata paths are required. No discovery, lock refresh, download,
    execution, inference or QPU call occurs. Returns build evidence and v1 IR.
    """
    return _Build(manifest_path, lock_path).run()
