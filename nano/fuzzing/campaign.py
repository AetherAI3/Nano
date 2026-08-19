"""Property runner and defect ledger for the deterministic compiler fuzz loop."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping, Tuple

from ..compiler import NanoCompileError, check_source, compile_module
from ..ir.graph import StrategyGraph
from ..ir.module import NanoModule
from ..ir.schema import IRValidationError
from ..runtime.interpreter import MarketFrame
from ..runtime.vm import run_module
from .generators import (
    EquivalentPrograms,
    InvalidProgram,
    ValidProgram,
    generate_equivalent_programs,
    generate_invalid_programs,
    generate_valid_programs,
)
from .probes import (
    ProbeFailure,
    audit_catalog_corpus,
    audit_receipt_boundaries,
    audit_risk_boundaries,
)


def canonical_json(value: Mapping[str, Any]) -> str:
    """The byte-comparison spelling used by every determinism property."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_document(document: Mapping[str, Any]) -> StrategyGraph | NanoModule:
    """Load a compiled document through the loader selected by its IR version."""
    version = document.get("nanoIrVersion")
    if version == "0.1.0":
        return StrategyGraph.from_dict(document)
    if version == "1.0.0":
        return NanoModule.from_dict(document)
    raise IRValidationError(f"Unsupported generated IR version {version!r}")


def execution_summary(source: str, frame: MarketFrame) -> Mapping[str, Any]:
    """Execute through the canonical VM and return only observable behavior."""
    result = run_module(compile_module(source), frame)
    return {
        "intents": [intent.to_dict() for intent in result.intents],
        "escalations": [escalation.to_dict() for escalation in result.escalations],
        "warmupBarsSkipped": result.warmup_bars_skipped,
        "log": [entry.to_dict() for entry in result.log],
    }


@dataclass(frozen=True)
class Defect:
    id: str
    property: str
    severity: str
    owning_subsystem: str
    case_id: str
    minimal_reproducer: str
    observed: str
    suggested_minimal_fix: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class CampaignResult:
    seed: int
    requested_cases: int
    valid_cases: int
    invalid_cases: int
    equivalent_cases: int
    corpus_digest: str
    semantic_digest: str
    coverage: Mapping[str, Any]
    defects: Tuple[Defect, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "requestedCases": self.requested_cases,
            "families": {
                "valid": self.valid_cases,
                "oneMutationInvalid": self.invalid_cases,
                "semanticEquivalent": self.equivalent_cases,
            },
            "corpusDigest": self.corpus_digest,
            "semanticDigest": self.semantic_digest,
            "coverage": dict(self.coverage),
            "defects": [defect.to_dict() for defect in self.defects],
        }


_PROPERTY_METADATA = {
    "parse-valid": (
        "high",
        "compiler.parser",
        "Narrow the grammar production or repair the parser rule handling the minimized source.",
    ),
    "typecheck-valid": (
        "high",
        "types.checker",
        "Repair the smallest checker rule that rejects this grammar-valid typed AST.",
    ),
    "compile-deterministic": (
        "high",
        "compiler.codegen",
        "Replace unordered traversal with a canonical declaration or node order.",
    ),
    "compile-load-roundtrip": (
        "critical",
        "compiler.codegen / ir.loader",
        "Align the emitted field with the loader contract without widening accepted IR.",
    ),
    "baseline-vm-differential": (
        "critical",
        "runtime.vm",
        "Correct the single differing opcode or control-flow path and pin this "
        "source as a regression.",
    ),
    "invalid-rejected": (
        "high",
        "compiler.frontend",
        "Reject the mutated construct at the earliest parser or checker boundary.",
    ),
    "diagnostic-location": (
        "medium",
        "compiler.diagnostics",
        "Propagate the mutated token's 1-based source position into the raised compile error.",
    ),
    "equivalent-semantics": (
        "high",
        "compiler.lowering / runtime.vm",
        "Canonicalize the non-semantic source variation or remove order-sensitive evaluation.",
    ),
    "lookahead-loader-boundary": (
        "critical",
        "ir.module",
        "Reject negative series offsets in attribute validation before module construction.",
    ),
    "capability-runtime-boundary": (
        "critical",
        "ir.module / runtime.vm",
        "Validate the module manifest immediately before execution and fail closed.",
    ),
    "catalog-contribution-corpus": (
        "high",
        "library.catalog / library.contribution",
        "Repair the smallest catalog entry, pinned IR pair, or public contribution "
        "control that breaks the minimized strategy.",
    ),
    "receipt-canonical-boundary": (
        "high",
        "runtime.receipt",
        "Reject the unsupported value with ReceiptError at its canonical path, "
        "without delegating acceptance to json.dumps.",
    ),
    "receipt-integer-domain": (
        "high",
        "runtime.receipt",
        "Accept canonical integers through +/-640 significant digits and reject "
        "larger values with a path-aware ReceiptError before encoding.",
    ),
    "receipt-container-depth": (
        "high",
        "runtime.receipt",
        "Accept at most 64 nested containers and reject the 65th with a path-aware "
        "ReceiptError before recursion or encoding.",
    ),
    "receipt-replay-deterministic": (
        "critical",
        "runtime.receipt / runtime.vm",
        "Remove the first nondeterministic field or execution path named by the "
        "minimized replay receipt.",
    ),
    "risk-actuating-enforcement": (
        "critical",
        "runtime.risk / runtime.vm",
        "Correct the single limit comparison or same-frame accounting path and pin "
        "the boundary as a focused runtime regression.",
    ),
    "risk-capacity-boundary": (
        "critical",
        "runtime.risk",
        "Fail closed on the malformed emitted-capacity value before arithmetic.",
    ),
}


