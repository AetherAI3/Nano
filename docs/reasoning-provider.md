# Reasoning-provider response contract

The host injects `ReasoningProvider` into `run_module`; Nano never selects a
model, loads credentials or opens a connection. A provider can call a local
model or replay a recorded response. Live inference is not deterministic;
replay requires the host to record and supply the same responses.

Before a response can affect a rule, the VM checks the complete returned record
against the signature's output declarations. Every declared output must be
present, no undeclared fields are accepted, and `None` is not an optional value.
Provider metadata belongs in the host's separate record, not in this mapping.

| Declared output | Accepted value |
| --- | --- |
| `float` | A finite built-in float or an integer that can widen to a finite float; never a boolean or numeric string |
| `int` | An exact built-in integer within the existing IR integer-magnitude bound; never a boolean or float |
| `bool` | An exact built-in boolean |
| `confidence` | A finite number in `[0, 1]`, with the same type rules as `float` |

An explicit numeric `range [low, high]` is inclusive and is checked in addition
to the type's own rules. The VM does not clamp, repair or ask a model to rewrite
an invalid response. Accepted numeric values retain their original representation
so existing valid recorded runs keep their replay bytes.

This VM currently consumes numeric and boolean output cells. Although source
annotations can express `string`, `duration` and `series<T>`, reasoning outputs
of those types are not implemented here. They produce
`infer.invalid` / `unsupported_output_type` before invoking the provider.
This change does not define vector, text or duration response semantics.

Invalid records are rejected atomically: an otherwise valid `score` cannot be
used when another declared field is missing or malformed. That inference result
is absent for the affected bar, so neither branch of a rule depending on it is
selected. Unrelated rules and valid responses at other bars continue normally.
A route whose guard is absent still takes its explicitly declared escalation
fallback; it does not log its execute label as approved.

The ordered log distinguishes:

- `infer.skipped`: no provider was supplied;
- `infer.invalid`: unsupported output declaration or malformed response;
- `infer.failed`: the injected provider raised an ordinary exception;
- `infer.called`: a response passed the output contract.

Invalid/failed diagnostics name the signature and a bounded reason or declared
output position. They do not include response values, undeclared field names or
exception messages. `KeyboardInterrupt` and other `BaseException` cancellation
signals still propagate. The existing `condition.unwarmed` event denotes an
absent condition generally; preceding `infer.*` events distinguish an invalid
provider response from indicator warm-up.

The host still owns admission, inference spend, deadlines, transcript storage
and every external effect. These checks validate a model's output shape and
bounds; they do not establish that its answer is correct or well calibrated.
