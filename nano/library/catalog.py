"""Deterministic metadata catalog for the packaged strategy library.

The canonical inputs are each strategy's leading ``//`` header and pinned IR
partner.  The JSON catalog is a generated projection for the CLI, documentation,
and hosted consumers; contributors never maintain a second metadata record.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from nano.ir.schema import (
    NANO_IR_VERSION_1_0,
    NANO_IR_VERSION_BASELINE,
    SUPPORTED_IR_VERSIONS,
)
from nano.library.contribution import source_provenance_issues


CATALOG_SCHEMA_VERSION = 1
METADATA_VERSION = "StrategyMetadataV1"
CATALOG_PARTS = ("catalog", "strategy_metadata_v1.json")

_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_CONFUSED_SLUG = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
_FIELD_RE = re.compile(
    r"(?m)^(REGIME|CONDITIONS|INVALIDATION|SHAPE|CALIBRATED ON|SOURCE):[ \t]*"
)
_CONFUSED_RE = re.compile(
    rf"(?m)(?:^|(?<=[.;] ))NOT[ \t]+({_CONFUSED_SLUG}):[ \t]*"
)
_REQUIRED_FIELDS = (
    "REGIME",
    "CONDITIONS",
    "INVALIDATION",
    "SHAPE",
    "CALIBRATED ON",
)
_CATALOG_KEYS = frozenset(
    {
        "type",
        "schemaVersion",
        "metadataVersion",
        "strategyCount",
        "categoryCounts",
        "irMaturityCounts",
        "strategies",
    }
)
_STRATEGY_KEYS = frozenset(
    {
        "metadataVersion",
        "id",
        "slug",
        "name",
        "category",
        "irMaturity",
        "irVersion",
        "regime",
        "conditions",
        "invalidation",
        "shape",
        "calibratedOn",
        "nearestConfused",
        "provenance",
        "requiredHostSignals",
        "sourcePath",
        "irPath",
    }
)
_CONFUSED_KEYS = frozenset({"slug", "id", "distinction"})


@dataclass(frozen=True)
class CatalogDiagnostic:
    """One catalogability failure with a source-shaped location."""

    path: str
    message: str
    line: Optional[int] = None

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{location}: {self.message}"


class CatalogValidationError(ValueError):
    """Raised when source/IR cannot produce trustworthy catalog metadata."""

    def __init__(self, diagnostics: Sequence[CatalogDiagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__("\n".join(item.render() for item in diagnostics))


@dataclass(frozen=True)
class ConfusedStrategyV1:
    """A nearby strategy and the distinction recorded by the source header."""

    slug: str
    distinction: str


@dataclass(frozen=True)
class StrategyMetadataV1:
    """The complete metadata projection for one strategy source/IR pair."""

    id: str
    slug: str
    name: str
    category: str
    ir_maturity: str
    ir_version: str
    regime: str
    conditions: str
    invalidation: str
    shape: str
    calibrated_on: str
    nearest_confused: tuple[ConfusedStrategyV1, ...]
    provenance: Optional[str]
    required_host_signals: tuple[str, ...]
    source_path: str
    ir_path: str

    def to_dict(self, slug_ids: Mapping[str, str]) -> dict[str, Any]:
        return {
            "metadataVersion": METADATA_VERSION,
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "category": self.category,
            "irMaturity": self.ir_maturity,
            "irVersion": self.ir_version,
            "regime": self.regime,
            "conditions": self.conditions,
            "invalidation": self.invalidation,
            "shape": self.shape,
            "calibratedOn": self.calibrated_on,
            "nearestConfused": [
                {
                    "slug": item.slug,
                    "id": slug_ids.get(item.slug),
                    "distinction": item.distinction,
                }
                for item in self.nearest_confused
            ],
            "provenance": self.provenance,
            "requiredHostSignals": list(self.required_host_signals),
            "sourcePath": self.source_path,
            "irPath": self.ir_path,
        }


@dataclass(frozen=True)
class _Marker:
    kind: str
    target: Optional[str]
    start: int
    end: int
    line: int


def library_resource_root() -> Any:
    """Return the package resource containing source, IR, and generated catalog."""

    return resources.files("nano.library")


def default_library_path() -> Path:
    """Return the filesystem library root used by repository generators."""

    return Path(__file__).resolve().parent


def catalog_resource(library_root: Optional[Any] = None) -> Any:
    resource = library_root or library_resource_root()
    return resource.joinpath(*CATALOG_PARTS)


def catalog_path(library_root: Optional[Path] = None) -> Path:
    return (library_root or default_library_path()).joinpath(*CATALOG_PARTS)


def _leading_header(source: str) -> tuple[str, list[int], list[str]]:
    """Return header text, per-character source lines, and original comment lines."""

    pieces: list[str] = []
    line_map: list[int] = []
    header_lines: list[str] = []
    saw_comment = False
    for number, raw_line in enumerate(source.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            if saw_comment:
                pieces.append("\n")
                line_map.append(number)
            continue
        if not stripped.startswith("//"):
            break
        saw_comment = True
        header_lines.append(stripped)
        # Metadata fields use the contribution header's exact ``// FIELD:``
        # spelling. A comment like ``//SOURCE:`` remains ordinary prose and
        # cannot bypass contribution.py's optional provenance policy.
        text = stripped[3:] if stripped.startswith("// ") else stripped
        pieces.append(text)
        line_map.extend([number] * len(text))
        pieces.append("\n")
        line_map.append(number)
    return "".join(pieces), line_map, header_lines


def _normalise(value: str) -> str:
    return " ".join(value.split())


def _line_at(line_map: Sequence[int], offset: int) -> int:
    if not line_map:
        return 1
    return line_map[min(offset, len(line_map) - 1)]


def _parse_header(
    source: str, *, source_path: str, slug: str
) -> tuple[dict[str, str], tuple[ConfusedStrategyV1, ...]]:
    blob, line_map, header_lines = _leading_header(source)
    if not blob:
        raise CatalogValidationError(
            (
                CatalogDiagnostic(
                    source_path,
                    "no leading `//` metadata header; expected REGIME, CONDITIONS, "
                    "INVALIDATION, SHAPE, CALIBRATED ON, and NOT <slug> fields",
                    1,
                ),
            )
        )

    markers: list[_Marker] = []
    for match in _FIELD_RE.finditer(blob):
        markers.append(
            _Marker(
                match.group(1),
                None,
                match.start(),
                match.end(),
                _line_at(line_map, match.start()),
            )
        )
    for match in _CONFUSED_RE.finditer(blob):
        markers.append(
            _Marker(
                "NOT",
                match.group(1),
                match.start(),
                match.end(),
                _line_at(line_map, match.start()),
            )
        )
    markers.sort(key=lambda item: item.start)

    diagnostics = [
        CatalogDiagnostic(source_path, issue)
        for issue in source_provenance_issues(header_lines)
    ]
    fields: dict[str, str] = {}
    confused: list[ConfusedStrategyV1] = []
    confused_occurrences: Counter[str] = Counter()
    occurrences: Counter[str] = Counter()

    for index, marker in enumerate(markers):
        end = markers[index + 1].start if index + 1 < len(markers) else len(blob)
        value = _normalise(blob[marker.end:end])
        if marker.kind == "NOT":
            assert marker.target is not None
            confused_occurrences[marker.target] += 1
            if confused_occurrences[marker.target] > 1:
                diagnostics.append(
                    CatalogDiagnostic(
                        source_path,
                        f"duplicate `NOT {marker.target}:` nearest-confused field",
                        marker.line,
                    )
                )
            if marker.target == slug:
                diagnostics.append(
                    CatalogDiagnostic(
                        source_path,
                        f"`NOT {marker.target}:` points back to the same strategy",
                        marker.line,
                    )
                )
            if not value:
                diagnostics.append(
                    CatalogDiagnostic(
                        source_path,
                        f"`NOT {marker.target}:` needs a concrete distinction",
                        marker.line,
                    )
                )
            confused.append(ConfusedStrategyV1(marker.target, value))
            continue

        occurrences[marker.kind] += 1
        if occurrences[marker.kind] > 1:
            # contribution.py owns the optional SOURCE policy so its wording
            # remains one precise contract instead of two competing errors.
            if marker.kind != "SOURCE":
                diagnostics.append(
                    CatalogDiagnostic(
                        source_path,
                        f"duplicate `{marker.kind}:` metadata field",
                        marker.line,
                    )
                )
            continue
        if not value:
            if marker.kind != "SOURCE":
                diagnostics.append(
                    CatalogDiagnostic(
                        source_path,
                        f"`{marker.kind}:` metadata field is empty",
                        marker.line,
                    )
                )
        fields[marker.kind] = value

    for field in _REQUIRED_FIELDS:
        if occurrences[field] == 0:
            diagnostics.append(
                CatalogDiagnostic(
                    source_path,
                    f"missing `// {field}:` metadata field",
                    1,
                )
            )
    if not confused:
        diagnostics.append(
            CatalogDiagnostic(
                source_path,
                "missing `NOT <strategy_slug>:` nearest-confused strategy field",
                1,
            )
        )

    if diagnostics:
        raise CatalogValidationError(diagnostics)
    return fields, tuple(confused)


def _required_host_signals(
    document: Mapping[str, Any], *, source_path: str
) -> tuple[str, ...]:
    version = document.get("nanoIrVersion")
    if version == NANO_IR_VERSION_BASELINE:
        values = [
            node.get("signal")
            for node in document.get("nodes", [])
            if isinstance(node, Mapping) and node.get("type") == "Condition"
        ]
    elif version == NANO_IR_VERSION_1_0:
        values = [
            declaration.get("name")
            for declaration in document.get("inputs", [])
            if isinstance(declaration, Mapping)
        ]
    else:
        raise CatalogValidationError(
            (
                CatalogDiagnostic(
                    source_path,
                    f"unsupported nanoIrVersion {version!r}; expected one of "
                    f"{SUPPORTED_IR_VERSIONS!r}",
                ),
            )
        )

    if any(not isinstance(value, str) or not value for value in values):
        raise CatalogValidationError(
            (
                CatalogDiagnostic(
                    source_path,
                    "IR contains a host input or condition without a non-empty name",
                ),
            )
        )
    return tuple(sorted(set(values)))


def parse_strategy_metadata(
    source: str,
    document: Mapping[str, Any],
    *,
    category: str,
    slug: str,
    source_path: Optional[str] = None,
) -> StrategyMetadataV1:
    """Parse one canonical source header and enrich it from its pinned IR."""

    display_path = source_path or f"library/{category}/{slug}.nano"
    diagnostics: list[CatalogDiagnostic] = []
    for label, value in (("category", category), ("slug", slug)):
        if not _SLUG_RE.fullmatch(value):
            diagnostics.append(
                CatalogDiagnostic(
                    display_path,
                    f"{label} {value!r} is not a stable lowercase snake_case identifier",
                )
            )
    name = document.get("name")
    if not isinstance(name, str) or not name:
        diagnostics.append(
            CatalogDiagnostic(display_path, "IR is missing a non-empty strategy `name`")
        )
    if diagnostics:
        raise CatalogValidationError(diagnostics)

    fields, confused = _parse_header(source, source_path=display_path, slug=slug)
    version = document.get("nanoIrVersion")
    required_host_signals = _required_host_signals(
        document, source_path=display_path
    )
    maturity = {
        NANO_IR_VERSION_BASELINE: "baseline",
        NANO_IR_VERSION_1_0: "v1",
    }[version]

    strategy_id = f"{category}/{slug}"
    return StrategyMetadataV1(
        id=strategy_id,
        slug=slug,
        name=name,
        category=category,
        ir_maturity=maturity,
        ir_version=version,
        regime=fields["REGIME"],
        conditions=fields["CONDITIONS"],
        invalidation=fields["INVALIDATION"],
        shape=fields["SHAPE"],
        calibrated_on=fields["CALIBRATED ON"],
        nearest_confused=confused,
        provenance=fields.get("SOURCE"),
        required_host_signals=required_host_signals,
        source_path=f"library/{strategy_id}.nano",
        ir_path=f"library/{strategy_id}_ir.json",
    )


def _walk_sources(root: Any) -> list[tuple[tuple[str, ...], Any]]:
    found: list[tuple[tuple[str, ...], Any]] = []

    def walk(directory: Any, prefix: tuple[str, ...]) -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = prefix + (child.name,)
            if child.is_dir():
                walk(child, relative)
            elif child.is_file() and child.name.endswith(".nano"):
                found.append((relative, child))

    walk(root, ())
    return found


def _load_entry(relative: tuple[str, ...], nano_resource: Any) -> StrategyMetadataV1:
    display_path = f"library/{'/'.join(relative)}"
    if len(relative) != 2:
        raise CatalogValidationError(
            (
                CatalogDiagnostic(
                    display_path,
                    "strategy must live exactly at library/<category>/<slug>.nano",
                ),
            )
        )
    category, filename = relative
    slug = filename[: -len(".nano")]
    partner = nano_resource.parent.joinpath(f"{slug}_ir.json")
    if not partner.is_file():
        raise CatalogValidationError(
            (
                CatalogDiagnostic(
                    display_path,
                    f"missing pinned IR partner `{slug}_ir.json`",
                ),
            )
        )
    try:
        source = nano_resource.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise CatalogValidationError(
            (CatalogDiagnostic(display_path, f"cannot read UTF-8 source: {error}"),)
        ) from error
    ir_display = f"library/{category}/{slug}_ir.json"
    try:
        document = json.loads(partner.read_text(encoding="utf-8"))
    except OSError as error:
        raise CatalogValidationError(
            (CatalogDiagnostic(ir_display, f"cannot read pinned IR: {error}"),)
        ) from error
    except json.JSONDecodeError as error:
        raise CatalogValidationError(
            (
                CatalogDiagnostic(
                    ir_display,
                    f"pinned IR is not valid JSON: {error.msg}",
                    error.lineno,
                ),
            )
        ) from error
    if not isinstance(document, Mapping):
        raise CatalogValidationError(
            (CatalogDiagnostic(ir_display, "pinned IR must be a JSON object"),)
        )
    return parse_strategy_metadata(
        source,
        document,
        category=category,
        slug=slug,
        source_path=display_path,
    )


def build_catalog(library_root: Optional[Any] = None) -> dict[str, Any]:
    """Build the complete catalog document in stable ID order."""

    root = library_root or library_resource_root()
    metadata: list[StrategyMetadataV1] = []
    diagnostics: list[CatalogDiagnostic] = []
    for relative, resource in _walk_sources(root):
        try:
            metadata.append(_load_entry(relative, resource))
        except CatalogValidationError as error:
            diagnostics.extend(error.diagnostics)

    ids = [item.id for item in metadata]
    slugs = [item.slug for item in metadata]
    for label, values in (("stable ID", ids), ("slug", slugs)):
        duplicates = sorted(
            value for value, count in Counter(values).items() if count > 1
        )
        for duplicate in duplicates:
            diagnostics.append(
                CatalogDiagnostic(
                    "library",
                    f"duplicate {label} {duplicate!r}; lookup would be ambiguous",
                )
            )
    if not metadata and not diagnostics:
        diagnostics.append(CatalogDiagnostic("library", "no strategy sources found"))
    if diagnostics:
        raise CatalogValidationError(diagnostics)

    metadata.sort(key=lambda item: item.id)
    slug_ids = {item.slug: item.id for item in metadata}
    category_counts = Counter(item.category for item in metadata)
    maturity_counts = Counter(item.ir_maturity for item in metadata)
    return {
        "type": "NanoStrategyCatalog",
        "schemaVersion": CATALOG_SCHEMA_VERSION,
        "metadataVersion": METADATA_VERSION,
        "strategyCount": len(metadata),
        "categoryCounts": {
            category: category_counts[category] for category in sorted(category_counts)
        },
        "irMaturityCounts": {
            maturity: maturity_counts[maturity]
            for maturity in ("baseline", "v1")
            if maturity_counts[maturity]
        },
        "strategies": [item.to_dict(slug_ids) for item in metadata],
    }


def render_catalog(document: Mapping[str, Any]) -> str:
    """Render a catalog with one canonical byte representation."""

    # The separator after the root opener is part of the byte contract.  It
    # changes the blob when the LF-only Git attribute first lands, forcing old
    # Windows checkouts to replace an already-materialized CRLF copy.
    rendered = json.dumps(document, ensure_ascii=False, indent=2)
    return rendered.replace("{\n", "{\n\n", 1) + "\n"


def generate_catalog_text(library_root: Optional[Any] = None) -> str:
    return render_catalog(build_catalog(library_root))


def write_catalog(library_root: Optional[Path] = None) -> Path:
    """Regenerate the checked-in artifact from source/IR and return its path."""

    root = library_root or default_library_path()
    output = catalog_path(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(generate_catalog_text(root), encoding="utf-8", newline="\n")
    return output


def _document_diagnostics(
    document: Any, *, artifact_path: str
) -> tuple[CatalogDiagnostic, ...]:
    diagnostics: list[CatalogDiagnostic] = []
    if not isinstance(document, Mapping):
        return (CatalogDiagnostic(artifact_path, "catalog must be a JSON object"),)
    if set(document) != _CATALOG_KEYS:
        missing = sorted(_CATALOG_KEYS - set(document))
        unknown = sorted(set(document) - _CATALOG_KEYS)
        diagnostics.append(
            CatalogDiagnostic(
                artifact_path,
                f"catalog keys must match StrategyMetadataV1 exactly; "
                f"missing={missing!r}, unknown={unknown!r}",
            )
        )
    if document.get("type") != "NanoStrategyCatalog":
        diagnostics.append(
            CatalogDiagnostic(artifact_path, "catalog `type` must be NanoStrategyCatalog")
        )
    if (
        type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != CATALOG_SCHEMA_VERSION
    ):
        diagnostics.append(
            CatalogDiagnostic(
                artifact_path,
                f"unsupported schemaVersion {document.get('schemaVersion')!r}",
            )
        )
    if document.get("metadataVersion") != METADATA_VERSION:
        diagnostics.append(
            CatalogDiagnostic(
                artifact_path,
                f"unsupported metadataVersion {document.get('metadataVersion')!r}",
            )
        )
    strategies = document.get("strategies")
    if not isinstance(strategies, list):
        diagnostics.append(
            CatalogDiagnostic(artifact_path, "`strategies` must be an array")
        )
        return tuple(diagnostics)
    if (
        type(document.get("strategyCount")) is not int
        or document.get("strategyCount") != len(strategies)
    ):
        diagnostics.append(
            CatalogDiagnostic(
                artifact_path,
                "strategyCount does not match the strategies array",
            )
        )
    ids: list[str] = []
    slugs: list[str] = []
    categories: list[str] = []
    maturities: list[str] = []
    required_strings = (
        "id",
        "slug",
        "name",
        "category",
        "irMaturity",
        "irVersion",
        "regime",
        "conditions",
        "invalidation",
        "shape",
        "calibratedOn",
        "sourcePath",
        "irPath",
    )
    for index, entry in enumerate(strategies):
        location = f"{artifact_path}#strategies[{index}]"
        if not isinstance(entry, Mapping):
            diagnostics.append(CatalogDiagnostic(location, "entry must be an object"))
            continue
        if set(entry) != _STRATEGY_KEYS:
            missing = sorted(_STRATEGY_KEYS - set(entry))
            unknown = sorted(set(entry) - _STRATEGY_KEYS)
            diagnostics.append(
                CatalogDiagnostic(
                    location,
                    f"strategy keys must match StrategyMetadataV1 exactly; "
                    f"missing={missing!r}, unknown={unknown!r}",
                )
            )
        if entry.get("metadataVersion") != METADATA_VERSION:
            diagnostics.append(
                CatalogDiagnostic(location, "entry has an unsupported metadataVersion")
            )
        for field in required_strings:
            if not isinstance(entry.get(field), str) or not entry.get(field):
                diagnostics.append(
                    CatalogDiagnostic(location, f"`{field}` must be a non-empty string")
                )
        strategy_id = entry.get("id")
        slug = entry.get("slug")
        category = entry.get("category")
        maturity = entry.get("irMaturity")
        version = entry.get("irVersion")
        if isinstance(strategy_id, str):
            ids.append(strategy_id)
        if isinstance(slug, str):
            slugs.append(slug)
        if isinstance(category, str):
            categories.append(category)
        if isinstance(maturity, str):
            maturities.append(maturity)
        if all(isinstance(value, str) for value in (strategy_id, slug, category)):
            if not _SLUG_RE.fullmatch(slug) or not _SLUG_RE.fullmatch(category):
                diagnostics.append(
                    CatalogDiagnostic(
                        location,
                        "category and slug must be stable lowercase snake_case identifiers",
                    )
                )
            expected_id = f"{category}/{slug}"
            if strategy_id != expected_id:
                diagnostics.append(
                    CatalogDiagnostic(location, f"stable ID must be {expected_id!r}")
                )
            if entry.get("sourcePath") != f"library/{expected_id}.nano":
                diagnostics.append(
                    CatalogDiagnostic(location, "sourcePath does not match the stable ID")
                )
            if entry.get("irPath") != f"library/{expected_id}_ir.json":
                diagnostics.append(
                    CatalogDiagnostic(location, "irPath does not match the stable ID")
                )
        expected_maturity = {
            NANO_IR_VERSION_BASELINE: "baseline",
            NANO_IR_VERSION_1_0: "v1",
        }.get(version)
        if expected_maturity is None or maturity != expected_maturity:
            diagnostics.append(
                CatalogDiagnostic(location, "IR maturity and version do not agree")
            )
        signals = entry.get("requiredHostSignals")
        if (
            not isinstance(signals, list)
            or any(not isinstance(value, str) or not value for value in signals)
            or signals != sorted(set(signals))
        ):
            diagnostics.append(
                CatalogDiagnostic(
                    location,
                    "requiredHostSignals must be unique non-empty strings in stable order",
                )
            )
        confused = entry.get("nearestConfused")
        if not isinstance(confused, list) or not confused:
            diagnostics.append(
                CatalogDiagnostic(location, "nearestConfused must be a non-empty array")
            )
        else:
            confused_slugs: list[str] = []
            for item in confused:
                if not isinstance(item, Mapping):
                    diagnostics.append(
                        CatalogDiagnostic(
                            location, "nearestConfused contains a malformed entry"
                        )
                    )
                    continue
                if set(item) != _CONFUSED_KEYS:
                    missing = sorted(_CONFUSED_KEYS - set(item))
                    unknown = sorted(set(item) - _CONFUSED_KEYS)
                    diagnostics.append(
                        CatalogDiagnostic(
                            location,
                            f"nearestConfused keys must match StrategyMetadataV1 "
                            f"exactly; missing={missing!r}, unknown={unknown!r}",
                        )
                    )
                confused_slug = item.get("slug")
                if isinstance(confused_slug, str):
                    confused_slugs.append(confused_slug)
                if (
                    not isinstance(confused_slug, str)
                    or not confused_slug
                    or _SLUG_RE.fullmatch(confused_slug) is None
                    or not isinstance(item.get("distinction"), str)
                    or not item.get("distinction")
                    or (
                        item.get("id") is not None
                        and not isinstance(item.get("id"), str)
                    )
                ):
                    diagnostics.append(
                        CatalogDiagnostic(
                            location, "nearestConfused contains a malformed entry"
                        )
                    )
            if len(confused_slugs) != len(set(confused_slugs)):
                diagnostics.append(
                    CatalogDiagnostic(
                        location, "nearestConfused slugs must be unique"
                    )
                )
        provenance = entry.get("provenance")
        if provenance is not None and (
            not isinstance(provenance, str) or not provenance
        ):
            diagnostics.append(
                CatalogDiagnostic(location, "provenance must be null or a non-empty string")
            )

    if ids != sorted(ids) or len(ids) != len(strategies):
        diagnostics.append(
            CatalogDiagnostic(
                artifact_path,
                "strategy entries are malformed or not in stable ID order",
            )
        )
    if len(ids) != len(set(ids)) or len(slugs) != len(set(slugs)):
        diagnostics.append(
            CatalogDiagnostic(artifact_path, "strategy IDs and slugs must be unique")
        )
    slug_ids = {
        entry.get("slug"): entry.get("id")
        for entry in strategies
        if isinstance(entry, Mapping)
        and isinstance(entry.get("slug"), str)
        and isinstance(entry.get("id"), str)
    }
    for index, entry in enumerate(strategies):
        if not isinstance(entry, Mapping) or not isinstance(
            entry.get("nearestConfused"), list
        ):
            continue
        for item in entry["nearestConfused"]:
            if not isinstance(item, Mapping) or not isinstance(item.get("slug"), str):
                continue
            if item.get("id") != slug_ids.get(item["slug"]):
                diagnostics.append(
                    CatalogDiagnostic(
                        f"{artifact_path}#strategies[{index}]",
                        "nearestConfused ID does not resolve from its slug",
                    )
                )
    counts = Counter(categories)
    expected_counts = {name: counts[name] for name in sorted(counts)}
    actual_category_counts = document.get("categoryCounts")
    if (
        not isinstance(actual_category_counts, Mapping)
        or set(actual_category_counts) != set(expected_counts)
        or any(
            type(actual_category_counts[name]) is not int
            or actual_category_counts[name] != count
            for name, count in expected_counts.items()
        )
    ):
        diagnostics.append(
            CatalogDiagnostic(
                artifact_path,
                "categoryCounts does not match the strategy entries",
            )
        )
    maturity_counts = Counter(maturities)
    expected_maturity_counts = {
        maturity: maturity_counts[maturity]
        for maturity in ("baseline", "v1")
        if maturity_counts[maturity]
    }
    actual_maturity_counts = document.get("irMaturityCounts")
    if (
        not isinstance(actual_maturity_counts, Mapping)
        or set(actual_maturity_counts) != set(expected_maturity_counts)
        or any(
            type(actual_maturity_counts[name]) is not int
            or actual_maturity_counts[name] != count
            for name, count in expected_maturity_counts.items()
        )
    ):
        diagnostics.append(
            CatalogDiagnostic(
                artifact_path,
                "irMaturityCounts does not match the strategy entries",
            )
        )
    return tuple(diagnostics)


def catalog_diagnostics(
    library_root: Optional[Any] = None,
) -> tuple[CatalogDiagnostic, ...]:
    """Return catalogability or generated-artifact drift diagnostics."""

    root = library_root or library_resource_root()
    try:
        expected = generate_catalog_text(root)
    except CatalogValidationError as error:
        return error.diagnostics
    artifact = catalog_resource(root)
    artifact_path = f"library/{'/'.join(CATALOG_PARTS)}"
    if not artifact.is_file():
        return (
            CatalogDiagnostic(
                artifact_path,
                "generated catalog is missing; run scripts/generate_catalog.py",
            ),
        )
    try:
        actual_bytes = artifact.read_bytes()
    except OSError as error:
        return (
            CatalogDiagnostic(
                artifact_path, f"cannot read generated catalog: {error}"
            ),
        )
    if actual_bytes != expected.encode("utf-8"):
        return (
            CatalogDiagnostic(
                artifact_path,
                "generated catalog is stale or non-canonical; run "
                "scripts/generate_catalog.py",
            ),
        )
    try:
        actual = actual_bytes.decode("utf-8")
        document = json.loads(actual)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:  # pragma: no cover
        return (
            CatalogDiagnostic(
                artifact_path, f"catalog is not valid UTF-8 JSON: {error}"
            ),
        )
    return _document_diagnostics(document, artifact_path=artifact_path)


def load_catalog(library_root: Optional[Any] = None) -> dict[str, Any]:
    """Load and validate the shipped generated artifact."""

    artifact = catalog_resource(library_root)
    artifact_path = f"library/{'/'.join(CATALOG_PARTS)}"
    try:
        document = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogValidationError(
            (CatalogDiagnostic(artifact_path, f"cannot load catalog: {error}"),)
        ) from error
    diagnostics = _document_diagnostics(document, artifact_path=artifact_path)
    if diagnostics:
        raise CatalogValidationError(diagnostics)
    return dict(document)


def strategy_rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(entry)
        for entry in document.get("strategies", [])
        if isinstance(entry, Mapping)
    ]


def searchable_text(strategy: Mapping[str, Any]) -> str:
    """Flatten authored and derived metadata for case-insensitive search."""

    values: list[Any] = [
        strategy.get("id"),
        strategy.get("slug"),
        strategy.get("name"),
        strategy.get("category"),
        strategy.get("irMaturity"),
        strategy.get("irVersion"),
        strategy.get("regime"),
        strategy.get("conditions"),
        strategy.get("invalidation"),
        strategy.get("shape"),
        strategy.get("calibratedOn"),
        strategy.get("provenance"),
        *strategy.get("requiredHostSignals", []),
    ]
    confused = strategy.get("nearestConfused", [])
    if isinstance(confused, list):
        for item in confused:
            if isinstance(item, Mapping):
                values.extend(
                    (item.get("slug"), item.get("id"), item.get("distinction"))
                )
    return " ".join(str(value) for value in values if value is not None).casefold()


__all__ = (
    "CATALOG_PARTS",
    "CATALOG_SCHEMA_VERSION",
    "METADATA_VERSION",
    "CatalogDiagnostic",
    "CatalogValidationError",
    "ConfusedStrategyV1",
    "StrategyMetadataV1",
    "build_catalog",
    "catalog_diagnostics",
    "catalog_path",
    "generate_catalog_text",
    "load_catalog",
    "parse_strategy_metadata",
    "render_catalog",
    "searchable_text",
    "strategy_rows",
    "write_catalog",
)
