"""Nano IR schema constants and validation errors.

The IR is the contract between the compiler and every runtime. No runtime
executes `.nano` source directly — everything becomes this IR.

## Two live IR versions

`0.1.0` is the original flat strategy document: a `Schedule`, some `Condition`s,
some `Intent`s, some `Agent`s, and an effect manifest. `1.0.0` adds typed params
and inputs, computed series, explicit rules with `else` branches, risk limits,
reasoning signatures, and confidence routes.

`NANO_IR_VERSION` still means `0.1.0` — the baseline. Ask for
`NANO_IR_VERSION_LATEST` when you want the newest version the compiler emits,
and `SUPPORTED_IR_VERSIONS` when you want everything a runtime will load.

**The compiler emits the lowest version that can express the program.** A
strategy using only v0.1.0 features still compiles to byte-identical v0.1.0 IR,
which is why the whole example corpus and strategy library are untouched by
v1.0, and why a host pinned to `0.1.0` keeps working. Reach a v1.0 feature and
the document becomes `1.0.0`. `nano compile --ir-version` forces the choice when
a host wants one shape regardless of content.

Runtimes load both. The v1.0 module (`nano/ir/module.py`) is the canonical
executable form and v0.1.0 graphs lift into it, so there is exactly one
evaluator and no chance of the two versions drifting apart in behavior.
"""

import math
from typing import Optional

NANO_IR_VERSION_BASELINE = "0.1.0"
NANO_IR_VERSION_1_0 = "1.0.0"

# `NANO_IR_VERSION` keeps its original meaning — the baseline document version
# that `StrategyGraph` reads and writes. Adding a second version is not a reason
# to redefine a constant hosts already import; a name whose meaning shifts under
# a consumer is worse than a new name beside it. Use
# `NANO_IR_VERSION_LATEST` for "newest the compiler can emit".
NANO_IR_VERSION = NANO_IR_VERSION_BASELINE
NANO_IR_VERSION_LATEST = NANO_IR_VERSION_1_0

# Every version a runtime in this package accepts, oldest first.
SUPPORTED_IR_VERSIONS = (NANO_IR_VERSION_BASELINE, NANO_IR_VERSION_1_0)

# Node vocabulary of the baseline document. A `0.1.0` graph containing anything
# outside this set is rejected — the version is a promise about shape, and a
# loader that quietly accepted more would make the promise worthless.
NODE_TYPES = frozenset({"Schedule", "Condition", "Intent", "Agent"})

CONDITION_OPERATORS = frozenset({"<", "<=", ">", ">=", "==", "!="})

# Language tiers (locked platform spec §2). A module declares its tier and
# cannot use constructs from a higher one, which keeps the entry language small
# and the audit surface predictable.
TIERS = ("nano", "nano+", "nano++")

# Constructs that require a tier above plain `nano`, and the tier each needs.
TIER_REQUIREMENTS = {
    "signature": "nano+",
    "route": "nano+",
    "escalate": "nano+",
    "infer": "nano+",
}

# Effect manifest vocabulary. Emitting an intent requires "intent.emit" in the
# strategy's declared effects — this is the capability boundary: a graph that
# does not declare it cannot propose actions, regardless of its nodes. The same
# rule extends to reasoning: a strategy that never declared `llmre.escalate`
# cannot hand a decision to a model, so escalation is rate-limitable and
# auditable exactly like any other side effect.
KNOWN_EFFECTS = frozenset(
    {
        "intent.emit",
        "llm.call",
        "llmre.escalate",
        "log.append",
        "sign.emit",
    }
)

# Canonical manifest order. Fixed rather than sorted, so a manifest is
# byte-stable and diffable between two compiles; the baseline pair
# ("intent.emit", "log.append") keeps its historical order.
EFFECT_ORDER = ("intent.emit", "llm.call", "llmre.escalate", "sign.emit", "log.append")

INTENT_ACTIONS = frozenset({"BUY", "SELL", "EXECUTE", "PAUSE", "OBSERVE"})

# The subset that asks the host to act in the market. `PAUSE` and `OBSERVE` are
# how a strategy asks to be stopped and how it reports, so a risk limit gates the
# first three and never the last two. Lives here because the type checker and the
# runtime gate must agree on the split, and both already read this module.
ACTUATING_INTENT_ACTIONS = frozenset({"BUY", "SELL", "EXECUTE"})

