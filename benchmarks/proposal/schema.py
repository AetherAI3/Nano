"""Structural dataset schema and canonical byte helpers. Imports nothing from Nano."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import CASE_SCHEMA, DATASET_SCHEMA

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CLASSES = ("supported_unambiguous", "ambiguous_or_unsupported", "adversarial_or_malformed")
SUBSETS = ("quantum", "nano_strategy")
PROPOSAL_KINDS = ("quantum_source", "quantum_command_json", "nano_strategy_source")
OUTCOMES = ("execute", "abstain")
QUANTUM_DIAGNOSTICS = ("NEGATED_COMMAND", "AMBIGUOUS_COMMAND", "MISSING_BOUNDS", "INVALID_BOUNDS",
                       "UNKNOWN_REFERENCE", "MISSING_REFERENCE", "UNSUPPORTED_COMMAND", "MISSING_CATALOG")
HARNESS_CATEGORIES = ("encoding", "json_syntax", "json_duplicate_key", "json_nonfinite", "schema_mismatch",
                      "input_rejected", "task_binding")
STRATEGY_DIAGNOSTICS = ("SYNTAX_ERROR", "TYPE_ERROR", "LOOKAHEAD_ERROR", "RESOURCE_LIMIT", "CAPABILITY_ERROR",
                        "IR_VERSION_ERROR", "IR_VALIDATION_ERROR")
CATEGORIES = QUANTUM_DIAGNOSTICS + HARNESS_CATEGORIES + STRATEGY_DIAGNOSTICS
REPAIR_KINDS = ("json_trailing_comma", "json_unclosed_object", "constant_field", "nano_missing_rparen")
SPLIT_COUNTS = {"supported_unambiguous": 20, "ambiguous_or_unsupported": 20, "adversarial_or_malformed": 20}
_CASE_KEYS = {"id", "class", "subset", "tags", "request", "proposal", "recorded_repair", "task", "expected", "note"}
_TASK_KEYS = {"domain", "supported", "operation", "resources", "evidence", "ceiling", "observation",
              "authorized_effects", "nodes"}


class DatasetError(ValueError):
    """The frozen dataset does not satisfy the versioned structural schema."""


def canonical_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def pretty_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=1, ensure_ascii=True, allow_nan=False) + "\n"


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    """Hash file bytes with CRLF normalized to LF, so checkouts agree across platforms."""
    return "sha256:" + hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DatasetError(message)


def _check_payload(payload: Any, label: str) -> None:
    _require(isinstance(payload, dict) and payload.get("kind") in PROPOSAL_KINDS, f"{label}: bad proposal kind")
    _require(type(payload.get("text")) is str, f"{label}: frozen proposal text missing")
    _require(set(payload) <= {"kind", "text", "recipe"}, f"{label}: unknown proposal fields")


def _check_task(task: Any, label: str) -> None:
    _require(isinstance(task, dict) and set(task) == _TASK_KEYS, f"{label}: task fields")
    _require(type(task["supported"]) is bool, f"{label}: task.supported must be bool")
    _require(isinstance(task["authorized_effects"], list), f"{label}: authorized_effects must be a list")
    if not task["supported"]:
        _require(task["operation"] is None and task["authorized_effects"] == [], f"{label}: unsupported task")
    else:
        _require(type(task["operation"]) is str and task["authorized_effects"], f"{label}: supported task")


def check_case(case: Any) -> None:
    _require(isinstance(case, dict) and set(case) == _CASE_KEYS, "case fields differ from the case schema")
    label = str(case["id"])
    _require(case["class"] in CLASSES and case["subset"] in SUBSETS, f"{label}: class/subset")
    _check_payload(case["proposal"], label)
    if case["recorded_repair"] is not None:
        _check_payload(case["recorded_repair"], label + " repair")
        _require(case["recorded_repair"]["kind"] == case["proposal"]["kind"], f"{label}: repair kind")
    _check_task(case["task"], label)
    expected = case["expected"]
    _require(isinstance(expected, dict) and set(expected) == {"outcome", "category", "repair"}, f"{label}: expected")
    _require(expected["outcome"] in OUTCOMES, f"{label}: outcome")
    if expected["outcome"] == "execute":
        _require(expected["category"] is None and case["task"]["supported"], f"{label}: execute needs a task")
    else:
        _require(expected["category"] in CATEGORIES, f"{label}: abstention category")
    _require(expected["repair"] is None or expected["repair"] in REPAIR_KINDS, f"{label}: repair kind")
    if expected["repair"] is not None:
        _require(case["recorded_repair"] is not None, f"{label}: expected repair needs a recorded candidate")
    is_strategy = case["proposal"]["kind"] == "nano_strategy_source"
    _require((case["subset"] == "nano_strategy") == is_strategy, f"{label}: subset label")


def check_dataset(document: Any, split: str) -> list[dict[str, Any]]:
    """Validate one split; return its cases. Exact frozen counts are enforced."""
    _require(isinstance(document, dict), "dataset must be an object")
    _require(document.get("schema") == DATASET_SCHEMA and document.get("case_schema") == CASE_SCHEMA,
             "dataset schema identifiers")
    _require(document.get("split") == split, "dataset split label")
    cases = document.get("cases")
    _require(isinstance(cases, list), "cases must be a list")
    for case in cases:
        check_case(case)
    ids = [case["id"] for case in cases]
    _require(len(ids) == len(set(ids)), "case ids must be unique")
    _require(all(i.startswith(split + "-") for i in ids), "case ids must carry the split prefix")
    for klass, count in SPLIT_COUNTS.items():
        _require(sum(c["class"] == klass for c in cases) == count, f"{split}: {klass} count")
    return cases


def load_split(split: str) -> list[dict[str, Any]]:
    return check_dataset(load_json(DATA / f"{split}.json"), split)
