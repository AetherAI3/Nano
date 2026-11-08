"""Frozen contract for the #87 proposal benchmark: locked eval, schema, verifier independence."""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.proposal.decode import DecodeError, strict_json  # noqa: E402
from benchmarks.proposal.schema import (  # noqa: E402
    DATA, DatasetError, canonical_text, check_dataset, file_sha256, load_json, load_split, sha256_text,
)
from benchmarks.proposal.verifier import strict_equal, verify_quantum, verify_strategy  # noqa: E402

BENCH = ROOT / "benchmarks" / "proposal"
# Pinned literally: editing eval.json and manifest.json together still fails here.
LOCKED_EVAL_SHA256 = "sha256:f7355ca49f59471373971155936bbe11d04827bd877e2ba4a5e5a528de2e5dee"
LOCKED_EVAL_SOURCES = {
    "eval_cases.py": "sha256:6eb0cd2cfc3d33cb8398cb64e2e2a848d6d4ca328f73b188e20f43e18f3237d2",
    "spec.py": "sha256:d4d83015434e728d3c731e9b8dc2f34b58d73384b359fdc4e398d890afd7e0ad",
}
LOCKED_CATALOG_SHA256 = "sha256:be67e36f9245ee8ded9c425e67cbd07e5351faca0df2bc48b9795de7ad72451f"
LOCKED_POLICY_SHA256 = "sha256:eb1573aad00b7e6028d34227835e117017f21d48dedaededcfc04572c760b84b"


def _manifest():
    return load_json(BENCH / "manifest.json")


def test_locked_eval_split_bytes_and_sources_are_unchanged():
    assert file_sha256(DATA / "eval.json") == LOCKED_EVAL_SHA256
    assert _manifest()["splits"]["eval"]["sha256"] == LOCKED_EVAL_SHA256
    assert _manifest()["splits"]["eval"]["locked"] is True
    for name, digest in LOCKED_EVAL_SOURCES.items():
        assert file_sha256(BENCH / "authoring" / name) == digest
    assert file_sha256(DATA / "catalog.json") == LOCKED_CATALOG_SHA256
    assert file_sha256(BENCH / "policy.json") == LOCKED_POLICY_SHA256


@pytest.mark.parametrize("split", ["dev", "eval"])
def test_manifest_freezes_split_ids_case_hashes_and_counts(split):
    entry = _manifest()["splits"][split]
    cases = load_split(split)
    assert file_sha256(DATA / f"{split}.json") == entry["sha256"]
    assert [c["id"] for c in cases] == entry["case_ids"]
    assert {c["id"]: sha256_text(canonical_text(c)) for c in cases} == entry["case_sha256"]
    assert entry["case_count"] == 60
    assert entry["counts"] == {f"{k}/{s}": n for k in ("adversarial_or_malformed", "ambiguous_or_unsupported",
                                                         "supported_unambiguous")
                               for s, n in (("nano_strategy", 3), ("quantum", 17))}


def test_dev_and_eval_share_no_case_ids_or_proposal_bytes():
    dev, held = load_split("dev"), load_split("eval")
    assert not {c["id"] for c in dev} & {c["id"] for c in held}
    assert not {c["proposal"]["text"] for c in dev} & {c["proposal"]["text"] for c in held}


def test_dataset_schema_rejects_count_and_field_drift():
    document = load_json(DATA / "dev.json")
    shrunk = copy.deepcopy(document)
    shrunk["cases"].pop()
    with pytest.raises(DatasetError):
        check_dataset(shrunk, "dev")
    relabelled = copy.deepcopy(document)
    relabelled["cases"][0]["expected"]["outcome"] = "execute_maybe"
    with pytest.raises(DatasetError):
        check_dataset(relabelled, "dev")
    extra = copy.deepcopy(document)
    extra["cases"][0]["score_hint"] = 1
    with pytest.raises(DatasetError):
        check_dataset(extra, "dev")


def test_required_coverage_tags_are_present_in_both_splits():
    required = {"ticker-collision", "negation", "trailing-command", "unit-error", "duplicate-key",
                "bool-quantity", "nonfinite", "resource-violation", "stale-schema", "non-quantum",
                "negative-control", "repairable", "repair-refused"}
    for split in ("dev", "eval"):
        tags = {t for c in load_split(split) for t in c["tags"]}
        assert not required - tags, split
    dev_tags = {t for c in load_split("dev") for t in c["tags"]}
    assert {"wrong-plan", "wrong-backend", "wrong-evidence", "stale-catalog"} <= dev_tags
    eval_tags = {t for c in load_split("eval") for t in c["tags"]}
    assert {"wrong-ceiling", "dropped-evidence", "fabricated-zero-budget", "effect-escalation"} <= eval_tags


@pytest.mark.parametrize("module", ["verifier.py", "decode.py", "schema.py"])
def test_scoring_modules_import_nothing_from_the_implementation_under_test(module):
    tree = ast.parse((BENCH / module).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"nano", "nano_qsearch"}


