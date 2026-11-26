"""Nano++ autonomous optimization loop — graph, mutation admission, quantum interface.

The loop is the universal lifecycle shared by trading, engineering, and
quantum workloads: observe -> propose -> compile -> execute -> measure ->
verify -> admit -> repeat. Each stage is a deterministic, recorded,
replayable node.
"""

import json
from pathlib import Path

import pytest

from nano.loop.graph import LoopGraph
from nano.loop.nodes import LOOP_STAGES, LoopValidationError
from nano.loop.mutation import (
    AdmissionDecision,
    Candidate,
    admit_mutation,
)
from nano.loop.quantum import QuantumJob, SimulatorRuntime

# A closed optimization loop: observe workload -> quantum-seed candidate
# selection -> classical scoring -> verify -> gate -> execute -> measure.
OPTIMIZATION_LOOP = {
    "type": "Loop",
    "nanoIrVersion": "0.1.0",
    "name": "OptimizationLoop",
    "effects": ["quantum.submit", "intent.emit", "sign.emit", "log.append"],
    "nodes": [
        {"id": "n1", "stage": "Observe", "inputs": []},
        {"id": "n2", "stage": "Optimize", "inputs": ["n1"], "attrs": {"kind": "quantum_seed"}},
        {"id": "n3", "stage": "Evaluate", "inputs": ["n2"], "attrs": {"scorer": "classical"}},
        {"id": "n4", "stage": "Verify", "inputs": ["n3"]},
        {"id": "n5", "stage": "Gate", "inputs": ["n4"]},
        {"id": "n6", "stage": "Execute", "inputs": ["n5"]},
        {"id": "n7", "stage": "Benchmark", "inputs": ["n6"]},
        {"id": "n8", "stage": "Sign", "inputs": ["n7"]},
    ],
}


# --- Loop graph -------------------------------------------------------------


def test_loop_graph_loads_and_round_trips():
    graph = LoopGraph.from_dict(OPTIMIZATION_LOOP)
    assert graph.name == "OptimizationLoop"
    assert [n.stage for n in graph.nodes][:3] == ["Observe", "Optimize", "Evaluate"]
    assert graph.to_dict() == OPTIMIZATION_LOOP


def test_all_universal_stages_are_known():
    assert {
        "Observe", "Infer", "Evaluate", "Optimize", "Mutate", "Verify", "Gate",
        "Execute", "Pause", "Replay", "Checkpoint", "Rollback", "Escalate",
        "Sign", "Benchmark",
    } == set(LOOP_STAGES)


def test_unknown_stage_rejected():
    bad = dict(OPTIMIZATION_LOOP, nodes=[{"id": "x", "stage": "Nuke", "inputs": []}])
    with pytest.raises(LoopValidationError):
        LoopGraph.from_dict(bad)


def test_forward_reference_rejected():
    bad = dict(
        OPTIMIZATION_LOOP,
        nodes=[
            {"id": "a", "stage": "Observe", "inputs": ["b"]},
            {"id": "b", "stage": "Execute", "inputs": []},
        ],
    )
    with pytest.raises(LoopValidationError):
        LoopGraph.from_dict(bad)


def test_quantum_stage_requires_manifest_effect():
    bad = dict(OPTIMIZATION_LOOP, effects=["intent.emit", "log.append"])
    with pytest.raises(LoopValidationError):
        LoopGraph.from_dict(bad)  # Optimize/quantum_seed needs quantum.submit


def test_example_loop_loads_and_round_trips():
    path = (
        Path(__file__).resolve().parent.parent
        / "nano" / "examples" / "optimization_loop.json"
    )
    data = json.loads(path.read_text())
    graph = LoopGraph.from_dict(data)
    assert graph.name == "OptimizationLoop"
    assert graph.to_dict() == data
    assert "Mutate" in {n.stage for n in graph.nodes}  # persistent, mutating loop


def test_content_address_is_stable_and_sensitive():
    a = LoopGraph.from_dict(OPTIMIZATION_LOOP)
    b = LoopGraph.from_dict(OPTIMIZATION_LOOP)
    assert a.content_hash() == b.content_hash()
    mutated = dict(OPTIMIZATION_LOOP, name="Other")
    assert LoopGraph.from_dict(mutated).content_hash() != a.content_hash()


