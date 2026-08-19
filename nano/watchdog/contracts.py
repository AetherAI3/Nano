"""The three records a Watchdog deployment is made of, and their vocabulary.

A signal spec says what the host promised to publish. An artifact says which
exact rule was admitted. A receipt says what happened on one frame. Between them
they answer the question an audit actually asks — *what was observed, which rule
evaluated it, what matched, and what was proposed* — without any of them knowing
what the host does next.

Two conventions run through all three.

**Every hash is over canonical bytes.** `json.dumps(..., sort_keys=True,
separators=(",", ":"))` and SHA-256, the same recipe ``NanoModule.content_hash``
uses, so two documents share a digest exactly when they would be read
identically. Field names here are snake_case rather than the IR's camelCase:
these are host-facing records, not IR, and borrowing the IR's spelling would
imply they travel through the same loader.

**Nothing is sampled.** There is no clock in this module and no counter. An
evaluation id is derived from what was evaluated, and `created_at` is injected by
the caller like every other time value in Nano — a receipt that stamped
``time.time()`` on itself could never be replayed byte-for-byte, which would cost
the one property the receipt exists to provide.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Tuple

from ..ir.module import NanoModule
from ..runtime.effects import Intent, LogEntry

# Bumped when the shape of these documents changes in a way a reader must
# notice. It is not the Nano version: a Watchdog record can gain a field without
# the language moving, and the language can move without disturbing a record.
WATCHDOG_CONTRACT_VERSION = "1"


class WatchdogError(ValueError):
    """Base class for every Watchdog failure."""


class WatchdogContractError(WatchdogError):
    """A signal spec, artifact, or receipt is not well formed."""


class WatchdogState:
    """The terminal state of one evaluation.

    Exactly one of these lands on every receipt, and only ``OK`` means the rule
    was actually evaluated against a readable frame.

    ``OK`` is not a health claim. An `OK` receipt proposing nothing says that no
    configured condition matched on a valid frame — a different statement from
    "the thing being watched is fine", and a host that conflates them has built a
    monitor that reports success when it is blind.
    """

    OK = "OK"
    INPUT_UNAVAILABLE = "INPUT_UNAVAILABLE"
    WATCHDOG_INVALID = "WATCHDOG_INVALID"
    WATCHDOG_EVALUATION_FAILED = "WATCHDOG_EVALUATION_FAILED"


WATCHDOG_STATES = (
    WatchdogState.OK,
    WatchdogState.INPUT_UNAVAILABLE,
    WatchdogState.WATCHDOG_INVALID,
    WatchdogState.WATCHDOG_EVALUATION_FAILED,
)

# What the host should do when a required input is not there. Nano declares it
# and carries it; the host enforces it. Both options are closed — there is no
# spelling of "carry on as though the value were zero", because that is the
# failure this module exists to make unrepresentable.
UNAVAILABLE_HOLD = "HOLD"
UNAVAILABLE_OBSERVE_ONLY = "OBSERVE_ONLY"
UNAVAILABLE_POLICIES = (UNAVAILABLE_HOLD, UNAVAILABLE_OBSERVE_ONLY)

# The spelling for a two-state signal. A boolean is exactly 0 or 1 and nothing
# else — not 0/1/2, not -1 for unknown. An unknown value is an absent value, and
# absence has its own channel.
BOOLEAN_DOMAIN = "0/1"


# ---------------------------------------------------------------------------
# canonical bytes
# ---------------------------------------------------------------------------


def canonical_json(document: Mapping[str, Any]) -> str:
    """The one serialization every Watchdog digest is taken over."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def content_address(text: str) -> str:
    """SHA-256 over UTF-8 bytes, spelled the way the rest of Nano spells it."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def frame_document(frame) -> dict:
    """Serialize a ``SignalFrame`` so a receipt carries the exact observation.

    Absence stays absence: a cell the host had no value for is ``null``, never
    zero. That is the rule ``nano/data/frames.py`` applies when reading a file,
    held here at the other end of the pipe.
    """
    return {
        "timestamps": [int(t) for t in frame.timestamps],
        "signals": {
            name: [None if value is None else float(value) for value in series]
            for name, series in frame.signals.items()
        },
    }


def declared_signal_names(signals: Iterable["WatchdogSignalSpecV1"]) -> Tuple[str, ...]:
    """Every declared name, rejecting a repeat.

    A contract is a set of promises. Declaring one name twice means two specs
    disagree about a unit, a bound, or whether the signal is required, and
    picking a winner silently is how a rule ends up validated against a contract
    nobody wrote.
    """
    names: list[str] = []
    for spec in signals:
        if spec.name in names:
            raise WatchdogContractError(
                f"Signal {spec.name!r} is declared more than once in the contract"
            )
        names.append(spec.name)
    return tuple(names)


def _require_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WatchdogContractError(f"{what} must be a non-empty string")
    return value


def _require_positive_int(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise WatchdogContractError(f"{what} must be a positive integer")
    return value


# ---------------------------------------------------------------------------
# signal spec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchdogSignalSpecV1:
    """One named numeric series the host promises to publish.

    ``value_domain`` and ``normalization`` are prose, deliberately — the useful
    ones ("0..100, clamped", "seconds since the last accepted tick") are not
    expressible as a range check, and a field that validated the easy half would
    imply the hard half had been checked too.

    Two conventions are worth stating because getting them wrong is quiet. A
    boolean is `BOOLEAN_DOMAIN`: exactly 0 or 1. And a vocabulary with no real
    ordering must not be flattened onto one number — encoding
    `unknown=0, degraded=1, healthy=2` invents the claim that degraded sits
    between the other two, and every `>=` written against it inherits the
    invention. Publish independent booleans instead. Nano cannot detect that
    mistake, because the numbers look identical to it, so it belongs in review.

    ``freshness_limit_ms`` is a bound on the *host*. Nano reads no clock and
    cannot tell a fresh value from a stale one; requiring the field means the
    obligation is written down and delegated rather than assumed.
    """

    name: str
    unit: str
    source: str
    required: bool
    freshness_limit_ms: int
    description: str
    value_domain: str
    normalization: str = ""

    def __post_init__(self) -> None:
        _require_text(self.name, "Signal 'name'")
        _require_text(self.unit, f"Signal {self.name!r} 'unit'")
        _require_text(self.source, f"Signal {self.name!r} 'source'")
        _require_text(self.description, f"Signal {self.name!r} 'description'")
        _require_text(self.value_domain, f"Signal {self.name!r} 'value_domain'")
        if not isinstance(self.required, bool):
            raise WatchdogContractError(
                f"Signal {self.name!r} 'required' must be a boolean"
            )
        _require_positive_int(
            self.freshness_limit_ms, f"Signal {self.name!r} 'freshness_limit_ms'"
        )
        if not isinstance(self.normalization, str):
            raise WatchdogContractError(
                f"Signal {self.name!r} 'normalization' must be a string"
            )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "unit": self.unit,
            "source": self.source,
            "required": self.required,
            "freshness_limit_ms": self.freshness_limit_ms,
            "description": self.description,
            "value_domain": self.value_domain,
            "normalization": self.normalization,
        }


# ---------------------------------------------------------------------------
# artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchdogArtifactV1:
    """One admitted rule: what it is, what it may read, and what it may propose.

    The module is carried as a field but is **not** part of the serialized
    document — `canonical_ir_hash` binds it instead. That keeps the artifact a
    small record a host can store and diff, and it makes the binding checkable:
    ``evaluate_watchdog`` re-derives the module's content hash before running it,
    so an artifact whose IR was swapped underneath it is rejected rather than
    executed. ``NanoModule.content_hash`` is a real content address over the
    canonical graph, which is what makes that check worth anything;
    ``StrategyGraph`` has no equivalent (see `docs/status.md`), which is why a
    Watchdog is always a module.

    ``name``, ``allowed_intents``, and ``cadence`` are derived from the compiled
    module rather than supplied. A field a caller can set is a claim; a field
    read out of the artifact it describes is a fact.
    """

    watchdog_id: str
    name: str
    revision: int
    nano_version: str
    source_hash: str
    canonical_ir_hash: str
    signals: Tuple[WatchdogSignalSpecV1, ...]
    allowed_intents: Tuple[str, ...]
    cadence: str
    risk_class: str
    unavailable_policy: str
    module: NanoModule = field(repr=False)

    def __post_init__(self) -> None:
        _require_text(self.watchdog_id, "Watchdog 'watchdog_id'")
        _require_text(self.name, "Watchdog 'name'")
        _require_positive_int(self.revision, "Watchdog 'revision'")
        _require_text(self.nano_version, "Watchdog 'nano_version'")
        _require_text(self.source_hash, "Watchdog 'source_hash'")
        _require_text(self.canonical_ir_hash, "Watchdog 'canonical_ir_hash'")
        _require_text(self.cadence, "Watchdog 'cadence'")
        _require_text(self.risk_class, "Watchdog 'risk_class'")
        if self.unavailable_policy not in UNAVAILABLE_POLICIES:
            raise WatchdogContractError(
                "Watchdog 'unavailable_policy' must be one of "
                f"{', '.join(UNAVAILABLE_POLICIES)}, got {self.unavailable_policy!r}"
            )
        if not self.signals:
            raise WatchdogContractError(
                "Watchdog requires a non-empty signal contract — a rule with no "
                "declared inputs cannot have its inputs checked"
            )
        declared_signal_names(self.signals)

    @property
    def required_signals(self) -> Tuple[str, ...]:
        """Declared names the host must publish for evaluation to proceed."""
        return tuple(spec.name for spec in self.signals if spec.required)

    def signal(self, name: str) -> WatchdogSignalSpecV1:
        for spec in self.signals:
            if spec.name == name:
                return spec
        raise KeyError(name)

    def to_dict(self, *, include_hash: bool = True) -> dict:
        """The canonical artifact document. Key order is fixed so it stays diffable."""
        out: dict = {
            "type": "Watchdog",
            "watchdog_contract_version": WATCHDOG_CONTRACT_VERSION,
            "watchdog_id": self.watchdog_id,
            "name": self.name,
            "revision": self.revision,
            "nano_version": self.nano_version,
            "source_hash": self.source_hash,
            "canonical_ir_hash": self.canonical_ir_hash,
            "required_signals": list(self.required_signals),
            "signals": [spec.to_dict() for spec in self.signals],
            "allowed_intents": list(self.allowed_intents),
            "cadence": self.cadence,
            "risk_class": self.risk_class,
            "unavailable_policy": self.unavailable_policy,
        }
        if include_hash:
            out["artifact_hash"] = self.artifact_hash()
        return out

    def artifact_hash(self) -> str:
        """Content address over the exact canonical bytes of this document.

        The hash field itself is excluded, for the reason ``moduleHash`` excludes
        it: a document cannot commit to its own digest.
        """
        return content_address(canonical_json(self.to_dict(include_hash=False)))


# ---------------------------------------------------------------------------
# receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchdogReceiptV1:
    """One evaluation, in enough detail to be argued with later.

    Every key is present on every receipt, including the ones that are null. An
    audit record with a stable key set diffs cleanly; one that drops fields when
    they are empty makes "this run had no missing inputs" and "this run predates
    the field" look the same.

    There is no gate decision here and no record of an effect. Those are the
    host's half of the boundary, and a receipt carrying them would blur the three
    questions it exists to keep apart: what the rule saw, what the rule proposed,
    and what the host authorized. A host attaches its decision beside this
    record, keyed by ``evaluation_id``.
    """

    evaluation_id: str
    watchdog_id: str
    watchdog_revision: int
    nano_version: str
    source_hash: str
    ir_hash: str
    frame_timestamp: Optional[int]
    input_frame: Mapping[str, Any]
    input_frame_hash: str
    input_state: str
    missing_inputs: Tuple[str, ...]
    unavailable_policy: str
    execution_log: Tuple[LogEntry, ...]
    proposed_intents: Tuple[Intent, ...]
    created_at: Optional[int] = None

    def __post_init__(self) -> None:
        if self.input_state not in WATCHDOG_STATES:
            raise WatchdogContractError(
                f"Receipt 'input_state' must be one of {', '.join(WATCHDOG_STATES)}, "
                f"got {self.input_state!r}"
            )

    @property
    def is_evaluated(self) -> bool:
        """Whether the rule actually ran against a readable frame.

        Deliberately not named `is_healthy`. It says the evaluation happened, not
        that what was evaluated is well.
        """
        return self.input_state == WatchdogState.OK

    def to_dict(self) -> dict:
        return {
            "type": "WatchdogReceipt",
            "watchdog_contract_version": WATCHDOG_CONTRACT_VERSION,
            "evaluation_id": self.evaluation_id,
            "watchdog_id": self.watchdog_id,
            "watchdog_revision": self.watchdog_revision,
            "nano_version": self.nano_version,
            "source_hash": self.source_hash,
            "ir_hash": self.ir_hash,
            "frame_timestamp": self.frame_timestamp,
            "input_frame": dict(self.input_frame),
            "input_frame_hash": self.input_frame_hash,
            "input_state": self.input_state,
            "missing_inputs": list(self.missing_inputs),
            "unavailable_policy": self.unavailable_policy,
            "execution_log": [entry.to_dict() for entry in self.execution_log],
            "proposed_intents": [intent.to_dict() for intent in self.proposed_intents],
            "created_at": self.created_at,
        }
