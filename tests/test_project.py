"""Behavioral contracts for read-only project discovery."""
from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from nano.cli.main import main
from nano.project import (
    DEFAULT_VOCABULARY, ProjectError, ProjectIndex, ProjectRecord, Vocabulary,
    canonical_json, classify_record, compile_query,
)

SHA = "a" * 40
OTHER_SHA = "b" * 40
ROOT = Path(__file__).resolve().parents[1]


def record(id="org/repo#1", **changes):
    data = dict(id=id, project_id="alpha", kind="pr",
                repository="org/repo", number=1, author="brandon",
                title="feat(frontend): add CSS search filters",
                files=["site/components/search.css"], source_sha=SHA,
                state="open", review="pending", ci="passed",
                ci_sha=SHA, review_sha=SHA, observed_at="2026-09-16T10:00:00Z",
                updated_at="2026-09-16T09:00:00Z")
    data.update(changes)
    return data


def predicates(text):
    q = compile_query(text)
    assert q.valid, q.to_dict()
    return [[(p.field, p.value, p.exclude) for p in clause] for clause in q.clauses]


def test_useful_capsule_has_evidence_without_full_body():
    result = classify_record(record(body="An enormous explanation.", depends_on=["org/repo#2"]))
    assert [(t["facet"], t["value"]) for t in result["tags"]] == [
        ("type", "feature"), ("area", "frontend"), ("tech", "css"), ("relation", "downstream")]
    assert "[feature][frontend][css][downstream][pending review]" in result["capsule"]
    assert "review=pending ci=passed" in result["capsule"]
    assert "enormous" not in canonical_json(result)
    assert all(tag["evidence"] for tag in result["tags"])
    assert result["sourceSha"] == SHA
    assert result["annotationHash"].startswith("sha256:")


def test_text_labels_cannot_manufacture_status_or_dependency_truth():
    result = classify_record(record(
        title="Approved, passing CI, downstream release", labels=["approved", "ci:passed", "downstream"],
        files=[], state="unknown", review="unknown", ci="unknown", depends_on=[]))
    assert result["status"]["ci"] == result["status"]["review"] == "unknown"
    assert not any(t["facet"] == "relation" for t in result["tags"])


@pytest.mark.parametrize("field,sha_field", [("ci", "ci_sha"), ("review", "review_sha")])
@pytest.mark.parametrize("sha,code", [("", "_HEAD_UNBOUND"), (OTHER_SHA, "_HEAD_MISMATCH")])
def test_stale_or_unbound_status_is_unknown(field, sha_field, sha, code):
    result = classify_record(record(**{sha_field: sha}))
    assert result["status"][field] == "unknown"
    assert field.upper() + code in result["diagnostics"]


def test_unknown_age_is_visible_and_no_clock_is_invented():
    result = classify_record(record(observed_at=""))
    assert result["status"]["observedAt"] is None
    assert "STATUS_OBSERVATION_TIME_MISSING" in result["diagnostics"]
    assert "asof=unknown" in result["capsule"]


def test_overrides_replace_hints_and_can_clear_a_facet():
    result = classify_record(record(tag_overrides={"area": ["backend"], "tech": []}))
    assert [(t["facet"], t["value"]) for t in result["tags"]] == [
        ("type", "feature"), ("area", "backend")]
    assert result["tags"][1]["evidence"] == ["user-override"]
    assert result["suggestedTitle"] == "feat(backend): add CSS search filters"


def test_naming_is_a_suggestion_and_preserves_unknown_literal_tags():
    source = record(title="[feature] [frontend] [customer-A] Add filters")
    result = classify_record(source)
    assert result["suggestedTitle"] == "feat(frontend): [customer-A] Add filters"
    assert source["title"] == result["title"]
    assert classify_record(record(title="Release notes", labels=[], files=[]))["suggestedTitle"] == "Release notes"


