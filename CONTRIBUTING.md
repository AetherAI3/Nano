# Contributing to Nano

Welcome. Nano is an alpha reference implementation, and its best contributions make the small language more useful without making its contract less clear. Quant researchers, application engineers, and documentation contributors all have a meaningful place here.

## Start here

| If you want toâ€¦ | Start withâ€¦ |
| --- | --- |
| Translate a familiar trading idea | [Add a strategy](#add-a-strategy) |
| Suggest grammar, IR, or runtime behavior | [Open a language proposal](https://github.com/DBarr3/Nano/issues/new?template=language-change.yml) |
| Report a reproducible defect | [Open a bug report](https://github.com/DBarr3/Nano/issues/new?template=bug-report.yml) |
| Improve an explanation or correct a claim | A focused documentation issue or pull request |

For security-sensitive reports, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Development setup

```bash
git clone https://github.com/DBarr3/Nano.git
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

1. Open a [strategy proposal](https://github.com/DBarr3/Nano/issues/new?template=strategy-library.yml) for a new idea or use an existing issue.
2. Choose one existing category in `nano/library/`, then add `<slug>.nano` and `<slug>_ir.json`.
3. Document the signal contract in `//` comments: formula or data source, unit/range, any normalization, and the lookback convention the host must supply.
4. Generate the expected IR with `compile_to_dict()` and keep it formatted like neighboring entries.
5. Run `python -m pytest tests/test_library.py -q`, then the full suite before opening the pull request.

Library entries are deliberately written in the v0.1.0 subset — one `every` block, one `if` rule, AND-chained numeric conditions — so they compile to byte-stable baseline IR. The v1.0 grammar allows more (multiple rules, `else`, arithmetic, `or`/`not`, declarations, computed indicators); reaching for it moves the entry to v1.0 IR, which is fine for `nano/examples/` but changes the library's pinned fixtures. The host supplies every signal and still owns every real-world action. See the [language reference](docs/language.md).

For a normal strategy addition, the library tests automatically discover the source/IR pair and verify compilation, validation, and replay. Add a focused fire/no-fire test only when the new example covers a meaningful runtime edge not already represented.

## Propose a language change

Start with the [language proposal form](https://github.com/DBarr3/Nano/issues/new?template=language-change.yml) before writing an implementation. It asks for the problem, proposed syntax, IR impact, deterministic/replay semantics, host-gate impact, and migration story.

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

- [Quick-start demo](examples/momentum_demo.py)
- [Strategy library](nano/library/README.md)
- [Language reference](docs/language.md)
- [Architecture](docs/architecture.md)
- [Status and maturity](docs/status.md)
- [Security policy](SECURITY.md)
