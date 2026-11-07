# Offline quantum host adapter

The [operations-v1 RFC](quantum-operations.md) is the semantic contract. The
separate `nano_qsearch.quantum_adapter` module supplies local circuit kernels,
sample verification, and explicit legacy migration. It has no submission API,
credentials, IBM runtime, scheduler, or provider receipt writer.

```python
from nano.quantum import build_plan, prepare_bits, sample
from nano_qsearch.quantum_adapter import lower_local, run_local, decode_verified

plan = build_plan([
    prepare_bits("100"),
    sample("prepare", expected_width=3, shots=2, max_circuits=1,
           max_wall_s=5, max_qpu_s=0),
])
circuits = lower_local(plan)
observations = run_local(circuits)
verified = decode_verified(plan, observations, {})
assert observations["pubs"][0]["samples"] == ["001", "001"]
assert verified.state == "verified"
assert verified.delivery_verdict == "not_applicable"
```

Known bits lower directly to X gates and measurements, with variable `i`
measured into bit `i` of the declared classical register. No QUBO, optimizer,
angles, ATLAS, or basis-preparation rewrite is involved. Qiskit `2.3.1` is
required and checked at runtime. The optional quantum extra also pins Aer
`0.17.2`; this ideal path uses Qiskit's `StatevectorSampler`.

Every boundary re-admits the typed plan. Execution rebuilds and compares the
circuit bundle so mutation cannot change its checked meaning. Local resource
preflight rejects excessive width, PUB count, and total shots before allocating
a statevector. Sampling runs in a child process with a hard deadline from the
declared wall limit; timeout terminates it and produces a typed diagnostic.
The complete result inventory must match admitted PUBs, registers, shots and
widths before decoding.

The decoder reverses display strings into variable order, checks every known
bit, splits payload/authentication segments by declared slot, checks the
supplied evidence bank's digest, and resolves the declared codec. Positive
authentication overhead requires an explicit host verifier that returns
`True`; an overhead width is not authentication evidence. Optimization verdicts
are recomputed from sample energy and the declared audit floor, separately from
packet decoding. These local results are unsealed conformance observations.

The generated 400-recipient, 1,200-PUB corpus is sampled on the ideal simulator
and compared through every `(recipient_id, chunk, slot, pub_index, record)`.
Negative controls cover missing/reordered PUBs, widths/registers, altered bits,
unknown codecs, wrong bank digests and failed authentication.

## Legacy bridge compatibility

`read_legacy` accepts existing `predator.qsearch.job.v1` and the packed
`predator.qsearch.request.v2` contract pinned to open PR #52 at
`b6ba6542b2c311d6354948e528bc64f147911178`. It preserves the original payload,
request descriptor, recipient mapping and compile metadata. It projects legacy
jobs as optimization operations; `purpose="qcomms"` never silently converts
them into known-bit preparation. There is no inspected `request.v1` schema.

Source admission and new-plan projection are separate. `LegacyImport` always
retains an admitted original payload; its `plan` is either a checked
operations-v1 plan or `None`. Callers must inspect `projection_diagnostics`
before attempting to lower a projection. Reading a historical record grants
no execution capability and does not change its target or bounds.

`to_legacy_job` lowers only an admitted `optimize_qubo` through an explicit
`LegacyCompileProfile` and the current `parse_job` validator. It refuses
preparations and effect changes. A local operation requires an explicit local
backend; neither `auto` nor a live target can turn it into hardware work.
The profile is a compatibility record, not #62/#63's prepared-artifact manifest
or receipt v3 identity. Those independently owned contracts remain unchanged.

The source reader admits PR52-only `ibm_kingston` and `ibm_marrakesh` targets,
while withholding their new-plan projection with `LEGACY_TARGET_UNSUPPORTED`.
It validates the pinned compile group without adding targets to the current
execution parser, substituting a backend, or rewriting a digest. Other unchanged
field groups reuse the existing strict legacy validators. The source-only
record remains private and is never passed to the current compiler.

Legacy zero-wall-limit records also remain readable with their exact original
zero. Their `plan` is `None` with `LEGACY_WALL_POLICY_REQUIRED`; zero is never
widened to one. Other valid legacy values that the new IR narrows, such as a
negative backend seed, produce a `LEGACY_PROJECTION_*` diagnostic while source
admission and exact round trips remain available. An explicit future migration
policy is required before these records can become new runnable proposals.
Existing execution parsers and operations-v1 remain unchanged. This adapter
does not merge or promote the #49/#51/#52 stack. Its pinned fixtures and derived
target variants test source provenance, exact payload preservation, strict
field types/digests, and malformed descriptor rejection.

Run `python -m pytest tests/test_quantum_contract.py tests/test_quantum_adapter.py -q`
after installing `.[dev,quantum]`. All work is local and requires no paid calls.
