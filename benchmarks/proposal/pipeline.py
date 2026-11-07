"""Adapters onto the implementation under test: Nano's public admission surfaces.

Quantum proposals pass through strict decoding and then
`nano.intent.quantum.validate_quantum_document`, which rebuilds the document
from its source and catalog. Strategy proposals pass through
`nano.compiler.check_report` (the versioned diagnostics of #65). Nothing here
knows the task; scoring is left to the independent verifier.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Callable

from nano.cli.commands import COMPILER_NAME, COMPILER_VERSION, DIAGNOSTIC_SCHEMA_VERSION, __version__
from nano.compiler import check_report, compile_to_dict
from nano.intent import compile_intent
from nano.intent.quantum import validate_quantum_document
from nano.intent.tokens import IntentInputError
from nano.quantum import QuantumValidationError

from .decode import DecodeError, decode_text, lenient_json, strict_json
from .schema import canonical_text, sha256_text


@dataclass(frozen=True)
class Admission:
    syntactic_valid: bool
    schema_valid: bool
    executable: bool
    category: str | None
    document: Any
    output_sha256: str
    lenient_executable: bool | None = None


def _rejected(syntactic: bool, category: str, error: Exception) -> Admission:
    record = {"category": category, "error": str(error)}
    return Admission(syntactic, False, False, category, None, sha256_text(canonical_text(record)))


def _from_document(document: dict[str, Any], lenient: bool | None = None) -> Admission:
    diagnostics = document["diagnostics"]
    executable = not diagnostics and bool(document["steps"])
    category = diagnostics[0]["code"] if diagnostics else None
    return Admission(True, True, executable, category, document, sha256_text(canonical_text(document)), lenient)


def _validate(document: Any, catalog: dict[str, Any]) -> dict[str, Any]:
    return validate_quantum_document(document, catalog=catalog).to_dict()


def admit_quantum_source(text: str, catalog: dict[str, Any]) -> Admission:
    try:
        compiled = compile_intent(text, profile="quantum", quantum_catalog=catalog)
        return _from_document(_validate(compiled.to_dict(), catalog))
    except IntentInputError as exc:
        return _rejected(True, "input_rejected", exc)
    except QuantumValidationError as exc:
        return _rejected(True, "schema_mismatch", exc)


def _lenient_executable(text: str, catalog: dict[str, Any]) -> bool:
    """Exposure probe: would Python's default decoder plus Nano admit an executable?"""
    try:
        document = _validate(lenient_json(text), catalog)
    except (ValueError, TypeError):
        return False
    return not document["diagnostics"] and bool(document["steps"])


def admit_quantum_json(text: str, catalog: dict[str, Any]) -> Admission:
    lenient = _lenient_executable(text, catalog)
    try:
        document = strict_json(text)
    except DecodeError as exc:
        return replace(_rejected(False, exc.category, exc), lenient_executable=lenient)
    try:
        return _from_document(_validate(document, catalog), lenient)
    except IntentInputError as exc:
        return replace(_rejected(True, "input_rejected", exc), lenient_executable=lenient)
    except QuantumValidationError as exc:
        return replace(_rejected(True, "schema_mismatch", exc), lenient_executable=lenient)


def diagnostic_envelope(source: str) -> str:
    """The documented `nano check --json` envelope, built in memory for one proposal."""
    result = {"input": "proposal.nano", **check_report(source).to_dict()}
    envelope = {"schemaVersion": DIAGNOSTIC_SCHEMA_VERSION,
                "compiler": {"name": COMPILER_NAME, "version": COMPILER_VERSION, "packageVersion": __version__},
                "success": result["success"], "results": [result]}
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def admit_strategy(text: str) -> Admission:
    report = check_report(text)
    if not report.success:
        code = report.diagnostics[0].code if report.diagnostics else "IR_VALIDATION_ERROR"
        syntactic = all(item.category != "syntax" for item in report.diagnostics)
        return Admission(syntactic, False, False, code, None, sha256_text(canonical_text(report.to_dict())))
    document = compile_to_dict(text)
    return Admission(True, True, True, None, document, sha256_text(canonical_text(document)))


def admitter(kind: str, catalog: dict[str, Any]) -> Callable[[str], Admission]:
    if kind == "quantum_source":
        return lambda text: admit_quantum_source(text, catalog)
    if kind == "quantum_command_json":
        return lambda text: admit_quantum_json(text, catalog)
    if kind == "nano_strategy_source":
        return admit_strategy
    raise ValueError(f"unknown proposal kind {kind!r}")


def admit_bytes(kind: str, payload: bytes, catalog: dict[str, Any]) -> Admission:
    try:
        text = decode_text(payload)
    except DecodeError as exc:
        return _rejected(False, exc.category, exc)
    return admitter(kind, catalog)(text)


NO_TRANSLATOR = Admission(True, False, False, "no_translator", None,
                          sha256_text(canonical_text({"category": "no_translator"})))
