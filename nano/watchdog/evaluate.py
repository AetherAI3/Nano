"""Compile a Watchdog, and evaluate one against one frame.

There is no evaluator in this file. ``compile_watchdog`` calls the compiler and
``evaluate_watchdog`` calls the VM — a second interpreter would eventually
disagree with the first, and the disagreement would surface as a control that
behaved one way in review and another way in production. What this module adds
around that single call is three things the base runtime deliberately has no
opinion about:

**An availability gate, in front.** ``run_module`` raises when a signal is not on
the frame, and treats a `None` cell as "not warmed up yet" — correct for a
strategy, where a bar with no data is a bar you skip. A control cannot skip. If
the number that would have decided whether to hold is not there, the honest
answer is `INPUT_UNAVAILABLE` and a list of what was missing, not a silence that
reads like calm. `missing != 0`, and there is no argument that turns it into one.

**An integrity check, in front of that.** The artifact names an IR hash; the
module it carries has one. If they disagree, the artifact is describing something
other than what is about to run, and it is refused.

**A receipt, behind.** Whatever happened — including a fault — comes back as a
record rather than an exception, because a Watchdog usually has siblings and one
broken rule must not take the loop down with it.

Whatever gate a host applies afterwards is the host's. Nano proposes; nothing
here decides.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from .. import __version__ as NANO_VERSION
from ..compiler import compile_module, source_hash
from ..runtime.effects import LogEntry
from ..runtime.vm import run_module
from .contracts import (
    UNAVAILABLE_HOLD,
    WatchdogArtifactV1,
    WatchdogError,
    WatchdogReceiptV1,
    WatchdogSignalSpecV1,
    WatchdogState,
    _snapshot_frame,
    canonical_json,
    content_address,
)
from .profile import (
    cadence_of,
    proposable_intents,
    referenced_signals,
    validate_watchdog,
)


class WatchdogIntegrityError(WatchdogError):
    """The artifact no longer describes the module it carries."""


def compile_watchdog(
    source: str,
    *,
    watchdog_id: str,
    revision: int,
    signals: Sequence[WatchdogSignalSpecV1],
    risk_class: str,
    unavailable_policy: str = UNAVAILABLE_HOLD,
) -> WatchdogArtifactV1:
    """Compile `.nano` source into an admitted Watchdog artifact.

    The compiler runs first and unchanged — a Watchdog is not a dialect, it is a
    Nano module that passed a further check. ``compile_module`` already
    round-trips its output through the IR loader, so by the time the profile sees
    it the module is valid IR; the profile then decides whether it is a valid
    *Watchdog*. Raises ``NanoCompileError`` for bad source and
    ``WatchdogProfileError`` for a rule outside the profile.
    """
    specs = tuple(signals)
    module = validate_watchdog(compile_module(source), specs)
    return WatchdogArtifactV1(
        watchdog_id=watchdog_id,
        name=module.name,
        revision=revision,
        nano_version=NANO_VERSION,
        source_hash=source_hash(source),
        canonical_ir_hash=module.content_hash(),
        signals=specs,
        allowed_intents=proposable_intents(module),
        cadence=cadence_of(module),
        risk_class=risk_class,
        unavailable_policy=unavailable_policy,
        module=module,
    )


def unavailable_signals(artifact: WatchdogArtifactV1, frame) -> Tuple[str, ...]:
    """Declared or referenced signals the frame cannot answer for, sorted.

    Two sources, unioned. A signal the contract marks `required` is gated because
    the host said the rule cannot be trusted without it. A signal the *rule reads*
    is gated whether or not the contract calls it required — otherwise
    `required=False` would be a way to talk the evaluator into comparing against
    a value it does not have.

    A signal counts as unavailable when the frame does not carry it, or when its
    value at the last bar is absent. The last bar is the observation the proposal
    is about; a gap earlier in the frame is history the VM already refuses to
    fire on and records as `condition.unwarmed`. Absence *now* means the rule
    cannot judge now. Absence *then* does not.
    """
    watched = set(artifact.required_signals) | set(referenced_signals(artifact.module))
    missing = []
    for name in sorted(watched):
        series = frame.signals.get(name)
        if series is None or len(series) == 0 or series[-1] is None:
            missing.append(name)
    return tuple(missing)


def evaluate_watchdog(
    artifact: WatchdogArtifactV1, frame, *, created_at: Optional[int] = None
) -> WatchdogReceiptV1:
    """Evaluate `artifact` against one frame and return a receipt.

    Never raises for a fault in the rule: a malformed or failing Watchdog comes
    back as `WATCHDOG_INVALID` or `WATCHDOG_EVALUATION_FAILED`, so the watchdogs
    beside it keep running.

    `created_at` is injected, like every other time value in Nano. Nothing here
    reads a clock, which is what lets ``replay_watchdog`` reproduce a receipt
    byte for byte.
    """
    snapshot = _snapshot_frame(frame)
    frame = snapshot.frame
    document = snapshot.document
    frame_hash = content_address(canonical_json(document))
    timestamp = frame.timestamps[-1] if frame.timestamps else None
    # A frame with no observation still needs somewhere to hang its log entry.
    # The VM makes the same substitution, for the same reason.
    log_timestamp = timestamp if timestamp is not None else 0

    def receipt(state: str, *, missing=(), log=(), intents=()) -> WatchdogReceiptV1:
        return WatchdogReceiptV1(
            # A content address, not a nonce: the same rule over the same frame
            # is the same evaluation. A sampled id would make byte-identical
            # replay impossible, and a store needing uniqueness can add its own
            # key beside this one.
            evaluation_id=content_address(
                canonical_json(
                    {"artifact": artifact.artifact_hash(), "input_frame": frame_hash}
                )
            ),
            watchdog_id=artifact.watchdog_id,
            watchdog_revision=artifact.revision,
            nano_version=artifact.nano_version,
            source_hash=artifact.source_hash,
            ir_hash=artifact.canonical_ir_hash,
            frame_timestamp=timestamp,
            input_frame=document,
            input_frame_hash=frame_hash,
            input_state=state,
            missing_inputs=tuple(missing),
            unavailable_policy=artifact.unavailable_policy,
            execution_log=tuple(log),
            proposed_intents=tuple(intents),
            created_at=created_at,
        )

    try:
        module = _admitted_module(artifact)
    except WatchdogError as error:
        return receipt(
            WatchdogState.WATCHDOG_INVALID,
            log=(LogEntry("watchdog.invalid", log_timestamp, str(error)),),
        )

    missing = unavailable_signals(artifact, frame)
    if missing:
        return receipt(
            WatchdogState.INPUT_UNAVAILABLE,
            missing=missing,
            log=(
                LogEntry(
                    "input.unavailable",
                    log_timestamp,
                    f"{', '.join(missing)} not published as a finite value at "
                    "the evaluated bar; "
                    f"unavailable_policy={artifact.unavailable_policy}",
                ),
            ),
        )

    try:
        # `validate=False` is safe here and only here: `module` is what
        # ``validate_watchdog`` returned, which is the output of
        # ``NanoModule.validate()`` — the exact check ``run_module`` would
        # repeat. Validating twice per evaluation would pay for the same pass
        # over the nodes on every tick of every watchdog.
        result = run_module(module, frame, validate=False)
    except Exception as error:  # noqa: BLE001 - isolation is the point
        # Deliberately broad. Anything the VM can raise has to become a filed
        # receipt rather than an exception crossing into a caller that is part
        # way through a list of other watchdogs. `Exception` still lets
        # KeyboardInterrupt and SystemExit past, which is correct.
        return receipt(
            WatchdogState.WATCHDOG_EVALUATION_FAILED,
            log=(
                LogEntry(
                    "watchdog.failed",
                    log_timestamp,
                    f"{type(error).__name__}: {error}",
                ),
            ),
        )

    # Escalations are not read: `llmre.escalate` and `route` are outside the
    # profile, so a compliant module cannot produce one.
    return receipt(WatchdogState.OK, log=result.log, intents=result.intents)


def _admitted_module(artifact: WatchdogArtifactV1):
    """Re-check that the artifact still describes the module it is about to run.

    The artifact commits to an IR hash. If the module underneath it no longer
    hashes to that value, something replaced the rule without revising the record
    that authorised it, and running it would produce a receipt naming an artifact
    that did not decide anything.
    """
    actual = artifact.module.content_hash()
    if actual != artifact.canonical_ir_hash:
        raise WatchdogIntegrityError(
            f"Watchdog {artifact.watchdog_id!r} rev {artifact.revision} declares IR "
            f"{artifact.canonical_ir_hash}, but the module it carries hashes to "
            f"{actual}"
        )
    return validate_watchdog(artifact.module, artifact.signals)
