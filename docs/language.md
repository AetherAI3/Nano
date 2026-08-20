# Nano language reference

This document describes the language accepted and executed by the current Nano
implementation. The installable package, the source language, the two Nano IR
document versions, and run receipts are versioned separately. The current source
language can compile to either Nano IR `0.1.0` or `1.0.0`; the compiler chooses
the lowest version that can represent a program.

Nano is a scheduled rule language whose runs are deterministic given the same
compiled module, injected frame, and reasoning-provider behavior. It derives
values from a host-supplied timeline, evaluates rules and routes, and proposes
intents or records escalations. It does not fetch data, keep ambient state, place
orders, or create a reasoning provider.

Every fenced `nano` program in this document is compiled and loaded by
`tests/test_language_spec.py`.

## A complete v1 example

```nano
tier nano+
strategy Truth {
  param fast: int = 3
  param floor: float = 0.6
  input close: series<float>
  input volume: series<float>

  let mean = EMA(close, fast)
  let rising = RISING(close, fast)

  signature Review {
    input score: float range [0, 100]
    output confidence: confidence range [0, 1]
  }
  let verdict = infer(Review, close)

  agent Desk { role validation }

  risk {
    max_drawdown 0.05
    min_confidence 0.6
  }

  route Trusted {
    execute live_path when verdict.confidence >= floor
    otherwise { escalate Desk }
  }

  every 1m {
    if (close > mean and rising) or not (volume > 0) {
      if close[1] <= mean {
        buy(BTC, 0.8)
      } else {
        observe()
      }
    } else {
      pause()
    }
  }

  every 1h {
    if close > 0 {
      sell(BTC, 0.7)
    }
  }
}
```

This example uses v1 IR. The host must inject `close`, `volume`,
`risk.drawdown`, timestamps, and—if reasoning is wanted—a provider implementing
the declared `Review` call. Without a provider, `infer` produces absent values
and the route escalates; the scheduled rules still evaluate their market inputs.

## Shipped construct matrix

“Parsed” means the recursive-descent parser has a source form. “Typed” names the
principal static check. “Compiled” names the v1 operation or baseline shape.
“Executed” is deliberately precise about what the VM does today. `auto` in the
IR column means the construct can remain in baseline only when the complete
program satisfies the baseline subset described below.