def _defect(
    *, property_name: str, case_id: str, reproducer: str, observed: object
) -> Defect:
    severity, subsystem, suggestion = _PROPERTY_METADATA[property_name]
    digest = hashlib.sha256(
        f"{property_name}\0{case_id}\0{reproducer}".encode("utf-8")
    ).hexdigest()[:12]
    return Defect(
        id=f"G5-{digest}",
        property=property_name,
        severity=severity,
        owning_subsystem=subsystem,
        case_id=case_id,
        minimal_reproducer=reproducer,
        observed=(
            observed
            if isinstance(observed, str)
            else f"{type(observed).__name__}: {observed}"
        ),
        suggested_minimal_fix=suggestion,
    )


def _record_probe_failures(
    failures: Tuple[ProbeFailure, ...], defects: list[Defect]
) -> None:
    for failure in failures:
        defects.append(
            _defect(
                property_name=failure.property,
                case_id=failure.case_id,
                reproducer=failure.minimal_reproducer,
                observed=failure.observed,
            )
        )


def _observe_valid(
    case: ValidProgram, defects: list[Defect], observations: list[str]
) -> None:
    try:
        case.parse()
    except Exception as error:  # the ledger must survive the defect it records
        defects.append(
            _defect(
                property_name="parse-valid",
                case_id=case.id,
                reproducer=case.source,
                observed=error,
            )
        )
        return

    try:
        check_source(case.source)
    except Exception as error:
        defects.append(
            _defect(
                property_name="typecheck-valid",
                case_id=case.id,
                reproducer=case.source,
                observed=error,
            )
        )
        return

    try:
        first = case.compile()
        second = case.compile()
        first_json = canonical_json(first)
        if first_json != canonical_json(second):
            raise AssertionError("the same source emitted different canonical IR")
        observations.append(first_json)
    except Exception as error:
        defects.append(
            _defect(
                property_name="compile-deterministic",
                case_id=case.id,
                reproducer=case.source,
                observed=error,
            )
        )
        return

    try:
        loaded = load_document(first)
        if canonical_json(loaded.to_dict()) != first_json:
            raise AssertionError("compile -> load -> serialize was not a fixed point")
        module = compile_module(case.source)
        if NanoModule.from_dict(module.to_dict()).to_dict() != module.to_dict():
            raise AssertionError("v1 module loader round-trip was not a fixed point")
    except Exception as error:
        defects.append(
            _defect(
                property_name="compile-load-roundtrip",
                case_id=case.id,
                reproducer=case.source,
                observed=error,
            )
        )
        return

    if case.baseline:
        try:
            comparison = case.compare_baseline_runtimes()
            observations.append(canonical_json({"intents": list(comparison.vm)}))
            if not (comparison.reference == comparison.lifted_vm == comparison.vm):
                raise AssertionError(
                    f"reference={comparison.reference!r}; "
                    f"lifted={comparison.lifted_vm!r}; vm={comparison.vm!r}"
                )
        except Exception as error:
            defects.append(
                _defect(
                    property_name="baseline-vm-differential",
                    case_id=case.id,
                    reproducer=case.source,
                    observed=error,
                )
            )


def _observe_invalid(case: InvalidProgram, defects: list[Defect]) -> None:
    try:
        document = case.compile()
    except NanoCompileError as error:
        if (
            case.expected_location is not None
            and (
                error.line,
                error.column,
            )
            != case.expected_location
        ):
            defects.append(
                _defect(
                    property_name="diagnostic-location",
                    case_id=case.id,
                    reproducer=case.source,
                    observed=(
                        f"expected {case.expected_location}, got "
                        f"{(error.line, error.column)}: {error}"
                    ),
                )
            )
        return
    except Exception as error:
        defects.append(
            _defect(
                property_name="invalid-rejected",
                case_id=case.id,
                reproducer=case.source,
                observed=f"wrong exception boundary: {error}",
            )
        )
        return

    defects.append(
        _defect(
            property_name="invalid-rejected",
            case_id=case.id,
            reproducer=case.source,
            observed=f"accepted as {canonical_json(document)}",
        )
    )


