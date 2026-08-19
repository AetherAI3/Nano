"""Run receipts — the ordered run log as a stable, externally-consumable artifact.

A run already produces everything an auditor needs: the intents proposed, the
escalations taken, and the ordered log behind both. What it did not produce was a
*document*: something with a version, a fixed serialization, and a promise about
which bytes stay put. This module is that document.

## The one serializer

``canonical_bytes`` is the only supported way to turn a receipt (or any part of
one) into bytes. Everything that claims byte-stability goes through it —
``verify_run`` here, ``Backtester.verify_replay`` in the bridge, and
``nano replay --report receipt`` in the CLI. One encoder is the point: two would
eventually disagree about a float, and the disagreement would surface as a
receipt that verifies in one tool and not in another.

It continues a convention this repository already had rather than inventing a
second one. ``NanoModule.content_hash`` (``nano/ir/module.py``) has always
content-addressed a module with ``json.dumps(document, sort_keys=True,
separators=(",", ":"))``; the sorted keys and compact separators here are that
same rule, so a ``moduleHash`` embedded in a receipt was computed the same way
the receipt around it was. Two deliberate additions: ``allow_nan=False`` (strictly
stricter — see below) and an explicit ASCII encode of the result. Its exclusion
rule is the precedent for this module's identity/provenance split too: a document
cannot commit to its own digest, and ``provenance`` records where a module came
from rather than what it does.

Its rules, all of them:

* **Object keys are sorted** (byte-wise, over the ASCII-escaped form). Not a
  fixed key order — sorting needs no table to keep in step with the code, and it
  is immune to the order a dictionary happened to be constructed in.
* **Array order is preserved**, because in a run log order *is* the meaning.
* **No whitespace.** ``separators=(",", ":")``.
* **ASCII only.** ``ensure_ascii=True``, and the result is encoded as ASCII, so
  a receipt survives any transport, console codepage, or editor that would have
  mangled UTF-8. A non-ASCII strategy name appears escaped, and the escaping is
  what is hashed.
* **No line terminator.** The canonical form is the digested text and nothing
  else. A writer that frames it as a file or a JSON Lines record appends exactly
  one ``\\n``; that byte is framing, not content, and is not digested.
* **Finite numbers only.** ``NaN``/``Infinity`` are not JSON. Python's encoder
  emits them anyway as bare tokens no conforming parser accepts, so a non-finite
  float raises ``ReceiptError`` rather than producing an unparseable receipt.
* **Absent, not null.** An object member with no value is omitted. The
  deterministic sections of a receipt therefore contain no JSON ``null`` at all.

## Why bytes and not dictionaries

Python says ``{"approved": True} == {"approved": 1}`` and
``{"x": 0.0} == {"x": -0.0}``. Every determinism check in this repository used
to be written as ``a.to_dict() != b.to_dict()``, which means a gate that flipped
between ``True`` and ``1``, or an arithmetic path that produced ``-0.0`` on one
run, passed as "bit-identical replay". Comparing canonical bytes closes that.

## What a receipt is, and is not

A receipt binds a *run* to the *executable identity* that produced it: the Nano
version, the IR version, the compiler, and the module content hash. Those four
answer "what would run this again".

They are deliberately separate from ``provenance`` (``sourceHash`` — where the
module came from) and from ``host`` (anything the caller wants recorded,
including a wall clock). Neither is authenticated. **An unsigned receipt is
evidence of reproducibility, not of authenticity**: anyone can write one. Binding
a receipt to a signer is Protocol-C's job (``nano.bridge.provenance``), it is
optional, and it is deliberately outside deterministic core behavior.

## No ambient anything

Nothing here reads a clock, entropy, the environment, or the network — see
``tests/test_receipts.py`` for the guard that keeps it that way. Every timestamp
in a receipt came out of the injected ``MarketFrame``. A host that wants to
record when a run happened puts it in ``host``, where it is visibly a claim.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, List, Mapping, Optional, Tuple, Union

from .. import __version__ as NANO_VERSION
from ..ir.module import COMPILER_NAME, COMPILER_VERSION, NanoModule
from ..ir.schema import NANO_IR_VERSION_1_0
from .interpreter import ExecutionResult, MarketFrame
from .vm import ModuleResult, ReasoningProvider, run_module

# Either runtime's result is acceptable: the VM's is a superset of the
# reference interpreter's, and the fields it adds read as empty for the older
# shape rather than being an error.
RunResult = Union[ModuleResult, ExecutionResult]

# Bump on any change to the emitted shape, and regenerate `tests/golden/`.
RECEIPT_VERSION = 1


class ReceiptError(ValueError):
    """A value cannot be represented in a receipt's canonical form."""