<!-- construct-matrix:start -->
| Construct | Parsed? | Typed? | Compiled? | IR version | Executed? | Minimum tier | Required effect | Example test |
|---|---|---|---|---|---|---|---|---|
| Strategy and tier | yes | name; tier is `nano`, `nano+`, or `nano++` | strategy root/module | auto; a non-`nano` tier needs 1.0.0 | module is validated, then loaded | `nano` | `log.append` | `test_tier_and_effect_matrix_matches_schema` |
| `param` | yes | scalar compile-time literal; annotation must accept it | v1 param table and `param.ref` when not folded | 1.0.0 | constant is broadcast where used | `nano` | none beyond logging | `test_v1_declarations_control_flow_and_warmup_execute` |
| declared `input` | yes | annotation must be a known type; declarations enable strict bare-name resolution | `input.ref` plus input manifest | 1.0.0 | host frame series is read by name | `nano` | none beyond logging | `test_v1_declarations_control_flow_and_warmup_execute` |
| implicit feed signal | yes | an undeclared bare name is `series<float>` only when no inputs are declared | baseline `Condition` or v1 `feed.signal` | auto | host frame series is read by name | `nano` | none beyond logging | `test_feed_signal_and_computed_indicator_forms_stay_distinct` |
| builtin `confidence` | yes | reserved name with type `confidence` | `builtin.confidence` | 1.0.0 when referenced | resolves the injected, derived, or default series below | `nano` | none beyond logging | `test_builtin_confidence_priority_and_default` |
| `let` | yes | inferred or annotated; may use earlier bindings | `let` | 1.0.0 | derived once over the frame | `nano` | none beyond logging | `test_v1_declarations_control_flow_and_warmup_execute` |
| arithmetic `+ - * / %` | yes | numeric; series lift pointwise; `/` has static type float | `arith.*` | 1.0.0 | present operands/results are float-coerced pointwise; absence propagates | `nano` | none beyond logging | `test_v1_declarations_control_flow_and_warmup_execute` |
| indexing/lookback `series[n]` | yes | series target and compile-time non-negative integer offset | `series.index` | 1.0.0 | reads `n` bars back; early cells are absent | `nano` | none beyond logging | `test_v1_declarations_control_flow_and_warmup_execute` |
| computed indicator | yes | registry signature, constant positive periods, and series arguments | `indicator` | 1.0.0 | deterministic prefix-causal kernel | `nano` | none beyond logging | `test_indicator_matrix_matches_registry` |
| multiple schedules | yes | every schedule is checked independently | one `schedule` per block | 1.0.0 | each schedule ticks against the injected timeline | `nano` | depends on body | `test_multiple_schedules_and_rules_execute` |
| multiple rules | yes | every condition must be bool or `series<bool>` | one `rule` per guarded or unconditional run | 1.0.0 | rules execute independently in source order | `nano` | depends on body | `test_multiple_schedules_and_rules_execute` |
| nested `if` and `else` | yes | each condition is boolean; both branches are checked | nested `rule` and `block` | 1.0.0 | one branch runs at the enclosing schedule's current bar | `nano` | depends on branches | `test_v1_declarations_control_flow_and_warmup_execute` |
| `and`, `or`, `not` | yes | boolean operands; series lift pointwise | `logic.and`, `logic.or`, `logic.not` | `and` may remain baseline only as a flat feed-comparison chain; otherwise 1.0.0 | evaluated pointwise; absence propagates | `nano` | none beyond logging | `test_v1_declarations_control_flow_and_warmup_execute` |
| actions | yes | fixed arity; asset identifier for buy/sell; literal confidence in `[0,1]` | baseline `Intent` or v1 `intent.emit` | auto | proposes `BUY`, `SELL`, `EXECUTE`, `PAUSE`, or `OBSERVE` | `nano` | `intent.emit` | `test_multiple_schedules_and_rules_execute` |
| `risk` | yes | one non-empty block; known, unique, in-range limits | `risk.limits` | 1.0.0 | gates actuating intents as listed below | `nano` | no new effect | `test_risk_matrix_matches_runtime_contract` |
| `agent` | yes | unique name; optional role from the closed role vocabulary | baseline `Agent` or v1 `agent` | auto; a role needs 1.0.0 | declaration is metadata and a named escalation target | `nano` | no new effect | `test_agent_roles_match_schema` |
| `signature` | yes | unique typed fields, at least one output, ordered optional ranges | `ai.signature` | 1.0.0 | declares the named call and its fields; provider receives the name and inputs | `nano+` | no effect by itself | `test_reasoning_route_and_escalation_runtime_boundaries` |
| `infer` | yes | declared signature, exact arity, assignable input types | `ai.infer` and record fields | 1.0.0 | injected provider is called once per bar; no provider means absent output | `nano+` | `llm.call` | `test_reasoning_route_and_escalation_runtime_boundaries` |
| `route` | yes | boolean guard and mandatory fallback escalation | `route` | 1.0.0 | evaluates every frame bar; logs the execute label or records escalation; does not dispatch that label | `nano+` | `llmre.escalate` | `test_reasoning_route_and_escalation_runtime_boundaries` |
| `escalate` | yes | quoted non-empty host target, or declared agent name | `llmre.escalate` | 1.0.0 | appends an escalation record; does not call a model by itself | `nano+` | `llmre.escalate` | `test_reasoning_route_and_escalation_runtime_boundaries` |
<!-- construct-matrix:end -->

## Grammar

This grammar is descriptive EBNF for the parser. Braces and parentheses are
literal. `IDENT` begins with ASCII letter or `_` and continues with ASCII
letters, digits, or `_`. `INTERVAL` is ASCII decimal digits followed by `s`,
`m`, `h`, or `d`. Line comments begin with `//`. There are no semicolons.

