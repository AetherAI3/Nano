"""Run receipts — the ordered run log as a stable, externally-consumable artifact.

``docs/receipts.md`` is the specification: the canonical serialization rules, the
receipt's sections, the timestamp convention, drift detection, and the stability
contract external consumers may build against. It is the one place those rules
are written down, so this module points at it rather than restating it — a second
copy drifts, and the first version of this file proved that by shipping a wrong
sort rule in both.

What lives here:

* ``canonical_bytes`` — the single supported way to turn a receipt, or any part
  of one, into bytes. ``verify_run`` below, ``Backtester.verify_replay``, and
  ``nano replay --report receipt`` all call it, because two encoders would
  eventually disagree about a float.
* ``build_receipt`` — assembles the document for one run.
* ``verify_run`` / ``differences`` — replay a run and say where it drifted.

Nothing here reads a clock, entropy, the environment, or the network;
``tests/test_determinism_guards.py`` enforces that with an AST scan rather than
leaving it to review.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, List, Mapping, Optional, Set, Tuple, Union

from .. import __version__ as NANO_VERSION
from ..ir.module import COMPILER_NAME, COMPILER_VERSION, NanoModule
from ..ir.schema import NANO_IR_VERSION_1_0
from .interpreter import ExecutionResult, MarketFrame
from .vm import ModuleResult, ReasoningProvider, run_module

# Either runtime's result is acceptable: the VM's is a superset of the reference
# interpreter's, and the two fields it adds read as empty for the older shape.
RunResult = Union[ModuleResult, ExecutionResult]

# Bump on any change to the emitted shape, and regenerate `tests/golden/`.
# `test_the_receipt_shape_is_pinned_to_this_version` holds the member names, so
# a shape change cannot land quietly with this number unchanged.
RECEIPT_VERSION = 1


class ReceiptError(ValueError):
    """A value cannot be represented in a receipt's canonical form."""


class ReplayDivergence(Exception):
    """Two runs of the same thing did not serialise to the same bytes."""


# ---------------------------------------------------------------------------
# the canonical serializer
# ---------------------------------------------------------------------------


def _describe(value: Any) -> str:
    """Name a value's type unambiguously.

    Qualified by module for anything outside builtins, because the interesting
    rejections are foreign scalars whose bare class name lies about what they
    are: ``numpy.bool_`` reports ``bool``, which would produce the message "bool
    is not encodable (allowed: ... boolean ...)".
    """
    kind = type(value)
    if kind.__module__ == "builtins":
        return kind.__name__
    return f"{kind.__module__}.{kind.__name__}"


def _check(value: Any, path: str, seen: Set[int]) -> None:
    """Reject anything the canonical form cannot represent, naming where it is.

    Everything ``json.dumps`` would accept-but-mangle is refused here instead:
    it coerces integer object keys to strings (so ``{1: "a", "1": "b"}`` becomes
    one key), and emits bare ``NaN`` for a float that has no JSON spelling. Both
    produce a receipt that round-trips to something other than what was
    recorded, which is worse than a refusal.

    Everything ``json.dumps`` would reject is refused here too, so the error
    carries a path. Only ``dict`` counts as an object: a ``MappingProxyType``
    reaching the encoder raised a bare ``TypeError`` pointing at nothing.
    """
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        # Pure ASCII is the overwhelming case and needs no work. A lone surrogate
        # survives `ensure_ascii` as a `\\udXXX` escape and then fails to decode
        # anywhere else, which breaks the "survives any transport" promise.
        if not value.isascii():
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ReceiptError(
                    f"{path or '/'}: string contains an unpaired surrogate and is "
                    "not valid Unicode, so it cannot appear in a receipt"
                ) from exc
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReceiptError(
                f"{path or '/'}: {value!r} is non-finite and has no JSON "
                "representation, so it cannot appear in a receipt"
            )
        return

    if isinstance(value, (dict, list, tuple)):
        if id(value) in seen:
            raise ReceiptError(
                f"{path or '/'}: contains itself; a receipt must be a finite tree"
            )
        seen.add(id(value))
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ReceiptError(
                        f"{path or '/'}: object keys must be strings, got "
                        f"{_describe(key)} {key!r}"
                    )
                _check(item, f"{path}/{key}", seen)
        else:
            for position, item in enumerate(value):
                _check(item, f"{path}/{position}", seen)
        seen.discard(id(value))
        return

    raise ReceiptError(
        f"{path or '/'}: {_describe(value)} is not canonically encodable "
        "(allowed: dict, list, tuple, str, int, float, bool, None)"
    )


