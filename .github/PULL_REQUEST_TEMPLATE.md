## Why does this change belong in Nano?

<!-- Describe the user problem, design rationale, or strategy thesis. -->

## What changed?

<!-- Summarize the implementation and documentation changes. -->

## Validation

- [ ] Focused tests passed.
- [ ] `python -m pytest -q` passed.
- [ ] If this adds or changes a library entry: `python scripts/check_contribution.py --all` passed.
- [ ] Documentation and examples match the shipped behavior.

<!--
If you could not run something, say so here instead of ticking the box. An
honest "not run, docs only" costs a reviewer nothing; an unchecked box costs
them a round trip.
-->


## Boundary check

- [ ] The change preserves deterministic reference execution for identical graph/frame inputs.
- [ ] The change does not bypass the host-owned `DecisionGate` or add external actuation to Nano source/runtime.
- [ ] If this adds a strategy, it includes paired `.nano` and `_ir.json` files plus documented host signal conventions.
- [ ] If this adds a watchdog rule, it emits `pause`/`observe` only and its signals are nonnegative and rise as the situation worsens.
