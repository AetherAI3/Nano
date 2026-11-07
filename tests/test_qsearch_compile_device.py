"""Lane HW0 N2: an ibm_fez job compiles against the resolved live backend's target, not FakeFez.

The "live" backend is a ``GenericBackendV2`` target (8-qubit line, ``cz`` basis)
renamed ``ibm_fez``: a target that differs from FakeFez. No IBM account, no Aer run.
"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.engine
pytest.importorskip("qiskit")
pytest.importorskip("aether_atlas")

from qiskit.providers.fake_provider import GenericBackendV2  # noqa: E402
from qiskit.transpiler import CouplingMap  # noqa: E402

from nano_qsearch.compile import (  # noqa: E402
    TARGET_METADATA_KEY, check_isa, compile_program, target_digest,
)
from nano_qsearch.job import parse_job  # noqa: E402
from nano_qsearch.runtime_ibm import check_device_binding  # noqa: E402


def _backend(coupling, basis=("cz", "rz", "sx", "x"), name="ibm_fez"):
    generic = GenericBackendV2(num_qubits=8, basis_gates=list(basis), coupling_map=coupling, seed=11)
    return SimpleNamespace(name=name, target=generic.target)


LINE = CouplingMap.from_line(8).get_edges()
RING = CouplingMap.from_ring(8).get_edges()


@pytest.fixture
def job(fixtures_dir):
    body = json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))
    return replace(parse_job(body), target="ibm_fez", variant="ordinary")


def test_ibm_fez_compiles_against_the_resolved_target_and_binds_it(job):
    live = _backend(LINE)
    plan = compile_program(job, backend=live)
    digest = target_digest(live.target)
    assert plan.compile_record["backend_name"] == "ibm_fez" and plan.compile_record["target_digest"] == digest
    assert plan.coupling_map == "target:ibm_fez" and plan.isa.num_qubits == 8
    assert plan.isa.metadata[TARGET_METADATA_KEY] == {"backend_name": "ibm_fez", "target_digest": digest}
    check_isa(plan.isa, live.target)                                     # satisfies the target it was built for
    check_device_binding(plan, SimpleNamespace(backend_name="ibm_fez", backend=live))
    with pytest.raises(ValueError, match="not supported by the target"):
        check_isa(plan.isa, _backend(LINE, basis=("ecr", "rz", "sx", "x")).target)
    assert plan.metrics["native_2q"] > 0 and set(plan.isa.count_ops()) - {"measure", "barrier"} <= {"cz", "rz", "sx", "x"}


def test_the_target_differs_from_fakefez_and_the_digest_follows_the_target(job):
    pytest.importorskip("qiskit_ibm_runtime")
    from qiskit_ibm_runtime.fake_provider import FakeFez
    line, ring = _backend(LINE), _backend(RING)
    assert target_digest(line.target) != target_digest(FakeFez().target) != target_digest(ring.target)
    a, b = compile_program(job, backend=line), compile_program(job, backend=line)
    assert a.compiled_plan_digest == b.compiled_plan_digest                 # deterministic per target
    from aether_atlas.qcompiler.compiled_plan import circuit_bytes
    assert circuit_bytes(a.isa) == circuit_bytes(b.isa)                     # so ATLAS's child identity repeats
    c = compile_program(job, backend=ring)
    assert c.compiled_plan_digest != a.compiled_plan_digest
    assert c.compile_record["target_digest"] != a.compile_record["target_digest"]
    with pytest.raises(ValueError):
        check_device_binding(a, SimpleNamespace(backend_name="ibm_fez", backend=ring))


def test_a_backend_is_refused_for_other_targets_or_names(job):
    with pytest.raises(ValueError, match="applies only"):
        compile_program(replace(job, target="fake_fez"), backend=_backend(LINE))
    with pytest.raises(ValueError, match="differs"):
        compile_program(job, backend=_backend(LINE, name="ibm_torino"))


def test_heron_compiles_without_a_device_binding(job):
    heron = compile_program(replace(job, target="heron_heavy_hex"))
    assert "target_digest" not in heron.compile_record and TARGET_METADATA_KEY not in (heron.isa.metadata or {})