class ReplayDivergence(Exception):
    """Two runs of the same thing did not serialise to the same bytes."""


# ---------------------------------------------------------------------------
# the canonical serializer
# ---------------------------------------------------------------------------


def _check(value: Any, path: str) -> None:
    """Reject anything the canonical form cannot represent, naming where it is.

    ``json.dumps`` would accept most of this: it coerces integer object keys to
    strings (so ``{1: "a", "1": "b"}`` silently becomes one key), and emits
    ``NaN`` for a float that has no JSON spelling. Both produce a receipt that
    round-trips to something other than what was recorded, which is worse than a
    refusal.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReceiptError(
                f"{path or '/'}: {value!r} is non-finite and has no JSON "
                "representation, so it cannot appear in a receipt"
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReceiptError(
                    f"{path or '/'}: object keys must be strings, got "
                    f"{type(key).__name__} {key!r}"
                )
            _check(item, f"{path}/{key}")
        return
    if isinstance(value, (list, tuple)):
        for position, item in enumerate(value):
            _check(item, f"{path}/{position}")
        return
    raise ReceiptError(
        f"{path or '/'}: {type(value).__name__} is not canonically encodable "
        "(allowed: object, array, string, number, boolean, null)"
    )


def canonical_text(document: Any) -> str:
    """The canonical serialization of `document`, as text.

    Sorted keys, no whitespace, ASCII-escaped, no trailing newline.
    """
    _check(document, "")
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def canonical_bytes(document: Any) -> bytes:
    """The canonical serialization of `document`, as bytes.

    ``ensure_ascii`` guarantees the text is pure ASCII, so this encoding cannot
    fail and cannot depend on a locale.
    """
    return canonical_text(document).encode("ascii")


def digest_of(payload: bytes) -> str:
    """``sha256:<hex>`` over exactly `payload`."""
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    """Content address of a receipt: sha256 over its canonical bytes.

    Deliberately not a field inside the receipt — a document cannot commit to its
    own digest, the same reason ``moduleHash`` is excluded from the module hash.
    """
    return digest_of(canonical_bytes(receipt))


def frame_digest(frame: MarketFrame) -> str:
    """Content address of the injected market data.

    Covers the timeline and every named series, so a receipt records *which*
    inputs produced it without carrying the whole frame. Signal insertion order
    does not matter; the values do.
    """
    return digest_of(
        canonical_bytes(
            {
                "timestamps": list(frame.timestamps),
                "signals": {
                    name: list(series) for name, series in frame.signals.items()
                },
            }
        )
    )


# ---------------------------------------------------------------------------
# building a receipt
# ---------------------------------------------------------------------------


def build_receipt(
    module: NanoModule,
    frame: MarketFrame,
    result: RunResult,
    *,
    host: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Assemble the receipt for one run of `module` over `frame`.

    `result` is whatever the runtime returned — a ``ModuleResult`` from the VM or
    an ``ExecutionResult`` from the reference interpreter. The fields the older
    shape does not have (escalations, warm-up accounting) read as empty rather
    than being an error, so a baseline run and a v1.0 run of the same lifted
    module produce comparable receipts.

    `host` is the caller's own context — deployment id, wall clock, operator.
    It is copied in verbatim, it is part of the digest, and it is the *only*
    section this function does not derive from the run.
    """
    identity: dict = {
        "nanoVersion": NANO_VERSION,
        "irVersion": NANO_IR_VERSION_1_0,
        "compiler": {"name": COMPILER_NAME, "version": COMPILER_VERSION},
        "module": module.name,
        "moduleHash": module.content_hash(),
        "tier": module.tier,
        "effects": list(module.effects),
        "warmupDeclared": module.warmup,
        # A run that consults a model is only reproducible against the same
        # provider, and the receipt cannot identify the provider. Saying so
        # here is the difference between "replayable" and "replayable if".
        "reasoningRequired": bool(module.of_op("ai.infer")),
    }
    if module.params:
        identity["params"] = [p.to_dict() for p in module.params]

    inputs: dict = {
        "bars": len(frame.timestamps),
        "signals": sorted(frame.signals),
        "frameHash": frame_digest(frame),
    }
    if frame.timestamps:
        inputs["firstTimestamp"] = frame.timestamps[0]
        inputs["lastTimestamp"] = frame.timestamps[-1]

    receipt: dict = {
        "receiptVersion": RECEIPT_VERSION,
        "identity": identity,
        "inputs": inputs,
        "run": {
            "intents": [i.to_dict() for i in getattr(result, "intents", ())],
            "escalations": [e.to_dict() for e in getattr(result, "escalations", ())],
            "log": [e.to_dict() for e in getattr(result, "log", ())],
            "warmupBarsSkipped": int(getattr(result, "warmup_bars_skipped", 0)),
        },
    }

    source_hash = module.source_hash
    if source_hash is not None:
        # Unauthenticated, and filed apart from identity for that reason.
        receipt["provenance"] = {"sourceHash": source_hash}
    if host is not None:
        receipt["host"] = dict(host)
    return receipt


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def verify_run(
    module: NanoModule,
    frame: MarketFrame,
    *,
    provider: Optional[ReasoningProvider] = None,
    host: Optional[Mapping[str, Any]] = None,
) -> bytes:
    """Run twice and prove the two runs serialise to identical bytes.

    Returns those bytes. Raises ``ReplayDivergence`` naming the paths that
    drifted — a divergence invalidates every number the run produced, so it fails
    rather than warns.

    A stateful or model-backed provider is exactly what this catches: the VM is
    only as deterministic as the provider it was handed.
    """
    first = build_receipt(
        module, frame, run_module(module, frame, provider=provider), host=host
    )
    second = build_receipt(
        module, frame, run_module(module, frame, provider=provider), host=host
    )
    left, right = canonical_bytes(first), canonical_bytes(second)
    if left != right:
        drift = differences(first, second) or ("(identical structure, different bytes)",)
        raise ReplayDivergence(
            f"{module.name!r} did not replay byte-identically; diverged at "
            + ", ".join(drift)
        )
    return left


