"""Public-API probes for library, receipt, and risk boundaries.

The compiler generators exercise source and IR shape.  These probes deliberately
start one layer farther out, at the public package APIs that consume compiled
programs.  They return data instead of raising so the long-running campaign can
preserve a minimized defect even when a boundary itself crashes.
"""

from __future__ import annotations

import hashlib
import importlib.resources as resources
import json
import math
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Tuple

from ..compiler import compile_module, compile_source, compile_to_dict
from ..ir.graph import StrategyGraph
from ..ir.module import NanoModule
from ..library.catalog import load_catalog, strategy_rows
from ..library.contribution import baseline_control_frames, module_control_frame
from ..runtime.effects import Intent
from ..runtime.interpreter import MarketFrame, execute
from ..runtime.receipt import (
    ReceiptError,
    build_receipt,
    canonical_bytes,
    differences,
    verify_run,
)
from ..runtime.risk import RiskGate
from ..runtime.vm import run_module
from .generators import generate_valid_programs
from .rng import DeterministicRng


@dataclass(frozen=True)
class ProbeFailure:
    property: str
    case_id: str
    minimal_reproducer: str
    observed: str


@dataclass(frozen=True)
class CatalogAudit:
    strategy_count: int
    baseline_ir_count: int
    v1_ir_count: int
    watchdog_count: int
    control_cases: int
    corpus_digest: str
    defects: Tuple[ProbeFailure, ...]


@dataclass(frozen=True)
class ReceiptAudit:
    canonical_cases: int
    replay_cases: int
    nonfinite_cases: int
    oversized_cases: int
    semantic_digest: str
    defects: Tuple[ProbeFailure, ...]


@dataclass(frozen=True)
class RiskAudit:
    actuating_cases: int
    equality_cases: int
    malformed_capacity_cases: int
    semantic_digest: str
    defects: Tuple[ProbeFailure, ...]


def _digest(parts: list[str]) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _failure(
    property_name: str, case_id: str, reproducer: str, error: object
) -> ProbeFailure:
    return ProbeFailure(
        property=property_name,
        case_id=case_id,
        minimal_reproducer=reproducer,
        observed=f"{type(error).__name__}: {error}",
    )


def _library_resource(root: Any, relative: str) -> Any:
    parts = PurePosixPath(relative).parts
    if parts and parts[0] == "library":
        parts = parts[1:]
    return root.joinpath(*parts)


