"""Compile a q-search job into logical and native (Heron-basis) programs.

Child-process code: imports numpy, qiskit and ``aether_atlas`` at call time.
The logical program comes from ATLAS ``build_nucleus_program`` (ordinary QAOA
or joint5 with ``find_nuclei`` nuclei); the native program is the same circuit
transpiled to ``{cz, rz, sx, x}`` on a heavy-hex coupling map (or to FakeFez,
whose recorded basis is the ISA's own gate names). ``variant ==
"auto"`` compiles both candidates and keeps the smaller native 2Q count.
``compiled_plan_digest`` binds the ISA bytes and inspection so a receipt names
one specific compiled program.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from typing import Any

from .canonical import canonical_digest
from .job import QSearchJob

BASIS = ["cz", "rz", "sx", "x"]
HEAVY_HEX_DISTANCES = (3, 5, 7, 9, 11)
PLAN_SCHEMA = "nano.qsearch.compiled-plan.v1"
QPY_CIRCUIT_NAME = "nano.qsearch.isa"


class TargetUnavailable(RuntimeError):
    """The requested compile target needs a package that is not importable."""


@dataclass(frozen=True)
class CompiledProgram:
    variant: str
    nuclei: list[list[int]]
    logical: Any                       # qiskit QuantumCircuit with measurements in logical order
    isa: Any                           # transpiled circuit
    mapping: list[int]                 # logical i → output wire of the logical circuit
    metrics: dict[str, int]            # logical_2q, native_2q, native_depth (chosen variant)
    candidates: dict[str, dict[str, int] | None]
    coupling_map: str
    physical_qubits: int
    compiled_plan_digest: str
    compile_record: dict[str, Any] = field(default_factory=dict)
    inspection: dict[str, Any] = field(default_factory=dict)   # the digest's inspection block


def heavy_hex_map(n: int):
    """Smallest heavy-hex coupling map (distance 3, 5, 7, ...) with at least ``n`` qubits."""
    from qiskit.transpiler import CouplingMap
    for d in HEAVY_HEX_DISTANCES:
        cm = CouplingMap.from_heavy_hex(d)
        if cm.size() >= n:
            return cm
    raise ValueError(f"no heavy-hex distance in {HEAVY_HEX_DISTANCES} fits {n} qubits")


def _heavy_hex_distance(cm) -> int:
    """Distance of a ``heavy_hex_map`` result, recovered from its size.

    ``CouplingMap.distance`` is a method (qubit-pair distance), so the code
    distance cannot ride on the object; heavy-hex size is (5d² − 2d − 1) / 2.
    """
    for d in HEAVY_HEX_DISTANCES:
        if (5 * d * d - 2 * d - 1) // 2 == cm.size():
            return d
    raise ValueError(f"coupling map of size {cm.size()} is not a heavy-hex map in {HEAVY_HEX_DISTANCES}")


def _two_qubit_count(circuit) -> int:
    return sum(1 for inst in circuit.data if len(inst.qubits) == 2 and inst.operation.name != "barrier")


def _transpile(circuit, job: QSearchJob):
    """Return ``(isa_circuit, coupling_map_name)`` for the job's target."""
    from qiskit import transpile
    if job.target == "heron_heavy_hex":
        cm = heavy_hex_map(job.n)
        isa = transpile(circuit, coupling_map=cm, basis_gates=BASIS, optimization_level=job.opt_level,
                        seed_transpiler=job.compile_seed)
        return isa, f"heavy_hex_d{_heavy_hex_distance(cm)}"
    try:
        from qiskit_ibm_runtime.fake_provider import FakeFez
        backend = FakeFez()
    except Exception as exc:  # noqa: BLE001 - any import or construction failure means the target is absent
        raise TargetUnavailable("qiskit_ibm_runtime FakeFez is not available") from exc
    isa = transpile(circuit, backend=backend, optimization_level=job.opt_level, seed_transpiler=job.compile_seed)
    return isa, "fake_fez"


def _basis(isa, job: QSearchJob) -> list[str]:
    """Gate names the plan is expressed in: ``BASIS`` for heron, the ISA's own ops for Fez."""
    if job.target == "heron_heavy_hex":
        return list(BASIS)
    return sorted(name for name in isa.count_ops() if name not in ("measure", "barrier"))


