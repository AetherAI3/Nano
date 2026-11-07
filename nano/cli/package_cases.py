"""Strict, versioned local fixture runner behind ``nano package test --cases``.

The contract is frozen in ``docs/package-cli.md`` (``test --cases PATH``). This
module is a pure library: no argparse, no file or network I/O, no environment
reads. It parses bytes it is handed, admits a validated ``NanoModule`` and
evaluates each case with ``run_module(module, frame, provider=None)``.

Intent versus action
--------------------
A fixture asserts **proposed** intents. The runner has no actuator: nothing in
this module, and no field a fixture can carry, reaches a broker, an effect
handler, a reasoning provider, a clock, an RNG, a model or a QPU. ``execute()``
lowers to an ``EXECUTE`` intent value in ``ModuleResult.intents`` -- the same
proposal a host's DecisionGate would later authorise or refuse -- and the
runner only compares that value. Reports therefore name the field
``proposedIntents`` and carry ``actionsExecuted`` equal to ``ACTIONS_EXECUTED``,
which is the constant ``0``.

Admission happens before any case runs: a module is refused with
``UNSUPPORTED_HANDLER`` unless its tier is ``nano``, its effects are a subset of
``ALLOWED_EFFECTS`` and it contains none of ``UNSUPPORTED_OPS`` (inference,
escalation, route and signature nodes).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from nano.ir.module import OPS, NanoModule
from nano.ir.schema import INTENT_ACTIONS, TIER_REQUIREMENTS
from nano.runtime import vm as _vm
from nano.runtime.interpreter import MarketFrame, RuntimeError_

CASES_VERSION = "1.0.0"
MAX_CASES = 256
MAX_BARS = 4096
MAX_CASES_BYTES = 1048576
ALLOWED_EFFECTS = frozenset({"intent.emit", "log.append"})

# The runner has no actuator; every report asserts this constant.
ACTIONS_EXECUTED = 0

# Every op that needs a tier above ``nano`` (``ai.signature``, ``ai.infer``,
# ``llmre.escalate``, ``route``) or an effect outside ``ALLOWED_EFFECTS``.
# Derived from the IR op table so a future reasoning op is refused by default.
UNSUPPORTED_OPS = frozenset(
    op
    for op, spec in OPS.items()
    if spec.construct in TIER_REQUIREMENTS
    or (spec.effect is not None and spec.effect not in ALLOWED_EFFECTS)
)

_ROOT_KEYS = frozenset({"nanoPackageCasesVersion", "cases"})
_CASE_KEYS = frozenset({"name", "frame", "expect"})
_FRAME_KEYS = frozenset({"timestamps", "signals"})
_EXPECT_KEYS = frozenset({"proposedIntents", "runtimeError"})
_INTENT_KEYS = frozenset({"intent", "timestamp", "asset", "confidence"})


# -- errors -------------------------------------------------------------------


class FixtureError(ValueError):
    """The fixture file is malformed; no case runs."""

    code = "FIXTURE_ERROR"
    category = "fixture"

    def __init__(self, pointer: str, message: str) -> None:
        self.pointer = pointer
        self.message = message
        super().__init__(f"{pointer}: {message}" if pointer else message)


class UnsupportedHandlerError(ValueError):
    """The module needs a capability the fixture runner cannot supply."""

    code = "UNSUPPORTED_HANDLER"
    category = "capability"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


# -- values -------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One fixture case. Exactly one of the two expectations is set."""

    name: str
    frame: MarketFrame
    expected_intents: Optional[Tuple[Mapping[str, Any], ...]] = None
    expected_runtime_error: Optional[str] = None


@dataclass(frozen=True)
class CaseSet:
    version: str
    cases: Tuple[Case, ...]


@dataclass(frozen=True)
class CaseResult:
    name: str
    status: str
    observed: Mapping[str, Any]
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "observed": _thaw(self.observed),
        }
        if self.reason is not None:
            out["reason"] = self.reason
        return out


# -- decoding -----------------------------------------------------------------


class _Object(dict):
    """A parsed JSON object that remembers its first duplicate key."""

    duplicate: Optional[str] = None


class _NonFinite:
    """Placeholder for NaN/Infinity/overflowing floats, located after parsing."""

    def __init__(self, token: str) -> None:
        self.token = token


def _object_pairs(pairs: List[Tuple[str, Any]]) -> _Object:
    obj = _Object()
    for key, value in pairs:
        if key in obj and obj.duplicate is None:
            obj.duplicate = key
        obj[key] = value
    return obj


def _parse_float(text: str) -> Any:
    value = float(text)
    return value if math.isfinite(value) else _NonFinite(text)


def _ptr(base: str, token: Any) -> str:
    """Append one RFC 6901 reference token."""
    return base + "/" + str(token).replace("~", "~0").replace("/", "~1")