def differences(left: Any, right: Any) -> Tuple[str, ...]:
    """Every path at which two receipts disagree, in document order.

    Object members are visited in sorted key order and array elements in
    position order, so the same pair of receipts always reports the same list.

    Paths look like ``/identity/moduleHash`` and ``/run/log/3/detail``. A length
    mismatch inside an array reports ``.../log[length]``.

    Type-sensitive on purpose: ``True`` and ``1`` differ here, as do ``0.0`` and
    ``-0.0``, because they differ in the bytes. Detection alone would only say
    *that* a run drifted; naming the path says *where*, which is the difference
    between a usable signal and an alarm.
    """
    found: List[str] = []
    _diff(left, right, "", found)
    return tuple(found)


def _diff(left: Any, right: Any, path: str, found: List[str]) -> None:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                found.append(f"{path}/{key}")
            else:
                _diff(left[key], right[key], f"{path}/{key}", found)
        return
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        if len(left) != len(right):
            found.append(f"{path}[length]")
        for position in range(min(len(left), len(right))):
            _diff(left[position], right[position], f"{path}/{position}", found)
        return
    if type(left) is not type(right):
        found.append(path or "/")
        return
    if isinstance(left, float):
        # `0.0 == -0.0` is true and their bytes differ, so equality is the wrong
        # test here: this function has to see everything `canonical_bytes` sees.
        if repr(left) != repr(right):
            found.append(path or "/")
        return
    if left != right:
        found.append(path or "/")


__all__ = [
    "RECEIPT_VERSION",
    "RunResult",
    "ReceiptError",
    "ReplayDivergence",
    "build_receipt",
    "canonical_bytes",
    "canonical_text",
    "differences",
    "digest_of",
    "frame_digest",
    "receipt_digest",
    "verify_run",
]
