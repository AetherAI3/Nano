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