```text
program       := ["tier" ("nano" | "nano+" | "nano++")] strategy
strategy      := "strategy" IDENT "{" member* "}"

member        := param | input | let | risk | signature | route | agent | schedule
param         := "param" IDENT [":" type] "=" literal
input         := "input" IDENT ":" type
let           := "let" IDENT [":" type] "=" expression
risk          := "risk" "{" (IDENT signedNumber)* "}"
signature     := "signature" IDENT "{" signatureField+ "}"
signatureField := ("input" | "output") IDENT ":" type
                  ["range" "[" signedNumber "," signedNumber "]"]
route         := "route" IDENT "{" "execute" IDENT ["when"] expression
                 "otherwise" "{" escalation "}" "}"
agent         := "agent" IDENT ["{" "role" IDENT "}"]
schedule      := "every" INTERVAL "{" statement* "}"

statement     := if | escalation | action
if            := "if" expression "{" statement* "}"
                 ["else" "{" statement* "}"]
escalation    := "escalate" (STRING | IDENT)
action        := ("buy" | "sell") "(" IDENT ["," signedNumber] ")"
               | ("execute" | "pause" | "observe") "(" ")"

expression    := orExpression
orExpression  := andExpression ("or" andExpression)*
andExpression := notExpression ("and" notExpression)*
notExpression := "not" notExpression | comparison
comparison    := additive [("<" | "<=" | ">" | ">=" | "==" | "!=") additive]
additive      := multiplicative (("+" | "-") multiplicative)*
multiplicative := unary (("*" | "/" | "%") unary)*
unary         := "-" unary | postfix
postfix       := primary ("[" expression "]" | "." IDENT)*
primary       := NUMBER | STRING | "true" | "false" | INTERVAL
               | IDENT ["(" [expression ("," expression)*] ")"]
               | "(" expression ")"

type          := "int" | "float" | "bool" | "string" | "duration"
               | "confidence" | "series<" type ">"
literal       := signedNumber | STRING | "true" | "false"
signedNumber  := ["-"] NUMBER
NUMBER        := DIGITS ["." DIGITS]
```

Nested series types are rejected. The public annotation vocabulary is the six
scalar types plus one-level series types. A `record<Signature>` type exists
internally for `infer` results but cannot be written as a source annotation or
constructed as a literal.

Number tokens use decimal notation, without exponent syntax. A leading `-` is a
unary operator or part of a declaration's signed number; unary `+` is not a
source form. Float literals must be finite. Integer magnitude is limited to 640
significant decimal digits, independent of the embedding Python process's digit
guard. Parameter literals are numbers, strings, or booleans—duration tokens are
expressions but are not parameter defaults in the current parser.

## Baseline v0.1 subset and IR selection

IR version selection is about serialized shape, not package version and not a
second parser. The v1 parser/checker accepts the whole language, then the
compiler emits the lowest IR version that can represent the checked program.

<!-- ir-selection-matrix:start -->
| Complete source shape | Automatic IR | Can force 0.1.0? | Can force 1.0.0? |
|---|---|---|---|
| Baseline subset below | 0.1.0 | yes | yes |
| Any v1 construct | 1.0.0 | no; compilation raises `IRVersionError` | yes |
<!-- ir-selection-matrix:end -->

A complete program is baseline-shaped only when all of these hold:

- its tier is `nano`;
- it has no params, declared inputs, lets, risk block, signatures, or routes;
- agents, if present, have no role;
- it has at most one schedule and at most one rule in that schedule;
- that rule is an `if` with no `else`, and its body contains only actions;
- its condition is an `and` chain of `feedSignal comparison number` leaves; and
- each feed signal is a bare name or the one-constant-integer label form.

Here is a baseline program. It compiles to IR `0.1.0` unless the caller
explicitly requests `1.0.0`:

```nano
strategy BaselineMomentum {
  agent Analyst

  every 5m {
    if RSI(14) < 30 and VOLUME > 1000000 {
      buy(BTC, 0.91)
      observe()
    }
  }
}
```

Baseline `RSI(14)` is a host-supplied signal label. The integer is not an
indicator calculation and is not retained in baseline IR. Baseline IR has the
flat node kinds `Schedule`, `Condition`, `Intent`, and `Agent`, and its historical
effect manifest is always `intent.emit`, then `log.append`.

