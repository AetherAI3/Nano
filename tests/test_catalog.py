"""Generated strategy metadata remains a projection of source and pinned IR."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

from nano.library.catalog import (
    CatalogDiagnostic,
    CatalogValidationError,
    build_catalog,
    catalog_diagnostics,
    catalog_path,
    generate_catalog_text,
    load_catalog,
    parse_strategy_metadata,
    write_catalog,
)


LIBRARY = Path(__file__).parents[1] / "nano" / "library"
ROOT = Path(__file__).parents[1]

_CHECKER_SPEC = importlib.util.spec_from_file_location(
    "_nano_check_contribution_test", ROOT / "scripts" / "check_contribution.py"
)
assert _CHECKER_SPEC is not None and _CHECKER_SPEC.loader is not None
check_contribution = importlib.util.module_from_spec(_CHECKER_SPEC)
_CHECKER_SPEC.loader.exec_module(check_contribution)

BASELINE_IR = {
    "type": "Strategy",
    "nanoIrVersion": "0.1.0",
    "name": "CatalogFixture",
    "effects": ["intent.emit", "log.append"],
    "nodes": [
        {"type": "Schedule", "interval": "1m"},
        {"type": "Condition", "signal": "SCORE", "operator": ">", "value": 0},
        {"type": "Intent", "action": "OBSERVE"},
    ],
}

HEADER = """\
// REGIME: orderly trend with liquid execution.
// CONDITIONS: score confirms the continuation.
// INVALIDATION: score loses zero.
// SHAPE: one-minute continuation after a shallow reset.
// CALIBRATED ON: normalized research fixtures; thresholds do not travel.
// NOT other_strategy: this waits for confirmation instead of predicting it.
strategy CatalogFixture {
    every 1m {
        if SCORE > 0 { observe() }
    }
}
"""


def _strategy(document, slug):
    return next(row for row in document["strategies"] if row["slug"] == slug)


def _tiny_library(root: Path) -> Path:
    category = root / "trend"
    category.mkdir(parents=True)
    (category / "catalog_fixture.nano").write_text(HEADER, encoding="utf-8")
    (category / "catalog_fixture_ir.json").write_text(
        json.dumps(BASELINE_IR), encoding="utf-8"
    )
    return root


def _unknown_root_key(document):
    document["unknownRoot"] = True


def _unknown_strategy_key(document):
    document["strategies"][0]["unknownEntry"] = True


def _omit_provenance(document):
    document["strategies"][0].pop("provenance")


def _duplicate_confused_slug(document):
    confused = document["strategies"][0]["nearestConfused"]
    confused.append(dict(confused[0]))


def test_checked_in_catalog_is_byte_identical_and_counts_the_landed_corpus():
    first = generate_catalog_text(LIBRARY)
    second = generate_catalog_text(LIBRARY)
    document = json.loads(first)

    assert first == second
    assert first.startswith("{\n\n")
    assert first.endswith("}\n")
    assert catalog_path(LIBRARY).read_bytes() == first.encode("utf-8")
    assert document["strategyCount"] == 53
    assert document["irMaturityCounts"] == {"baseline": 41, "v1": 12}
    assert document["categoryCounts"] == {
        "event_volatility": 11,
        "mean_reversion": 6,
        "momentum": 6,
        "risk": 7,
        "trend": 7,
        "volatility": 4,
        "volume": 4,
        "watchdog": 8,
    }
    assert catalog_diagnostics(LIBRARY) == ()


def test_ids_slugs_and_serialized_order_are_stable_and_unambiguous():
    document = build_catalog(LIBRARY)
    rows = document["strategies"]
    ids = [row["id"] for row in rows]
    slugs = [row["slug"] for row in rows]

    assert ids == sorted(ids)
    assert len(ids) == len(set(ids)) == len(set(slugs)) == 53
    assert all(row["metadataVersion"] == "StrategyMetadataV1" for row in rows)


def test_baseline_and_v1_host_inputs_come_from_their_pinned_ir_shapes():
    document = load_catalog()
    baseline = _strategy(document, "golden_cross")
    v1 = _strategy(document, "ema_pullback_continuation")

    assert baseline["irMaturity"] == "baseline"
    assert baseline["requiredHostSignals"] == ["SMA_SPREAD"]
    assert v1["irMaturity"] == "v1"
    assert v1["requiredHostSignals"] == ["close"]


@pytest.mark.parametrize(
    ("mutated", "message"),
    [
        (HEADER.replace("// REGIME:", "// MARKET:"), "missing `// REGIME:`"),
        (HEADER.replace("// REGIME:", "//REGIME:"), "missing `// REGIME:`"),
        (HEADER.replace("orderly trend with liquid execution.", ""), "field is empty"),
        (
            HEADER.replace(
                "// CONDITIONS:", "// REGIME: duplicate.\n// CONDITIONS:"
            ),
            "duplicate `REGIME:`",
        ),
        (HEADER.replace("// NOT other_strategy:", "// DIFFERENT:"), "nearest-confused"),
        (
            HEADER.replace("NOT other_strategy", "NOT catalog_fixture"),
            "points back to the same strategy",
        ),
        (
            HEADER.replace(
                "// NOT other_strategy:",
                "// NOT other_strategy: first distinction.\n"
                "// NOT other_strategy:",
            ),
            "duplicate `NOT other_strategy:`",
        ),
        (HEADER.replace("// REGIME:", "// SOURCE:\n// REGIME:"), "SOURCE"),
    ],
)
def test_malformed_headers_fail_with_field_specific_diagnostics(mutated, message):
    with pytest.raises(CatalogValidationError, match=message):
        parse_strategy_metadata(
            mutated,
            BASELINE_IR,
            category="trend",
            slug="catalog_fixture",
            source_path="nano/library/trend/catalog_fixture.nano",
        )


def test_category_and_slug_must_be_stable_identifiers():
    with pytest.raises(CatalogValidationError, match="lowercase snake_case"):
        parse_strategy_metadata(
            HEADER,
            BASELINE_IR,
            category="Trend Rules",
            slug="Catalog-Fixture",
        )


def test_unknown_ir_maturity_is_rejected_instead_of_guessed():
    document = dict(BASELINE_IR, nanoIrVersion="2.0.0")
    with pytest.raises(CatalogValidationError, match="unsupported nanoIrVersion"):
        parse_strategy_metadata(
            HEADER,
            document,
            category="trend",
            slug="catalog_fixture",
        )


def test_nearest_confused_slug_may_be_one_word():
    metadata = parse_strategy_metadata(
        HEADER.replace("other_strategy", "breakout"),
        BASELINE_IR,
        category="trend",
        slug="catalog_fixture",
    )
    assert metadata.nearest_confused[0].slug == "breakout"


def test_prose_not_travel_is_not_mistaken_for_a_strategy_slug():
    source = HEADER.replace(
        "thresholds do not travel.", "the absolute threshold does NOT travel: scale it."
    )
    metadata = parse_strategy_metadata(
        source,
        BASELINE_IR,
        category="trend",
        slug="catalog_fixture",
    )
    assert [item.slug for item in metadata.nearest_confused] == ["other_strategy"]


def test_optional_source_is_projected_only_when_authored():
    source = HEADER.replace(
        "// REGIME:", "// SOURCE: public exchange specification.\n// REGIME:"
    )
    metadata = parse_strategy_metadata(
        source,
        BASELINE_IR,
        category="trend",
        slug="catalog_fixture",
    )
    assert metadata.provenance == "public exchange specification."


def test_source_without_the_contribution_header_spelling_is_not_provenance():
    source = HEADER.replace("// REGIME:", "//SOURCE: not a field.\n// REGIME:")
    metadata = parse_strategy_metadata(
        source,
        BASELINE_IR,
        category="trend",
        slug="catalog_fixture",
    )
    assert metadata.provenance is None


def test_catalog_check_detects_source_drift_without_a_sidecar_edit(tmp_path):
    library = _tiny_library(tmp_path / "library")
    write_catalog(library)
    assert catalog_diagnostics(library) == ()

    source = library / "trend" / "catalog_fixture.nano"
    source.write_text(
        source.read_text(encoding="utf-8").replace("orderly trend", "choppy trend"),
        encoding="utf-8",
    )
    diagnostics = catalog_diagnostics(library)
    assert len(diagnostics) == 1
    assert "stale or non-canonical" in diagnostics[0].message


def test_catalog_check_rejects_newline_normalization_as_byte_drift(tmp_path):
    library = _tiny_library(tmp_path / "library")
    artifact = write_catalog(library)
    artifact.write_bytes(artifact.read_bytes().replace(b"\n", b"\r\n"))

    diagnostics = catalog_diagnostics(library)
    assert len(diagnostics) == 1
    assert "stale or non-canonical" in diagnostics[0].message


def test_loaded_artifact_rejects_count_and_entry_shape_mutations(tmp_path):
    library = _tiny_library(tmp_path / "library")
    artifact = write_catalog(library)
    document = json.loads(artifact.read_text(encoding="utf-8"))
    document["strategyCount"] = 2
    document["categoryCounts"] = {"trend": 2}
    document["irMaturityCounts"] = {"baseline": 2}
    document["strategies"][0]["slug"] = "Bad Slug"
    document["strategies"][0]["requiredHostSignals"] = ["SCORE", "SCORE"]
    artifact.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CatalogValidationError) as raised:
        load_catalog(library)
    rendered = str(raised.value)
    assert "strategyCount does not match" in rendered
    assert "requiredHostSignals must be unique" in rendered
    assert "categoryCounts does not match" in rendered
    assert "irMaturityCounts does not match" in rendered
    assert "stable lowercase snake_case" in rendered


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_unknown_root_key, "catalog keys must match StrategyMetadataV1 exactly"),
        (_unknown_strategy_key, "strategy keys must match StrategyMetadataV1 exactly"),
        (_omit_provenance, "provenance"),
        (_duplicate_confused_slug, "nearestConfused slugs must be unique"),
    ],
)
def test_loaded_artifact_rejects_closed_schema_mutations(tmp_path, mutate, message):
    library = _tiny_library(tmp_path / "library")
    artifact = write_catalog(library)
    document = json.loads(artifact.read_text(encoding="utf-8"))
    mutate(document)
    artifact.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CatalogValidationError, match=message):
        load_catalog(library)


def test_multiple_nearest_confused_entries_resolve_when_a_slug_exists():
    document = load_catalog()
    known_ids = {row["slug"]: row["id"] for row in document["strategies"]}
    confused = [
        item
        for row in document["strategies"]
        for item in row["nearestConfused"]
    ]

    assert confused
    assert all(item["id"] == known_ids.get(item["slug"]) for item in confused)
    assert any(item["id"] is None for item in confused)


def test_contribution_entry_check_surfaces_catalog_parser_diagnostics(monkeypatch):
    def reject(*args, **kwargs):
        raise CatalogValidationError(
            (
                CatalogDiagnostic(
                    "nano/library/trend/golden_cross.nano",
                    "mutated metadata is not catalogable",
                    4,
                ),
            )
        )

    monkeypatch.setattr(check_contribution, "parse_strategy_metadata", reject)
    problems = []
    check_contribution.check_entry(
        LIBRARY / "trend" / "golden_cross.nano", False, problems
    )
    assert any("golden_cross.nano:4" in problem for problem in problems)
    assert any("not catalogable" in problem for problem in problems)


def test_contribution_check_fails_on_generated_catalog_drift(monkeypatch, capsys):
    monkeypatch.setattr(
        check_contribution,
        "catalog_diagnostics",
        lambda root: (
            CatalogDiagnostic(
                "library/catalog/strategy_metadata_v1.json", "mutation survived"
            ),
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_contribution.py",
            str(LIBRARY / "trend" / "golden_cross.nano"),
        ],
    )

    assert check_contribution.main() == 1
    assert "nano/library/catalog/strategy_metadata_v1.json" in capsys.readouterr().err


def test_python_sources_do_not_import_the_unpacked_scripts_namespace():
    violations = []
    for source_root in (ROOT / "nano", ROOT / "scripts", ROOT / "tests"):
        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(name == "scripts" or name.startswith("scripts.") for name in names):
                    violations.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert violations == [], (
        "top-level scripts are CLI entry files, not an installed package: "
        + ", ".join(violations)
    )
