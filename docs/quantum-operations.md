# RFC: typed quantum operations v1

Status: **offline language contract and conformance fixtures** for [#61](https://github.com/AetherAI3/Nano/issues/61).
This first stage defines records and admission rules. It adds neither a source
grammar, a provider executor, nor a replacement for existing signed receipts.
Circuit kernels and compatibility adapters belong in subsequent focused changes.

## Problem and boundary

The existing `predator.qsearch.job.v1` record represents QUBO optimization. Its
`p >= 1` and angle vectors are meaningful for QAOA. They are inappropriate for
encoding declared known bits. A `purpose="qcomms"` label does not change the
operation or prove packet delivery.

The model executes on classical compute and proposes a record. A host checks
policy, resolves external data, owns credentials, schedules work, and decides
whether to submit a circuit. IBM samples submitted circuits. A deterministic
decoder checks samples and resolves admitted records. This contract makes no
claim about running an LLM on IBM, transporting weights, encryption, or quantum
advantage.

## Record and builder

The exact envelope is:

```json
{"schema":"nano.quantum.operations.v1","operations":["typed operation objects"]}
```

The string above illustrates placement; actual operations must be objects.
[`operations-v1.schema.json`](../nano/quantum/schemas/operations-v1.schema.json)
defines structural validation. `nano.quantum.parse_plan` additionally enforces
cross-reference, dimension, ordering, uniqueness and budget invariants. Schema
validation alone never admits a plan. Unknown keys and operations fail closed.
Integer fields reject booleans and floats; numeric coefficients must be finite;
names must be nonempty, trimmed single-line UTF-8 text. JSON Schema's mathematical
integer type can accept a decoded `2.0`; the strict parser deliberately rejects
native float values for all integer fields. The stdlib implementation imports no
Qiskit runtime and performs no ambient I/O.

```python
from nano.quantum import build_plan, prepare_bits, sample

plan = build_plan([
    prepare_bits("100"),  # variable order x0, x1, x2; no QUBO or angles
    sample("prepare", expected_width=3, shots=2, max_circuits=1,
           max_wall_s=5, max_qpu_s=0),
])
assert plan.to_payload()["schema"] == "nano.quantum.operations.v1"
```

Builders produce ordinary JSON operation objects. `build_plan` and `parse_plan`
both validate the whole proposal before constructing immutable typed records.
`to_payload()` returns a detached copy. The `plan_digest` is the existing Nano
canonical SHA-256 digest over the exact envelope, with no embedded self digest.
Source text and spans belong to the frontend's separate provenance record; they
must not be added to this semantic envelope. Equivalent frontend phrases can
therefore select the same plan while retaining different source provenance.

Public dataclass construction is not admission. A host adapter must call
`checked_plan` before use; this reparses the payload and compares the typed fields
and digest. The result-shape validator does so itself.

## Operations

| Operation | Fields beyond `id`, `op` | Meaning |
| --- | --- | --- |
| `prepare_bits` | `variables`, `bits`, `packets` | Prepare declared classical bits in variable order. No objective, QAOA parameters, superposition or search is implied. |
| `sample` | `preparation_id`, `backend`, `shots`, `limits`, `seed`, `result` | Sample an earlier admitted preparation, with explicit limits and exact expected PUB shape. |
| `optimize_qubo` | `variables`, `qubo`, `method`, `audit`, `backend`, `shots`, `limits`, `seed`, `result` | Propose a bounded QAOA method against its declared objective and independent energy audit. |

IDs are unique across the envelope. Variables are a nonempty ordered unique
list. A preparation has exactly one binary digit per variable. A sample cannot
refer forward, refer to itself or refer to an optimizer. There is no implicit
Bell, GHZ, arbitrary-circuit or superposition operation.

`limits` contains positive integer `max_circuits` and `max_wall_s`, plus
nonnegative integer `max_qpu_s`. Each execution operation's circuit cap must
bound the complete envelope's PUB count. The reserved backend `local_ideal`
requires zero QPU seconds. Other backend strings identify resources for a host
catalog; they grant no authority and this parser does not resolve them.
Shot counts and seeds are explicit positive/nonnegative integers, respectively.

`result` contains `pub_index`, `register`, `width`, and `bit_order="msb_first"`.
Execution operations allocate contiguous PUB indices in operation order.
Preparation width, objective width and expected result width must agree.

An optimizer's `qubo` contains a finite square, exactly symmetric `q`, finite
`offset`, and `convention="symmetric_half"`. Its `method` contains `name="qaoa"`,
`variant` (`ordinary`, `joint5` or `auto`), positive `p`, and exactly `p` finite
`gammas` and `betas`. `audit.classical_min_energy` is a declared finite audit
reference, not a parser proof that the value is globally optimal. Host checks
and actual audit evidence remain necessary. `auto` means the bridge's bounded
compile variants; it does not invent another optimization algorithm.

## Packets, evidence and typed observations

Each preparation may carry packets. A packet has exactly:

```text
recipient_id, chunk, slot,
evidence_bank: {reference, digest}, codec: {name, version},
payload_width, authentication_width, bit_significance, expected_records
```

Packet slots are contiguous in list order; that order defines consecutive
segments of the preparation's variable-order bits. Each segment has useful
payload bits followed by its declared authentication-overhead bits. Segment
widths must cover the preparation exactly. An empty packet list is allowed for
known-bit preparation without a record-delivery claim. Recipient/chunk pairs
are unique across preparations; distinct chunks for one recipient are allowed.

V1 codecs are `raw_bits` and `evidence_index`, both version `1`. Every packet
expects one record. `bit_significance` explicitly declares `lsb_first` or
`msb_first` for interpretation **inside its payload segment**. Authentication
overhead is accounting metadata; declaring its width proves neither an
authentication algorithm nor successful verification.

An evidence pointer selects external data. The data must separately be supplied
by the caller, and `check_evidence_bank(packet, catalog)` recomputes its canonical
JSON digest before returning a detached copy. It never fetches a URL, reads a
path or follows instructions from an evidence record. A syntactically valid
digest is not evidence that matching content exists.

`PubResult` and `QuantumResult` represent local fixture observations under
`nano.quantum.fixture-result.v1`; their
[structural schema](../nano/quantum/schemas/fixture-result-v1.schema.json) is
separate from provider receipts. `parse_result` checks the plan digest, complete
ordered PUB inventory, operation IDs, register, display convention, exact shot
count and bit width. Missing, extra, duplicated or reordered PUBs fail.

Optimization and delivery verdicts are separate fields with values
`not_applicable`, `unverified`, `pass`, or `fail`. They are observations supplied
by the relevant auditor/decoder. Shape validation does **not** prove either
verdict. In particular, a passing energy audit never establishes packet delivery;
successful decoding never establishes optimality. This record is not a sealed
receipt, a cost statement or hardware provenance.

## Ordering and compatibility

Logical `bits[i]` means the value of `variables[i]`, measured into classical bit
`i` of the named register. The expected display string puts bit `n-1` first, so
known variable-order bits `100` correspond to display sample `001`. Decode that
display string into variable order before splitting packet segments and applying
each packet's bit significance. The conformance fixtures intentionally use
asymmetric strings.

IBM documents [bit ordering](https://quantum.cloud.ibm.com/docs/en/guides/bit-ordering)
and [Sampler inputs and outputs](https://quantum.cloud.ibm.com/docs/en/guides/sampler-input-output).
Sampler outputs contain one result per input PUB, with data grouped by classical
register. This RFC fixes a single named register per operation and explicit
ordered shot strings; it does not admit parameter-broadcast shapes implicitly.
The repository's optional quantum dependencies pin Qiskit `2.3.1` and Aer
`0.17.2`. New executable compatibility tests must assert those versions and
exercise the actual local circuit; the first-stage pure contract tests make no
claim that an SDK or provider was run.

The bridge baseline is main `d740bb530262c1c76a2b18c8a8aa47476f8b6b96`.
Track [PR #52](https://github.com/AetherAI3/Nano/pull/52) at
`b6ba6542b2c311d6354948e528bc64f147911178` or its reviewed successor when adding
packed adapters. Its `predator.qsearch.request.v2` maps explicit
`pub_index`/`agent_id`/`chunk`/`slot` records, defaults to 16 PUBs, and admits up to
4096 only through an explicit host configuration. Our 1200-PUB fixture requires
that explicit cap. Main does not yet contain those request records.

Keep all legacy job/request and signed-receipt parsers readable. An optimization
adapter may map a declared `optimize_qubo` to the existing QUBO job contract.
It must bind compile profile and source/destination digests explicitly. A
preparation adapter must not fabricate a diagonal QUBO or unused QAOA angles to
fit the old API. Main cannot represent that operation honestly yet. The PR52
basis rewrite remains a legacy optimization rewrite, not a new language
primitive. Known-bit/packed bridge kernels therefore require a separate small
implementation change; no live path is promoted by this RFC.

## Effects and migration stages

| Stage | Effect | Authority |
| --- | --- | --- |
| Build, validate, resolve supplied evidence | `pure` | Caller-provided immutable data only |
| Lower a checked proposal | `local_compile` | Separate explicit adapter/profile |
| Ideal or fixture samples | `local_simulation` | Explicit local adapter; never a QPU submission |
| Prepare a submission proposal | `proposal_only` | No scheduling, spending or credentials |
| Submit, cancel, retrieve provider state | Host-owned external effect | Separate host policy and provenance |

Migrate by selecting the new schema explicitly; never reinterpret legacy
`purpose="qcomms"`. Keep current `x0_first` local receipts and `msb_first` hardware
samples distinct. Existing domain-separated hardware seals retain their exact
meaning. A local observation cannot be relabeled as hardware evidence. Live
promotion depends on explicit compile profiles, receipt provenance and host
gates; an identifier or successful offline parse cannot bypass those gates.

The staged work is: (1) this RFC/schema/conformance contract; (2) explicit local
known-bit kernels and compatibility adapters with round trips; (3) independent
review of any provider-facing changes. A new source grammar or receipt format
requires its own proposal. Existing Intent market defaults and ATS fixtures are
outside this contract and remain byte-stable.

## Conformance evidence

Run `python -m pytest tests/test_quantum_contract.py -q`. Frozen JSON fixtures
cover known bits, one record, packed records and genuine QUBO optimization.
Mutation cases cover structural/type/width/reference/budget/codec/digest failures
and missing/extra PUBs. The compact `large-mapping.json` pins a deterministic
400-recipient, three-chunk, 1200-PUB generator and both semantic-plan and mapping
digests. The test checks every recipient/chunk/slot/PUB association.

These checks establish contract admission and expected ordering. They do not
execute circuits, verify delivery, contact IBM or spend money. Subsequent local
kernel evidence must demonstrate builder → IR → actual local circuit → fixture
samples → verified decoded records while preserving this frozen contract.
