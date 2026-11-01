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

`to_legacy_job` lowers only an admitted `optimize_qubo` through an explicit
`LegacyCompileProfile` and the current `parse_job` validator. It refuses
preparations and effect changes. A local operation requires an explicit local
backend; neither `auto` nor a live target can turn it into hardware work.
The profile is a compatibility record, not #62/#63's prepared-artifact manifest
or receipt v3 identity. Those independently owned contracts remain unchanged.

The current main parser does not admit PR52-only `ibm_kingston` or
`ibm_marrakesh` jobs. Legacy zero-wall-limit records also cannot project into
operations-v1's positive wall bound. Both fail explicitly; existing legacy
parsers and original payloads remain unchanged. This adapter does not merge or
promote the #49/#51/#52 stack. Its pinned fixtures preserve source provenance
and test valid exact round trips and malformed descriptor rejection.

Run `python -m pytest tests/test_quantum_contract.py tests/test_quantum_adapter.py -q`
after installing `.[dev,quantum]`. All work is local and requires no paid calls.