IR `1.0.0` is the typed DAG form. The loader accepts and validates both versions;
`load_module` lifts a baseline graph into a v1 module so the VM has one execution
path. `compile_source` is the baseline-only API. `compile_module` always returns
the executable v1 module form, while `compile_to_dict` preserves the selected
document version.

## Declarations, names, and types

Params are compile-time constants. Their type may be inferred from a number,
string, or boolean literal or stated explicitly. `int` widens to `float` or
`confidence`; floats do not narrow to integers. Params cannot be series.

Inputs name values supplied by the host frame. Once a strategy declares any
input, undeclared bare names are errors. With no input declarations, an unknown
bare name remains an implicit host `series<float>` for baseline compatibility.
The explicit one-integer call form, such as `SENTIMENT(1)`, remains a deliberate
host feed signal even in strict mode.

The checker accepts scalar input annotations and a one-level series wrapper over
any scalar type, but the current VM input channel is always an aligned frame
series and float-coerces every present cell. A scalar annotation does not create
one global runtime scalar, and arbitrary string or duration series have no usable
frame representation today. Shipped market-data programs therefore declare
numeric or boolean series inputs.

`confidence` is a reserved builtin of static type `confidence`; declarations
cannot shadow it. At runtime it resolves, in order, to the frame series named
`confidence`, then the `confidence` field of the first `infer` result, then a
broadcast `1.0` only when the module contains no infer call. If an infer exists
but has no provider or returns no confidence field, the builtin is absent rather
than defaulting to `1.0`.

Lets are immutable derived bindings, checked in source order. A let may use an
earlier let, but there is no reassignment or forward reference.

Arithmetic accepts numeric operands. If either operand is a series, the result
is a series evaluated pointwise. In the static type system, integer arithmetic
remains `int` except `/`, which has type `float`. At execution, every present
arithmetic operand and result is float-coerced, including statically integer
`+`, `-`, `*`, `%`, and unary `-`; division or modulo by zero yields absence.
Comparisons accept numeric pairs or matching scalar types and yield bool or
`series<bool>`. Logical operators require booleans and also lift over series. A
rule or route condition must be bool or `series<bool>`. Comparisons do not chain.

Indexing is backward-only: `close[0]` is current, `close[3]` is three bars ago.
The offset must fold to a non-negative integer at compile time; it may use integer
params and integer `+`, `-`, `*`, and `%`. A negative offset is a future read and
is rejected. Indexing adds its offset to the program's warm-up requirement.

## Schedules, rules, branches, and actions

A strategy may contain any number of `every` blocks and each may contain any
number of statements. Each top-level `if` becomes an independent rule. A run of
bare actions or escalations becomes one unconditional rule. Empty schedules are
valid but do nothing.

The scheduler receives timestamps from the host. A schedule fires at the first
timestamp and then at the first supplied timestamp at least one whole interval
after its previous firing; it does not consult a wall clock. Rules sharing a
timestamp run in source order.

An absent top-level condition—normally an indicator or index still warming up—
does not select `else`; the rule is skipped and the run records
`condition.unwarmed`. A present false condition selects `else`. An absent nested
condition runs neither nested branch.

The actions are:

| Source | Proposed intent | Arguments |
|---|---|---|
| `buy(ASSET[, confidence])` | `BUY` | asset identifier; optional numeric literal in `[0,1]` |
| `sell(ASSET[, confidence])` | `SELL` | asset identifier; optional numeric literal in `[0,1]` |
| `execute()` | `EXECUTE` | none |
| `pause()` | `PAUSE` | none |
| `observe()` | `OBSERVE` | none |

All five are intent proposals carrying `intent.emit`. Nano does not place an
order. The host decides what to do with a surviving intent.

## Feed signals and computed indicators

The two call forms are structural:

- `RSI(14)` has one compile-time integer and is a host-supplied feed signal
  named `RSI`; `14` documents the feed contract.
- `RSI(close, 14)` supplies a series and is computed by Nano's indicator
  registry.
- Elementwise helpers whose first registered parameter is scalar float are real
  calls even with one integer: `ABS(3)` computes absolute value.