@pytest.mark.parametrize("title", ["feat(frontend)!: replace CSS API", "feat!: replace CSS API"])
def test_naming_preserves_breaking_change_marker(title):
    result = classify_record(record(title=title, tag_overrides={"area": ["frontend"]}))
    assert result["suggestedTitle"] == "feat(frontend)!: replace CSS API"


def test_record_input_is_detached_and_frozen():
    source = record()
    value = ProjectRecord.from_dict(source)
    source["files"].append("server/main.py")
    assert value.files == ("site/components/search.css",)
    with pytest.raises(FrozenInstanceError):
        value.title = "changed"


@pytest.mark.parametrize("changes", [
    {"kind": "shell"}, {"number": True}, {"ci": "success"},
    {"source_sha": "39348930"}, {"ci_sha": "invalid"}, {"files": "site/style.css"},
    {"updated_at": "2026-09-16"}, {"observed_at": "yesterday"},
    {"tag_overrides": {"review": ["approved"]}}, {"extra": "ignored"},
    {"id": "bad\x1bterminal"}, {"body": "x" * 16001},
    {"id": "fake\nci=passed"}, {"project_id": "two projects"},
    {"tag_overrides": {1: ["css"], "area": ["frontend"]}},
])
def test_malformed_records_are_rejected(changes):
    with pytest.raises(ProjectError):
        classify_record(record(**changes))


@pytest.mark.parametrize("text,expected", [
    ("frontend css pending review", [[("area", "frontend", False), ("tech", "css", False), ("review", "pending", False)]]),
    ("[feature][frontend][css][pending review]", [[("type", "feature", False), ("area", "frontend", False), ("tech", "css", False), ("review", "pending", False)]]),
    ("show me open PRs with failed CI", [[("state", "open", False), ("kind", "pr", False), ("ci", "failed", False)]]),
    ("frontend not merged", [[("area", "frontend", False), ("state", "merged", True)]]),
    ("css without failed ci", [[("tech", "css", False), ("ci", "failed", True)]]),
    ("-[css]", [[("tech", "css", True)]]),
    ('"pending review" "not merged"', [[("text", "pending review", False), ("text", "not merged", False)]]),
    ('text:"CI (passed)"', [[("text", "ci (passed)", False)]]),
    ('"-css"', [[("text", "-css", False)]]),
    ('-"pending review"', [[("text", "pending review", True)]]),
    ("[all] [not]", [[("tag", "all", False), ("tag", "not", False)]]),
    ("front [end]", [[("text", "front", False), ("tag", "end", False)]]),
    ("dropdown animation", [[("text", "dropdown", False), ("text", "animation", False)]]),
    ("#42 sha:39348930", [[("number", "42", False), ("sha", "39348930", False)]]),
    ("repo:org/repo author:brandon", [[("repo", "org/repo", False), ("author", "brandon", False)]]),
    ("frontend or backend", [[("area", "frontend", False)], [("area", "backend", False)]]),
])
def test_query_meaning(text, expected):
    assert predicates(text) == expected


@pytest.mark.parametrize("text", [
    'css "unclosed', "[css", "ci:whatever", "state:banana", "owner:joe",
    "css not", "not not css", "css or", "and css", "css and and frontend",
    "frontend or group by area", "group by banana", "group by area group by type",
    "(frontend or backend)", "sort by unknown", "sha:123", "before:tomorrow", "after:2026-02-30",
])
def test_unresolved_query_never_silently_runs(text):
    query = compile_query(text)
    assert not query.valid
    assert query.diagnostics
    with pytest.raises(ProjectError, match="diagnostics"):
        ProjectIndex([record()]).search(query, project_id="alpha")


def test_exclusions_and_or_use_complete_meaning_and_unknown_is_not_success():
    records = [
        record("pass"),
        record("fail", ci="failed"),
        record("unknown", ci="unknown"),
        record("merged", state="merged"),
        record("other", title="fix(api): repair endpoint", files=["api/routes.py"], ci="failed"),
    ]
    index = ProjectIndex(records)
    assert {x["recordId"] for x in index.search("css not failed ci not merged", project_id="alpha")["items"]} == {"pass"}
    assert {x["recordId"] for x in index.search("ci:unknown", project_id="alpha")["items"]} == {"unknown"}
    result = index.search("css or api", project_id="alpha")
    assert result["total"] == 5
    assert len({x["recordId"] for x in result["items"]}) == 5


