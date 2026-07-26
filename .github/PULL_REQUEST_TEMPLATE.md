## Why does this change belong in Nano?

<!-- Describe the user problem, design rationale, or strategy thesis. -->

## What changed?

<!-- Summarize the implementation and documentation changes. -->

## Validation

- [ ] Focused tests passed.
- [ ] `python -m pytest -q` passed.
- [ ] Documentation and examples match the shipped behavior.

## Boundary check

- [ ] The change preserves deterministic reference execution for identical graph/frame inputs.
- [ ] The change does not bypass the host-owned `DecisionGate` or add external actuation to Nano source/runtime.
- [ ] If this adds a strategy, it includes paired `.nano` and `_ir.json` files plus documented host signal conventions.
