"""A deterministic fixture agent for the versioned diagnostic interface.

This policy allows one missing ``)`` insertion before ``{``. It consumes JSON,
checks its source digest and token metadata, and never interprets repair hints.
Program validity and the fixture's independent task oracle are separate results.
"""

from __future__ import annotations

import json
from typing import Callable

from nano.compiler import compile_to_dict, source_hash, tokenize


def _read_result(document: str, source: str) -> dict:
    envelope = json.loads(document)
    if not isinstance(envelope, dict) or envelope.get("schemaVersion") != "1.0.0":
        raise ValueError("Fixture requires diagnostic schema 1.0.0")
    results = envelope.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise ValueError("Fixture requires exactly one input result")
    result = results[0]
    if not isinstance(result, dict):
        raise ValueError("Fixture input result must be an object")
    if result.get("sourceDigest") != source_hash(source):
        raise ValueError("Diagnostic source digest does not match fixture source")
    if (type(result.get("success")) is not bool
            or type(envelope.get("success")) is not bool
            or envelope["success"] != result["success"]):
        raise ValueError("Diagnostic success flags disagree")
    if not isinstance(result.get("diagnostics"), list):
        raise ValueError("Diagnostic list is missing")
    return result


def _allowed_correction(source: str, result: dict) -> str | None:
    diagnostics = result["diagnostics"]
    if result["success"] or len(diagnostics) != 1:
        return None
    diagnostic = diagnostics[0]
    if not isinstance(diagnostic, dict):
        return None
    allowed = {"code": "SYNTAX_ERROR", "category": "syntax", "severity": "error",
               "expected": ")", "actual": "{"}
    if any(diagnostic.get(key) != value for key, value in allowed.items()):
        return None
    span = diagnostic.get("span")
    if not isinstance(span, dict) or span.get("start") != span.get("end"):
        return None
    start = span.get("start")
    if not isinstance(start, dict):
        return None
    line, column = start.get("line"), start.get("column")
    if type(line) is not int or type(column) is not int:
        return None
    # Lexer coordinates advance only at LF, not at other Unicode separators.
    lines = source.split("\n")
    if not 1 <= line <= len(lines) or not 1 <= column <= len(lines[line - 1]):
        return None
    offset = sum(len(text) + 1 for text in lines[:line - 1]) + column - 1
    if source[offset:offset + 1] != "{":
        return None

    # Check lexer tokens, so quotes/comments cannot fabricate an open group.
    balance = 0
    for token in tokenize(source):
        if (token.line, token.column) >= (line, column):
            if (token.line, token.column, token.type) != (line, column, "LBRACE"):
                return None
            break
        if token.type == "LPAREN":
            balance += 1
        elif token.type == "RPAREN":
            balance -= 1
    if balance <= 0:
        return None
    return source[:offset] + ")" + source[offset:]


def reference_task_matches(source: str) -> bool:
    """Independent graph oracle: observe each 1m bar when RSI is below 30.

    This checks the fixture's declared task. It makes no claim about general
    natural-language task correctness and executes no strategy or provider.
    """
    document = compile_to_dict(source)
    return document.get("nanoIrVersion") == "0.1.0" and document.get("nodes") == [
        {"type": "Schedule", "interval": "1m"},
        {"type": "Condition", "signal": "RSI", "operator": "<", "value": 30},
        {"type": "Intent", "action": "OBSERVE"},
    ]


def run_repair_fixture(
    source: str, check_json: Callable[[str], str],
    *, task_oracle: Callable[[str], bool] = reference_task_matches,
) -> dict:
    """Consume one report, apply at most one allowlisted edit, recheck once."""
    original = source
    initial = _read_result(check_json(source), source)
    final = initial
    corrected = _allowed_correction(source, initial)
    edits = 0
    if corrected is not None:
        source = corrected
        edits = 1
        final = _read_result(check_json(source), source)
    valid = final["success"]
    return {
        "initialValid": initial["success"], "finalValid": valid,
        "validProgramRecovered": not initial["success"] and valid,
        "semanticTaskCorrect": bool(task_oracle(source)) if valid else False,
        "corrections": edits, "rechecks": edits,
        "checkCount": 1 + edits,
        "sourceBeforeDigest": source_hash(original),
        "sourceAfterDigest": source_hash(source), "finalSource": source,
    }
