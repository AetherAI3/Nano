# Project search, PR tags, and compact memory

Nano now ships a small, model-free project discovery layer in `nano.project`.
It classifies supplied PR/commit/memory metadata, compiles search phrases,
groups matching records, suggests conventional titles, and emits compact agent
capsules. It does not fetch GitHub, rename PRs, write memory, or execute actions.

This is the Nano-side implementation for [proposal #41](https://github.com/AetherAI3/Nano/issues/41).
Online search, GitHub ingestion, and APR annotation persistence are integration
work in Aether Cloud/Agent. No UI deployment is implied by this package.

## The user experience

In an APR project, one search field accepts:

- frontend css pending review
- open PRs with failed CI
- frontend not merged
- [feature][frontend][css][pending review]
- css or memory graph
- "dropdown animation"
- author:brandon path:site/ after:2026-09-01
- css group by review

Show the parsed filters as removable chips. Show free text separately, preserve
the original query, and render diagnostics next to the offending span.
Offer "Group by area / type / review / CI" as ordinary controls; users need not
learn syntax. Do not execute an older valid query's results under newer invalid
text. Cancel or sequence network requests so stale responses cannot replace a
newer query.

A PR card can read:

> [feature] [frontend] [css] [downstream] [pending review]

Its compact agent capsule also includes the record identity, bounded title,
lifecycle state, review and CI observations, abbreviated source SHA, and
observation time. The JSON retains the full SHA and tag evidence. Agents can
discover candidates cheaply and then fetch the exact PR, diff, checks, or
memory revision before doing work. Tags are retrieval hints, not proof that
code works or that a PR is merge-ready.

Keep the existing APR project cards. Add a small search/count entry point in
the project view, useful saved filters, and grouped PR rows. In the graph, use
the same facets to filter or highlight existing nodes and explain connections.
Selecting a chip should mean the same thing in the PR list and graph.

## Python and CLI

The checked-in fixture is synthetic example data, not live Aether work.

```python
from nano.project import ProjectIndex, classify_record, compile_query

record = {
    "id": "pr:9001:41", "project_id": "example-project", "kind": "pr",
    "repository": "example/project", "number": 41,
    "title": "feat(frontend): add CSS search filters",
    "files": ["site/components/search.css"],
    "source_sha": "a" * 40,
    "state": "open", "review": "pending", "ci": "passed",
    "ci_sha": "a" * 40, "review_sha": "a" * 40,
    "observed_at": "2026-09-16T10:00:00Z",
    "updated_at": "2026-09-16T09:00:00Z",
    "depends_on": ["pr:9001:40"],
}
annotation = classify_record(record)
query = compile_query("frontend css pending review group by area")
result = ProjectIndex([record]).search(query, project_id="example-project")
assert result["total"] == 1
print(annotation["capsule"])
```

```bash
nano project parse "frontend css pending review"
nano project classify nano/project/fixtures/record.json
nano project search nano/project/fixtures/records.json "css group by review" --project-id example-project
```

All commands emit JSON. Parse diagnostics return exit 1; input read failures
return 3. The embedding API is pure; the CLI alone reads the supplied JSON file.
There are no mandatory dependencies beyond the Python standard library.

## Record and annotation contract

Required input: id, project_id, kind (pr/commit/memory), and title.
Optional fields: repository, number, author, body, files, labels, source_sha,
state, review, ci, ci_sha, review_sha, observed_at, updated_at, depends_on,
blocks, tag_overrides, files_complete. Unknown fields are rejected.
Identity fields (id, project_id, repository, author) cannot contain whitespace.

Use host-owned stable record IDs, such as a provider repository numeric ID plus
PR number. repository is a display/search projection, not an authorization
identity. A commit's source_sha is a full 40- or 64-character lowercase Git
object ID. Resolve abbreviations through the host before recording them.

| Facet | How it is derived |
| --- | --- |
| type | Conventional title, recognized labels, or explicit bracket tags |
| area | Recognized labels, title vocabulary, and known path components |
| tech | Recognized labels/title words and changed-file suffixes |
| relation | downstream only from supplied depends_on; upstream only from blocks |
| state | Supplied host lifecycle observation |
| review | Supplied host observation bound by review_sha to source_sha |
| ci | Supplied host observation bound by ci_sha to source_sha |

A guessed path tag is explicitly derived evidence. It must not become a
canonical claim about architecture, behavior, test success, or dependency
ordering. Body text is searchable but does not generate operational status.
A label such as "approved" or title saying "CI passed" never establishes that
status. Missing/mismatched head binding makes review/CI unknown. This also
applies to "none": no checks observed is different from checks not read.

Nano records observed_at but reads no clock. The host owns TTL and refresh:
before presenting a snapshot as current, check its age, provider authority,
and current source SHA. Status without an observation time is diagnosed.
An old approved snapshot is historical evidence, not current approval.

files_complete defaults to false. A tag's absence means it was not indexed,
not that no file of that kind exists. Show partial coverage where the host has
only a bounded changed-file list.

Overrides replace inferred type/area/tech values, including an empty list to
remove them. They cannot manufacture status or dependency facts:

```json
{"tag_overrides":{"area":["backend"],"tech":[]}}
```

Each output includes nanoProjectVersion 1.0.0, vocabularyHash, annotationHash,
record/project identities, full source SHA, original and suggested titles,
evidence-bearing tags, status, explicit dependencies, diagnostics, and capsule.
annotationHash identifies this derived output; it is not a signature or proof
of authenticated input. Naming is a suggestion only; Nano never writes GitHub.

## Search contract

nanoProjectQueryVersion 1.0.0 is independent of strategy and market Intent IR.
The packaged schema is nano/project/schemas/project-query-1.0.0.schema.json.
It describes compiler output for other clients. Compile source text with
compile_query on the server; do not trust a browser's valid flag or query hash
as validation or authorization. Python validation also enforces aggregate
predicate limits and nonempty OR branches across the complete query.

A query is anyOf OR clauses, each containing allOf predicates.
Whitespace and "and" combine predicates with AND. "or" separates clauses.
"not", "without", "exclude", and a leading minus negate the following predicate.
Repeated fields are AND-ed. Use explicit OR clauses for alternatives.

Fields: type, area, tech, relation, state, review, ci, kind, repo, author, label,
tag, number, sha, path, text, after, before.
Short aliases: technology -> tech, repository -> repo, status -> state.
#41 is number:41. Bracketed chips accept known phrases or a literal tag value.
Explicit label filters match labels exactly; tag matches any semantic tag value.

Quoted text is literal, including words such as "not" or "pending review".
Unknown bare words remain full-text predicates. Only the documented filler
words show/find/search/me/the/with/that/are/all are skipped outside quotes or
bracketed chips. Quote those words to search for them literally.
A term such as "pending" alone is text; "pending review" is a review filter.
Unknown named filters, malformed quotes, invalid dates/statuses, dangling
negation/operators, and unsupported parentheses produce diagnostics.
Invalid queries cannot run. Current bounds: 4,096 characters, 256 tokens,
64 predicates, and 8 OR clauses.

Unquoted top-level group by FIELD selects one grouping dimension; sort by
updated/number/title selects deterministic order. Date filters compare UTC
calendar dates: after is inclusive, before exclusive. Relative dates are not
interpreted by v1; the UI can supply explicit dates. Paths match a prefix, or
a glob if * or ? is present. Text matching is case-insensitive substring
matching, including phrases, over supplied title/body/labels/paths/identity.

Unknown operational status is neither success nor failure: "not failed CI"
excludes unknown CI. To retrieve it, use ci:unknown. Missing dates likewise
do not pass a negated date condition. By contrast, "not css" means the css tag
is absent from the supplied index, subject to files_complete.

The result contains the total within this supplied project snapshot, bounded
items, deterministic nextOffset, and group counts across all matches before
pagination. Multi-valued facet group counts can sum above the result total.
Each card is an independent copy; editing a returned card cannot mutate the
index. Duplicate record identities are rejected rather than silently selected.

## Thousands of PRs

ProjectIndex builds facet indexes once at ingestion, then filters candidates
before text/path scans. It supports bounded pages (1..100 results) and stable
tie-breaking. The test suite exercises 10,000 supplied records, exclusions,
separate project scopes, total counts, and full-match group counts.

This is a reference in-memory index, not a database service. Production Cloud
should persist the same facets and full-text fields behind its search API,
using parameterized queries and snapshot/cursor pagination. The first-page
DevContext bootstrap is not a full search corpus. Do not show its capped list
as the total searchable history.

Host work:

1. Authorize owner -> project -> durable repository binding before retrieval.
2. Backfill PR/commit metadata with explicit pagination and progress.
3. Update the current projection on PR, review, check, and commit events.
4. Bind checks/reviews to the current PR head and keep observation time.
5. Compile once per query and query the authorized index; do not call an LLM.
6. Return corpus coverage, indexed count, result total, and source freshness.
7. Maintain stable pagination within a snapshot; offset pagination here is for
   an unchanged reference index, not a changing remote database.

A project_id filter is not row-level security. Nano receives authorized data;
it cannot verify who owns a supplied ID.

## APR memory integration

APR Project Memory is distinct from the owner-wide graph/Observatory
projection. The semantic annotation should be a rebuildable search projection
attached to a verified record/revision, not a replacement for immutable APR
memory or an alternative identity store.

At the existing verified memory commit/finalize boundary, a host can classify
the supplied commit subject, changed paths, labels, and resolved PR relation.
Attach the annotation under a versioned namespace, indexed by owner/project,
stable record ID, source SHA, and memory revision. Reprocessing identical
input produces the same annotation hash. Keep structural graph authority and
source verification in the existing APR pipeline.

Join current PR/review/CI overlays onto that history for search. Update these
overlays when reviews/checks change without pretending the old memory revision
changed. A commit must not be assigned to a PR merely because its words look
similar; use verified provider associations or explicit supplied references.

The Agent command currently separates memory commit from push:

```text
aether -m commit --message "Record the verified CSS search work" --link-git <full-git-sha>
aether -m push
```

Publish the source Git commit first so the Gateway can verify it. "-m" selects
Project Memory when it is in first position. Nano does not change this command
or upload a snapshot. The short SHA typed into search is a lookup prefix and
may yield multiple matches; it does not establish an exact memory revision.

Integration evidence inspected:

- [APR project/binding and bounded provider reads](https://github.com/AetherAI3/AETHER-CLOUD/blob/186e77a6dc53b4624403d8a3fcba50b4f028f747/docs/reports/2026-09-09-github-apr-project-tooling.md)
- [PR bootstrap and unknown check contract](https://github.com/AetherAI3/AETHER-CLOUD/blob/186e77a6dc53b4624403d8a3fcba50b4f028f747/lib/devcontext/contracts.py)
- [Project Memory implementation boundaries](https://github.com/AetherAI3/AETHER-CLOUD/blob/186e77a6dc53b4624403d8a3fcba50b4f028f747/docs/specs/aether-project-runtime/APR-10-IMPLEMENTATION-REVIEW.md)
- [Agent memory command](https://github.com/AetherAI3/Aether-Agent/blob/ccbe1595e0f1635e6f59b15170a4308282a5745a/docs/project-memory.md)

## Improvement without an always-running agent

Store user corrections as overrides and review project aliases explicitly.
Add the corrected example to the corpus, version the vocabulary, and rebuild
annotations. For example, "skins" can become a project-specific alias for CSS:

```python
from nano.project import DEFAULT_VOCABULARY, Vocabulary
project_words = Vocabulary(
    DEFAULT_VOCABULARY.aliases + (("skins", "tech", "css"),),
    version="1.0.1",
)
```

The query and index must use the same vocabulary hash. This allows useful
local conventions to accumulate without silently changing global meaning.
An optional future model may suggest new aliases or richer summaries; those
are proposals with review/evidence, and are outside this implementation.
