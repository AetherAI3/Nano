"""Replay a receipt against the artifact that produced it.

The claim being checked is narrow and total: the same artifact over the same
input frame produces a byte-identical receipt — the same proposals, in the same
order, with the same ordered evaluation log. A mismatch is not a warning. It
means one of the inputs is not what the record says it was, or something in the
evaluation path is reading a value nobody injected, and either way the receipts
already archived cannot be trusted to describe what happened.

**The frame is rebuilt from the receipt, not reused from the caller.** Handing
the original ``SignalFrame`` object back to the evaluator would prove only that
the evaluator is a function, which is already true and already tested. Reading it
back out of ``input_frame`` proves the thing an audit needs: that the receipt on
its own is sufficient to reproduce the decision, with no live system standing by
to re-answer questions about what the inputs were.

**What this does not cover.** The gate is the host's, so replay here ends where
Nano ends — at the proposal. A host verifying its own gate replays that half with
``Backtester.verify_replay`` (``nano/bridge/backtester.py``), which is a
different check with a different failure mode. Neither substitutes for the other.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from ..runtime.interpreter import MarketFrame
from ..runtime.receipt import differences
from .contracts import (
    WatchdogArtifactV1,
    WatchdogContractError,
    WatchdogError,
    WatchdogReceiptV1,
    canonical_json,
)
from .evaluate import evaluate_watchdog


class WatchdogReplayMismatch(WatchdogError):
    """A replay did not reproduce the receipt. Treat as an integrity failure."""


def frame_from_document(document: Mapping[str, Any]) -> MarketFrame:
    """Rebuild a frame from a receipt's ``input_frame``.

    ``null`` becomes ``None``, never ``0.0``. Reading absence back as a number
    here would make a replay of a gap-bearing frame quietly disagree with the
    original run — the exact failure the missing-input semantics exist to
    prevent, reintroduced at the verification step.
    """
    try:
        if type(document) is not dict:
            raise TypeError("input_frame must be a built-in object")
        timestamps, signals = document["timestamps"], document["signals"]
        if type(timestamps) is not list or type(signals) is not dict:
            raise TypeError("timestamps must be a list and signals must be an object")
        if any(type(timestamp) is not int for timestamp in timestamps):
            raise TypeError("timestamps must contain integers")
        normalized_signals = {}
        for name, series in signals.items():
            if type(name) is not str or type(series) is not list:
                raise TypeError("signals must map string names to built-in lists")
            values = []
            for value in series:
                if value is None:
                    values.append(None)
                elif type(value) in (bool, int, float) and math.isfinite(float(value)):
                    values.append(float(value))
                else:
                    raise ValueError("signal values must be finite numbers or null")
            normalized_signals[name] = tuple(values)
        return MarketFrame(
            timestamps=tuple(timestamps),
            signals=normalized_signals,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise WatchdogReplayMismatch(
            f"Receipt 'input_frame' is not a readable frame: {error}"
        ) from error


def replay_watchdog(
    artifact: WatchdogArtifactV1, receipt: WatchdogReceiptV1
) -> WatchdogReceiptV1:
    """Re-derive `receipt` from `artifact` and its recorded frame.

    Returns the replayed receipt when it matches. Raises
    ``WatchdogReplayMismatch`` naming the fields that diverged when it does not.

    ``created_at`` is carried across rather than re-taken: it is injected on the
    way in, so a replay that stamped a fresh value would report a mismatch on
    every run and teach whoever reads the alert to ignore it.
    """
    frame = frame_from_document(receipt.input_frame)
    replayed = evaluate_watchdog(artifact, frame, created_at=receipt.created_at)

    recorded_document, replayed_document = receipt.to_dict(), replayed.to_dict()
    try:
        recorded_bytes = canonical_json(recorded_document)
        replayed_bytes = canonical_json(replayed_document)
    except WatchdogContractError as error:
        raise WatchdogReplayMismatch(
            f"Receipt cannot be replayed as canonical bytes: {error}"
        ) from error
    if replayed_bytes != recorded_bytes:
        raise WatchdogReplayMismatch(_describe(recorded_document, replayed_document))
    return replayed


def _describe(recorded: Mapping[str, Any], replayed: Mapping[str, Any]) -> str:
    """Name every field that diverged, so the alert points somewhere."""
    diverged = differences(recorded, replayed)
    return (
        f"Replay of watchdog {recorded.get('watchdog_id')!r} rev "
        f"{recorded.get('watchdog_revision')} diverged from the receipt on: "
        f"{', '.join(diverged)}. The receipt no longer describes what this "
        "artifact does with these inputs."
    )