def _walk(value: Any) -> Iterator[Tuple[str, Any]]:
    """Pre-order, document-order traversal without recursion."""
    stack: List[Tuple[str, Any]] = [("", value)]
    while stack:
        pointer, node = stack.pop()
        yield pointer, node
        if isinstance(node, dict):
            children = [(_ptr(pointer, k), v) for k, v in node.items()]
        elif isinstance(node, list):
            children = [(_ptr(pointer, i), v) for i, v in enumerate(node)]
        else:
            continue
        stack.extend(reversed(children))


def _decode(raw: bytes) -> Any:
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("load_cases expects bytes")
    if len(raw) > MAX_CASES_BYTES:
        raise FixtureError("", f"fixture exceeds {MAX_CASES_BYTES} bytes")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FixtureError("", f"fixture is not valid UTF-8: {exc.reason}") from None
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_pairs,
            parse_constant=_NonFinite,
            parse_float=_parse_float,
        )
    except RecursionError:
        raise FixtureError("", "fixture nesting is too deep") from None
    except ValueError as exc:
        raise FixtureError("", f"fixture is not valid JSON: {exc}") from None
    for pointer, node in _walk(value):
        if isinstance(node, _NonFinite):
            raise FixtureError(pointer, f"non-finite number {node.token} is not allowed")
        if isinstance(node, _Object) and node.duplicate is not None:
            raise FixtureError(
                _ptr(pointer, node.duplicate), f"duplicate key {node.duplicate!r}"
            )
    return value


# -- schema -------------------------------------------------------------------


