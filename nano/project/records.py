"""Evidence-bearing annotations over supplied PR, commit, and memory records."""
from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import PurePosixPath
from typing import Mapping

from .catalog import (DEFAULT_VOCABULARY, KINDS, SEMANTIC_FIELDS, SLUG,
                      STATUS_VALUES, VERSION, ProjectError, Vocabulary, digest)

_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CONVENTIONAL = re.compile(r"^(feat|fix|refactor|docs|test|chore|perf|build|ci)"
                           r"(?:\(([^)]+)\))?(!?):\s*(.+)$", re.I)
_TYPE = {"feat": "feature", "perf": "refactor", "build": "chore", "ci": "chore"}
_PREFIX = {"feature": "feat", "fix": "fix", "refactor": "refactor",
           "docs": "docs", "test": "test", "chore": "chore"}


def timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        return parsed
    except (TypeError, ValueError) as exc:
        raise ProjectError("timestamps must be ISO-8601 with an explicit timezone") from exc


def _text(value: object, name: str, maximum: int, *, required: bool = False) -> None:
    if type(value) is not str or len(value) > maximum or (required and not value.strip()):
        raise ProjectError(f"{name} must be {'nonempty ' if required else ''}text <= {maximum} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProjectError(f"{name} contains invalid Unicode") from exc
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ProjectError(f"{name} contains control characters")


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    project_id: str
    kind: str
    title: str
    repository: str = ""
    number: int | None = None
    author: str = ""
    body: str = ""
    files: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    source_sha: str = ""
    state: str = "unknown"
    review: str = "unknown"
    ci: str = "unknown"
    ci_sha: str = ""
    review_sha: str = ""
    observed_at: str = ""
    updated_at: str = ""
    depends_on: tuple[str, ...] = ()
    blocks: tuple[str, ...] = ()
    tag_overrides: tuple[tuple[str, tuple[str, ...]], ...] = ()
    files_complete: bool = False

    def __post_init__(self) -> None:
        for name, maximum in (("id", 256), ("project_id", 128), ("title", 500),
                              ("repository", 200), ("author", 100), ("body", 16000)):
            _text(getattr(self, name), name, maximum, required=name in ("id", "project_id", "title"))
        for name in ("id", "project_id", "repository", "author"):
            if any(c.isspace() for c in getattr(self, name)):
                raise ProjectError(f"{name} must not contain whitespace")
        if self.kind not in KINDS:
            raise ProjectError("kind must be pr, commit, or memory")
        if self.number is not None and (type(self.number) is not int or self.number <= 0):
            raise ProjectError("number must be a positive integer")
        for name, maximum in (("files", 2000), ("labels", 128), ("depends_on", 64), ("blocks", 64)):
            values = getattr(self, name)
            if type(values) is not tuple or len(values) > maximum:
                raise ProjectError(f"{name} must be an immutable bounded tuple")
            for item in values:
                _text(item, name, 512, required=True)
        for field, allowed in STATUS_VALUES.items():
            if getattr(self, field) not in allowed:
                raise ProjectError(f"invalid {field}")
        for name in ("source_sha", "ci_sha", "review_sha"):
            value = getattr(self, name)
            if type(value) is not str or (value and not _SHA.fullmatch(value)):
                raise ProjectError(f"{name} must be a full lowercase Git SHA")
        for name in ("observed_at", "updated_at"):
            _text(getattr(self, name), name, 40)
            timestamp(getattr(self, name))
        if type(self.files_complete) is not bool:
            raise ProjectError("files_complete must be boolean")
        if type(self.tag_overrides) is not tuple:
            raise ProjectError("tag_overrides must be immutable")
        seen = set()
        for item in self.tag_overrides:
            if type(item) is not tuple or len(item) != 2:
                raise ProjectError("invalid tag override")
            facet, values = item
            if facet not in ("type", "area", "tech") or facet in seen:
                raise ProjectError("overrides may replace type, area, or tech once each")
            seen.add(facet)
            if type(values) is not tuple or len(values) > 16:
                raise ProjectError("override values must be a bounded tuple")
            if any(type(v) is not str or not SLUG.fullmatch(v) for v in values):
                raise ProjectError("override values must be slugs")

    @classmethod
    def from_dict(cls, value: Mapping) -> ProjectRecord:
        if not isinstance(value, Mapping) or any(type(k) is not str for k in value):
            raise ProjectError("record must be an object")
        allowed = {f.name for f in fields(cls)}
        if set(value) - allowed:
            raise ProjectError("unknown record fields: " + ", ".join(sorted(set(value) - allowed)))
        data = dict(value)
        for name in ("files", "labels", "depends_on", "blocks"):
            if name in data:
                if type(data[name]) not in (list, tuple):
                    raise ProjectError(f"{name} must be an array")
                data[name] = tuple(data[name])
        if "tag_overrides" in data:
            if not isinstance(data["tag_overrides"], Mapping):
                raise ProjectError("tag_overrides must be an object")
            converted = []
            for facet, values in data["tag_overrides"].items():
                if type(facet) is not str:
                    raise ProjectError("override fields must be strings")
                if type(values) not in (list, tuple):
                    raise ProjectError("override values must be arrays")
                converted.append((facet, tuple(values)))
            data["tag_overrides"] = tuple(sorted(converted))
        try:
            return cls(**data)
        except TypeError as exc:
            raise ProjectError(f"invalid record: {exc}") from exc


def classify_record(record: ProjectRecord | Mapping, *,
                    vocabulary: Vocabulary = DEFAULT_VOCABULARY) -> dict:
    """Return a derived annotation, not an APR fact or an authorization."""
    if not isinstance(record, ProjectRecord):
        record = ProjectRecord.from_dict(record)
    evidence: dict[tuple[str, str], set[str]] = {}

    def add(facet: str, value: str, reason: str) -> None:
        evidence.setdefault((facet, value), set()).add(reason)

    def phrase(text: str, reason: str) -> None:
        found = vocabulary.lookup(text)
        if found and found[0] in ("type", "area", "tech"):
            add(*found, reason)

    for label in record.labels:
        normalized = label.strip().casefold()
        if ":" in normalized:
            facet, value = normalized.split(":", 1)
            if facet in ("type", "area", "tech") and SLUG.fullmatch(value.strip()):
                add(facet, value.strip(), "label:" + label)
        else:
            phrase(normalized, "label:" + label)
    conventional = _CONVENTIONAL.match(record.title)
    if conventional:
        kind, scope, _, _ = conventional.groups()
        add("type", _TYPE.get(kind.casefold(), kind.casefold()), "conventional-title")
        if scope:
            phrase(scope, "title-scope:" + scope)
    for bracket in re.findall(r"\[([^\]]{1,100})\]", record.title):
        phrase(bracket, "title-tag:" + bracket)
    lowered = record.title.casefold()
    for alias, facet, value in vocabulary.aliases:
        if facet in ("area", "tech") and re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", lowered):
            add(facet, value, "title:" + alias)
    for name in record.files:
        path = PurePosixPath(name.replace("\\", "/"))
        suffix = path.suffix.casefold()
        tech = {".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
                ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
                ".jsx": "javascript", ".py": "python", ".sql": "sql"}.get(suffix)
        if tech:
            add("tech", tech, "path:" + name)
        if suffix in (".tsx", ".jsx"):
            add("tech", "react", "path:" + name)
        parts = {p.casefold() for p in path.parts}
        for area, hints in (("frontend", {"frontend", "components", "styles", "site", "ui"}),
                            ("backend", {"backend", "server"}),
                            ("api", {"api", "routes"}),
                            ("infra", {".github", "terraform", "infra"}),
                            ("cli", {"cli"}), ("memory", {"memory", "memory_graph", "project_memory"})):
            if parts & hints:
                add("area", area, "path:" + name)
    if record.depends_on:
        add("relation", "downstream", "depends_on")
    if record.blocks:
        add("relation", "upstream", "blocks")
    for facet, values in record.tag_overrides:
        evidence = {key: reasons for key, reasons in evidence.items() if key[0] != facet}
        for value in values:
            add(facet, value, "user-override")
    tags = [{"facet": f, "value": v, "evidence": sorted(reasons)[:3]}
            for (f, v), reasons in sorted(evidence.items(),
                key=lambda kv: (SEMANTIC_FIELDS.index(kv[0][0]), kv[0][1]))]
    status = {field: getattr(record, field) for field in STATUS_VALUES}
    diagnostics = []
    for field, sha in (("ci", record.ci_sha), ("review", record.review_sha)):
        if status[field] != "unknown" and (not record.source_sha or sha != record.source_sha):
            status[field] = "unknown"
            diagnostics.append(field.upper() + ("_HEAD_MISMATCH" if sha else "_HEAD_UNBOUND"))
    if any(v != "unknown" for v in status.values()) and not record.observed_at:
        diagnostics.append("STATUS_OBSERVATION_TIME_MISSING")
    status["observedAt"] = record.observed_at or None
    base_title = conventional.group(4) if conventional else record.title
    def remove_semantic_tag(match):
        found = vocabulary.lookup(match.group(1))
        return "" if found and found[0] in ("type", "area", "tech") else match.group(0)
    base_title = re.sub(r"\[([^\]]{1,100})\]", remove_semantic_tag, base_title)
    base_title = " ".join(base_title.split())
    types = sorted(v for f, v in evidence if f == "type")
    areas = sorted(v for f, v in evidence if f == "area")
    suggested = record.title
    if len(types) == 1 and types[0] in _PREFIX and base_title:
        scope = f"({areas[0]})" if len(areas) == 1 else ""
        breaking = conventional.group(3) if conventional else ""
        suggested = f"{_PREFIX[types[0]]}{scope}{breaking}: {base_title}"
    chips = [tag["value"] for tag in tags]
    if status["review"] == "pending":
        chips.append("pending review")
    elif status["review"] != "unknown":
        chips.append(status["review"].replace("_", " "))
    title = " ".join(record.title.split())
    capsule = (f"{record.id} | {title[:117] + '...' if len(title) > 120 else title} | "
               + "".join(f"[{chip}]" for chip in chips[:12])
               + f" | {status['state']} review={status['review']} ci={status['ci']}"
               + (f" | sha={record.source_sha[:12]}" if record.source_sha else "")
               + f" | asof={record.observed_at or 'unknown'}")
    result = {
        "nanoProjectVersion": VERSION, "vocabularyHash": vocabulary.content_hash,
        "recordId": record.id, "projectId": record.project_id, "kind": record.kind,
        "repository": record.repository, "number": record.number,
        "title": record.title, "suggestedTitle": suggested, "sourceSha": record.source_sha or None,
        "tags": tags, "status": status, "updatedAt": record.updated_at or None,
        "dependencies": {"dependsOn": sorted(set(record.depends_on)), "blocks": sorted(set(record.blocks))},
        "filesComplete": record.files_complete, "diagnostics": diagnostics, "capsule": capsule,
    }
    result["annotationHash"] = digest(result)
    return result
