"""Versioned deterministic catalog data and lexical lookup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

MAX_CATALOGS = 16
MAX_ALIASES_PER_CATALOG = 2048


class CatalogError(ValueError):
    """Catalog identities, aliases, or precedence cannot be resolved safely."""


@dataclass(frozen=True)
class Catalog:
    """Pure data describing vocabulary owned by one domain."""

    identity: str
    version: str
    precedence: int
    aliases: Tuple[Tuple[str, str, str], ...]
    capabilities: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.identity or not self.version:
            raise CatalogError("catalog identity and version must be non-empty")
        if len(self.aliases) > MAX_ALIASES_PER_CATALOG:
            raise CatalogError(
                f"catalog {self.identity!r} exceeds {MAX_ALIASES_PER_CATALOG} aliases"
            )
        seen: set[tuple[str, str]] = set()
        for alias, role, canonical in self.aliases:
            if not alias or alias != alias.casefold():
                raise CatalogError(
                    f"catalog alias {alias!r} must be non-empty and case-folded"
                )
            key = (alias, role)
            if key in seen:
                raise CatalogError(
                    f"catalog {self.identity!r} repeats alias {alias!r} for {role}"
                )
            seen.add(key)

    def capability(self, name: str) -> str | None:
        return dict(self.capabilities).get(name)


@dataclass(frozen=True)
class LexicalMatch:
    alias: str
    role: str
    canonical: str
    catalog: str
    version: str
    precedence: int


@dataclass(frozen=True)
class CatalogBundle:
    catalogs: Tuple[Catalog, ...]

    def __post_init__(self) -> None:
        if not self.catalogs:
            raise CatalogError("at least one intent catalog is required")
        if len(self.catalogs) > MAX_CATALOGS:
            raise CatalogError(f"intent compilation supports at most {MAX_CATALOGS} catalogs")
        identities = [catalog.identity for catalog in self.catalogs]
        if len(set(identities)) != len(identities):
            raise CatalogError("catalog identities must be unique")

        ownership: dict[tuple[str, str], LexicalMatch] = {}
        for catalog in self.ordered():
            for alias, role, canonical in catalog.aliases:
                key = (alias, role)
                prior = ownership.get(key)
                if prior is not None and prior.precedence == catalog.precedence:
                    raise CatalogError(
                        f"alias collision for {alias!r}/{role}: "
                        f"{prior.catalog} and {catalog.identity} have equal precedence"
                    )
                ownership.setdefault(
                    key,
                    LexicalMatch(
                        alias,
                        role,
                        canonical,
                        catalog.identity,
                        catalog.version,
                        catalog.precedence,
                    ),
                )

    def ordered(self) -> Tuple[Catalog, ...]:
        return tuple(sorted(self.catalogs, key=lambda item: (-item.precedence, item.identity)))

    def lookup(self, alias: str, role: str | None = None) -> Tuple[LexicalMatch, ...]:
        found: list[LexicalMatch] = []
        claimed: set[str] = set()
        for catalog in self.ordered():
            for candidate, candidate_role, canonical in catalog.aliases:
                if candidate != alias or (role is not None and candidate_role != role):
                    continue
                if candidate_role in claimed:
                    continue
                claimed.add(candidate_role)
                found.append(
                    LexicalMatch(
                        candidate,
                        candidate_role,
                        canonical,
                        catalog.identity,
                        catalog.version,
                        catalog.precedence,
                    )
                )
        return tuple(found)

    def vocabulary(self) -> Tuple[str, ...]:
        return tuple(sorted({alias for catalog in self.catalogs for alias, _, _ in catalog.aliases}))

    def catalog_receipt(self) -> list[dict[str, object]]:
        return [
            {
                "id": catalog.identity,
                "version": catalog.version,
                "precedence": catalog.precedence,
            }
            for catalog in self.ordered()
        ]

    def capability(self, name: str) -> str | None:
        for catalog in self.ordered():
            value = catalog.capability(name)
            if value is not None:
                return value
        return None


def default_catalogs() -> CatalogBundle:
    from .catalogs.core import CORE_CATALOG
    from .catalogs.market import MARKET_CATALOG

    return CatalogBundle((CORE_CATALOG, MARKET_CATALOG))


def edit_distance(left: str, right: str, *, limit: int = 2) -> int:
    """Bounded Levenshtein distance with deterministic early exits."""

    if left == right:
        return 0
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for row, lchar in enumerate(left, 1):
        current = [row]
        row_minimum = row
        for column, rchar in enumerate(right, 1):
            value = min(
                current[column - 1] + 1,
                previous[column] + 1,
                previous[column - 1] + (lchar != rchar),
            )
            current.append(value)
            row_minimum = min(row_minimum, value)
        if row_minimum > limit:
            return limit + 1
        previous = current
    return previous[-1]


def suggestions(word: str, bundle: CatalogBundle, *, limit: int = 2) -> Tuple[str, ...]:
    """Rank ordinary-vocabulary suggestions without applying them."""

    if len(word) < 3 or len(word) > 24:
        return ()
    ranked = [
        (distance, candidate)
        for candidate in bundle.vocabulary()
        if " " not in candidate
        for distance in [edit_distance(word, candidate, limit=limit)]
        if 0 < distance <= limit
    ]
    return tuple(candidate for _, candidate in sorted(ranked)[:5])


__all__ = [
    "Catalog",
    "CatalogBundle",
    "CatalogError",
    "LexicalMatch",
    "MAX_ALIASES_PER_CATALOG",
    "MAX_CATALOGS",
    "default_catalogs",
    "edit_distance",
    "suggestions",
]
