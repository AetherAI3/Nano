"""Documentation drift guards.

Prose goes stale quietly; code examples go stale loudly, but only if something
runs them. A hardening audit found the README's headline strategy did not compile
(`observe market` — the grammar requires `observe()`) and that BUILD_ORDER's exit
criterion used `buy()`, which needs an asset. Both had been wrong through several
releases because nothing checked.

So the docs are part of the test suite now. Every fenced `nano` block in the
repository has to compile, and the advertised test count has to stay in sight of
reality.

A block opts out with a `// doc: illustrative` first line — for grammar sketches
and deliberately-rejected examples. The escape hatch is explicit and greppable,
which is the point: an example that cannot compile should say so rather than
quietly being one nobody checks.
"""

import json
import re
from pathlib import Path

import pytest

from nano.compiler import check_source
from nano.compiler.errors import NanoCompileError

ROOT = Path(__file__).resolve().parent.parent

# Files carrying `.nano` examples a reader is expected to be able to run.
DOC_FILES = sorted(
    {
        *ROOT.glob("*.md"),
        *ROOT.glob("docs/**/*.md"),
        *ROOT.glob("nano/library/README.md"),
        *ROOT.glob("examples/README.md"),
    }
)

_FENCE = re.compile(r"^```nano[ \t]*$(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)

# Marks a block as prose rather than a runnable program.
OPT_OUT = "// doc: illustrative"


def _blocks():
    """Every runnable fenced `nano` block, as (relative path, start line, source)."""
    found = []
    for path in DOC_FILES:
        text = path.read_text(encoding="utf-8")
        for match in _FENCE.finditer(text):
            source = match.group(1)
            if source.lstrip().startswith(OPT_OUT):
                continue
            line = text.count("\n", 0, match.start()) + 2
            found.append((path.relative_to(ROOT).as_posix(), line, source))
    return found


BLOCKS = _blocks()


def _rough_test_total() -> int:
    """Count `def test_` across the suite — enough to catch a stale badge."""
    return sum(
        len(re.findall(r"^def test_", path.read_text(encoding="utf-8"), re.MULTILINE))
        for path in (ROOT / "tests").glob("test_*.py")
    )


def test_the_docs_actually_contain_examples():
    """A guard on the guard: a broken fence regex would silently pass everything.

    Three is the current count, and the floor is deliberately set just under it.
    This assertion is not about having many examples — it is about noticing if the
    regex ever stops matching, which would turn every check below into a no-op that
    reports success.
    """
    assert len(BLOCKS) >= 3, f"only found {len(BLOCKS)} nano blocks — check the regex"


@pytest.mark.parametrize(
    "location, source",
    [(f"{path}:{line}", source) for path, line, source in BLOCKS],
    ids=[f"{path}:{line}" for path, line, _ in BLOCKS],
)
def test_every_documented_example_compiles(location: str, source: str):
    try:
        check_source(source)
    except NanoCompileError as error:
        pytest.fail(
            f"{location}: documented example does not compile — "
            f"{error.line}:{error.column}: {error.message}\n"
            f"Mark it `{OPT_OUT}` if it is prose rather than a program."
        )


def test_the_advertised_test_count_is_not_stale():
    """The README badge and prose quote a test count, and numbers rot silently.

    Rather than pin an exact figure this test would itself have to chase, assert
    that whatever the README claims stays within sight of the real total — enough
    to catch "173" surviving into a 300-test suite.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claimed = {int(n) for n in re.findall(r"tests-(\d+)%20passing", readme)}
    claimed |= {int(n) for n in re.findall(r"\*\*(\d+) tests passing\*\*", readme)}
    claimed |= {int(n) for n in re.findall(r"(\d+) passing tests", readme)}
    if not claimed:
        pytest.skip("README no longer advertises a test count")

    # The bound is directional, not a percentage. The README quotes the figure
    # `pytest` prints, which parametrisation pushes above the number of `def test_`
    # functions — so the real total is always at least the function count, and a
    # claim below it is stale. The upper bound only catches a wild typo.
    functions = _rough_test_total()
    for number in sorted(claimed):
        assert functions <= number <= functions * 4, (
            f"README claims {number} tests; the suite defines {functions} test "
            "functions, so the collected total is at least that. Update the badge "
            "and the prose together."
        )


def test_the_package_version_is_declared_once_and_agrees_everywhere():
    """`pyproject.toml` and `nano.__version__` must not drift apart.

    They are edited by hand, in separate files, at the end of a release — the
    exact conditions under which one gets updated and the other does not. A
    wheel whose metadata says one version while `nano.__version__` says another
    is the kind of mismatch a host only discovers from a bug report.

    Parsed with a regex rather than `tomllib` because the package supports
    Python 3.10, where `tomllib` does not exist and a third-party TOML reader
    would be a dependency this project refuses to take.
    """
    import nano

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # Match `version = "..."` only inside the `[project]` table, so a version
    # pinned under some future `[tool.*]` section cannot be mistaken for it.
    project_table = re.split(r"^\[", pyproject, flags=re.MULTILINE)
    declared = [
        match.group(1)
        for section in project_table
        if section.startswith("project]")
        for match in [re.search(r'^version\s*=\s*"([^"]+)"', section, re.MULTILINE)]
        if match
    ]

    assert len(declared) == 1, (
        f"expected exactly one version in pyproject.toml's [project] table, "
        f"found {declared}"
    )
    assert declared[0] == nano.__version__, (
        f"pyproject.toml declares {declared[0]} but nano.__version__ is "
        f"{nano.__version__}. Both are canonical; update them together."


# --------------------------------------------------------------------------
# Library-shape guards.
#
# The *count* claims — the library total, the root README's per-category row,
# and that every category on disk is listed — are guarded in
# tests/test_library.py, next to the corpus. What is left here is the part that
# lives in the docs and nowhere else: the packaging depth the wheel can reach,
# and the baseline/v1 split the README advertises.
# --------------------------------------------------------------------------

LIBRARY = ROOT / "nano" / "library"


def _strategy_paths():
    """Every published strategy source. One level deep, matching packaging.

    `pyproject.toml` ships `library/*/*.nano`, exactly one directory deep, while
    the CI wheel verifier builds its expected set with a recursive glob. A
    strategy in a nested subdirectory would therefore be demanded by CI and
    excluded by packaging, so this guard counts what packaging can actually see
    and the assertion below catches anything hiding deeper.
    """
    return sorted(LIBRARY.glob("*/*.nano"))


def _ir_version(path):
    return json.loads(
        path.with_name(f"{path.stem}_ir.json").read_text(encoding="utf-8")
    )["nanoIrVersion"]


def test_no_strategy_hides_below_the_packaged_depth():
    shallow = {p.resolve() for p in _strategy_paths()}
    everywhere = {p.resolve() for p in LIBRARY.glob("**/*.nano")}
    assert everywhere == shallow, (
        "these strategies sit deeper than nano/library/<category>/<name>.nano, "
        "so setuptools package-data will drop them from the wheel while CI's "
        f"recursive expected-set still demands them: {sorted(everywhere - shallow)}"
    )


def test_the_library_readme_states_the_real_baseline_v1_split():
    """The split is a claim about the corpus, so derive it from the fixtures.

    The total is guarded in tests/test_library.py; this is the half nothing else
    checks. A reader deciding whether an entry needs a vocabulary of agreed
    indicator names or just OHLCV is relying on these two numbers.
    """
    text = (LIBRARY / "README.md").read_text(encoding="utf-8")
    paths = _strategy_paths()
    versions = [_ir_version(p) for p in paths]
    baseline = versions.count("0.1.0")
    v1 = versions.count("1.0.0")
    assert baseline + v1 == len(paths), "an entry pins an unexpected IR version"

    split_claim = re.search(r"(\d+) baseline and (\d+) v1", text)
    assert split_claim, "library README no longer states the baseline/v1 split"
    assert (int(split_claim.group(1)), int(split_claim.group(2))) == (baseline, v1), (
        f"the README claims {split_claim.group(0)!r}; the directory holds "
        f"{baseline} baseline and {v1} v1 entries"
    )

    # The two-corpora table states the same two numbers a second time, several
    # sections earlier. A number written twice drifts once — checking only the
    # sentence would leave the table free to say 32 forever.
    row = re.search(r"^\| Count \| (\d+) \| (\d+) \|$", text, re.MULTILINE)
    assert row, "the two-corpora table no longer states the counts"
    assert (int(row.group(1)), int(row.group(2))) == (baseline, v1), (
        f"the two-corpora table claims {row.group(0)!r}; the directory holds "
        f"{baseline} baseline and {v1} v1 entries"
    )


def test_the_dagger_marks_exactly_the_v1_entries():
    """The category table marks v1 entries with a dagger; the mark must be true.

    Without this the dagger is decoration a reader cannot rely on — and a reader
    checking which entries need only OHLCV is relying on exactly that.
    """
    text = (LIBRARY / "README.md").read_text(encoding="utf-8")
    marked = set(re.findall(r"`([a-z0-9_]+)`†", text))
    expected = {p.stem for p in _strategy_paths() if _ir_version(p) == "1.0.0"}
    assert marked == expected, (
        f"daggered but baseline: {sorted(marked - expected)}; "
        f"v1 but undaggered: {sorted(expected - marked)}"
    )


def test_readme_maturity_matches_the_packaging_classifier():
    """The README may not assert a maturity the packaging metadata denies.

    A corpus change once rewrote "Alpha reference implementation (v0.1.0)" to
    "Reference implementation, v1.0.0" — repairing a stale version number and
    silently upgrading the project's stated maturity in the same edit, leaving
    the README less qualified than `Development Status :: 4 - Beta`. Advertised
    maturity is a project decision, so this pins the prose to the classifier
    rather than to anyone's judgement.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    classifier = re.search(r'"Development Status :: \d+ - ([\w/]+)"', pyproject)
    assert classifier, "pyproject no longer declares a Development Status classifier"
    # `5 - Production/Stable` names the stage in its last path segment; a `\w+`
    # capture would stop at the slash and demand the README read "Production".
    stage = classifier.group(1).rsplit("/", 1)[-1]

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claim = re.search(r"^\*\*(\w+) reference implementation", readme, re.MULTILINE)
    assert claim, "README no longer states a maturity for the reference implementation"
    assert claim.group(1).lower() == stage.lower(), (
        f"README advertises a {claim.group(1)!r} reference implementation while "
        f"pyproject.toml classifies the package as {stage!r}"
    )


def test_readme_distinguishes_language_ir_version_from_package_release():
    """The IR compatibility label must not masquerade as a package release."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Nano IR/language v1.0.0" in readme
    assert "independently versioned installable package" in readme
    assert "`pyproject.toml` is authoritative for the package release" in readme


def test_the_trademark_notice_travels_with_the_distribution():
    """The nominative-use notice must live somewhere an installed wheel carries.

    `nano/library/README.md` is not packaged — `package-data` ships only
    `library/*/*.nano` and `library/*/*.json` — so a notice that lives only there
    is absent from every installed distribution, which is exactly where the
    strategy filenames and the `BollingerLowerReclaim` identifier show up with no
    context. Whichever file `pyproject.toml` declares as `readme` becomes the
    wheel's `METADATA` long description, so the sentence has to be in THAT file —
    an earlier version of this test hardcoded `README.md` and stayed green when
    the declaration was repointed elsewhere, which is the same "assertion
    narrower than the claim" defect it exists to catch.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^readme\s*=\s*"(.+)"', pyproject, re.MULTILINE)
    assert declared, "pyproject no longer declares a readme"
    shipped = ROOT / declared.group(1)
    assert shipped.exists(), f"pyproject ships {declared.group(1)!r}, which does not exist"

    text = shipped.read_text(encoding="utf-8")
    assert "nominatively" in text and "No affiliation" in text, (
        f"pyproject ships {declared.group(1)!r} as the distribution's long "
        "description, and the trademark nominative-use notice is not in it. "
        "Putting the notice in a file the wheel does not carry leaves every "
        "installed copy without it."
    )