def test_unknown_state_does_not_satisfy_not_merged():
    index = ProjectIndex([record(state="unknown")])
    assert index.search("not merged", project_id="alpha")["total"] == 0


def test_open_includes_drafts_with_explicit_draft_exclusion_available():
    index = ProjectIndex([record("open"), record("draft", state="draft"),
                          record("closed", state="closed"), record("merged", state="merged"),
                          record("unknown", state="unknown")])
    result = index.search("open PRs group by state", project_id="alpha")
    assert {item["recordId"] for item in result["items"]} == {"open", "draft"}
    assert result["groups"] == [{"value": "draft", "count": 1}, {"value": "open", "count": 1}]
    assert [item["recordId"] for item in index.search("open not draft", project_id="alpha")["items"]] == ["open"]
    assert {item["recordId"] for item in index.search("not open", project_id="alpha")["items"]} == {"closed", "merged"}


def test_group_counts_cover_all_matches_before_pagination():
    index = ProjectIndex([record(str(n), number=n + 1, review="approved" if n < 3 else "pending") for n in range(8)])
    first = index.search("css group by review sort by number", project_id="alpha", limit=2)
    second = index.search("css group by review sort by number", project_id="alpha", limit=2, offset=2)
    assert first["total"] == 8 and len(first["items"]) == 2
    assert first["groups"] == [{"value": "approved", "count": 3}, {"value": "pending", "count": 5}]
    assert [r["number"] for r in first["items"]] == [8, 7]
    assert [r["number"] for r in second["items"]] == [6, 5]
    assert first["nextOffset"] == 2


def test_scope_is_mandatory_and_never_expanded_by_query_text():
    index = ProjectIndex([record("a"), record("b", project_id="beta")])
    assert index.search("", project_id="alpha")["total"] == 1
    assert index.search("beta", project_id="alpha")["total"] == 0
    assert index.search("", project_id="missing")["total"] == 0
    with pytest.raises(ProjectError):
        index.search("", project_id="")


def test_date_filters_are_utc_anchored_without_an_ambient_clock():
    index = ProjectIndex([
        record("late", updated_at="2026-09-16T23:30:00-04:00"),
        record("early", updated_at="2026-09-16T01:00:00Z"),
        record("unknown", updated_at=""),
    ])
    assert [x["recordId"] for x in index.search("after:2026-09-17", project_id="alpha")["items"]] == ["late"]
    assert [x["recordId"] for x in index.search("before:2026-09-17", project_id="alpha")["items"]] == ["early"]
    assert index.search("not after:2026-09-17", project_id="alpha")["total"] == 1


def test_paths_labels_shas_and_literals_remain_searchable():
    index = ProjectIndex([record(body='Keep "Hello NASA"', labels=["customer:critical"])])
    for query in ('path:site/', 'path:*.css', 'label:customer:critical', f'sha:{SHA[:8]}',
                  'tag:css', '"Hello NASA"'):
        assert index.search(query, project_id="alpha")["total"] == 1


def test_returned_cards_cannot_mutate_the_index():
    index = ProjectIndex([record()])
    first = index.search("", project_id="alpha")
    first["items"][0]["status"]["ci"] = "failed"
    assert index.search("", project_id="alpha")["items"][0]["status"]["ci"] == "passed"


def test_versioned_aliases_make_project_corrections_explicit():
    custom = Vocabulary(DEFAULT_VOCABULARY.aliases + (("skins", "tech", "css"),), version="1.0.1")
    index = ProjectIndex([record()], vocabulary=custom)
    assert index.search("skins", project_id="alpha")["total"] == 1
    assert classify_record(record(title="feat: skins"), vocabulary=custom)["vocabularyHash"] == custom.content_hash
    with pytest.raises(ProjectError, match="hashes differ"):
        index.search(compile_query("css"), project_id="alpha")
    with pytest.raises(ProjectError, match="collision"):
        Vocabulary(DEFAULT_VOCABULARY.aliases + (("css", "area", "backend"),))


