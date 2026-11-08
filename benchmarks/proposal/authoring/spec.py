"""Constructors for hand-written cases. Pure data; no compiler is imported here.

Task constructors encode what the requester asked for and what the host
authorizes. Identities are typed by hand from ``CATALOG`` below; they are not
looked up through Nano.
"""
from __future__ import annotations

from typing import Any

BANK_A = "sha256:9522f328d9c15188074b03ba0037da549a525db95c75d7d58f8434d63660a998"
BANK_B = "sha256:eef8807308b34c1c306179948b0c2f1c2b58520ad3a4cbfe28113be8cbe9c84e"


def _plan(ident: str, ref: str, kind: str, prep: str, shots: int, circuits: int) -> dict[str, Any]:
    return {"id": ident, "operation_ref": ref, "operation_kind": kind,
            "admitted_preparation_ref": prep, "max_shots": shots, "max_circuits": circuits}


_ALPHA = _plan("plan:alpha", "operation:alpha-sample", "sample", "preparation:alpha-bits", 2000, 4)
CATALOG: dict[str, Any] = {
    "version": "quantum-catalog/1",
    "backends": {
        "local": {"id": "local_aer", "kind": "local"},
        "aer": {"id": "local_aer", "kind": "local"},
        "kyiv": {"id": "ibm_kyiv", "kind": "hardware"},
        "ibm-kyiv": {"id": "ibm_kyiv", "kind": "hardware"},
        "torino": {"id": "ibm_torino", "kind": "hardware"},
    },
    "plans": {
        "alpha-plan": _ALPHA,
        "alpha": dict(_ALPHA),
        "beta-plan": _plan("plan:beta", "operation:beta-qubo", "optimize_qubo", "preparation:beta-bits", 500, 2),
        "gamma-plan": _plan("plan:gamma", "operation:gamma-prepare", "prepare_bits", "preparation:gamma-bits", 1, 1),
        "spy": _plan("plan:spy-research", "operation:spy-sample", "sample", "preparation:spy-bits", 100, 1),
    },
    "banks": {
        "bank-a": {"id": "evidence-bank:a", "digest": BANK_A, "schema_version": "evidence-bank/1"},
        "bank-b": {"id": "evidence-bank:b", "digest": BANK_B, "schema_version": "evidence-bank/1"},
    },
    "jobs": {
        "job-q": {"id": "job:q", "plan_id": "plan:alpha", "state": "queued"},
        "job-c": {"id": "job:c", "plan_id": "plan:alpha", "state": "cancelled"},
        "job-p": {"id": "job:p", "plan_id": "plan:beta", "state": "accounting-pending"},
        "job-v": {"id": "job:v", "plan_id": "plan:alpha", "state": "verified"},
        "job-f": {"id": "job:f", "plan_id": "plan:beta", "state": "failed"},
    },
    "receipts": {
        "rcpt-v": {"id": "receipt:v", "plan_id": "plan:alpha", "job_id": "job:v", "state": "verified"},
        "rcpt-p": {"id": "receipt:p", "plan_id": "plan:beta", "job_id": "job:p", "state": "accounting-pending"},
    },
}

EVIDENCE = {"a": {"ref": "evidence-bank:a", "digest": BANK_A},
            "b": {"ref": "evidence-bank:b", "digest": BANK_B}}
LOCAL, KYIV, TORINO = "local_aer", "ibm_kyiv", "ibm_torino"
ALPHA, BETA, GAMMA, SPY = "plan:alpha", "plan:beta", "plan:gamma", "plan:spy-research"


# ---- tasks -----------------------------------------------------------------

def _task(operation: str, effect: str, resources: dict[str, str], *, evidence: str | None = None,
          ceiling: dict[str, Any] | None = None, observation: str | None = None) -> dict[str, Any]:
    return {"domain": "quantum", "supported": True, "operation": operation, "resources": resources,
            "evidence": EVIDENCE[evidence] if evidence else None, "ceiling": ceiling,
            "observation": observation, "authorized_effects": [effect], "nodes": None}


def check(plan: str, evidence: str | None = None) -> dict[str, Any]:
    return _task("check", "pure", {"plan_id": plan}, evidence=evidence)


