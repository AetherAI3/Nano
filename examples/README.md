# examples/

Runnable, host-integration Python scripts — distinct from
[`nano/examples/`](../nano/examples/), which is the `.nano`/IR conformance
corpus (the language test suite). Nothing here is part of the language
contract; these are demonstrations of how a host application wires Nano's
pieces together.

## provenance_signing_demo.py

Wraps a toy risk engine with [`ProvenanceGate`](../nano/bridge/provenance.py)
so every decision-gate decision — approved or rejected — is signed via
[Protocol-C](https://github.com/AetherAI3/PROTOCOL-C) into an append-only,
independently verifiable audit log, with zero changes to the strategy or the
decision itself.

Requires the optional dependency:

```bash
pip install git+https://github.com/AetherAI3/PROTOCOL-C.git   # not yet on PyPI
```

Run from the repo root (`pip install -e .` first, or `PYTHONPATH=.`):

```bash
python examples/provenance_signing_demo.py
```

See [`nano/bridge/provenance.py`](../nano/bridge/provenance.py) and the
[Provenance paper](../docs/papers/04-provenance.md) for how a production deployment wires this in.