Computed period arguments must be positive compile-time integers. A registry
parameter declared `series<T>` needs history and rejects a scalar. A float
parameter accepts a scalar or lifts pointwise over a series. Indicator warm-up
is added to any lookback already needed by its arguments. Every shipped kernel
is prefix-causal: output at bar `i` depends only on bars at or before `i`.

In the table, `p` means the first period argument, `max(fast, slow)` has its
literal meaning, and an argument's own lookback is added to the listed kernel
warm-up.

<!-- indicator-matrix:start -->
| Indicator | Signature | Kernel warm-up | Semantics |
|---|---|---|---|
| `SMA` | `SMA(series<float>, int) -> series<float>` | `p - 1` | simple moving average |
| `EMA` | `EMA(series<float>, int) -> series<float>` | `p - 1` | exponential average, seeded by the first full-window SMA |
| `WMA` | `WMA(series<float>, int) -> series<float>` | `p - 1` | linearly weighted moving average |
| `STDDEV` | `STDDEV(series<float>, int) -> series<float>` | `p - 1` | population standard deviation |
| `ZSCORE` | `ZSCORE(series<float>, int) -> series<float>` | `p - 1` | `(value - SMA) / STDDEV`; zero at zero dispersion |
| `SUM` | `SUM(series<float>, int) -> series<float>` | `p - 1` | trailing sum |
| `BARS_SINCE` | `BARS_SINCE(series<bool>) -> series<float>` | `0` | bars since the last true cell |
| `COUNT_TRUE` | `COUNT_TRUE(series<bool>, int) -> series<float>` | `p - 1` | true count in a full trailing window |
| `RISING` | `RISING(series<float>, int) -> series<bool>` | `p - 1` | strict pairwise rise over the trailing window |
| `FALLING` | `FALLING(series<float>, int) -> series<bool>` | `p - 1` | strict pairwise fall over the trailing window |
| `PERCENTRANK` | `PERCENTRANK(series<float>, int) -> series<float>` | `p` | fraction of preceding values at or below current |
| `RSI` | `RSI(series<float>, int) -> series<float>` | `p` | Wilder relative strength index, 0–100 |
| `ROC` | `ROC(series<float>, int) -> series<float>` | `p` | percentage rate of change |
| `MOM` | `MOM(series<float>, int) -> series<float>` | `p` | absolute change from `p` bars ago |
| `CHANGE` | `CHANGE(series<float>) -> series<float>` | `1` | bar-over-bar difference |
| `HIGHEST` | `HIGHEST(series<float>, int) -> series<float>` | `p - 1` | trailing maximum |
| `LOWEST` | `LOWEST(series<float>, int) -> series<float>` | `p - 1` | trailing minimum |
| `TR` | `TR(series<float>, series<float>, series<float>) -> series<float>` | `1` | true range from high, low, close |
| `ATR` | `ATR(series<float>, series<float>, series<float>, int) -> series<float>` | `p` | Wilder-smoothed average true range |
| `SUPERTREND` | `SUPERTREND(series<float>, series<float>, series<float>, int, float) -> series<float>` | `p` | SuperTrend line |
| `SUPERTREND_DIR` | `SUPERTREND_DIR(series<float>, series<float>, series<float>, int, float) -> series<bool>` | `p` | true while the SuperTrend line is below price |
| `STOCH_K` | `STOCH_K(series<float>, series<float>, series<float>, int) -> series<float>` | `p - 1` | stochastic percent K, 0–100 |
| `WILLR` | `WILLR(series<float>, series<float>, series<float>, int) -> series<float>` | `p - 1` | Williams percent R, -100–0 |
| `CCI` | `CCI(series<float>, series<float>, series<float>, int) -> series<float>` | `p - 1` | commodity channel index |
| `OBV` | `OBV(series<float>, series<float>) -> series<float>` | `1` | on-balance volume, seeded at zero |
| `VWAP` | `VWAP(series<float>, series<float>, int) -> series<float>` | `p - 1` | rolling volume-weighted average price |
| `MACD_LINE` | `MACD_LINE(series<float>, int, int) -> series<float>` | `max(fast, slow) - 1` | fast EMA minus slow EMA |
| `MACD_SIGNAL` | `MACD_SIGNAL(series<float>, int, int, int) -> series<float>` | `max(fast, slow) - 1 + signal - 1` | EMA of the MACD line |
| `MACD_HIST` | `MACD_HIST(series<float>, int, int, int) -> series<float>` | `max(fast, slow) - 1 + signal - 1` | MACD line minus signal line |
| `BB_MIDDLE` | `BB_MIDDLE(series<float>, int) -> series<float>` | `p - 1` | Bollinger middle SMA |
| `BB_UPPER` | `BB_UPPER(series<float>, int, float) -> series<float>` | `p - 1` | middle plus multiplier times standard deviation |
| `BB_LOWER` | `BB_LOWER(series<float>, int, float) -> series<float>` | `p - 1` | middle minus multiplier times standard deviation |
| `BB_PCT_B` | `BB_PCT_B(series<float>, int, float) -> series<float>` | `p - 1` | position within Bollinger bands |
| `BB_WIDTH` | `BB_WIDTH(series<float>, int, float) -> series<float>` | `p - 1` | band width as percentage of middle |
| `CROSSOVER` | `CROSSOVER(series<float>, series<float>) -> series<bool>` | `1` | first series crosses above second |
| `CROSSUNDER` | `CROSSUNDER(series<float>, series<float>) -> series<bool>` | `1` | first series crosses below second |
| `ABS` | `ABS(float) -> float` | `0` | absolute value; lifts over series |
| `SQRT` | `SQRT(float) -> float` | `0` | square root; negative input is absent; lifts |
| `MIN` | `MIN(float, float) -> float` | `0` | lesser value; lifts |
| `MAX` | `MAX(float, float) -> float` | `0` | greater value; lifts |
<!-- indicator-matrix:end -->

