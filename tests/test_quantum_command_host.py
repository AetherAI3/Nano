"""Complete command -> checked operations -> local result without providers."""
import copy
import importlib.util
import json
from importlib.resources import files

import pytest

from nano.intent import compile_intent
from nano.quantum import QuantumValidationError, build_plan, prepare_bits, sample
from nano_qsearch.quantum_commands import run_local_command


def context():
    catalog = json.loads(files("nano.intent").joinpath("fixtures/quantum_commands_v1.json").read_text(encoding="utf-8"))["catalog"]
    plan = build_plan([prepare_bits("100"), sample("prepare", expected_width=3, shots=100,
                      max_circuits=1, max_wall_s=15, max_qpu_s=0)])
    catalog["plans"]["demo-plan"].update(operation_ref="sample", admitted_preparation_ref="prepare",
                                          operation_digest=plan.plan_digest)
    return catalog, plan


def command(source, catalog):
    return compile_intent(source, profile="quantum", quantum_catalog=catalog)


@pytest.mark.skipif(importlib.util.find_spec("qiskit") is None, reason="requires pinned quantum extra")
def test_phrase_to_typed_plan_to_actual_local_samples_to_structured_reply(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "qiskit_ibm_runtime", None)
    catalog, plan = context()
    ir = command("simulate quantum plan demo-plan on local with 100 shots 1 circuit 0 qpu seconds", catalog)
    result = run_local_command(ir, catalog=catalog, plans={"plan:demo": plan}, evidence_catalog={},
                               backend_bindings={"local_simulator": "local_ideal"})
    assert result["state"] == "verified" and result["sample_count"] == 100
    assert result["typed_plan_ref"] == plan.plan_digest and result["plan_digest"] == ir.plan_digest
    assert result["observed_from"] == "local_ideal" and result["effects"] == ["local_simulation"]
    assert result["delivery_verdict"] == result["optimization_verdict"] == "not_applicable"
    compiled = run_local_command(command("compile quantum plan demo-plan on local", catalog),
                                 catalog=catalog, plans={"plan:demo": plan}, evidence_catalog={},
                                 backend_bindings={"local_simulator": "local_ideal"})
    assert compiled["state"] == "compiled" and compiled["local_circuit_count"] == 1


def test_catalog_name_alone_never_admits_local_work():
    catalog, plan = context()
    catalog["plans"]["demo-plan"].pop("operation_digest")
    ir = command("check quantum plan demo-plan", catalog)
    with pytest.raises(QuantumValidationError, match="BINDING"):
        run_local_command(ir, catalog=catalog, plans={"plan:demo": plan}, evidence_catalog={})
    with pytest.raises(QuantumValidationError, match="REFERENCE"):
        run_local_command(ir, catalog=catalog, plans={}, evidence_catalog={})


def test_quantities_must_match_the_exact_resolved_plan_before_lowering():
    catalog, plan = context()
    ir = command("simulate quantum plan demo-plan on local with 99 shots 1 circuit 0 qpu seconds", catalog)
    with pytest.raises(QuantumValidationError, match="BOUNDS"):
        run_local_command(ir, catalog=catalog, plans={"plan:demo": plan}, evidence_catalog={})
    two = command("simulate quantum plan demo-plan on local with 100 shots 2 circuits 0 qpu seconds", catalog)
    with pytest.raises(QuantumValidationError, match="BOUNDS"):
        run_local_command(two, catalog=catalog, plans={"plan:demo": plan}, evidence_catalog={})
    changed = copy.deepcopy(catalog)
    changed["plans"]["demo-plan"]["operation_digest"] = "sha256:" + "0" * 64
    with pytest.raises(QuantumValidationError, match="COMMAND"):
        run_local_command(ir, catalog=changed, plans={"plan:demo": plan}, evidence_catalog={})


def test_prepare_submission_and_status_remain_proposals_or_supplied_observations(monkeypatch):
    import nano_qsearch.quantum_commands as host
    monkeypatch.setattr(host, "lower_local", lambda _: pytest.fail("unexpected lowering"))
    catalog, _ = context()
    ir = command("prepare submission quantum plan demo-plan on ibm-fez with 100 shots 1 circuit 2 qpu seconds", catalog)
    result = run_local_command(ir, catalog=catalog, plans={}, evidence_catalog={})
    assert result["state"] == "proposed" and result["effects"] == ["proposal_only"]
    status = run_local_command(command("inspect status job demo-job", catalog),
                               catalog=catalog, plans={}, evidence_catalog={})
    assert status["state"] == "queued" and status["attestation_verified"] is False
    invalid = run_local_command(command("do not simulate quantum plan demo-plan", catalog),
                                catalog=catalog, plans={}, evidence_catalog={})
    assert invalid["state"] == "clarification"


def test_catalog_bounds_backend_and_evidence_remain_bound_before_any_work():
    catalog, plan = context()
    source = "compile quantum plan demo-plan on local"
    with pytest.raises(QuantumValidationError, match="BINDING"):
        run_local_command(command(source, catalog), catalog=catalog, plans={"plan:demo": plan}, evidence_catalog={})
    catalog["plans"]["demo-plan"]["max_shots"] = 50
    with pytest.raises(QuantumValidationError, match="BOUNDS"):
        run_local_command(command(source, catalog), catalog=catalog, plans={"plan:demo": plan}, evidence_catalog={})
    catalog["plans"]["demo-plan"]["max_shots"] = 1000
    selected = command("check quantum plan demo-plan using evidence bank demo-bank", catalog)
    with pytest.raises(QuantumValidationError, match="EVIDENCE"):
        run_local_command(selected, catalog=catalog, plans={"plan:demo": plan}, evidence_catalog={})


def test_definition_only_plan_never_claims_an_actual_compiled_circuit():
    catalog, _ = context()
    plan = build_plan([prepare_bits("100")])
    catalog["plans"]["demo-plan"].update(operation_kind="prepare_bits", operation_ref="prepare",
                                          operation_digest=plan.plan_digest)
    ir = command("compile quantum plan demo-plan on local", catalog)
    with pytest.raises(QuantumValidationError, match="UNSUPPORTED"):
        run_local_command(ir, catalog=catalog, plans={"plan:demo": plan}, evidence_catalog={},
                          backend_bindings={"local_simulator": "local_ideal"})