# Risk-limit vocabulary for the `risk { ... }` block. Each entry is
# (canonical unit, inclusive lower bound, inclusive upper bound or None).
# Fractions are fractions, never percentages: `max_daily_loss 0.02` is 2%.
# Accepting both conventions in one block is how a 2% stop becomes a 200% one.
RISK_LIMITS = {
    "max_position_size": ("fraction of equity", 0.0, 1.0),
    "max_daily_loss": ("fraction of equity", 0.0, 1.0),
    "max_drawdown": ("fraction of equity", 0.0, 1.0),
    "max_open_positions": ("count", 0, None),
    "max_orders_per_day": ("count", 0, None),
    "stop_trading_after_losses": ("consecutive losses", 1, None),
    "min_confidence": ("confidence in [0, 1]", 0.0, 1.0),
}

# Limits whose value must be a whole number.
INTEGER_RISK_LIMITS = frozenset(
    {"max_open_positions", "max_orders_per_day", "stop_trading_after_losses"}
)

# Agent roles. A role is routing metadata, not a capability: a `validation`
# agent may veto an escalation result, an `execution` agent is a named handoff
# target, and `research` is consulted about novel situations.
AGENT_ROLES = ("research", "validation", "execution", "observer")

# Canonical integers are deliberately bounded without consulting or mutating
# CPython's process-wide ``int_max_str_digits`` setting.  Receipt and module
# hashes must not depend on how the embedding host configured its interpreter.
# A numeric comparison also avoids converting a hostile oversized integer to
# text merely to reject it.
MAX_CANONICAL_INTEGER_DIGITS = 640
_MAX_CANONICAL_INTEGER_MAGNITUDE = 10**MAX_CANONICAL_INTEGER_DIGITS

# Param declarations are compile-time JSON scalars.  Series and record values
# belong to runtime inputs/nodes and cannot be smuggled into the param table.
PARAM_SCALAR_TYPES = ("bool", "confidence", "duration", "float", "int", "string")


class IRValidationError(ValueError):
    """The document is not valid Nano IR."""


class ManifestViolation(IRValidationError):
    """A node requires an effect the manifest does not declare."""


class TierViolation(IRValidationError):
    """A module uses a construct its declared tier does not permit."""


def validate_integer_magnitude(value: int) -> Optional[str]:
    """Return a stable problem phrase when an integer exceeds the IR bound.

    Callers establish that ``value`` is a plain integer first.  Keeping this
    helper numeric means its result is independent of the host's ambient
    integer-to-string conversion limit.
    """
    if abs(value) >= _MAX_CANONICAL_INTEGER_MAGNITUDE:
        return f"must contain at most {MAX_CANONICAL_INTEGER_DIGITS} decimal digits"
    return None


def validate_risk_limit(name: str, value: object) -> Optional[str]:
    """Check one risk limit's value. Returns a problem phrase, or None if it fits.

    One function because there are two call sites that must never disagree: the
    type checker, which sees a `risk { ... }` block, and the IR loader, which
    sees a document that never passed through the checker. They had already
    drifted — only the loader rejected a non-finite limit — which is exactly what
    a limit shared between a compiler and a loader cannot afford.

    The return is a phrase rather than an exception so each caller can raise its
    own error type at its own position: the checker owes a 1-based line and
    column, the loader owes a node id.

    `name` must already be in `RISK_LIMITS`. Both callers reject an unknown name
    first, with their own wording and their own ordering, so a check here would
    be a third spelling of an error neither of them would ever see.

    `bool` is excluded by hand rather than by importing `nano.types.lookahead`:
    `nano.types` imports this module, so the dependency only runs one way.
    """
    unit, low, high = RISK_LIMITS[name]

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "must be numeric"
    if not math.isfinite(value):
        # NaN sits below every threshold and infinity above every one. A limit
        # that cannot be compared is not a limit.
        return "must be a finite number"
    if name in INTEGER_RISK_LIMITS and not isinstance(value, int):
        return f"is measured in {unit} and must be a whole number, got {value}"
    if value < low or (high is not None and value > high):
        bound = f"[{low}, {high}]" if high is not None else f">= {low}"
        return f"is measured in {unit} and must be {bound}, got {value}"
    return None