The five event/temporal primitives have these exact gap and boundary rules:

- `BARS_SINCE`: true yields `0.0`; later false cells increment. Output is absent
  before the first true. An absent cell emits absence and resets history, so
  later false cells stay absent until a new true.
- `COUNT_TRUE`: counts true cells, including current, in the full contiguous
  trailing `p`-bar window. Any absent cell in the window yields absence. With
  `p = 1`, the result is `1.0` or `0.0`.
- `RISING`: requires strict pairwise increase from oldest to current. Equality is
  false and a gap yields absence. With `p = 1`, a present cell yields true.
- `FALLING`: mirrors `RISING` with strict pairwise decrease.
- `PERCENTRANK`: compares current with exactly the preceding `p` cells and
  returns `count(previous <= current) / p`. The current cell is not in the
  comparison set; ties are inclusive. Any gap yields absence. With `p = 1`, the
  result is `0.0` or `1.0`.

## Warm-up and absence

The compiler records the maximum lookback of every expression used by the
module. Index offsets accumulate through lets and indicators. The VM derives
series across the full frame, using `None` for cells that cannot yet be computed.
It never fills a missing historical value from the future.

Warm-up is an availability contract, not a profitability claim and not a blanket
instruction to drop the first `module.warmup` timestamps. Each top-level rule
samples its actual condition at its schedule ticks. If that cell is absent, the
rule is skipped and counted in `warmupBarsSkipped`; independent rules whose
conditions are already present may still run.

## Tiers and effects

Omitting a tier header means `tier nano`.

<!-- tier-effect-matrix:start -->
| Source construct | Minimum tier | Effect added when used | Current runtime boundary |
|---|---|---|---|
| pure declarations, expressions, indicators, schedules, agents, risk | `nano` | none beyond `log.append` | deterministic local evaluation |
| builtin `confidence` | `nano` | none beyond `log.append` | injected frame, first infer result, or no-infer default |
| any action | `nano` | `intent.emit` | proposes an intent to the host |
| `signature` | `nano+` | none | declares provider schema only |
| `infer` | `nano+` | `llm.call` | calls only the injected provider |
| `route` | `nano+` | `llmre.escalate` | guard audit plus fallback escalation |
| `escalate` | `nano+` | `llmre.escalate` | records a hand-off request |
| `nano++` header | `nano++` | none by itself | accepted tier; no exclusive source construct today |
<!-- tier-effect-matrix:end -->