def audit_catalog_corpus() -> CatalogAudit:
    """Compile, load, and meaningfully replay every catalog entry."""
    failures: list[ProbeFailure] = []
    observations: list[str] = []
    try:
        document = load_catalog()
        rows = sorted(strategy_rows(document), key=lambda row: str(row.get("id")))
    except Exception as error:
        failure = _failure(
            "catalog-contribution-corpus",
            "catalog-load",
            "load_catalog()",
            error,
        )
        return CatalogAudit(0, 0, 0, 0, 0, _digest([]), (failure,))

    root = resources.files("nano.library")
    baseline_count = sum(row.get("irVersion") == "0.1.0" for row in rows)
    v1_count = sum(row.get("irVersion") == "1.0.0" for row in rows)
    watchdog_count = sum(row.get("category") == "watchdog" for row in rows)

    for row in rows:
        strategy_id = str(row.get("id"))
        source_path = str(row.get("sourcePath"))
        ir_path = str(row.get("irPath"))
        reproducer = f"catalog entry {strategy_id}: {source_path} + {ir_path}"
        try:
            source = _library_resource(root, source_path).read_text(encoding="utf-8")
            pinned = json.loads(
                _library_resource(root, ir_path).read_text(encoding="utf-8")
            )
            compiled = compile_to_dict(source)
            if compiled != pinned:
                raise AssertionError("compiled source differs from its catalog IR")
            if compiled.get("nanoIrVersion") != row.get("irVersion"):
                raise AssertionError(
                    f"catalog says {row.get('irVersion')!r}, compiler emitted "
                    f"{compiled.get('nanoIrVersion')!r}"
                )

            if compiled["nanoIrVersion"] == "0.1.0":
                graph = StrategyGraph.from_dict(compiled)
                if graph.to_dict() != compiled:
                    raise AssertionError("baseline loader round-trip drifted")
                firing, silent = baseline_control_frames(compile_source(source))
                first = execute(graph, firing)
                second = execute(graph, firing)
                if not first.intents:
                    raise AssertionError("baseline positive control emitted no intent")
                if execute(graph, silent).intents:
                    raise AssertionError("baseline negative control emitted an intent")
            elif compiled["nanoIrVersion"] == "1.0.0":
                module = NanoModule.from_dict(compiled)
                if module.to_dict() != compiled:
                    raise AssertionError("v1 loader round-trip drifted")
                frame = module_control_frame(module)
                first = run_module(module, frame)
                second = run_module(module, frame)
                if not any(entry.event == "condition.evaluated" for entry in first.log):
                    raise AssertionError("v1 control never reached a condition")
            else:
                raise AssertionError(
                    f"unsupported catalog IR {compiled.get('nanoIrVersion')!r}"
                )

            if first.to_dict() != second.to_dict():
                raise AssertionError(
                    "paired contribution controls replayed differently"
                )
            observations.append(
                json.dumps(
                    {
                        "id": strategy_id,
                        "irVersion": compiled["nanoIrVersion"],
                        "result": first.to_dict(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            )
        except Exception as error:
            failures.append(
                _failure(
                    "catalog-contribution-corpus",
                    strategy_id,
                    reproducer,
                    error,
                )
            )

    return CatalogAudit(
        strategy_count=len(rows),
        baseline_ir_count=baseline_count,
        v1_ir_count=v1_count,
        watchdog_count=watchdog_count,
        control_cases=len(rows),
        corpus_digest=_digest(observations),
        defects=tuple(failures),
    )


def _canonical_document(rng: DeterministicRng, index: int) -> dict[str, Any]:
    items = [
        ("alpha", index),
        ("enabled", bool(index % 2)),
        ("nested", {"z": -0.0 if index % 3 == 0 else 0.0, "a": [index, None]}),
        ("text", f"case-{index}-\u2713"),
    ]
    rng.shuffle(items)
    return dict(items)


def audit_receipt_boundaries(*, seed: int, cases: int) -> ReceiptAudit:
    """Probe canonical bytes, replay, nonfinite values, and size boundaries."""
    if cases < 1:
        raise ValueError("cases must be at least 1")
    rng = DeterministicRng(seed)
    failures: list[ProbeFailure] = []
    observations: list[str] = []

    for index in range(cases):
        document = _canonical_document(rng, index)
        reordered = dict(reversed(tuple(document.items())))
        try:
            first = canonical_bytes(document)
            second = canonical_bytes(reordered)
            if first != second:
                raise AssertionError(
                    "object construction order changed canonical bytes"
                )
            if differences(document, reordered):
                raise AssertionError("equivalent documents reported receipt drift")
            observations.append(first.decode("ascii"))
        except Exception as error:
            failures.append(
                _failure(
                    "receipt-canonical-boundary",
                    f"canonical-{index}",
                    repr(document),
                    error,
                )
            )

    nonfinite = (float("nan"), float("inf"), float("-inf"))
    for label, value in zip(("nan", "inf", "negative-inf"), nonfinite):
        try:
            canonical_bytes({"value": value})
        except ReceiptError:
            continue
        except Exception as error:
            failures.append(
                _failure(
                    "receipt-canonical-boundary",
                    f"nonfinite-{label}",
                    f"canonical_bytes({{'value': float({label!r})}})",
                    error,
                )
            )
        else:
            failures.append(
                _failure(
                    "receipt-canonical-boundary",
                    f"nonfinite-{label}",
                    f"canonical_bytes({{'value': float({label!r})}})",
                    "nonfinite value was serialized",
                )
            )

    # docs/receipts.md promises host integers are emitted faithfully even past
    # JavaScript's exact range.  This value also crosses CPython's default digit
    # guard, making interpreter-dependent crashes reachable in one tiny case.
    try:
        oversized_bytes = canonical_bytes({"value": 10**5000})
        observations.append(hashlib.sha256(oversized_bytes).hexdigest())
    except Exception as error:
        failures.append(
            _failure(
                "receipt-oversized-integer",
                "oversized-integer",
                "canonical_bytes({'value': 10**5000})",
                error,
            )
        )

    # A canonical tree may be too deep for the implementation.  A stable
    # ReceiptError refusal is acceptable; leaking RecursionError is not.
    nested: Any = 0
    for _ in range(2000):
        nested = [nested]
    try:
        observations.append(hashlib.sha256(canonical_bytes(nested)).hexdigest())
    except ReceiptError as error:
        observations.append(f"deep-refused:{error}")
    except Exception as error:
        failures.append(
            _failure(
                "receipt-oversized-depth",
                "oversized-depth",
                "x = 0; repeat 2000 times: x = [x]; canonical_bytes(x)",
                error,
            )
        )

    replay_cases = 0
    generated = generate_valid_programs(seed=seed ^ 0x51A7, count=cases * 2)
    for case in generated:
        if case.family == "reasoning":
            continue
        if replay_cases >= cases:
            break
        replay_cases += 1
        try:
            module = compile_module(case.source)
            frame = case.frame(length=max(12, module.warmup + 2))
            result = run_module(module, frame)
            receipt = build_receipt(module, frame, result)
            verified = verify_run(module, frame)
            if verified != canonical_bytes(receipt):
                raise AssertionError("verify_run bytes differ from the recorded run")
            observations.append(hashlib.sha256(verified).hexdigest())
        except Exception as error:
            failures.append(
                _failure(
                    "receipt-replay-deterministic",
                    case.id,
                    case.source,
                    error,
                )
            )

    return ReceiptAudit(
        canonical_cases=cases,
        replay_cases=replay_cases,
        nonfinite_cases=len(nonfinite),
        oversized_cases=2,
        semantic_digest=_digest(observations),
        defects=tuple(failures),
    )


def _risk_frame(measurement: str | None, value: Any) -> MarketFrame:
    signals = {} if measurement is None else {measurement: (value,)}
    return MarketFrame(timestamps=(0,), signals=signals)


def audit_risk_boundaries(*, seed: int, cases: int) -> RiskAudit:
    """Exercise actuating equality semantics and hostile capacity values."""
    if cases < 1:
        raise ValueError("cases must be at least 1")
    failures: list[ProbeFailure] = []
    observations: list[str] = []
    scenarios = (
        (
            "max_drawdown",
            0.05,
            "risk.drawdown",
            0.05,
            False,
            math.nextafter(0.05, 1.0),
            True,
        ),
        (
            "max_daily_loss",
            0.02,
            "risk.daily_loss",
            0.02,
            False,
            math.nextafter(0.02, 1.0),
            True,
        ),
        ("max_orders_per_day", 5, "risk.orders_today", 5.0, True, 4.0, False),
        (
            "stop_trading_after_losses",
            3,
            "risk.consecutive_losses",
            3.0,
            True,
            2.0,
            False,
        ),
        ("min_confidence", 0.6, None, 0.6, False, math.nextafter(0.6, 0.0), True),
    )
    scenario_offset = DeterministicRng(seed).randbelow(len(scenarios))

    for index in range(cases):
        (
            limit,
            declared,
            measurement,
            equality,
            equality_breaches,
            adjacent,
            adjacent_breaches,
        ) = scenarios[(scenario_offset + index) % len(scenarios)]
        confidence = equality if measurement is None else 1.0
        intent = Intent("BUY", 0, "BTC", confidence)
        try:
            gate = RiskGate({limit: declared}, _risk_frame(measurement, equality))
            equality_violations = gate.review(intent, 0)
            if bool(equality_violations) is not equality_breaches:
                raise AssertionError(
                    f"{limit} equality expected breach={equality_breaches}, got "
                    f"{[item.detail for item in equality_violations]!r}"
                )

            adjacent_confidence = adjacent if measurement is None else 1.0
            adjacent_intent = Intent("SELL", 0, "ETH", adjacent_confidence)
            adjacent_gate = RiskGate(
                {limit: declared}, _risk_frame(measurement, adjacent)
            )
            adjacent_violations = adjacent_gate.review(adjacent_intent, 0)
            if bool(adjacent_violations) is not adjacent_breaches:
                raise AssertionError(
                    f"{limit} adjacent expected breach={adjacent_breaches}, got "
                    f"{[item.detail for item in adjacent_violations]!r}"
                )
            observations.append(
                f"{limit}:{bool(equality_violations)}:{bool(adjacent_violations)}"
            )
        except Exception as error:
            failures.append(
                _failure(
                    "risk-actuating-enforcement",
                    f"risk-equality-{index}",
                    f"RiskGate({{{limit!r}: {declared!r}}}).review(BUY, equality)",
                    error,
                )
            )

    malformed: tuple[Any, ...] = (
        True,
        -1,
        1.5,
        float("nan"),
        float("inf"),
        "1",
        None,
        10**1000,
    )
    for index, capacity in enumerate(malformed):
        try:
            gate = RiskGate(
                {"max_orders_per_day": 5},
                _risk_frame("risk.orders_today", 0.0),
            )
            violations = gate.review(
                Intent("BUY", 0, "BTC", 1.0),
                0,
                emitted_capacity=capacity,
            )
            if [item.limit for item in violations] != ["max_orders_per_day"]:
                raise AssertionError(
                    f"hostile capacity did not fail closed: {violations!r}"
                )
            if (
                "accepted intent capacity outside valid domain"
                not in violations[0].detail
            ):
                raise AssertionError(
                    f"unstable capacity diagnostic: {violations[0].detail}"
                )
            observations.append(f"capacity-{index}:rejected")
        except Exception as error:
            failures.append(
                _failure(
                    "risk-capacity-boundary",
                    f"risk-capacity-{index}",
                    f"RiskGate(...).review(BUY, emitted_capacity={capacity!r})",
                    error,
                )
            )

    # VM integration: the host has one slot left and two same-frame proposals.
    source = (
        "strategy Capacity {\n"
        "    risk {\n"
        "        max_orders_per_day 2\n"
        "    }\n"
        "    every 1m {\n"
        "        if TRIGGER > 0 {\n"
        "            buy(BTC, 1.0)\n"
        "            sell(ETH, 1.0)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    try:
        frame = MarketFrame(
            timestamps=(0,),
            signals={"TRIGGER": (1.0,), "risk.orders_today": (1.0,)},
        )
        result = run_module(compile_module(source), frame)
        surviving = [(intent.action, intent.asset) for intent in result.intents]
        if surviving != [("BUY", "BTC")]:
            raise AssertionError(f"same-frame capacity survived as {surviving!r}")
        if sum(entry.event == "intent.suppressed" for entry in result.log) != 1:
            raise AssertionError("same-frame overflow was not logged once")
        observations.append("same-frame:one-survivor")
    except Exception as error:
        failures.append(
            _failure(
                "risk-actuating-enforcement",
                "risk-same-frame-capacity",
                source,
                error,
            )
        )

    return RiskAudit(
        actuating_cases=cases + 1,
        equality_cases=cases,
        malformed_capacity_cases=len(malformed),
        semantic_digest=_digest(observations),
        defects=tuple(failures),
    )


__all__ = [
    "CatalogAudit",
    "ProbeFailure",
    "ReceiptAudit",
    "RiskAudit",
    "audit_catalog_corpus",
    "audit_receipt_boundaries",
    "audit_risk_boundaries",
]
