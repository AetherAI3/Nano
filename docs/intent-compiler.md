# Nano Intent Compiler

Nano has two deterministic frontends with different contracts. The strategy
compiler parses the strict `.nano` language into `StrategyGraph`/`NanoModule`
artifacts governed by `nanoIrVersion`. The Intent compiler parses short human
requests into host-neutral command proposals governed by
`nanoIntentVersion`. They share Nano's principles, but not tokens, grammar, AST,
IR, effects, or version numbers.

```text
.nano source -> nano.compiler -> Strategy IR -> runtime proposes strategy intents
human text   -> nano.intent   -> Intent IR   -> host decides UI/data/model/action
```

Natural-language vocabulary therefore never enters `nano/compiler/lexer.py` or
the strategy recursive-descent parser. An Intent plan never becomes a
`StrategyGraph` or `NanoModule`. Existing strategy source, its lowest-capable IR
emission, runtime behavior, receipts, and replay remain unchanged.

## Deterministic pipeline

`nano.intent` performs a bounded sequence with no I/O:

1. NFKC Unicode, punctuation, casing, and whitespace normalization.
2. Handwritten bounded tokenization with normalized source spans.
3. Versioned catalog lookup and security-shape recognition.
4. Entity and relation binding.
5. Typed sentence-frame parsing into an immutable Intent AST.
6. Host-neutral response, capability, effect, and confirmation planning.
7. Validation and canonical serialization as Nano Intent IR `0.1.0`.

Catalogs are deterministic data, not callbacks. `core@0.1.0` owns universal
forms, operators, time, negation, conjunction, effects, and consent language.
`market@0.1.0` owns market topics, security aliases/shapes, and capability
mappings. Higher precedence wins an alias; equal-precedence collisions are
rejected. Future code, design, project, and device catalogs can join the same
contract without changing core grammar.

Security recognition is deliberately syntactic. Known aliases such as `SPY`
bind directly; other valid shapes retain the user's spelling, are canonicalized
without correction, and appear in `receipt.unresolvedEntities` for host
validation. Fuzzy ordinary-word suggestions are receipt-only and never turn an
ambiguous phrase into an executable plan.

## Host boundary

Nano only proposes the declared `steps`, `effects`, and `confirmation` plan. It
does not open a tile, read market data, call a model, send data, or place a
trade. Capability names such as `market.earnings.calendar` and
`fast_nonthinking` are provider- and UI-component-neutral. A host maps them to
its own implementation, may strengthen confirmation, and may refuse any plan.

The `0.1.0` policy guarantees:

- noun phrases and local imperatives never emit `llm.call`;
- ambiguous input emits no executable step or automatic model proposal;
- one phrase emits at most one `llm.call`;
- compound language work is coalesced after ordered local/data steps;
- `external.write` always requests explicit confirmation;
- trade requests remain declared host-governed effects and ATSv2 Web can deny
  them without invoking a model.

## Contract and versioning

The canonical schema is
`nano/intent/schemas/nano-intent-0.1.0.schema.json`; ATSv2 fixtures are in
`nano/intent/fixtures/atsv2-golden-0.1.0.json`. Canonical JSON uses sorted keys,
ASCII escapes, no insignificant whitespace, and no line terminator. Identical
source and catalog versions produce identical bytes.

`nanoIntentVersion` is independent from strategy `nanoIrVersion`:

- patch changes may clarify behavior or add optional receipt diagnostics;
- minor changes may add optional capabilities or fields old hosts can ignore;
- removed/renamed fields, changed meanings, or new required behavior need a
  major contract version and new golden fixtures.

Provider/model names and live UI identifiers are not compatible additions.

## CLI

```text
nano intent parse "spy earnings"
nano intent compile "when is spy earnings" --json
nano intent explain "describe spy earnings forecasts over next 3 quarters"
```

`parse` prints the AST surface (tokens, entities, relations, clauses, frame),
`compile` prints the validated plan, and `explain` renders the deterministic
receipt and ranked alternatives.