def _qpy_sha256(circuit) -> str:
    """SHA-256 of the QPY bytes of the circuit's gates, under a fixed name, without its layout.

    Two parts of a transpiled circuit vary between identical compiles and are
    dropped: the process-global counter name of an unnamed circuit
    (``circuit-43``), and the ``TranspileLayout``, whose placement of idle
    ancilla qubits is not stable across runs. Registers, instructions, global
    phase and metadata are kept; ``measurement_map`` in the plan records where
    each logical bit is read.
    """
    from qiskit import QuantumCircuit, qpy
    bare = QuantumCircuit(*circuit.qregs, *circuit.cregs, name=QPY_CIRCUIT_NAME,
                          global_phase=circuit.global_phase, metadata=dict(circuit.metadata or {}))
    for inst in circuit.data:
        bare.append(inst)
    buffer = io.BytesIO()
    qpy.dump(bare, buffer)
    return hashlib.sha256(buffer.getvalue()).hexdigest()


def _candidate(job: QSearchJob, variant: str, nuclei: list[list[int]]):
    from aether_atlas.qcompiler.nucleus_program import build_nucleus_program
    logical, mapping = build_nucleus_program(
        [list(row) for row in job.q], variant=variant, nuclei=[tuple(v) for v in nuclei],
        gammas=list(job.gammas), betas=list(job.betas), objective_offset=job.offset)
    isa, cm_name = _transpile(logical, job)
    metrics = {"logical_2q": _two_qubit_count(logical), "native_2q": _two_qubit_count(isa),
               "native_depth": int(isa.depth(lambda inst: len(inst.qubits) == 2 and inst.operation.name != "barrier"))}
    return logical, isa, list(mapping), metrics, cm_name


def compile_program(job: QSearchJob) -> CompiledProgram:
    from aether_atlas.qcompiler.nuclei import find_nuclei
    nuclei = [list(t) for t in find_nuclei([list(row) for row in job.q])] if job.variant in ("auto", "joint5") else []
    if job.variant == "joint5" and not nuclei:
        raise ValueError("joint5 requested but the objective has no edge-disjoint triangle")
    variants = {"auto": ["ordinary"] + (["joint5"] if nuclei else []), "ordinary": ["ordinary"], "joint5": ["joint5"]}[job.variant]
    built = {v: _candidate(job, v, nuclei if v == "joint5" else []) for v in variants}
    candidates = {"ordinary": built["ordinary"][3] if "ordinary" in built else None,
                  "joint5": built["joint5"][3] if "joint5" in built else None}
    chosen = min(built, key=lambda v: (built[v][3]["native_2q"], v != "ordinary"))
    logical, isa, mapping, metrics, cm_name = built[chosen]
    chosen_nuclei = nuclei if chosen == "joint5" else []
    measurement_map = [[isa.find_bit(inst.qubits[0]).index, isa.find_bit(inst.clbits[0]).index]
                       for inst in isa.data if inst.operation.name == "measure"]
    record = {"target": job.target, "coupling_map": cm_name, "physical_qubits": isa.num_qubits, "basis": _basis(isa, job),
              "opt_level": job.opt_level, "seed": job.compile_seed, "candidates": candidates}
    # The initial layout without ancillas is stable across identical compiles (only ancilla labels move).
    initial_layout = ([int(q) for q in isa.layout.initial_index_layout(filter_ancillas=True)]
                      if isa.layout is not None else None)
    inspection = {"ops": {k: int(v) for k, v in isa.count_ops().items()}, **metrics,
                  "measurement_map": measurement_map, "initial_layout": initial_layout}
    digest = canonical_digest({"schema": PLAN_SCHEMA,
                               **{k: record[k] for k in ("target", "coupling_map", "physical_qubits", "basis", "opt_level", "seed")},
                               "variant": chosen, "nuclei": chosen_nuclei, "qpy_sha256": _qpy_sha256(isa),
                               "inspection": inspection})
    return CompiledProgram(variant=chosen, nuclei=chosen_nuclei, logical=logical, isa=isa, mapping=mapping,
                           metrics=metrics, candidates=candidates, coupling_map=cm_name,
                           physical_qubits=isa.num_qubits, compiled_plan_digest=digest, compile_record=record,
                           inspection=inspection)


__all__ = ["BASIS", "CompiledProgram", "HEAVY_HEX_DISTANCES", "PLAN_SCHEMA", "TargetUnavailable",
           "compile_program", "heavy_hex_map"]