def test_packaged_corpus_searches_prs_commits_and_memory():
    fixtures = ROOT / "nano/project/fixtures"
    records = json.loads((fixtures / "records.json").read_text())
    index = ProjectIndex(records)
    for example in json.loads((fixtures / "queries.json").read_text()):
        result = index.search(example["source"], project_id="example-project")
        assert [item["recordId"] for item in result["items"]] == example["expectedIds"]


def test_annotation_and_search_replay_ignore_ingestion_order():
    first = record(labels=["frontend", "feature"], files=["site/a.css", "site/b.css"])
    reordered = dict(first, labels=list(reversed(first["labels"])), files=list(reversed(first["files"])))
    assert classify_record(first) == classify_record(reordered)
    entries = [first, record("second", number=2), record("third", ci="failed")]
    left = ProjectIndex(entries).search("css group by ci", project_id="alpha")
    right = ProjectIndex(reversed(entries)).search("css group by ci", project_id="alpha")
    assert canonical_json(left) == canonical_json(right)
    reordered_words = Vocabulary(tuple(reversed(DEFAULT_VOCABULARY.aliases)))
    assert compile_query("frontend css", vocabulary=reordered_words) == compile_query("frontend css")


def test_duplicate_identity_is_not_arbitrarily_selected():
    with pytest.raises(ProjectError, match="duplicate"):
        ProjectIndex([record(), record(title="different")])


@pytest.mark.parametrize("limit,offset", [(0, 0), (101, 0), (True, 0), (20, -1)])
def test_pagination_is_bounded(limit, offset):
    with pytest.raises(ProjectError):
        ProjectIndex([record()]).search("", project_id="alpha", limit=limit, offset=offset)


def test_bounded_query_work():
    for text in ("x" * 4097, " ".join("x" for _ in range(257)), " or ".join("css" for _ in range(9))):
        with pytest.raises(ProjectError):
            compile_query(text)


def test_ten_thousand_records_are_indexed_once_then_filtered_and_paged():
    records = [record(f"org/repo#{n}", number=n, ci="failed" if n % 10 == 0 else "passed",
                      project_id="beta" if n % 2 == 0 else "alpha")
               for n in range(1, 10001)]
    index = ProjectIndex(records)
    result = index.search("frontend ci:failed group by review", project_id="beta", limit=10)
    assert result["total"] == 1000
    assert len(result["items"]) == 10
    assert result["groups"] == [{"value": "pending", "count": 1000}]
    assert all(item["projectId"] == "beta" for item in result["items"])
    assert index.search("ci:failed", project_id="alpha")["total"] == 0


def test_cli_parse_classify_and_search(tmp_path, capsys):
    assert main(["project", "parse", "frontend css pending review"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"]
    assert main(["project", "parse", "css not"]) == 1
    assert not json.loads(capsys.readouterr().out)["valid"]
    path = tmp_path / "record.json"
    path.write_text(json.dumps(record()))
    assert main(["project", "classify", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["recordId"] == "org/repo#1"
    path.write_text(json.dumps([record()]))
    assert main(["project", "search", str(path), "frontend", "--project-id", "alpha"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1
    path.write_text("invalid")
    assert main(["project", "classify", str(path)]) == 1
    assert "invalid records JSON" in capsys.readouterr().err
    assert main(["project", "classify", str(tmp_path / "missing")]) == 3


def test_pure_project_core_has_no_host_network_or_model_dependency():
    banned = {"os", "subprocess", "requests", "httpx", "socket", "urllib", "openai", "anthropic"}
    for path in (ROOT / "nano/project").glob("*.py"):
        if path.name == "cli.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] in banned for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in banned
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in ("now", "utcnow", "today")
