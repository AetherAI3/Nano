"""Bounded allowlisted repair for the #87 proposal benchmark (dev split and synthetic inputs only)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.proposal.pipeline import admit_quantum_json, admit_quantum_source, admit_strategy  # noqa: E402
from benchmarks.proposal.repair import correction, decide  # noqa: E402
from benchmarks.proposal.runner import evaluate  # noqa: E402
from benchmarks.proposal.schema import DATA, REPAIR_KINDS, ROOT as BENCH, load_json, load_split  # noqa: E402

CATALOG = load_json(DATA / "catalog.json")
DEV = {case["id"]: case for case in load_split("dev")}


@pytest.fixture(scope="module")
def candidate():
    return evaluate(["dev"], "allowlist-v1", "test")


def _row(report, case_id):
    return next(r for r in report["arms"]["recorded_repair"]["dev"]["rows"] if r["id"] == case_id)


def _clean(case_id):
    return DEV[case_id]["recorded_repair"]["text"]


def test_policy_contract_lists_exactly_the_implemented_kinds():
    policy = load_json(BENCH / "policy.json")
    assert sorted(policy["kinds"]) == sorted(REPAIR_KINDS) and policy["max_repairs_per_case"] == 1


@pytest.mark.parametrize("case_id, kind", [("dev-q-m16", "json_trailing_comma"), ("dev-q-m17", "constant_field"),
                                           ("dev-n-m01", "nano_missing_rparen")])
def test_allowlisted_repairs_are_accepted_once_and_then_verified(candidate, case_id, kind):
    row = _row(candidate, case_id)
    assert row["repair"] == {"candidate": True, "attempted": True, "accepted": True, "kind": kind,
                             "reason": "accepted"}
    assert row["first"]["executable"] is False and row["final"]["executable"] is True
    assert row["verifier"]["bound"] is True and row["task_success"] is True and row["expectation_match"]


def test_syntax_repair_that_recovers_validity_but_not_the_task_is_a_semantic_regression(candidate):
    row = _row(candidate, "dev-n-m03")
    assert row["repair"]["accepted"] is True and row["final"]["executable"] is True
    assert row["verifier"]["bound"] is False and row["post_repair_semantic_regression"] is True
    assert row["outcome"] == "abstain" and row["observed_category"] == "task_binding"


@pytest.mark.parametrize("case_id", ["dev-q-m11", "dev-q-m14"])
def test_type_and_stale_version_rewrites_are_refused(candidate, case_id):
    row = _row(candidate, case_id)
    assert row["repair"]["attempted"] is True and row["repair"]["accepted"] is False
    assert row["repair"]["reason"] == "no_allowlisted_correction"
    assert row["final"]["executable"] is False and row["outcome"] == "abstain"


def test_candidate_that_also_changes_the_target_is_refused():
    original = DEV["dev-q-m16"]["proposal"]["text"]
    tampered = _clean("dev-q-m16").replace('"plan_id":"plan:beta"', '"plan_id":"plan:alpha"')
    first = admit_quantum_json(original, CATALOG)
    decision = decide("allowlist-v1", "quantum_command_json", original, first, tampered)
    assert decision.attempted and not decision.accepted and decision.reason == "candidate_differs"


def test_repair_is_never_attempted_after_a_diagnostic_abstention():
    source = "simlate plan alpha-plan on local with 100 shots 1 circuit 0 qpu seconds"
    first = admit_quantum_source(source, CATALOG)
    assert first.category == "UNSUPPORTED_COMMAND"
    decision = decide("allowlist-v1", "quantum_source", source, first, source.replace("simlate", "simulate"))
    assert decision.reason == "not_triggered" and not decision.attempted


@pytest.mark.parametrize("case_id", ["dev-q-m09", "dev-q-m10", "dev-q-m12"])
def test_duplicate_and_nonfinite_payloads_are_not_repairable(case_id):
    text = DEV[case_id]["proposal"]["text"]
    first = admit_quantum_json(text, CATALOG)
    decision = decide("allowlist-v1", "quantum_command_json", text, first, text)
    assert decision.reason == "not_triggered" and not decision.accepted


def test_constant_field_repair_never_rewrites_a_present_value():
    document = json.loads(_clean("dev-q-m17"))
    document["profile"] = "market"
    text = json.dumps(document)
    assert correction("quantum_command_json", text, admit_quantum_json(text, CATALOG)) is None


def test_comma_inside_a_string_is_neither_counted_nor_removed():
    document = json.loads(_clean("dev-q-m16"))
    text = json.dumps(document)[:-1] + ',"note":"a\\",}",}'
    kind, repaired = correction("quantum_command_json", text, admit_quantum_json(text, CATALOG))
    assert kind == "json_trailing_comma" and repaired["note"] == 'a",}'


def test_two_trailing_commas_are_ambiguous_and_refused():
    broken = _clean("dev-q-m16").replace('"}]}', '"},],}')
    assert correction("quantum_command_json", broken, admit_quantum_json(broken, CATALOG)) is None


def test_strategy_repair_reuses_the_diagnostic_fixture_policy():
    source = DEV["dev-n-m01"]["proposal"]["text"]
    derived = correction("nano_strategy_source", source, admit_strategy(source))
    assert derived == ("nano_missing_rparen", _clean("dev-n-m01"))
    valid_wrong = DEV["dev-n-m02"]["proposal"]["text"]
    assert correction("nano_strategy_source", valid_wrong, admit_strategy(valid_wrong)) is None


def test_candidate_hard_acceptance_holds_and_repairs_do_not_change_other_rows(candidate):
    baseline = evaluate(["dev"], "none", "test")
    for arm in ("no_model", "recorded_proposal", "recorded_repair"):
        assert candidate["arms"][arm]["dev"]["hard_acceptance"]["passed"]
    before = {r["id"]: r for r in baseline["arms"]["recorded_repair"]["dev"]["rows"]}
    for row in candidate["arms"]["recorded_repair"]["dev"]["rows"]:
        if not row["repair"]["candidate"]:
            assert row == before[row["id"]]
