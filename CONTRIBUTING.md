# Contributing to Nano

Welcome. Nano is an alpha reference implementation, and its best contributions make the small language more useful without making its contract less clear. Quant researchers, application engineers, and documentation contributors all have a meaningful place here.

## Start here

**New here? [Walk through a first contribution](docs/first-contribution.md)** —
the whole path once, end to end, with a real rule. Fifteen minutes, no compiler
knowledge required.

| If you want to… | Start with… |
| --- | --- |
| Translate a familiar trading idea | [Add a strategy](#add-a-strategy) |
| Write a deterministic control over system or policy state | [Add a watchdog rule](#add-a-watchdog-rule) |
| Suggest grammar, IR, or runtime behavior | [Open a language proposal](https://github.com/AetherAI3/Nano/issues/new?template=language-change.yml) |
| Report a reproducible defect | [Open a bug report](https://github.com/AetherAI3/Nano/issues/new?template=bug-report.yml) |
| Improve an explanation or correct a claim | A focused documentation issue or pull request |

For security-sensitive reports, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Development setup

```bash
git clone https://github.com/AetherAI3/Nano.git
cd Nano
python -m venv .venv
```

Activate the environment:

```bash
# macOS/Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Then install and test:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

Python 3.10+ is required. The reference suite is intentionally fast; run it often.

## Add a strategy

The [strategy library](nano/library/README.md) is the easiest way to learn and extend Nano. Every strategy is a paired artifact: readable `.nano` source plus checked-in IR that the suite must reproduce exactly.

1. Open a [strategy proposal](https://github.com/AetherAI3/Nano/issues/new?template=strategy-library.yml) for a new idea or use an existing issue.
2. Choose one existing category in `nano/library/`, then add `<slug>.nano`.
3. Write the [comment header](nano/library/README.md#the-comment-header). Five required fields — `REGIME:`, `CONDITIONS:`, `INVALIDATION:`, `SHAPE:`, `CALIBRATED ON:` — plus a `NOT <neighbour>:` line, plus a definition for any signal you introduce: formula or data source, unit/range, normalization, and the lookback convention the host must supply.
4. Generate the IR partner and check the whole entry with one command:

   ```bash
   python scripts/check_contribution.py --write nano/library/<category>/<slug>.nano
   ```

   `--write` emits the IR in the library's format, so nothing needs hand-reflowing to match its neighbours. Do not paste `nano compile` output into the fixture — it is correct IR in the wrong shape. Re-run without `--write` to confirm the entry is clean.
5. Run `python -m pytest tests/test_library.py -q`, then the full suite before opening the pull request.

CI runs `python scripts/check_contribution.py --all` on every pull request, so a
clean local run is a clean CI run.

Library entries are deliberately written in the v0.1.0 subset — one `every` block, one `if` rule, AND-chained numeric conditions — so they compile to byte-stable baseline IR. The v1.0 grammar allows more (multiple rules, `else`, arithmetic, `or`/`not`, declarations, computed indicators); reaching for it moves the entry to v1.0 IR, which is fine for `nano/examples/` but changes the library's pinned fixtures. The host supplies every signal and still owns every real-world action. See the [language reference](docs/language.md).

For a normal strategy addition, the library tests automatically discover the source/IR pair and verify compilation, validation, and replay. Add a focused fire/no-fire test only when the new example covers a meaningful runtime edge not already represented.

## Add a watchdog rule

`nano/library/watchdog/` holds deterministic controls over **host-measured
system and policy state** rather than markets — trusted-route availability,
credential age, and the other measurable parts of a security or compliance
control. The path is identical to [Add a strategy](#add-a-strategy); only the
signal table differs.

Two category rules, both enforced by `tests/test_library.py`:

- **`pause` and `observe` only.** A watchdog is a control. A rule proposing a
  direction is a trading rule and belongs in another category.
- **Every signal is nonnegative and rises as the situation worsens**, so a
  control is always a `>=` against a ceiling — the same convention as `risk/`,
  which is what lets a stack of them read the same way top to bottom. If your
  natural measurement falls as things get worse, the host publishes the
  complement and your header documents the transform.

Nano evaluates the threshold and returns a proposed intent. It does not inspect
a network, read a key store, or enforce anything; the host measures the state,
supplies the number, and decides what to do with the proposal. See
[deterministic watchdogs and compliance controls](README.md#deterministic-watchdogs-and-compliance-controls).

The library category is the corpus; [`nano/watchdog/`](docs/watchdog_profile.md)
is the runtime side of the same idea, admitting a rule under a narrower contract
and issuing a receipt for each evaluation. Every library entry is admissible
under that profile and a test pins it — but contributing one asks nothing extra
of you. Follow the two rules above and the entry qualifies; the signal contract
belongs to the host deploying the rule.

## Attribution and provenance

Authorship is already recorded by git and by the pull request, so the library
has no author field to fill in and no hand-maintained contributors list to fall
out of date.

What an entry *does* record is where its **idea** came from. `SOURCE:` is an
optional comment-header field for exactly that:

```text
// SOURCE: Donchian channel breakout, as described publicly in trend-following
// literature. Translated to Nano; not derived from any proprietary code.
```

Use it whenever you are translating a publicly described idea. Leave it out when
the rule is your own work. Do not transcribe proprietary strategy code — bring
the publicly described idea across cleanly and say where it came from.

## Propose a language change

Start with the [language proposal form](https://github.com/AetherAI3/Nano/issues/new?template=language-change.yml) before writing an implementation. It asks for the problem, proposed syntax, IR impact, deterministic/replay semantics, host-gate impact, and migration story.

This discipline matters: Nano's boundary is intentional. Do not add ambient I/O, an external-actuation primitive, a clock/RNG dependency, or a behavior that lets source bypass the host `DecisionGate`.

## Ground rules

1. **Determinism is non-negotiable.** The reference compiler and runtime do not read an ambient clock, RNG, network, or mutable global state.
2. **Programs propose; gates decide.** Nano emits intents. The host owns policy, persistence, and any external effect.
3. **Tests travel with behavior.** New behavior, examples, and bug fixes include focused coverage; a library pair must compile to its expected IR and replay deterministically.
4. **Documentation is part of the contract.** Label shipped behavior, experiments, and research honestly. Do not promote a claim past its evidence.
5. **Keep changes reviewable.** Prefer one clear reason per pull request and leave a concise record of why the change belongs in Nano.

## Pull requests

- Branch from `main`; keep pull requests focused and small.
- Use concise commit subjects such as `feat: add strategy fixture` or `docs: clarify signal contract`.
- Complete the pull-request template, including validation and the relevant boundary checks.
- For a strategy, include the source, expected IR, signal comments, and test result together.

## Useful references

- [Your first contribution](docs/first-contribution.md)
- [Quick-start demo](examples/momentum_demo.py)
- [Strategy library](nano/library/README.md)
- [Language reference](docs/language.md)
- [Architecture](docs/architecture.md)
- [Status and maturity](docs/status.md)
- [Security policy](SECURITY.md)
