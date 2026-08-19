# Your first contribution

This walks the whole path once, end to end, with a rule that is actually in the
library. Nothing here requires knowing how the compiler works.

The path is: **idea → `.nano` source → generated IR → deterministic replay → CI → pull request.**

Fifteen minutes, most of it spent writing the comment header.

## Before you start

```bash
git clone https://github.com/AetherAI3/Nano.git
cd Nano
python -m venv .venv

# macOS/Linux: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1

python -m pip install -e ".[dev]"
python -m pytest -q
```

Python 3.10+. The suite runs in a few seconds; run it often.

## 1. The idea

Pick something you can state as one threshold against one number.

> When gross exposure reaches three times equity, propose that the book pause.

That is `nano/library/risk/leverage_ceiling.nano`, and it is a good shape for a
first contribution: one signal, one comparison, one intent, no ambiguity about
when it should fire.

Nano's grammar is small on purpose. A library entry stays inside the v0.1.0
subset — one `every` block, one `if` rule, AND-chained numeric conditions —
because that is what keeps the checked-in IR byte-stable across versions. If
your idea needs `else`, arithmetic, or `or`, it is a great
[language proposal](https://github.com/AetherAI3/Nano/issues/new?template=language-change.yml)
but not a first library entry.

## 2. Name the signal, and decide who computes it

This is the step people skip, and it is the whole design.

Nano does not fetch anything. Your rule compares a number the **host** supplies.
So before writing a line of source, answer: what is the number called, what
range is it in, and who is responsible for producing it?

```text
GROSS_LEVERAGE — gross exposure divided by equity. Nonnegative, rises as
                 the book gets riskier. The host owns the definition of
                 "gross" and publishes the value each 5m tick.
```

Two conventions matter here:

- **Direction.** In `risk/` and `watchdog/`, every series is nonnegative and
  rises as the situation worsens, so every control is a `>=` against a ceiling.
  A stack of them then reads the same way top to bottom. If your natural
  measurement falls as things get worse, the host publishes the complement.
- **No negative literals.** Baseline IR cannot carry one. Signals with a
  negative natural range are shifted or negated by the feed — `WILLR_POS`,
  `ZSCORE_NEG` — and the header documents the transform.

If the signal already appears in the
[library README tables](../nano/library/README.md#signal-conventions), that
table is its definition and you do not need to redefine it. If it is new, it
gets defined in your header.

## 3. Write the source

Create `nano/library/risk/leverage_ceiling.nano`:

```nano
// Leverage ceiling: pause when gross exposure reaches 3x equity.
// REGIME: all of them. Leverage is a constraint, not a forecast.
// CONDITIONS: none. Armed whenever the book is open.
// INVALIDATION: none. It is reset by reducing size, deliberately, outside this
// rule.
// SHAPE: 5m; gross exposure moves on fills and on marks, and 5m is fast enough
// to catch both without re-checking a number that has not changed.
// NOT position_concentration_cap: that measures how the book is distributed,
// this measures how large it is in total. A perfectly diversified book at 5x
// gross passes the concentration cap and belongs to this rule.
// CALIBRATED ON: nothing instrument-specific, but 3x is a house figure rather
// than a universal one - a futures book and a cash equity book do not mean the
// same thing by "gross".
strategy LeverageCeiling {

    agent RiskDesk

    every 5m {

        if GROSS_LEVERAGE >= 3 {

            pause()

        }

    }

}
```

The header is longer than the program. That is normal and it is the point: the
source says what the rule computes, the header says when it is wrong. The five
required fields and the `NOT` convention are documented in
[the library README](../nano/library/README.md#the-comment-header).

`agent RiskDesk` names who the intent escalates to. It is a label in the IR, not
a process — Nano cannot notify anyone.

## 4. Generate the IR and check everything

```bash
python scripts/check_contribution.py --write nano/library/risk/leverage_ceiling.nano
```

That writes `leverage_ceiling_ir.json` in the library's exact format — scalars
one per line, `effects` inline, one node per line, so a strategy reads as a list
of steps in a diff. Do not hand-format it, and do not use `nano compile`'s
output directly; its default JSON indentation is correct IR in the wrong shape.

Run it again without `--write` and it checks, rather than writes:

```console
$ python scripts/check_contribution.py nano/library/risk/leverage_ceiling.nano
1 entry ready for review.
```

It verifies the pair compiles to the pinned IR, that the IR round-trips, that
two runs over one frame produce identical results, that every signal is
documented somewhere a host implementer will look, that the header carries all
five fields, and that the entry declares no effects beyond `intent.emit` and
`log.append`.

That last one is the boundary. A library entry proposes intents and writes its
own run log. It cannot place an order, call an API, read a clock, or reach the
network — and nothing in the grammar lets it.

## 5. Run the suite

```bash
python -m pytest tests/test_library.py -q
```

The library tests discover your pair automatically. You do **not** need to write
a test for a normal entry.

Add a focused fire/no-fire test only when your rule covers a runtime edge the
suite does not already exercise — a new intent action in a category, a boundary
that has to be exclusive, twin rules that must never both fire. When you do,
pair every no-fire assertion with a positive control on the same frame:

```python
def test_credential_age_alert_observes_rather_than_pausing():
    graph = _load("watchdog/credential_age_alert.nano")
    frame = MarketFrame(
        timestamps=(0, 86400, 172800),
        signals={"CREDENTIAL_AGE_DAYS": (79.0, 80.0, 91.0)},  # 79 is NOT >= 80
    )
    result = execute(graph, frame)
    assert [(i.action, i.timestamp) for i in result.intents] == [
        ("OBSERVE", 86400),
        ("OBSERVE", 172800),
    ]
```

A test that only asserts "nothing fired" passes just as well against a rule that
never fires at all.

## 6. Open the pull request

```bash
git checkout -b feat/leverage-ceiling
git add nano/library/risk/leverage_ceiling.nano nano/library/risk/leverage_ceiling_ir.json
git commit -m "feat(library): leverage ceiling risk control"
git push -u origin feat/leverage-ceiling
```

Fill in the template. The validation and boundary checkboxes are the ones a
reviewer reads first — if you could not run something, say so rather than
ticking it.

CI runs the full suite on Python 3.10 through 3.13, builds the wheel, verifies
your files are packaged in it, and runs `scripts/check_contribution.py --all`.
Everything CI runs, you can run locally first.

## When it is not a normal strategy

| You want to… | Go to… |
| --- | --- |
| Add a rule reading system or policy state, not markets | The [`watchdog/` category](../nano/library/README.md#watchdog-signals-watchdog) — same path, different signal table |
| Understand how a watchdog is admitted, evaluated, and receipted at runtime | [The Watchdog profile](watchdog_profile.md) — the runtime side; you do not need it to contribute an entry |
| Propose a category that does not exist | Open a [strategy proposal](https://github.com/AetherAI3/Nano/issues/new?template=strategy-library.yml) first; a new category also updates `EXPECTED_CATEGORIES` in `tests/test_library.py` and the README table |
| Change the grammar, IR, or runtime | A [language proposal](https://github.com/AetherAI3/Nano/issues/new?template=language-change.yml) before any implementation |
| Report something reproducibly broken | A [bug report](https://github.com/AetherAI3/Nano/issues/new?template=bug-report.yml) |
| Report something security-sensitive | [SECURITY.md](../SECURITY.md), privately, not a public issue |

## Two things that will not be merged

Both follow from the same design, so they are worth stating plainly.

**Anything that lets a rule act.** Nano emits intents; the host's
`DecisionGate` decides. No ambient I/O, no external actuation, no clock or RNG
dependency, no path around the gate. This is not a roadmap item.

**Proprietary strategy code, transcribed.** Translate only an idea you can
describe cleanly. If you consulted public material, record it truthfully in one
non-empty `SOURCE:` line; if provenance is unknown, omit the line rather than
inventing a citation. Do not paste someone's implementation.