Manifests are emitted in canonical order:
`intent.emit`, `llm.call`, `llmre.escalate`, `sign.emit`, `log.append`.
`sign.emit` is a recognized IR capability but no shipped source construct adds
it. The IR loader rejects a node whose required effect is absent and rejects a
reasoning construct below `nano+` even in hand-written IR.

## Agents, signatures, inference, routes, and escalation

Agent roles are exactly `research`, `validation`, `execution`, and `observer`.
A bare agent has no role. A role is metadata, not authority: declaring
`agent Broker { role execution }` does not place an order or invoke an agent.

A signature declares named typed inputs and at least one typed output. Field
names are unique across both sides. Optional ranges must have lower bound no
greater than upper bound. Today those ranges travel as schema metadata; the VM
does not validate a provider's returned values against them.

`infer(Signature, ...)` must match the declared input count and assignable scalar
types. It returns an internal record whose declared outputs are read with member
syntax, such as `verdict.confidence`. The VM calls the host-injected provider
once per bar. With no provider it logs `infer.skipped` once and makes every
result cell absent. A live provider may be nondeterministic; replay requires the
host to inject a recorded or otherwise deterministic provider.

A route has a name, an execute label, a boolean guard, and exactly one fallback
escalation. The optional word `when` does not change meaning. Routes are not
scheduled: the VM evaluates them at every supplied frame bar. A present true
guard logs `route.executed` with the execute label. A false or absent guard
records the fallback escalation. **The current VM does not resolve or call the
execute label.**

`escalate Desk` requires a declared agent and records `isAgent: true`.
`escalate "desk"` records a non-agent host target. Escalation records a hand-off;
it does not itself call `infer` or a model.

## Risk limits

A strategy may have one non-empty risk block. Limit names are unique. Fractions
are fractions: `0.05` means five percent, not 0.05 percent. Counting limits must
be integer literals in source and plain JSON integers in raw IR.

```nano
strategy GuardedBreakout {
  risk {
    max_drawdown 0.05
    max_orders_per_day 3
    min_confidence 0.6
  }

  every 5m {
    if BREAKOUT > 0 {
      buy(BTC, 0.8)
    }
  }
}
```

<!-- risk-matrix:start -->
| Limit | Declared domain | Allowed while | Runtime measurement | Enforcement owner |
|---|---|---|---|---|
| `max_position_size` | fraction `[0,1]` | host-defined | none in Nano | host; Nano logs `risk.unenforced` |
| `max_daily_loss` | fraction `[0,1]` | `risk.daily_loss <= limit` | `risk.daily_loss`; negative profit/no-loss is valid | Nano |
| `max_drawdown` | fraction `[0,1]` | `risk.drawdown <= limit` | non-negative `risk.drawdown` | Nano |
| `max_open_positions` | integer count `>= 0` | host-defined | none in Nano | host; Nano logs `risk.unenforced` |
| `max_orders_per_day` | integer count `>= 0` | `risk.orders_today + accepted actuating intents at this timestamp < limit` | non-negative `risk.orders_today` | Nano |
| `stop_trading_after_losses` | integer count `>= 1` | `risk.consecutive_losses < limit` | non-negative `risk.consecutive_losses` | Nano |
| `min_confidence` | confidence `[0,1]` | declared intent confidence `>= limit` | the intent's literal confidence | Nano |
<!-- risk-matrix:end -->

Immediately before a `BUY`, `SELL`, or `EXECUTE` proposal, the VM checks every
enforced limit. A breach removes that proposal and appends `risk.violation` plus
`intent.suppressed`; it does not substitute another intent. `PAUSE`, `OBSERVE`,
and escalation are never risk-suppressed.

Thresholds for fraction ceilings and the confidence floor are inclusive. Count
limits name the first unacceptable count. With `max_orders_per_day 3`, observed
count 2 can admit one actuating proposal, while observed count 3 admits none.
Accepted actuating proposals at the same timestamp are added before reviewing
the next proposal, preventing two rules from overbooking one frame.

