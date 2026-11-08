"""Independent deterministic task verifier. Imports nothing from Nano.

It is written from each case's hand-written task fields and the documented
`nano.quantum.command.v1` / Nano IR 0.1.0 shapes. It never compiles, validates
or re-derives anything through the implementation being scored; it only reads
the document that the implementation admitted and compares it to the task.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

# From docs/quantum-command-profile.md "Closed command vocabulary".
EFFECT_BY_OPERATION = {"check": "pure", "compile": "local_compile", "simulate": "local_simulation",
                       "inspect_status": "read_supplied_state", "explain_receipt": "read_supplied_state",
                       "prepare_submission": "proposal_only"}
CONSTANTS = {"schema": "nano.quantum.command.v1", "profile": "quantum",
             "operation_schema": "nano.quantum.operations.v1"}
ROLE_BY_ARGUMENT = {"plan_id": "plan", "backend": "backend", "job_id": "job", "receipt_id": "receipt"}
OBSERVED_ROLE = {"inspect_status": "job", "explain_receipt": "receipt"}


@dataclass(frozen=True)
class Verdict:
    bound: bool
    reasons: tuple[str, ...]


def strict_equal(actual: Any, expected: Any) -> bool:
    """JSON equality with native types: True is not 1 and 1.0 is not 1."""
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and set(actual) == set(expected)
                and all(strict_equal(actual[k], v) for k, v in expected.items()))
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(strict_equal(a, e) for a, e in zip(actual, expected)))
    return type(actual) is type(expected) and actual == expected


def expected_args(task: dict[str, Any]) -> dict[str, Any]:
    args = dict(task["resources"])
    if task["evidence"] is not None:
        args["evidence_bank_ref"] = task["evidence"]["ref"]
    if task["ceiling"] is not None:
        args.update(task["ceiling"])
    return args


def _snapshot_reasons(task: dict[str, Any], document: dict[str, Any]) -> list[str]:
    snapshot = document.get("catalog_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("version") != "quantum-catalog/1":
        return ["catalog_snapshot"]
    resources = snapshot.get("resources")
    if not isinstance(resources, dict):
        return ["catalog_snapshot"]
    reasons = []
    for argument, ident in task["resources"].items():
        row = resources.get(ROLE_BY_ARGUMENT[argument])
        if not isinstance(row, dict) or not strict_equal(row.get("id"), ident):
            reasons.append(f"resource:{argument}")
    bank = resources.get("bank")
    evidence = task["evidence"]
    if evidence is None and bank is not None:
        reasons.append("evidence:unrequested")
    if evidence is not None and (not isinstance(bank, dict) or bank.get("id") != evidence["ref"]
                                 or bank.get("digest") != evidence["digest"]):
        reasons.append("evidence:identity")
    role = OBSERVED_ROLE.get(task["operation"])
    if role is not None:
        row = resources.get(role)
        if not isinstance(row, dict) or row.get("state") != task["observation"]:
            reasons.append("observation:state")
    return reasons


def _quantity_reasons(task: dict[str, Any], document: dict[str, Any]) -> list[str]:
    quantities = document.get("quantities")
    source = document.get("source")
    ceiling = task["ceiling"] or {}
    if not isinstance(quantities, list) or len(quantities) != len(ceiling) or type(source) is not str:
        return ["quantities"]
    seen = set()
    for quantity in quantities:
        if not isinstance(quantity, dict) or quantity.get("kind") not in ceiling or quantity["kind"] in seen:
            return ["quantities"]
        seen.add(quantity["kind"])
        span = quantity.get("span")
        if not strict_equal(quantity.get("value"), ceiling[quantity["kind"]]):
            return ["quantities:value"]
        if (not isinstance(span, list) or len(span) != 2 or not all(type(i) is int for i in span)
                or source[span[0]:span[1]] != quantity.get("raw")):
            return ["quantities:span"]
    return []


def verify_quantum(task: dict[str, Any], document: Any) -> Verdict:
    if not task["supported"] or task["domain"] != "quantum":
        return Verdict(False, ("task_not_authorized",))
    if not isinstance(document, dict):
        return Verdict(False, ("document",))
    reasons = [f"constant:{k}" for k, v in CONSTANTS.items() if document.get(k) != v]
    if document.get("diagnostics") != []:
        reasons.append("diagnostics")
    operation = task["operation"]
    if document.get("operation") != operation:
        return Verdict(False, tuple(reasons + ["operation"]))
    args = expected_args(task)
    if not strict_equal(document.get("args"), args):
        reasons.append("args")
    effect = EFFECT_BY_OPERATION[operation]
    if not strict_equal(document.get("effects"), [effect]) or effect not in task["authorized_effects"]:
        reasons.append("effects")
    step = {"kind": effect, "capability": "quantum." + operation, "args": args}
    if not strict_equal(document.get("steps"), [step]):
        reasons.append("steps")
    reasons += _snapshot_reasons(task, document) + _quantity_reasons(task, document)
    source = document.get("source")
    if type(source) is not str or document.get("source_hash") != (
            "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()):
        reasons.append("source_hash")
    return Verdict(not reasons, tuple(reasons))


def verify_strategy(task: dict[str, Any], ir: Any) -> Verdict:
    if not task["supported"] or task["domain"] != "nano_strategy":
        return Verdict(False, ("task_not_authorized",))
    if not isinstance(ir, dict) or not strict_equal(ir.get("nodes"), task["nodes"]):
        return Verdict(False, ("nodes",))
    actions = [n.get("action") for n in ir["nodes"] if n.get("type") == "Intent"]
    if not actions or any(a not in task["authorized_effects"] for a in actions):
        return Verdict(False, ("effects",))
    return Verdict(True, ())


def verify(task: dict[str, Any], subset: str, document: Any) -> Verdict:
    if subset == "nano_strategy":
        return verify_strategy(task, document)
    return verify_quantum(task, document)


def proposed_effects(subset: str, document: Any) -> list[Any]:
    """Effects a document proposes, read directly from its fields (for the authorization audit)."""
    if not isinstance(document, dict):
        return []
    if subset == "nano_strategy":
        nodes = document.get("nodes")
        if not isinstance(nodes, list):
            return []
        return [n.get("action") for n in nodes if isinstance(n, dict) and n.get("type") == "Intent"]
    effects = document.get("effects")
    return list(effects) if isinstance(effects, list) else []
