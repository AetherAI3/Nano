"""Small, versioned vocabulary shared by project tagging and search."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

VERSION = "1.0.0"
SEMANTIC_FIELDS = ("type", "area", "tech", "relation")
STATUS_VALUES = {
    "state": ("open", "closed", "merged", "draft", "unknown"),
    "review": ("pending", "approved", "changes_requested", "unknown"),
    "ci": ("passed", "failed", "running", "queued", "cancelled", "none", "unknown"),
}
KINDS = ("pr", "commit", "memory")
FIELDS = (*SEMANTIC_FIELDS, *STATUS_VALUES, "kind", "repo", "author", "label", "tag",
          "number", "sha", "path", "text", "after", "before")
GROUP_FIELDS = (*SEMANTIC_FIELDS, *STATUS_VALUES, "kind", "repo", "author")
SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class ProjectError(ValueError):
    """Invalid record, vocabulary, or query contract."""


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


def _entries(field: str, value: str, *words: str) -> tuple:
    return tuple((word, field, value) for word in words)


@dataclass(frozen=True)
class Vocabulary:
    """Pure aliases, never callbacks or learned mutable state.

    Extend aliases with project-specific phrases. New semantic values are
    allowed; operational states remain a closed, host-observed vocabulary.
    """

    aliases: tuple[tuple[str, str, str], ...]
    version: str = VERSION

    def __post_init__(self) -> None:
        if type(self.aliases) is not tuple or not 1 <= len(self.aliases) <= 512:
            raise ProjectError("vocabulary requires 1..512 immutable aliases")
        if type(self.version) is not str or not self.version:
            raise ProjectError("vocabulary version is required")
        seen = set()
        for item in self.aliases:
            if type(item) is not tuple or len(item) != 3:
                raise ProjectError("alias must be (phrase, field, value)")
            phrase, field, value = item
            if not all(type(x) is str for x in item):
                raise ProjectError("alias entries must be strings")
            if not phrase or len(phrase) > 100 or phrase != " ".join(phrase.casefold().split()):
                raise ProjectError("aliases must be normalized lowercase phrases")
            if field not in (*SEMANTIC_FIELDS, *STATUS_VALUES, "kind"):
                raise ProjectError("aliases may name semantic, status, or kind fields")
            if not SLUG.fullmatch(value):
                raise ProjectError("alias value must be a slug")
            allowed = STATUS_VALUES.get(field, KINDS if field == "kind" else None)
            if allowed and value not in allowed:
                raise ProjectError("invalid operational alias value")
            if phrase in seen:
                raise ProjectError(f"alias collision: {phrase}")
            seen.add(phrase)

    @property
    def content_hash(self) -> str:
        return digest({"version": self.version, "aliases": sorted(self.aliases)})

    def lookup(self, phrase: str) -> tuple[str, str] | None:
        phrase = " ".join(phrase.casefold().split())
        return next(((f, v) for p, f, v in self.aliases if p == phrase), None)


DEFAULT_VOCABULARY = Vocabulary((
    *_entries("type", "feature", "feature", "features", "feat"),
    *_entries("type", "fix", "fix", "fixes", "bug", "bugs", "bugfix"),
    *_entries("type", "refactor", "refactor", "refactoring"),
    *_entries("type", "docs", "docs", "documentation"),
    *_entries("type", "test", "test", "tests", "testing"),
    *_entries("type", "chore", "chore", "chores"),
    *_entries("area", "frontend", "frontend", "front end", "ui", "ux"),
    *_entries("area", "backend", "backend", "back end", "server"),
    *_entries("area", "api", "api", "apis"),
    *_entries("area", "infra", "infra", "infrastructure", "devops"),
    *_entries("area", "security", "security", "auth", "authentication"),
    *_entries("area", "memory", "memory", "memory graph"),
    *_entries("area", "agent", "agent", "agents"),
    *_entries("area", "cli", "cli", "command line"),
    *_entries("tech", "css", "css", "styles", "styling", "stylesheet", "stylesheets"),
    *_entries("tech", "react", "react", "jsx", "tsx"),
    *_entries("tech", "typescript", "typescript", "ts"),
    *_entries("tech", "javascript", "javascript", "js"),
    *_entries("tech", "python", "python", "py"),
    *_entries("tech", "sql", "sql"),
    *_entries("relation", "downstream", "downstream"),
    *_entries("relation", "upstream", "upstream"),
    *_entries("state", "open", "open"),
    *_entries("state", "closed", "closed"),
    *_entries("state", "merged", "merged", "landed"),
    *_entries("state", "draft", "draft", "drafts"),
    *_entries("review", "pending", "pending review", "waiting for review", "needs review",
              "awaiting review", "unreviewed"),
    *_entries("review", "approved", "approved"),
    *_entries("review", "changes_requested", "changes requested", "needs changes"),
    *_entries("ci", "failed", "failed ci", "ci failed", "failing ci", "failed checks",
              "failing checks", "red ci"),
    *_entries("ci", "passed", "passed ci", "ci passed", "passing ci", "passing checks",
              "green ci"),
    *_entries("ci", "running", "ci running", "running ci"),
    *_entries("ci", "queued", "ci queued", "queued ci"),
    *_entries("kind", "pr", "pr", "prs", "pull request", "pull requests"),
    *_entries("kind", "commit", "commit", "commits"),
    *_entries("kind", "memory", "memories", "memory records"),
))
