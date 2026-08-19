"""Deterministic watchdogs — a profile, an artifact, a receipt, and a replay.

A Watchdog is an ordinary Nano rule admitted under a narrower contract, for the
job the README describes: a small program that reads host-published numbers,
recognises a policy condition, and proposes a bounded response somebody else
decides about.

    host observations
        -> validated Nano rule
        -> deterministic evaluation
        -> proposed Intent(s)
        -> host DecisionGate
        -> recorded Decision

Nano owns the first four steps and nothing after them. The last two are the
host's, and no function in this package can reach them.

Nothing here is a new language or a new runtime. ``compile_watchdog`` is
``nano.compiler.compile_module`` plus an admission check; ``evaluate_watchdog``
is one call to ``nano.runtime.run_module`` with a gate in front and a record
behind. What the package adds is the part the base runtime is right not to have
an opinion about:

| Concern | Why it is not in the runtime |
|---|---|
| Signal contract | A strategy's undeclared name is a feature; a control's is a typo that never fires |
| Missing input | A strategy skips an unwarmed bar; a control cannot skip and call it calm |
| Artifact identity | A strategy is compiled and run; a control is admitted, versioned, and later questioned |
| Isolation | A strategy runs alone; a watchdog runs beside others that must survive it |

Start with ``docs/watchdog_profile.md``, which states the profile and the two
rules that are easiest to get wrong: what a signal contract is for, and why "no
intent" is never a health claim.
"""

from .contracts import (
    BOOLEAN_DOMAIN,
    UNAVAILABLE_HOLD,
    UNAVAILABLE_OBSERVE_ONLY,
    UNAVAILABLE_POLICIES,
    WATCHDOG_CONTRACT_VERSION,
    WATCHDOG_STATES,
    WatchdogArtifactV1,
    WatchdogContractError,
    WatchdogError,
    WatchdogReceiptV1,
    WatchdogSignalSpecV1,
    WatchdogState,
    canonical_json,
    content_address,
    frame_document,
)
from .evaluate import (
    WatchdogIntegrityError,
    compile_watchdog,
    evaluate_watchdog,
    unavailable_signals,
)
from .profile import (
    WATCHDOG_EFFECTS,
    WATCHDOG_INTENT_ACTIONS,
    WATCHDOG_OPS,
    WATCHDOG_TIER,
    WatchdogProfileError,
    cadence_of,
    proposable_intents,
    referenced_signals,
    validate_watchdog,
)
from .replay import WatchdogReplayMismatch, frame_from_document, replay_watchdog

__all__ = [
    "BOOLEAN_DOMAIN",
    "UNAVAILABLE_HOLD",
    "UNAVAILABLE_OBSERVE_ONLY",
    "UNAVAILABLE_POLICIES",
    "WATCHDOG_CONTRACT_VERSION",
    "WATCHDOG_EFFECTS",
    "WATCHDOG_INTENT_ACTIONS",
    "WATCHDOG_OPS",
    "WATCHDOG_STATES",
    "WATCHDOG_TIER",
    "WatchdogArtifactV1",
    "WatchdogContractError",
    "WatchdogError",
    "WatchdogIntegrityError",
    "WatchdogProfileError",
    "WatchdogReceiptV1",
    "WatchdogReplayMismatch",
    "WatchdogSignalSpecV1",
    "WatchdogState",
    "cadence_of",
    "canonical_json",
    "compile_watchdog",
    "content_address",
    "evaluate_watchdog",
    "frame_document",
    "frame_from_document",
    "proposable_intents",
    "referenced_signals",
    "replay_watchdog",
    "unavailable_signals",
    "validate_watchdog",
]
