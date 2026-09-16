"""Bounded read-only project query compilation with explicit Boolean clauses."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .catalog import (DEFAULT_VOCABULARY, FIELDS, GROUP_FIELDS, KINDS,
                      STATUS_VALUES, VERSION, ProjectError, Vocabulary, digest)

_FIELD_ALIASES = {"technology": "tech", "repository": "repo", "status": "state"}
_FILLERS = {"show", "find", "search", "me", "the", "with", "that", "are", "all"}
_VALUE_ALIASES = {"review": {"pending_review": "pending", "changes": "changes_requested"},
                  "ci": {"pass": "passed", "fail": "failed", "green": "passed", "red": "failed"},
                  "kind": {"pull_request": "pr", "prs": "pr"}}


@dataclass(frozen=True)
class Predicate:
    field: str
    value: str
    exclude: bool = False

    def __post_init__(self) -> None:
        if self.field not in FIELDS or type(self.value) is not str or not self.value or len(self.value) > 512:
            raise ProjectError("invalid query predicate")
        if type(self.exclude) is not bool:
            raise ProjectError("exclude must be boolean")
        allowed = STATUS_VALUES.get(self.field, KINDS if self.field == "kind" else None)
        if allowed and self.value not in allowed:
            raise ProjectError(f"unknown {self.field} value: {self.value}")
        if self.field == "number" and not re.fullmatch(r"[1-9][0-9]{0,9}", self.value):
            raise ProjectError("number must be a positive PR number")
        if self.field == "sha" and not re.fullmatch(r"[0-9a-f]{7,64}", self.value):
            raise ProjectError("sha requires 7..64 lowercase hex characters")
        if self.field in ("after", "before"):
            try:
                if date.fromisoformat(self.value).isoformat() != self.value:
                    raise ValueError()
            except ValueError as exc:
                raise ProjectError("date filters require YYYY-MM-DD") from exc

    def to_dict(self) -> dict:
        return {"field": self.field, "value": self.value, "exclude": self.exclude}


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    start: int
    end: int

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "span": [self.start, self.end]}


@dataclass(frozen=True)
class ProjectQuery:
    source: str
    clauses: tuple[tuple[Predicate, ...], ...]
    vocabulary_hash: str
    group_by: str | None = None
    sort: str = "updated"
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        if type(self.source) is not str or len(self.source) > 4096:
            raise ProjectError("query source must be text <= 4096 characters")
        if type(self.clauses) is not tuple or not 1 <= len(self.clauses) <= 8:
            raise ProjectError("queries require 1..8 immutable OR clauses")
        if any(type(c) is not tuple or any(not isinstance(p, Predicate) for p in c) for c in self.clauses):
            raise ProjectError("clauses must contain immutable predicates")
        if sum(map(len, self.clauses)) > 64:
            raise ProjectError("queries support at most 64 predicates")
        if len(self.clauses) > 1 and any(not c for c in self.clauses):
            raise ProjectError("OR clauses must not be empty")
        if self.group_by is not None and self.group_by not in GROUP_FIELDS:
            raise ProjectError("invalid grouping field")
        if self.sort not in ("updated", "number", "title"):
            raise ProjectError("invalid sort")
        if type(self.diagnostics) is not tuple or any(not isinstance(d, Diagnostic) for d in self.diagnostics):
            raise ProjectError("diagnostics must be immutable")

    @property
    def valid(self) -> bool:
        return not self.diagnostics

    def to_dict(self) -> dict:
        return {"nanoProjectQueryVersion": VERSION, "source": self.source,
                "vocabularyHash": self.vocabulary_hash, "valid": self.valid,
                "anyOf": [{"allOf": [p.to_dict() for p in c]} for c in self.clauses],
                "groupBy": self.group_by, "sort": self.sort,
                "diagnostics": [d.to_dict() for d in self.diagnostics]}

    @property
    def content_hash(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True)
class _Token:
    value: str
    literal: bool
    start: int
    end: int
    tag: bool = False


def _tokens(source: str) -> tuple[list[_Token], list[Diagnostic]]:
    result, diagnostics = [], []
    i = 0
    while i < len(source):
        if source[i].isspace():
            i += 1
            continue
        start, value, in_quote = i, [], False
        if source[i] == "[" or source[i:i + 2] == "-[":
            bracket = i + (source[i] == "-")
            close = source.find("]", bracket + 1)
            if close < 0:
                close = len(source)
                diagnostics.append(Diagnostic("UNCLOSED_TAG", "Close the bracketed tag.", start, close))
            value = source[bracket + 1:close].strip()
            result.append(_Token(("-" if bracket != i else "") + value, False, start,
                                 min(close + 1, len(source)), True))
            i = close + 1
            if len(result) > 256:
                raise ProjectError("queries support at most 256 tokens")
            continue
        literal = source[i] == '"' or source[i:i + 2] == '-"'
        while i < len(source) and (in_quote or not source[i].isspace()):
            char = source[i]
            if not in_quote and char == "[":
                break
            if char == '"':
                in_quote = not in_quote
            elif in_quote and char == "\\" and i + 1 < len(source) and source[i + 1] in '\\"':
                i += 1
                value.append(source[i])
            else:
                value.append(char)
            i += 1
        if in_quote:
            diagnostics.append(Diagnostic("UNCLOSED_QUOTE", "Close the quoted search phrase.", start, i))
        result.append(_Token("".join(value), literal, start, i))
        if len(result) > 256:
            raise ProjectError("queries support at most 256 tokens")
    return result, diagnostics


def compile_query(source: str, *, vocabulary: Vocabulary = DEFAULT_VOCABULARY) -> ProjectQuery:
    """Compile natural phrases or explicit filters. Unknown words stay text.

    Clauses are OR-ed; predicates in a clause are AND-ed. There is no I/O,
    implicit current time, generated SQL, permission expansion, or model call.
    """
    if type(source) is not str or len(source) > 4096:
        raise ProjectError("query source must be text <= 4096 characters")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProjectError("invalid Unicode") from exc
    tokens, diagnostics = _tokens(source)
    clauses: list[list[Predicate]] = [[]]
    group_by, sort = None, "updated"
    i, negate, pending_join = 0, False, False
    aliases = sorted(vocabulary.aliases, key=lambda item: (-len(item[0].split()), item[0]))

    def error(token: _Token, message: str, code: str = "UNSUPPORTED_QUERY") -> None:
        diagnostics.append(Diagnostic(code, message, token.start, token.end))

    while i < len(tokens):
        token = tokens[i]
        word = token.value.casefold()
        i += 1
        if not (token.literal or token.tag) and word in ("not", "without", "exclude"):
            if negate:
                error(token, "Repeated negation is ambiguous.")
            negate = True
            pending_join = True
            continue
        if not (token.literal or token.tag) and word in ("and", "or"):
            if not clauses[-1] or pending_join or negate:
                error(token, "An operator needs a predicate on each side.")
            if word == "or" and clauses[-1]:
                if len(clauses) >= 8:
                    raise ProjectError("queries support at most 8 OR clauses")
                clauses.append([])
            pending_join = True
            continue
        if not (token.literal or token.tag) and word in _FILLERS and not negate:
            continue
        if not (token.literal or token.tag) and word in ("group", "sort") and i < len(tokens) and tokens[i].value.casefold() == "by":
            if negate or pending_join:
                error(token, "Grouping/sorting cannot complete a Boolean condition.")
            i += 1
            if i == len(tokens):
                error(token, f"{word} by needs a field.")
                continue
            field = _FIELD_ALIASES.get(tokens[i].value.casefold(), tokens[i].value.casefold())
            i += 1
            if word == "group":
                if field not in GROUP_FIELDS or group_by is not None:
                    error(token, "Choose one supported grouping field.")
                else:
                    group_by = field
            elif field not in ("updated", "number", "title"):
                error(token, "Sort by updated, number, or title.")
            else:
                sort = field
            continue
        grammatical = re.sub(r'"(?:\\.|[^"\\])*"', '', source[token.start:token.end])
        if not token.literal and any(c in grammatical for c in "()"):
            error(token, "Parentheses are not supported; use AND clauses separated by OR, or quote literal text.")
        if word.startswith("-") and source[token.start] == "-":
            if negate:
                error(token, "Repeated negation is ambiguous.")
            negate, word = True, word[1:]
        field, value = "text", word
        if token.tag:
            found = vocabulary.lookup(word)
            field, value = found if found else ("tag", word)
        elif not token.literal and ":" in word:
            field, value = word.split(":", 1)
            field = _FIELD_ALIASES.get(field, field)
            if field not in FIELDS:
                error(token, f"Unknown filter {field!r}.", "UNKNOWN_FILTER")
                negate, pending_join = False, False
                continue
            value = _VALUE_ALIASES.get(field, {}).get(value, value)
        elif not token.literal and re.fullmatch(r"#\d+", word):
            field, value = "number", word[1:]
        elif not token.literal:
            for alias, candidate_field, candidate_value in aliases:
                parts = alias.split()
                rest = tokens[i:i + len(parts) - 1]
                if [word, *[t.value.casefold() for t in rest]] == parts and not any(t.literal or t.tag for t in rest):
                    field, value = candidate_field, candidate_value
                    i += len(parts) - 1
                    break
        try:
            clauses[-1].append(Predicate(field, value, negate))
        except ProjectError as exc:
            error(token, str(exc), "INVALID_FILTER")
        negate, pending_join = False, False
        if sum(map(len, clauses)) > 64:
            raise ProjectError("queries support at most 64 predicates")
    if negate or pending_join:
        token = tokens[-1]
        error(token, "The query ends before its condition is complete.")
    # Invalid queries never run; retain a valid-shaped diagnostic envelope.
    if len(clauses) > 1:
        clauses = [c for c in clauses if c] or [[]]
    return ProjectQuery(source, tuple(tuple(c) for c in clauses), vocabulary.content_hash,
                        group_by, sort, tuple(diagnostics))
