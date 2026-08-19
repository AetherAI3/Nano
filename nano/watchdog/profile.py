"""The Watchdog profile — a strict subset of Nano, enforced as an allow-list.

A Watchdog is a rule evaluated continuously against host telemetry whose
proposals feed a control decision. That job earns a narrower contract than a
strategy gets:

* it may propose `PAUSE` or `OBSERVE`, and nothing else;
* it may not reach a model, escalate, or route;
* it declares exactly one cadence; and
* every signal it reads must appear in its declared signal contract.

The last rule is the one that carries its weight. With no `input` declarations an
unknown name is a host feed signal — that is what makes `if RSI(14) < 30` work
with no preamble, and it must keep working (see ``nano/types/env.py``). The cost
is that `QUEUE_SATURATION_PTC` also compiles, into a rule that can never fire. On
a directional strategy that is a missed trade someone eventually notices. On a
control it is a policy gap that reports success forever, because a rule that
never fires and a rule with nothing to report look identical from outside. The
signal contract is what makes the difference visible at admission time.

**The opcode check is an allow-list, not a ban-list.** A new opcode added to
``nano/ir/module.py`` is denied here until someone admits it deliberately. That
is the posture the effect manifest already takes — a capability you did not grant
is one you do not have — and it fails closed as the language grows rather than
the day after.

**Models may author a Watchdog; they may never sit inside one.** Nothing stops a
model writing the source, reviewing it, or explaining a run log afterwards. What
the profile denies is a stochastic stage *inside* an evaluation, because a rule
containing one cannot be replayed, and a control that cannot be replayed cannot
be audited.
"""

from __future__ import annotations

from typing import Sequence, Tuple

from ..ir.module import NanoModule
from ..ir.schema import IRValidationError
from .contracts import WatchdogError, WatchdogSignalSpecV1, declared_signal_names


class WatchdogProfileError(WatchdogError):
    """A rule reaches outside the Watchdog profile."""


# The base tier. Every construct that needs `nano+` — signatures, routes,
# escalation, inference — is exactly what a Watchdog may not contain, so the tier
# the module declares is a one-word summary of that fact for anyone reading the
# artifact instead of the graph.
WATCHDOG_TIER = "nano"

# A Watchdog proposes a hold or a look. It does not take a side.
WATCHDOG_INTENT_ACTIONS = frozenset({"PAUSE", "OBSERVE"})

# Emit proposals, keep a log. `llm.call` and `llmre.escalate` are reasoning
# capabilities; `sign.emit` signs receipts with time and entropy, which is a host
# side-channel rather than part of a deterministic evaluation.
WATCHDOG_EFFECTS = frozenset({"intent.emit", "log.append"})

# Every opcode a Watchdog may contain. Absent by design:
#   ai.signature / ai.infer   a model in the loop
#   llmre.escalate / route    handing the decision somewhere else
#   record.field              only meaningful on the result of an inference
#   builtin.confidence        an implicit input no signal contract declares
WATCHDOG_OPS = frozenset(
    {
        # data sources
        "input.ref",
        "param.ref",
        "feed.signal",
        "const",
        # derivation
        "let",
        "series.index",
        "indicator",
        # arithmetic and logic
        "arith.add",
        "arith.sub",
        "arith.mul",
        "arith.div",
        "arith.mod",
        "arith.neg",
        "compare.lt",
        "compare.le",
        "compare.gt",
        "compare.ge",
        "compare.eq",
        "compare.ne",
        "logic.and",
        "logic.or",
        "logic.not",
        # control flow
        "schedule",
        "block",
        "rule",
        # effects and declarations
        "intent.emit",
        "risk.limits",
        "agent",
    }
)

# Opcodes that read a value out of the frame. Both spellings matter: `feed.signal`
# is the undeclared v0.1.0 form and `input.ref` the declared v1.0 one, and the VM
# resolves them through the same path (``_Machine._read_frame``). Checking one
# would leave the other unchecked.
FRAME_READING_OPS = ("feed.signal", "input.ref")


def referenced_signals(module: NanoModule) -> Tuple[str, ...]:
    """Every frame signal the module reads, sorted and deduplicated."""
    names = {
        node.attrs["name"] for op in FRAME_READING_OPS for node in module.of_op(op)
    }
    return tuple(sorted(names))