Risk measurements are namespaced under `risk.`, which cannot be a Nano source
identifier. The host supplies them as frame series. Missing cells, missing
series, booleans, non-finite values, negative drawdown, and negative counts fail
closed. Negative `risk.daily_loss` is valid because it represents profit/no
loss. `min_confidence` also fails closed on absent intent confidence; the compiler
rejects actuating actions without declared confidence when any floor is present,
including a floor of `min_confidence 0`.

`max_position_size` cannot be evaluated because an intent has no size.
`max_open_positions` cannot be evaluated because an intent does not say whether
it opens or closes a position. Both still parse, validate, travel in IR, and
produce `risk.unenforced` declaration logs so the host can apply them.

## IR loading, execution, and receipts

The compiler, loaders, and runtime form this path:

1. tokenize and parse source;
2. type-check names, types, tiers, effects, risk, and lookback;
3. emit baseline IR `0.1.0` or typed DAG IR `1.0.0`;
4. validate the serialized document;
5. lift baseline documents to an executable v1 module; and
6. derive series, evaluate routes and scheduled rules, then return intents,
   escalations, ordered log entries, and the unwarmed-rule count.

Load validation is a security boundary. It rejects unsupported versions,
unknown nodes/effects, forward or missing references, invalid arity/attributes,
missing effects, tier violations, invalid parameter values, duplicate risk
nodes, and risk values outside the source contract. Runtime revalidates modules
by default before execution.

A run receipt is a versioned artifact produced after execution; it is not Nano
source or Nano IR.

<!-- receipt-matrix:start -->
| Receipt claim | Current behavior |
|---|---|
| format version | integer `receiptVersion: 1` |
| executable IR identity | always `identity.irVersion: "1.0.0"`; baseline input was lifted before execution |
| content identity | module and frame SHA-256 content addresses plus package/compiler identity |
| run account | ordered intents, escalations, log, and `warmupBarsSkipped` |
| provenance | source hash when present; optional caller-supplied host snapshot is unauthenticated |
| canonical bytes | sorted string keys, preserved array order, compact ASCII JSON, no line terminator, finite floats only, integer magnitude at most 640 digits, container nesting at most 64 |
| CLI framing | `nano replay --report receipt` writes canonical bytes followed by one `\n`; the newline is not part of the digest |
<!-- receipt-matrix:end -->

Receipt v1 has a fixed vocabulary of members it may emit. `host`, `provenance`,
`identity.params`, and the first/last timestamp bounds are conditional; a v1
producer cannot add a new optional member silently. An unsigned receipt is
reproducibility evidence, not proof of who produced it. Runs containing `infer`
are reproducible only given the same provider behavior;
`identity.reasoningRequired` records that qualification. See
[Run receipts](receipts.md) for the complete byte contract.

## Deliberate non-features and truth boundaries

The following are not shipped language behavior:

- imports, loops, mutable variables, user-defined functions, exceptions, files,
  network access, market-data fetching, or ambient clock/randomness;
- user-defined actions or dynamic action arguments;
- forward indexing, chained comparisons, or nested series types;
- order size, quantity, portfolio mutation, position accounting, or order
  placement;
- a `trade.propose` source form or IR operation, or any typed proposal-size
  field;
- automatic invocation of an agent declaration or a route's execute label;
- automatic construction, selection, recording, or validation of a reasoning
  provider;
- an exclusive `nano++` construct in the current parser/checker/VM;
- registry functions named `ALL_TRUE` or `ANY_TRUE`—compose window predicates
  from `COUNT_TRUE` instead; and
- any guarantee that a strategy is profitable, safe for a particular account,
  or accepted by a host after Nano proposes an intent.

Future architecture or proposal documents do not change this list. A feature is
part of this reference only after it is parsed, typed, compiled into a supported
IR version, accepted by the loader, and given current runtime semantics.

## Diagnostics

Lexer, parser, and checker failures are `NanoCompileError` subclasses carrying
1-based line and column positions. The principal specializations are
`NanoSyntaxError`, `NanoTypeError`, and `LookaheadError`. Serialized-document
failures are `IRValidationError`, `ManifestViolation`, or `TierViolation`.
Runtime failures, such as a required frame signal being absent, are separate
from source or IR validation failures.

For the component boundary, see [Architecture](architecture.md). For canonical
run artifacts, see [Run receipts](receipts.md).