def canonical_bytes(document: Any) -> bytes:
    """The canonical serialization of `document`. See ``docs/receipts.md`` §1.

    Sorted keys, no whitespace, ASCII-escaped, no line terminator. Raises
    ``ReceiptError`` — a ``ValueError`` — for anything the form cannot represent.

    ``ensure_ascii`` guarantees the text is pure ASCII, so the encode below
    cannot fail and cannot depend on a locale.
    """
    _check(document, "", set())
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


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
    an ``ExecutionResult`` from the reference interpreter.

    `host` is the caller's own context — deployment id, wall clock, operator. It
    is deep-copied, it is part of the digest, and it is the *only* section this
    function does not derive from the run.
    """
    identity: dict = {
        "nanoVersion": NANO_VERSION,
        "irVersion": NANO_IR_VERSION_1_0,
        "compiler": {"name": COMPILER_NAME, "version": COMPILER_VERSION},
        "module": module.name,
        "moduleHash": module.content_hash(),
        "tier": module.tier,
        # Declared order, not sorted: `moduleHash` is taken over the manifest as
        # spelled, so emitting a sorted copy would make the receipt disagree with
        # the hash printed beside it.
        "effects": list(module.effects),
        "warmupDeclared": module.warmup,
        # A run that consults a model is only reproducible against the same
        # provider, and the receipt cannot identify the provider. Saying so here
        # is the difference between "replayable" and "replayable if".
        "reasoningRequired": bool(module.of_op("ai.infer")),
    }
    if module.params:
        identity["params"] = [p.to_dict() for p in module.params]

    inputs: dict = {
        "bars": len(frame.timestamps),
        # Sorted, so the artifact does not inherit the column order of whatever
        # CSV the frame was loaded from.
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
            # `intents` and `log` are read directly: both result types define
            # them, so a `getattr` default would turn a future rename into a
            # receipt that silently reports zero intents. Only the two fields
            # `ExecutionResult` genuinely lacks are defaulted.
            "intents": [i.to_dict() for i in result.intents],
            "escalations": [e.to_dict() for e in getattr(result, "escalations", ())],
            "log": [entry.to_dict() for entry in result.log],
            "warmupBarsSkipped": int(getattr(result, "warmup_bars_skipped", 0)),
        },
    }

    source_hash = module.source_hash
    if source_hash is not None:
        # Unauthenticated, and filed apart from identity for that reason.
        receipt["provenance"] = {"sourceHash": source_hash}
    if host is not None:
        # Validated *before* it is copied. `copy.deepcopy` on an uncopyable value
        # — a lock, a socket, an open file — raises a bare TypeError naming
        # nothing, which is exactly the failure `_check` exists to replace, and
        # `host` is the one section where arbitrary caller data lands. After
        # `_check` only canonical types remain, and all of them copy trivially.
        #
        # Deep rather than shallow: a nested structure the caller keeps mutating
        # would otherwise alias into a receipt that has already been digested.
        snapshot = dict(host)
        _check(snapshot, "/host", set())
        receipt["host"] = copy.deepcopy(snapshot)
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
    rather than warns. Raises ``ReceiptError`` if a run produced a value the
    canonical form cannot represent; that is a different fault from drift and is
    deliberately not folded into ``ReplayDivergence``.

    A stateful or model-backed provider is exactly what the first case catches:
    the VM is only as deterministic as the provider it was handed.
    """
    first = build_receipt(
        module, frame, run_module(module, frame, provider=provider), host=host
    )
    second = build_receipt(
        module, frame, run_module(module, frame, provider=provider), host=host
    )
    left, right = canonical_bytes(first), canonical_bytes(second)
    if left != right:
        raise ReplayDivergence(
            f"{module.name!r} did not replay byte-identically; diverged at "
            + ", ".join(differences(first, second))
        )
    return left


def differences(left: Any, right: Any) -> Tuple[str, ...]:
    """Every path at which two documents disagree, in document order.

    Object members are visited in sorted key order and array elements in position
    order, so the same pair always reports the same list. Paths look like
    ``/identity/moduleHash`` and ``/run/log/3/detail``; a length mismatch inside
    an array reports ``.../log[length]`` *and* still descends into the elements
    the two do share.

    Type-sensitive on purpose: ``True`` and ``1`` differ here, as do ``0.0`` and
    ``-0.0``, because they differ in the bytes. It sees exactly what
    ``canonical_bytes`` sees, which is why callers can report its output as the
    reason two byte strings differed. Detection alone would only say *that* a run
    drifted; naming the path says *where*.
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
        # Descend anyway: a truncated log whose surviving entries also changed
        # has two independent problems, and reporting only the length would send
        # a reader looking for one.
        for position in range(min(len(left), len(right))):
            _diff(left[position], right[position], f"{path}/{position}", found)
        return
    if type(left) is not type(right):
        found.append(path or "/")
        return
    if isinstance(left, float):
        # `0.0 == -0.0` is true and their bytes differ, so equality is the wrong
        # test here.
        if repr(left) != repr(right):
            found.append(path or "/")
        return
    if left != right:
        found.append(path or "/")


__all__ = [
    "RECEIPT_VERSION",
    "ReceiptError",
    "ReplayDivergence",
    "RunResult",
    "build_receipt",
    "canonical_bytes",
    "differences",
    "digest_of",
    "frame_digest",
    "receipt_digest",
    "verify_run",
]
