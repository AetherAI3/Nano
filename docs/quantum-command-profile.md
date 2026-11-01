# Explicit quantum command profile

Select the domain explicitly. Market remains the default and its existing ATS
canonical fixtures are unchanged. Quantum commands use an independent
`nano.quantum.command.v1` document and the agreed
[`nano.quantum.operations.v1` contract](quantum-operations.md).

```python
from nano.intent import compile_intent

command = compile_intent(
    "simulate quantum plan demo-plan on local with 100 shots 1 circuit 0 qpu seconds",
    profile="quantum", quantum_catalog=caller_catalog,
)
document = command.to_dict()
```

`parse_intent`, `compile_intent`, `compile_to_dict`, and `explain_intent` accept
the same explicit profile/catalog keywords. Quantum dispatch happens before
market normalization, ticker binding, frame selection or policy. The quantum
parse result is `QuantumCommandIR`, rather than the market `IntentAST`.
Unknown profiles and mixed market/quantum catalogs fail explicitly.

The CLI reads only the supplied catalog file:

```text
nano intent compile "check quantum plan demo-plan" --profile quantum --catalog catalog.json --json
nano intent explain "inspect status job demo-job" --profile quantum --catalog catalog.json
```

## Closed command vocabulary

| Command | Permitted equivalent forms | Effect |
| --- | --- | --- |
| `check quantum plan NAME` | `validate quantum plan NAME`, `check plan NAME` | `pure` |
| `compile quantum plan NAME on BACKEND` | `build quantum plan NAME on BACKEND` | `local_compile` proposal |
| `simulate quantum plan NAME on BACKEND with N shots M circuits Q qpu seconds` | singular quantity units and optional `quantum` | `local_simulation` proposal |
| `inspect status job NAME` | `show status job NAME`, `show quantum job NAME status` | `read_supplied_state` |
| `explain receipt NAME` | `describe receipt NAME` | `read_supplied_state` |
| `prepare submission quantum plan NAME on BACKEND with N shots M circuits Q qpu seconds` | optional `quantum`, singular quantity units | `proposal_only` |

Check/compile may explicitly select `using evidence bank NAME`; this remains a
reference, not evidence content. Simulation requires positive integer shots and
circuits, and zero QPU seconds on a local backend. Submission preparation
requires a hardware backend and positive QPU seconds. Positive fractional QPU
seconds stay exact in the proposal; the integer-bound operations-v1 bridge
requires an explicit later host admission decision, never silent rounding.
No submission is performed by this profile or the local command adapter.

The bounded full-match grammar never executes a suggestion or ignores a suffix.
Negation, compound ambiguity, missing references/bounds, unknown resources and
unsupported Bell/GHZ/arbitrary circuits emit diagnostics with no steps or
effects. Suggestions are limited to three command words at one spelling edit.
Shots, circuit counts and QPU seconds carry native numeric types, units, raw
text and half-open spans into the **original source**. Backend/plan/bank/job/
receipt entities are separate from ticker entities.

## Identity, validation and replies

The caller supplies a strict `quantum-catalog/1` with `backends`, `plans`,
`banks`, `jobs`, and `receipts`. Aliases for the same canonical resource must
agree; supplied receipt/job/plan associations must be consistent. Catalog
names identify resources and supply observations. They grant no authority and
cause no resource fetch, evidence dereference or provider call.

`plan_digest` hashes semantic command arguments/effects and the referenced
catalog snapshot, including operation references and declared bounds. Source
text and spans are separate provenance under `source_hash`. Permitted
paraphrases share semantic identity and retain distinct source provenance.
Changed resource content changes identity. `validate_quantum_document` and
`QuantumCommandIR.from_dict` rebuild a proposed document from source/catalog
and compare every field with strict native types. A future model translator
receives the same validator; no model translator is required or enabled here.

`structured_reply` reports `proposed` for an action proposal. Status/receipt
explanation repeats only the caller-supplied observed state and canonical
plan/job/receipt references, labels `observed_from="caller_catalog"`, and sets
`attestation_verified=false`. Queued, cancelled, accounting-pending and verified
observations are distinct. Repeating a supplied verified state is not receipt
signature verification. No automatic model call is involved.

The separate host entry point `nano_qsearch.quantum_commands.run_local_command`
requires a supplied checked typed plan, its exact `operation_digest` in the
catalog, matching operation/preparation references, inherited catalog bounds,
and an explicit backend-ID binding to `local_ideal`. Selected evidence-bank
identity/digest must agree with typed packets. Requested simulation quantities
must match the resolved plan. It lowers through the
[bounded local adapter](quantum-local-adapter.md); check remains a checked
proposal, actual circuit compilation can report `compiled`, and actual ideal
samples verified by the decoder can report `verified` with the typed-plan
reference. It creates no provider receipt or #62/#63 prepared-artifact identity.

## Frozen conformance and reproducible evidence

The [corpus](../nano/intent/fixtures/quantum_commands_v1.json) was frozen before
parser implementation: 64 cases, 22 supported and 42 expected abstentions. It
contains paraphrases, exact arguments/capabilities, source spans, diagnostics,
negation, ambiguity, unsupported kernels and near-collision market vocabulary.
A metadata-only correction normalizes ATS hashing to LF for platform parity;
all 64 case objects remain unchanged. ATS fixture blob SHA-256:
`6a2b3824af76f791d107a3000cec6e0dbc668eb6df7ee30cbbfe66b913d62eb4`.

```text
python -m pytest tests/test_quantum_intent.py tests/test_quantum_command_host.py tests/test_intent.py tests/test_intent_cli.py -q
python scripts/evaluate_quantum_profile.py --output quantum-evaluation.json
python examples/quantum_command_demo.py
```

The evaluator reports supported-only and complete-corpus exact argument/
capability accuracy, abstention and diagnostic accuracy, zero market/ticker
emissions, and measured compile p50/p95/max latency. These are local corpus
measurements, not a claim of open-ended natural-language accuracy or provider
performance. The demo records phrase -> typed operations -> actual ideal
samples -> structured reply using no model, credentials or IBM.

The profile uses its own strict boundary checks while #56/#57 and their
separate market/runtime fixes remain independent work. Bridge compatibility is
pinned to open #52, not assumed merged. Integration order is the RFC, offline
adapter, then this profile. Live execution still depends on independently
reviewed compile profiles and receipt provenance.