def proposable_intents(module: NanoModule) -> Tuple[str, ...]:
    """Every intent action the module can propose, sorted and deduplicated.

    Derived rather than declared. This is the ceiling on what the artifact can
    ever hand a gate, and a ceiling read out of the graph cannot disagree with
    the graph.
    """
    return tuple(sorted({node.attrs["action"] for node in module.of_op("intent.emit")}))


def cadence_of(module: NanoModule) -> str:
    """The single schedule interval a Watchdog runs on.

    One cadence, because the artifact states one and a record that averages two
    schedules into a single field is a record that is wrong. A rule needing two
    rhythms is two Watchdogs, which is also how it should be paused, revised, and
    audited.
    """
    schedules = module.of_op("schedule")
    if len(schedules) != 1:
        raise WatchdogProfileError(
            f"A Watchdog declares exactly one cadence; {module.name!r} declares "
            f"{len(schedules)}"
        )
    return schedules[0].attrs["interval"]


def validate_watchdog(
    module: NanoModule, signals: Sequence[WatchdogSignalSpecV1]
) -> NanoModule:
    """Hold `module` to the Watchdog profile against its declared contract.

    Returns the validated module so a caller can use the result rather than
    re-deriving it. Raises ``WatchdogProfileError`` on the first violation.

    IR validation runs first. ``NanoModule`` is a frozen dataclass, so in-process
    code can build one without passing ``from_dict`` — the same hole
    ``run_module`` closes by re-validating, and for the same reason: a profile
    check over a module that was never held to the IR contract is checking the
    wrong thing.

    Order is deliberate. The opcode check runs before the tier check so a rule
    carrying `ai.signature` is told about `ai.signature` rather than about the
    tier that admits it — the opcode is the thing the author has to remove.
    """
    try:
        checked = module.validate()
    except IRValidationError as error:
        raise WatchdogProfileError(f"Watchdog IR is not valid: {error}") from error

    _check_opcodes(checked)
    _check_tier(checked)
    _check_effects(checked)
    _check_intents(checked)
    cadence_of(checked)
    _check_signal_contract(checked, signals)
    return checked


def _check_opcodes(module: NanoModule) -> None:
    denied = sorted({node.op for node in module.nodes} - WATCHDOG_OPS)
    if denied:
        raise WatchdogProfileError(
            f"Watchdog {module.name!r} uses opcodes outside the profile: "
            f"{', '.join(denied)}"
        )


def _check_tier(module: NanoModule) -> None:
    if module.tier != WATCHDOG_TIER:
        raise WatchdogProfileError(
            f"A Watchdog declares tier {WATCHDOG_TIER!r}; {module.name!r} declares "
            f"{module.tier!r}. The higher tiers exist to admit reasoning "
            "constructs, and a Watchdog contains none."
        )


def _check_effects(module: NanoModule) -> None:
    granted = sorted(set(module.effects) - WATCHDOG_EFFECTS)
    if granted:
        raise WatchdogProfileError(
            f"Watchdog {module.name!r} declares effects outside the profile: "
            f"{', '.join(granted)}"
        )


def _check_intents(module: NanoModule) -> None:
    denied = sorted(set(proposable_intents(module)) - WATCHDOG_INTENT_ACTIONS)
    if denied:
        raise WatchdogProfileError(
            f"Watchdog {module.name!r} proposes {', '.join(denied)}; the profile "
            f"permits {', '.join(sorted(WATCHDOG_INTENT_ACTIONS))}"
        )


def _check_signal_contract(
    module: NanoModule, signals: Sequence[WatchdogSignalSpecV1]
) -> None:
    declared = set(declared_signal_names(signals))
    for name in referenced_signals(module):
        if name not in declared:
            known = ", ".join(sorted(declared)) or "nothing"
            raise WatchdogProfileError(
                f"Watchdog {module.name!r} reads signal {name!r}, which its signal "
                f"contract does not declare (declared: {known}). An undeclared "
                "name compiles into a rule that can never fire, so a typo would "
                "become a control that silently never fires."
            )