# --- Safe self-mutation: 4-stage admission ----------------------------------


def _candidate(**over):
    base = dict(
        name="faster_scorer",
        parent_hash="blake3:parent",
        replay_matches=True,
        benchmark_delta=0.12,      # +12% improvement
        regressions=0,
        static_ok=True,
        capabilities=("quantum.submit",),
        provenance="aqrc",
    )
    base.update(over)
    return Candidate(**base)


def test_clean_candidate_is_admitted():
    decision = admit_mutation(_candidate(), allowed_capabilities=("quantum.submit",))
    assert isinstance(decision, AdmissionDecision)
    assert decision.admitted is True
    assert decision.stage_reached == "Admission"
    assert decision.artifact is not None  # produced, not deployed live


def test_replay_mismatch_blocks_at_verification():
    decision = admit_mutation(_candidate(replay_matches=False))
    assert decision.admitted is False
    assert decision.stage_reached == "Verification"
    assert "replay" in decision.reason.lower()


def test_regression_blocks_at_verification():
    decision = admit_mutation(_candidate(regressions=3))
    assert decision.admitted is False
    assert decision.stage_reached == "Verification"


def test_capability_escalation_blocks_at_admission():
    decision = admit_mutation(
        _candidate(capabilities=("quantum.submit", "shell.exec")),
        allowed_capabilities=("quantum.submit",),
    )
    assert decision.admitted is False
    assert decision.stage_reached == "Admission"
    assert "capability" in decision.reason.lower()


def test_no_benefit_candidate_rejected_before_admission():
    decision = admit_mutation(_candidate(benchmark_delta=-0.01))
    assert decision.admitted is False
    assert decision.stage_reached == "Verification"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("benchmark_delta", float("nan")),
        ("benchmark_delta", float("inf")),
        ("benchmark_delta", float("-inf")),
        ("benchmark_delta", True),
        ("benchmark_delta", "0.12"),
        ("benchmark_delta", 10**400),
        ("regressions", -1),
        ("regressions", False),
        ("regressions", 0.0),
        ("replay_matches", "pending"),
        ("static_ok", "pending"),
    ],
)
def test_unusable_evidence_stops_candidate_at_verification(field, value):
    decision = admit_mutation(_candidate(**{field: value}), allowed_capabilities=("quantum.submit",))
    assert not decision.admitted
    assert decision.stage_reached == "Verification"
    assert decision.artifact is None


@pytest.mark.parametrize("minimum", [float("nan"), float("inf"), -0.01, False])
def test_invalid_gain_threshold_stops_admission(minimum):
    decision = admit_mutation(_candidate(), allowed_capabilities=("quantum.submit",), min_gain=minimum)
    assert not decision.admitted
    assert decision.stage_reached == "Verification"
    assert decision.artifact is None


def test_admission_is_deterministic():
    a = admit_mutation(_candidate(), allowed_capabilities=("quantum.submit",))
    b = admit_mutation(_candidate(), allowed_capabilities=("quantum.submit",))
    assert a.to_dict() == b.to_dict()


# --- Abstract quantum runtime interface -------------------------------------


def test_simulator_runtime_is_deterministic():
    job = QuantumJob(circuit_id="qaoa_40q", shots=1024, params=(0.1, 0.2))
    a = SimulatorRuntime().submit(job)
    b = SimulatorRuntime().submit(job)
    assert a.to_dict() == b.to_dict()
    assert 0.0 <= a.fidelity <= 1.0
    assert a.backend == "simulator"


def test_different_jobs_produce_different_results():
    r1 = SimulatorRuntime().submit(QuantumJob(circuit_id="a", shots=100, params=()))
    r2 = SimulatorRuntime().submit(QuantumJob(circuit_id="b", shots=100, params=()))
    assert r1.distribution != r2.distribution


def test_runtime_is_vendor_agnostic():
    # Nano knows "quantum jobs", not IBM/IonQ. A job carries no vendor field.
    job = QuantumJob(circuit_id="c", shots=10, params=())
    assert not hasattr(job, "vendor")
    assert not hasattr(job, "ibm_backend")
