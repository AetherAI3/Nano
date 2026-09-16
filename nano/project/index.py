"""In-memory reference index for a host-authorized project snapshot."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timezone
from fnmatch import fnmatchcase
from typing import Iterable, Mapping

from .catalog import DEFAULT_VOCABULARY, STATUS_VALUES, VERSION, ProjectError, Vocabulary
from .query import Predicate, ProjectQuery, compile_query
from .records import ProjectRecord, classify_record, timestamp


@dataclass(frozen=True)
class _Entry:
    record: ProjectRecord
    annotation: dict
    text: str


class ProjectIndex:
    """Build once on ingestion; search without reclassifying every keystroke.

    Scope is mandatory but is NOT an authorization mechanism. Hosts must supply
    only accessible records. This index never contacts GitHub or mutates memory.
    """

    def __init__(self, records: Iterable[ProjectRecord | Mapping], *,
                 vocabulary: Vocabulary = DEFAULT_VOCABULARY) -> None:
        self.vocabulary = vocabulary
        self._entries: list[_Entry] = []
        self._facets: dict[tuple[str, str], set[int]] = {}
        self._projects: dict[str, set[int]] = {}
        seen = set()
        for raw in records:
            record = raw if isinstance(raw, ProjectRecord) else ProjectRecord.from_dict(raw)
            identity = (record.project_id, record.id)
            if identity in seen:
                raise ProjectError("duplicate record identity; supply one current snapshot per record")
            seen.add(identity)
            annotation = classify_record(record, vocabulary=vocabulary)
            text = " ".join((record.id, record.title, record.body, record.repository,
                             *record.labels, *record.files)).casefold()
            index = len(self._entries)
            self._entries.append(_Entry(record, annotation, text))
            self._projects.setdefault(record.project_id, set()).add(index)
            values = [(tag["facet"], tag["value"]) for tag in annotation["tags"]]
            values += [(f, annotation["status"][f]) for f in STATUS_VALUES]
            # A draft is still open; "open not draft" excludes drafts.
            if annotation["status"]["state"] == "draft":
                values.append(("state", "open"))
            values += [("kind", record.kind), ("repo", record.repository.casefold()),
                       ("author", record.author.casefold())]
            if record.number is not None:
                values.append(("number", str(record.number)))
            values += [("label", label.casefold()) for label in record.labels]
            values += [("tag", tag["value"]) for tag in annotation["tags"]]
            for field, value in values:
                self._facets.setdefault((field, value), set()).add(index)

    def _matching(self, predicate: Predicate, candidates: set[int]) -> set[int]:
        field, value = predicate.field, predicate.value
        unknown: set[int] = set()
        if field == "text":
            found = {i for i in candidates if value in self._entries[i].text}
        elif field == "sha":
            found = {i for i in candidates if self._entries[i].record.source_sha.startswith(value)}
        elif field == "path":
            found = {i for i in candidates if any(
                fnmatchcase(path.replace("\\", "/").casefold(), value)
                if "*" in value or "?" in value else path.replace("\\", "/").casefold().startswith(value)
                for path in self._entries[i].record.files)}
        elif field in ("after", "before"):
            found = set()
            for i in candidates:
                updated = timestamp(self._entries[i].record.updated_at)
                if updated is None:
                    unknown.add(i)
                else:
                    day = updated.astimezone(timezone.utc).date().isoformat()
                    if (day >= value if field == "after" else day < value):
                        found.add(i)
        else:
            found = candidates & self._facets.get((field, value), set())
            if field in STATUS_VALUES and value != "unknown":
                unknown = self._facets.get((field, "unknown"), set())
        return candidates - found - unknown if predicate.exclude else found

    def search(self, query: str | ProjectQuery, *, project_id: str,
               limit: int = 20, offset: int = 0) -> dict:
        if type(project_id) is not str or not project_id:
            raise ProjectError("an explicit project_id scope is required")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ProjectError("limit must be 1..100")
        if type(offset) is not int or offset < 0:
            raise ProjectError("offset must be a nonnegative integer")
        plan = compile_query(query, vocabulary=self.vocabulary) if isinstance(query, str) else query
        if not isinstance(plan, ProjectQuery) or not plan.valid:
            raise ProjectError("query has diagnostics; resolve them before searching")
        if plan.vocabulary_hash != self.vocabulary.content_hash:
            raise ProjectError("query and index vocabulary hashes differ")
        scope = self._projects.get(project_id, set())
        matches = set()
        for clause in plan.clauses:
            candidates = set(scope)
            # Apply indexed predicates first, preserving equivalent AND semantics.
            for predicate in sorted(clause, key=lambda p: p.field in ("text", "path", "sha", "after", "before")):
                candidates = self._matching(predicate, candidates)
                if not candidates:
                    break
            matches |= candidates

        def sort_key(index: int) -> tuple:
            record = self._entries[index].record
            if plan.sort == "title":
                return record.title.casefold(), record.id
            if plan.sort == "number":
                return record.number is None, -(record.number or 0), record.id
            updated = timestamp(record.updated_at)
            return updated is None, -(updated.timestamp() if updated else 0), record.id

        ordered = sorted(matches, key=sort_key)
        groups: dict[str, int] = {}
        if plan.group_by:
            for index in matches:
                entry = self._entries[index]
                field = plan.group_by
                if field in STATUS_VALUES:
                    values = [entry.annotation["status"][field]]
                elif field in ("repo", "author", "kind"):
                    values = [getattr(entry.record, "repository" if field == "repo" else field) or "unknown"]
                else:
                    values = [tag["value"] for tag in entry.annotation["tags"] if tag["facet"] == field] or ["unknown"]
                for value in set(values):
                    groups[value] = groups.get(value, 0) + 1
        page = ordered[offset:offset + limit]
        return {
            "nanoProjectVersion": VERSION, "projectId": project_id,
            "queryHash": plan.content_hash, "query": plan.to_dict(),
            "total": len(ordered), "offset": offset, "limit": limit,
            "nextOffset": offset + limit if offset + limit < len(ordered) else None,
            "groups": [{"value": value, "count": count} for value, count in sorted(groups.items())],
            "items": [deepcopy(self._entries[i].annotation) for i in page],
        }