def test_strict_decoder_rejects_duplicates_nonfinite_and_syntax():
    for text, category in (('{"a":1,"a":2}', "json_duplicate_key"), ('{"a":NaN}', "json_nonfinite"),
                           ('{"a":-Infinity}', "json_nonfinite"), ('{"a":1,}', "json_syntax")):
        with pytest.raises(DecodeError) as caught:
            strict_json(text)
        assert caught.value.category == category


def test_strict_equal_distinguishes_bool_int_and_float():
    assert strict_equal({"a": [1, 0.5]}, {"a": [1, 0.5]})
    assert not strict_equal(True, 1)
    assert not strict_equal(1.0, 1)
    assert not strict_equal({"a": 1, "b": 2}, {"a": 1})


# A hand-written v1 document (not produced by the compiler) for verifier controls.
_SOURCE = "check plan alpha-plan"
_CHECK_TASK = {"domain": "quantum", "supported": True, "operation": "check", "resources": {"plan_id": "plan:alpha"},
               "evidence": None, "ceiling": None, "observation": None, "authorized_effects": ["pure"],
               "nodes": None}
_CHECK_DOC = {
    "schema": "nano.quantum.command.v1", "profile": "quantum", "operation_schema": "nano.quantum.operations.v1",
    "source": _SOURCE, "source_hash": "sha256:" + hashlib.sha256(_SOURCE.encode()).hexdigest(),
    "plan_digest": "sha256:" + "0" * 64, "operation": "check", "args": {"plan_id": "plan:alpha"},
    "quantities": [], "entities": [], "diagnostics": [], "effects": ["pure"],
    "steps": [{"kind": "pure", "capability": "quantum.check", "args": {"plan_id": "plan:alpha"}}],
    "catalog_snapshot": {"version": "quantum-catalog/1", "resources": {"plan": {"id": "plan:alpha"}}},
}


def test_verifier_binds_a_hand_written_matching_document():
    assert verify_quantum(_CHECK_TASK, _CHECK_DOC).bound


def test_negative_control_wrong_but_valid_operation_fails_the_verifier():
    wrong = copy.deepcopy(_CHECK_DOC)
    wrong.update(operation="compile", effects=["local_compile"])
    wrong["args"]["backend"] = "local_aer"
    wrong["steps"] = [{"kind": "local_compile", "capability": "quantum.compile", "args": wrong["args"]}]
    verdict = verify_quantum(_CHECK_TASK, wrong)
    assert not verdict.bound and "operation" in verdict.reasons


@pytest.mark.parametrize("mutation", [
    lambda d: d["args"].update(plan_id="plan:beta"),
    lambda d: d["catalog_snapshot"]["resources"].update(bank={"id": "evidence-bank:a", "digest": "sha256:" + "1" * 64}),
    lambda d: d.update(effects=["proposal_only"]),
    lambda d: d.update(source_hash="sha256:" + "f" * 64),
    lambda d: d.update(diagnostics=[{"code": "AMBIGUOUS_COMMAND"}]),
    lambda d: d.update(schema="nano.quantum.command.v0"),
])
def test_verifier_rejects_identity_evidence_authority_and_provenance_drift(mutation):
    document = copy.deepcopy(_CHECK_DOC)
    mutation(document)
    assert not verify_quantum(_CHECK_TASK, document).bound


def test_negative_control_fabricated_zero_budget_ceiling_fails_the_verifier():
    task = {**_CHECK_TASK, "operation": "prepare_submission", "authorized_effects": ["proposal_only"],
            "resources": {"plan_id": "plan:alpha", "backend": "ibm_kyiv"},
            "ceiling": {"shots": 100, "circuits": 1, "qpu_seconds": 2}}
    source = "prepare submission plan alpha-plan on kyiv with 100 shots 1 circuit 2 qpu seconds"
    args = {"plan_id": "plan:alpha", "backend": "ibm_kyiv", "shots": 100, "circuits": 1, "qpu_seconds": 0}
    fabricated = {**_CHECK_DOC, "source": source, "operation": "prepare_submission", "args": args,
                  "source_hash": "sha256:" + hashlib.sha256(source.encode()).hexdigest(),
                  "effects": ["proposal_only"],
                  "steps": [{"kind": "proposal_only", "capability": "quantum.prepare_submission", "args": args}]}
    assert not verify_quantum(task, fabricated).bound


def test_unsupported_tasks_never_bind_and_strategy_actions_are_authorized():
    unsupported = {**_CHECK_TASK, "supported": False, "operation": None, "authorized_effects": []}
    assert not verify_quantum(unsupported, _CHECK_DOC).bound
    nodes = [{"type": "Schedule", "interval": "1m"},
             {"type": "Condition", "signal": "RSI", "operator": "<", "value": 30}, {"type": "Intent", "action": "BUY"}]
    task = {**_CHECK_TASK, "domain": "nano_strategy", "operation": "strategy_rule", "nodes": nodes,
            "authorized_effects": ["OBSERVE"]}
    assert not verify_strategy(task, {"nodes": nodes}).bound
    assert verify_strategy({**task, "authorized_effects": ["BUY"]}, {"nodes": json.loads(json.dumps(nodes))}).bound