def _exact_keys(
    value: Any, pointer: str, what: str, allowed: frozenset, required: Tuple[str, ...]
) -> None:
    if not isinstance(value, dict):
        raise FixtureError(pointer, f"{what} must be an object")
    for key in value:
        if key not in allowed:
            raise FixtureError(_ptr(pointer, key), f"unknown key {key!r} in {what}")
    for key in required:
        if key not in value:
            raise FixtureError(_ptr(pointer, key), f"missing key {key!r} in {what}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _timestamps(value: Any, pointer: str) -> Tuple[int, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_BARS:
        raise FixtureError(pointer, f"timestamps must be a list of 1..{MAX_BARS} integers")
    for index, item in enumerate(value):
        if not _is_int(item):
            raise FixtureError(_ptr(pointer, index), "timestamp must be an integer")
        if index and item <= value[index - 1]:
            raise FixtureError(_ptr(pointer, index), "timestamps must be strictly increasing")
    return tuple(value)


def _signals(value: Any, pointer: str, bars: int) -> Mapping[str, Tuple[Any, ...]]:
    if not isinstance(value, dict):
        raise FixtureError(pointer, "signals must be an object")
    series: Dict[str, Tuple[Any, ...]] = {}
    for name, items in value.items():
        at = _ptr(pointer, name)
        if not isinstance(items, list):
            raise FixtureError(at, "signal series must be a list")
        if len(items) != bars:
            raise FixtureError(at, f"signal has {len(items)} points for {bars} timestamps")
        for index, item in enumerate(items):
            if not (_is_number(item) or isinstance(item, bool)):
                raise FixtureError(
                    _ptr(at, index), "signal value must be a number or boolean"
                )
        series[name] = tuple(items)
    return MappingProxyType(series)


def _frame(value: Any, pointer: str) -> MarketFrame:
    _exact_keys(value, pointer, "frame", _FRAME_KEYS, ("timestamps", "signals"))
    timestamps = _timestamps(value["timestamps"], _ptr(pointer, "timestamps"))
    signals = _signals(value["signals"], _ptr(pointer, "signals"), len(timestamps))
    try:
        return MarketFrame(timestamps=timestamps, signals=signals)
    except ValueError as exc:
        raise FixtureError(pointer, str(exc)) from None


def _intent(value: Any, pointer: str) -> Mapping[str, Any]:
    """One intent exactly as ``Intent.to_dict()`` emits it."""
    _exact_keys(value, pointer, "intent", _INTENT_KEYS, ("intent", "timestamp"))
    action = value["intent"]
    if not isinstance(action, str) or action not in INTENT_ACTIONS:
        raise FixtureError(
            _ptr(pointer, "intent"),
            f"intent must be one of {', '.join(sorted(INTENT_ACTIONS))}",
        )
    if not _is_int(value["timestamp"]):
        raise FixtureError(_ptr(pointer, "timestamp"), "timestamp must be an integer")
    if "asset" in value and not (isinstance(value["asset"], str) and value["asset"]):
        raise FixtureError(_ptr(pointer, "asset"), "asset must be a non-empty string")
    if "confidence" in value and not _is_number(value["confidence"]):
        raise FixtureError(_ptr(pointer, "confidence"), "confidence must be a number")
    return MappingProxyType(dict(value))


def _expect(value: Any, pointer: str) -> Dict[str, Any]:
    _exact_keys(value, pointer, "expect", _EXPECT_KEYS, ())
    if len(value) != 1:
        raise FixtureError(
            pointer, "expect must have exactly one of 'proposedIntents' or 'runtimeError'"
        )
    if "runtimeError" in value:
        message = value["runtimeError"]
        if not isinstance(message, str) or not message:
            raise FixtureError(
                _ptr(pointer, "runtimeError"), "runtimeError must be a non-empty string"
            )
        return {"expected_runtime_error": message}
    at = _ptr(pointer, "proposedIntents")
    intents = value["proposedIntents"]
    if not isinstance(intents, list):
        raise FixtureError(at, "proposedIntents must be a list")
    return {
        "expected_intents": tuple(
            _intent(item, _ptr(at, index)) for index, item in enumerate(intents)
        )
    }


def _case(value: Any, pointer: str) -> Case:
    _exact_keys(value, pointer, "case", _CASE_KEYS, ("name", "frame", "expect"))
    name = value["name"]
    if not isinstance(name, str) or not name:
        raise FixtureError(_ptr(pointer, "name"), "name must be a non-empty string")
    frame = _frame(value["frame"], _ptr(pointer, "frame"))
    return Case(name=name, frame=frame, **_expect(value["expect"], _ptr(pointer, "expect")))


def load_cases(raw: bytes) -> CaseSet:
    """Parse and validate fixture bytes; raise ``FixtureError`` on any violation."""
    value = _decode(raw)
    _exact_keys(value, "", "fixture", _ROOT_KEYS, ())
    version = value.get("nanoPackageCasesVersion")
    if not isinstance(version, str) or version != CASES_VERSION:
        raise FixtureError(
            "/nanoPackageCasesVersion", f"nanoPackageCasesVersion must be {CASES_VERSION!r}"
        )
    if "cases" not in value:
        raise FixtureError("/cases", "missing key 'cases' in fixture")
    raw_cases = value["cases"]
    if not isinstance(raw_cases, list) or not 1 <= len(raw_cases) <= MAX_CASES:
        raise FixtureError("/cases", f"cases must be a list of 1..{MAX_CASES} cases")
    cases: List[Case] = []
    seen: set = set()
    for index, item in enumerate(raw_cases):
        loaded = _case(item, _ptr("/cases", index))
        if loaded.name in seen:
            raise FixtureError(f"/cases/{index}/name", f"duplicate case name {loaded.name!r}")
        seen.add(loaded.name)
        cases.append(loaded)
    return CaseSet(version=CASES_VERSION, cases=tuple(cases))


# -- admission ----------------------------------------------------------------


def admit_module(module: NanoModule) -> None:
    """Refuse a module that needs a handler the runner cannot supply."""
    if not isinstance(module, NanoModule):
        raise TypeError("admit_module expects a NanoModule")
    if module.tier != "nano":
        raise UnsupportedHandlerError(
            f"module tier {module.tier!r} is not supported; package tests run tier 'nano' only"
        )
    extra = sorted(set(module.effects) - ALLOWED_EFFECTS)
    if extra:
        raise UnsupportedHandlerError(
            f"effects {', '.join(extra)} need a handler the fixture runner does not provide"
        )
    for node in module.nodes:
        if node.op in UNSUPPORTED_OPS:
            raise UnsupportedHandlerError(
                f"node {node.id!r} ({node.op}) needs a handler the fixture runner "
                "does not provide"
            )


# -- running ------------------------------------------------------------------


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _error_result(case: Case, message: str) -> CaseResult:
    observed = {"runtimeError": message}
    expected = case.expected_runtime_error
    if expected is None:
        return CaseResult(case.name, "fail", observed, f"unexpected runtime error: {message}")
    if expected not in message:
        return CaseResult(
            case.name, "fail", observed,
            f"runtime error {message!r} does not contain {expected!r}",
        )
    return CaseResult(case.name, "pass", observed)


def _intent_result(case: Case, intents: List[Dict[str, Any]]) -> CaseResult:
    observed = {"proposedIntents": [dict(i) for i in intents]}
    if case.expected_runtime_error is not None:
        return CaseResult(
            case.name, "fail", observed,
            f"expected runtime error containing {case.expected_runtime_error!r}, "
            f"but the run proposed {_canonical(intents)}",
        )
    expected = [dict(i) for i in case.expected_intents or ()]
    if expected != intents:
        return CaseResult(
            case.name, "fail", observed,
            f"expected proposedIntents {_canonical(expected)}, observed {_canonical(intents)}",
        )
    return CaseResult(case.name, "pass", observed)


def run_cases(module: NanoModule, cases: CaseSet) -> Tuple[CaseResult, ...]:
    """Admit ``module``, then evaluate every case in order and report each.

    Only ``RuntimeError_`` (the VM's rejection of a frame) becomes an observed
    ``runtimeError``; any other exception is a defect and propagates.
    """
    admit_module(module)
    if not isinstance(cases, CaseSet):
        raise TypeError("run_cases expects a CaseSet from load_cases")
    if not cases.cases:
        raise FixtureError("/cases", "an empty case set cannot pass")
    results: List[CaseResult] = []
    for case in cases.cases:
        try:
            outcome = _vm.run_module(module, case.frame, provider=None)
        except RuntimeError_ as exc:
            results.append(_error_result(case, str(exc)))
            continue
        results.append(_intent_result(case, [i.to_dict() for i in outcome.intents]))
    return tuple(results)