def _observe_equivalent(
    case: EquivalentPrograms, defects: list[Defect], observations: list[str]
) -> None:
    try:
        frame = case.frame()
        summaries = [execution_summary(source, frame) for source in case.sources]
        observations.append(canonical_json(summaries[0]))
        if any(summary != summaries[0] for summary in summaries[1:]):
            raise AssertionError(canonical_json({"summaries": summaries}))
    except Exception as error:
        defects.append(
            _defect(
                property_name="equivalent-semantics",
                case_id=case.id,
                reproducer="\n--- equivalent variant ---\n".join(case.sources),
                observed=error,
            )
        )


def _observe_hostile_ir(defects: list[Defect]) -> None:
    source = (
        "strategy Guarded {\n"
        "    input close: series<float>\n"
        "    every 1m {\n"
        "        if close > close[1] {\n"
        "            buy(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    module = compile_module(source)
    frame = MarketFrame(timestamps=(0, 60, 120), signals={"close": (1.0, 2.0, 3.0)})

    index_node = next(node for node in module.nodes if node.op == "series.index")
    negative_node = replace(index_node, attrs={**index_node.attrs, "offset": -1})
    negative = replace(
        module,
        nodes=tuple(
            negative_node if node.id == negative_node.id else node
            for node in module.nodes
        ),
    )
    try:
        run_module(negative, frame)
    except IRValidationError:
        pass
    except Exception as error:
        defects.append(
            _defect(
                property_name="lookahead-loader-boundary",
                case_id="hostile-negative-offset",
                reproducer=canonical_json(negative.to_dict(include_hash=False)),
                observed=f"wrong exception: {error}",
            )
        )
    else:
        defects.append(
            _defect(
                property_name="lookahead-loader-boundary",
                case_id="hostile-negative-offset",
                reproducer=canonical_json(negative.to_dict(include_hash=False)),
                observed="negative offset reached runtime",
            )
        )

    ungranted = replace(module, effects=("log.append",))
    try:
        run_module(ungranted, frame)
    except IRValidationError:
        pass
    except Exception as error:
        defects.append(
            _defect(
                property_name="capability-runtime-boundary",
                case_id="hostile-ungranted-effect",
                reproducer=canonical_json(ungranted.to_dict(include_hash=False)),
                observed=f"wrong exception: {error}",
            )
        )
    else:
        defects.append(
            _defect(
                property_name="capability-runtime-boundary",
                case_id="hostile-ungranted-effect",
                reproducer=canonical_json(ungranted.to_dict(include_hash=False)),
                observed="ungranted intent.emit reached runtime",
            )
        )


def _digest(parts: list[str]) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def run_campaign(*, seed: int, cases: int) -> CampaignResult:
    """Run all three generator families and return an always-serializable ledger."""
    if cases < 1:
        raise ValueError("cases must be at least 1")

    valid = generate_valid_programs(seed=seed, count=cases)
    invalid = generate_invalid_programs(seed=seed ^ 0xBAD5EED, count=cases)
    equivalent = generate_equivalent_programs(
        seed=seed ^ 0xE011, count=max(4, cases // 3)
    )

    corpus_parts = [case.source for case in valid]
    corpus_parts.extend(case.source for case in invalid)
    corpus_parts.extend(source for case in equivalent for source in case.sources)

    defects: list[Defect] = []
    observations: list[str] = []
    for case in valid:
        _observe_valid(case, defects, observations)
    for case in invalid:
        _observe_invalid(case, defects)
    for case in equivalent:
        _observe_equivalent(case, defects, observations)
    _observe_hostile_ir(defects)

    catalog = audit_catalog_corpus()
    receipt = audit_receipt_boundaries(seed=seed ^ 0x5ECE17, cases=cases)
    risk = audit_risk_boundaries(seed=seed ^ 0xA15C, cases=cases)
    _record_probe_failures(catalog.defects, defects)
    _record_probe_failures(receipt.defects, defects)
    _record_probe_failures(risk.defects, defects)
    observations.extend(
        (catalog.corpus_digest, receipt.semantic_digest, risk.semantic_digest)
    )

    return CampaignResult(
        seed=seed,
        requested_cases=cases,
        valid_cases=len(valid),
        invalid_cases=len(invalid),
        equivalent_cases=len(equivalent),
        corpus_digest=_digest(corpus_parts),
        semantic_digest=_digest(observations),
        coverage={
            "catalog": {
                "strategies": catalog.strategy_count,
                "baselineIr": catalog.baseline_ir_count,
                "v1Ir": catalog.v1_ir_count,
                "watchdog": catalog.watchdog_count,
                "controls": catalog.control_cases,
            },
            "receipt": {
                "canonicalCases": receipt.canonical_cases,
                "replayCases": receipt.replay_cases,
                "nonfiniteCases": receipt.nonfinite_cases,
                "oversizedCases": receipt.oversized_cases,
            },
            "risk": {
                "actuatingCases": risk.actuating_cases,
                "equalityCases": risk.equality_cases,
                "malformedCapacityCases": risk.malformed_capacity_cases,
            },
        },
        defects=tuple(defects),
    )


__all__ = [
    "CampaignResult",
    "Defect",
    "canonical_json",
    "execution_summary",
    "load_document",
    "run_campaign",
]