def compile_(plan: str, backend: str, evidence: str | None = None) -> dict[str, Any]:
    return _task("compile", "local_compile", {"plan_id": plan, "backend": backend}, evidence=evidence)


def _ceiling(shots: int, circuits: int, qpu: int | float) -> dict[str, Any]:
    return {"shots": shots, "circuits": circuits, "qpu_seconds": qpu}


def simulate(plan: str, backend: str, shots: int, circuits: int, qpu: int | float) -> dict[str, Any]:
    return _task("simulate", "local_simulation", {"plan_id": plan, "backend": backend},
                 ceiling=_ceiling(shots, circuits, qpu))


def prepare(plan: str, backend: str, shots: int, circuits: int, qpu: int | float) -> dict[str, Any]:
    return _task("prepare_submission", "proposal_only", {"plan_id": plan, "backend": backend},
                 ceiling=_ceiling(shots, circuits, qpu))


def status(job: str, state: str) -> dict[str, Any]:
    return _task("inspect_status", "read_supplied_state", {"job_id": job}, observation=state)


def receipt(ident: str, state: str) -> dict[str, Any]:
    return _task("explain_receipt", "read_supplied_state", {"receipt_id": ident}, observation=state)


def unsupported(domain: str = "quantum") -> dict[str, Any]:
    """A request the host does not authorize: ambiguous, unsupported or out of bounds."""
    return {"domain": domain, "supported": False, "operation": None, "resources": {}, "evidence": None,
            "ceiling": None, "observation": None, "authorized_effects": [], "nodes": None}


def rule(interval: str, operator: str, value: int, action: str, asset: str | None = None) -> dict[str, Any]:
    """A Nano strategy task: one schedule, one RSI condition, one intent."""
    intent: dict[str, Any] = {"type": "Intent", "action": action}
    if asset is not None:
        intent["asset"] = asset
    nodes = [{"type": "Schedule", "interval": interval},
             {"type": "Condition", "signal": "RSI", "operator": operator, "value": value}, intent]
    return {"domain": "nano_strategy", "supported": True, "operation": "strategy_rule", "resources": {},
            "evidence": None, "ceiling": None, "observation": None, "authorized_effects": [action],
            "nodes": nodes}


def strategy_unsupported() -> dict[str, Any]:
    return unsupported("nano_strategy")


# ---- proposals --------------------------------------------------------------

def src(text: str) -> dict[str, Any]:
    """A quantum command proposed as source text."""
    return {"kind": "quantum_source", "text": text}


def doc(source: str, *, sets: tuple = (), deletes: tuple = (), raw: tuple = (),
        replace_all: tuple = ()) -> dict[str, Any]:
    """A command document materialized once at freeze time, then edited by hand.

    ``sets``: (dotted path, value); ``deletes``: dotted paths; ``raw``: exact
    single-occurrence (old, new) text replacements on the canonical text;
    ``replace_all``: (old, new) replacements of every occurrence.
    """
    return {"kind": "quantum_command_json", "recipe": {
        "source": source, "sets": [list(item) for item in sets], "deletes": list(deletes),
        "raw": [list(item) for item in raw], "replace_all": [list(item) for item in replace_all]}}


def rawjson(text: str) -> dict[str, Any]:
    return {"kind": "quantum_command_json", "text": text}


def strategy(text: str) -> dict[str, Any]:
    return {"kind": "nano_strategy_source", "text": text}


# ---- cases ------------------------------------------------------------------

CLASSES = {"S": "supported_unambiguous", "A": "ambiguous_or_unsupported", "M": "adversarial_or_malformed"}


def case(ident: str, klass: str, request: str, proposal: dict[str, Any], task: dict[str, Any], *,
         outcome: str, category: str | None = None, repair: dict[str, Any] | None = None,
         expect_repair: str | None = None, tags: tuple[str, ...] = (), note: str = "") -> dict[str, Any]:
    subset = "nano_strategy" if proposal["kind"] == "nano_strategy_source" else "quantum"
    return {"id": ident, "class": CLASSES[klass], "subset": subset, "tags": sorted(tags),
            "request": request, "proposal": proposal, "recorded_repair": repair, "task": task,
            "expected": {"outcome": outcome, "category": category, "repair": expect_repair},
            "note": note}
