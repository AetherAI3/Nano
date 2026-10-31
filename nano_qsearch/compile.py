"""Compile a q-search job into logical and native (Heron-basis) programs.

Child-process code: imports numpy, qiskit and ``aether_atlas`` at call time.
The logical program comes from ATLAS ``build_nucleus_program`` (ordinary QAOA
or joint5 with ``find_nuclei`` nuclei); the native program is the same circuit
transpiled to ``{cz, rz, sx, x}`` on a heavy-hex coupling map (or to FakeFez,
whose recorded basis is the ISA's own gate names). ``variant ==
"auto"`` compiles both candidates and keeps the smaller native 2Q count.
``compiled_plan_digest`` binds the ISA bytes and inspection so a receipt names
one specific compiled program.

Device compile (lane HW0, N2): an ``ibm_fez`` job compiled with
``backend=`` (the live backend ATLAS ``resolve_qsearch_context`` resolved) is
transpiled against **that backend's** ``Target`` (coupling map and supported
instructions), not FakeFez, checked against it instruction by instruction, and
frozen: the backend name and ``target_digest`` (a digest of the target's
structure) ride in the ISA's metadata, so the QPY bytes bind them, and in the
plan digest. ``fake_fez`` and ``heron_heavy_hex`` compile exactly as before;
one function, the target source chosen by the job's target.

Two rewrites are available and off by default (``CompileOptions``, read from
the environment when no options are passed): ``basis_prep`` prepares the unique
minimiser of a strictly diagonal QUBO directly, and ``layout`` places a circuit
without two-qubit gates on the best-calibrated Fez qubits. When either is on,
the compile record and the digest carry a ``rewrites`` block saying what was
applied; with both off the compile, record and digest are unchanged. With a
live backend, ``layout="target"`` ranks by that backend's own (live) errors.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import canonical_digest
from .job import QSearchJob

BASIS = ["cz", "rz", "sx", "x"]
HEAVY_HEX_DISTANCES = (3, 5, 7, 9, 11)
PLAN_SCHEMA = "nano.qsearch.compiled-plan.v1"
QPY_CIRCUIT_NAME = "nano.qsearch.isa"
TARGET_SCHEMA = "nano.qsearch.device-target.v1"
DEVICE_TARGETS = ("ibm_fez",)          # targets compiled against a resolved live backend when one is given
TARGET_METADATA_KEY = "nano_device_target"
_UNCHECKED_OPS = frozenset({"barrier"})


@dataclass(frozen=True)
class FrozenTarget:
    """A live backend resolved once for a whole request (J6): its name, its ``Target`` and that target's digest.

    ``compile_jobs`` compiles every job of a request against one of these, so a 1,200-job
    request reads the backend's target once and digests it once, never per job.
    """

    name: str
    target: Any
    target_digest: str

    @classmethod
    def of(cls, backend: Any) -> "FrozenTarget":
        if isinstance(backend, cls):
            return backend
        target = backend.target
        return cls(name=str(backend.name), target=target, target_digest=target_digest(target))
BASIS_PREP_ENV = "NANO_QSEARCH_BASIS_PREP"
LAYOUT_ENV = "NANO_QSEARCH_LAYOUT"
LAYOUT_BACKEND_ENV = "NANO_QSEARCH_LAYOUT_BACKEND"
CALIBRATION_PREFIX = "calibration:"
LAYOUT_MODES = ("target", "calibration")
FEZ_TARGETS = ("fake_fez", "ibm_fez")      # targets whose physical qubits are the Fez register
SNAPSHOT_ENTRIES = {"fake_fez": ("fake_fez", "ibm_fez"), "ibm_fez": ("ibm_fez",)}   # entries a target may rank from


class TargetUnavailable(RuntimeError):
    """The requested compile target needs a package that is not importable."""


@dataclass(frozen=True)
class CompileOptions:
    """Default-off compile rewrites; ``CompileOptions()`` is the unchanged compile (same digest).

    ``basis_prep``: a strictly diagonal QUBO (no coupling, no zero diagonal entry)
    has one minimiser, a computational basis state, so it is prepared directly
    (``x`` on its 1 bits, then measure) instead of through the QAOA layer; the
    output no longer depends on the angles. Any other QUBO, and an explicit
    ``joint5`` request, compiles as before.

    ``layout``: on a Fez target, a circuit without two-qubit gates puts logical
    bit ``i`` on the ``i``-th best physical qubit, ranked by readout error plus
    half the ``x`` error (a bit is 1 about half the time); qubits with a missing
    or out-of-range error or T1 are never chosen. ``"target"`` ranks by the
    transpile target's own instruction errors (the record then carries a digest of
    the numbers ranked); ``"calibration"`` by one backend of a calibration snapshot
    (``backends.<name>.{num_qubits, readout_error, one_qubit_error.x, t1_s}``):
    the job's target, or ``layout_backend`` where ``SNAPSHOT_ENTRIES`` allows it
    (a ``fake_fez`` rehearsal may read the ``ibm_fez`` entry; nothing else may
    stand in for another device).
    """

    basis_prep: bool = False
    layout: str | None = None
    calibration: Mapping[str, Any] | None = None      # the snapshot's ``backends`` mapping
    calibration_sha256: str | None = None
    layout_backend: str | None = None

    def __post_init__(self) -> None:
        if self.layout not in (None, *LAYOUT_MODES):
            raise ValueError(f"layout must be None or one of {list(LAYOUT_MODES)}")
        if (self.layout == "calibration") != (self.calibration is not None):
            raise ValueError("a calibration snapshot is required by, and only by, layout='calibration'")
        if self.layout_backend is not None and self.layout != "calibration":
            raise ValueError("layout_backend applies only to layout='calibration'")

    @property
    def enabled(self) -> bool:
        return self.basis_prep or self.layout is not None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> CompileOptions:
        """``NANO_QSEARCH_BASIS_PREP=1``; ``NANO_QSEARCH_LAYOUT=target`` or ``calibration:<snapshot.json>``
        (snapshot entry from ``NANO_QSEARCH_LAYOUT_BACKEND``, default the job's target). Unset: all off.
        A malformed value or an unreadable snapshot is a ``ValueError`` (a refusal, not a silent default)."""
        environ = os.environ if environ is None else environ
        flag = environ.get(BASIS_PREP_ENV, "")
        if flag not in ("", "0", "1"):
            raise ValueError(f"{BASIS_PREP_ENV} must be 0 or 1")
        raw = environ.get(LAYOUT_ENV, "")
        layout, backends, sha = None, None, None
        if raw == "target":
            layout = "target"
        elif raw.startswith(CALIBRATION_PREFIX):
            try:
                data = Path(raw[len(CALIBRATION_PREFIX):]).read_bytes()
                backends = json.loads(data)["backends"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{LAYOUT_ENV}: calibration snapshot is unreadable") from exc
            if not isinstance(backends, Mapping):
                raise ValueError(f"{LAYOUT_ENV}: calibration snapshot has no backends mapping")
            layout, sha = "calibration", hashlib.sha256(data).hexdigest()
        elif raw:
            raise ValueError(f"{LAYOUT_ENV} must be 'target' or 'calibration:<path>'")
        return cls(basis_prep=flag == "1", layout=layout, calibration=backends, calibration_sha256=sha,
                   layout_backend=environ.get(LAYOUT_BACKEND_ENV) or None)


def diagonal_ground(q) -> tuple[int, ...] | None:
    """The unique minimiser of a strictly diagonal QUBO (bit ``i`` is 1 iff ``Q_ii < 0``), else ``None``.

    With no coupling ``xᵀQx = Σ Q_ii x_i``; every nonzero diagonal entry fixes its bit, a zero one leaves
    two minimisers, so that case (and any coupling) is not a basis state and gets ``None``. A non-finite
    entry is a ``ValueError`` (``parse_job`` already refuses one; this guards direct callers).
    """
    n = len(q)
    if any(not math.isfinite(q[i][j]) for i in range(n) for j in range(n)):
        raise ValueError("QUBO has a non-finite entry")
    if any(q[i][j] != 0.0 for i in range(n) for j in range(n) if i != j) or any(q[i][i] == 0.0 for i in range(n)):
        return None
    return tuple(1 if q[i][i] < 0 else 0 for i in range(n))


def _finite_in(value: Any, low: float, high: float) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
            and low <= value < high)


def rank_qubits(errors: Mapping[int, tuple[Any, Any, Any]], n: int) -> list[int]:
    """The ``n`` best qubits of ``{q: (readout_error, x_error, t1)}``, best first (ties: lower index).

    Cost ``-log(1 - readout) - ½·log(1 - x)``: the ESP loss of measuring the qubit plus, half the time,
    one ``x``. A qubit with any value missing or out of range (error not in [0, 1), T1 not positive) is skipped.
    """
    scored = sorted((-math.log1p(-ro) - 0.5 * math.log1p(-x), q) for q, (ro, x, t1) in errors.items()
                    if _finite_in(ro, 0.0, 1.0) and _finite_in(x, 0.0, 1.0) and _finite_in(t1, 1e-12, math.inf))
    if len(scored) < n:
        raise ValueError(f"layout: only {len(scored)} calibrated qubits for {n} logical bits")
    return [q for _, q in scored[:n]]


def _target_errors(target) -> dict[int, tuple[Any, Any, Any]]:
    out = {}
    for q in range(target.num_qubits):
        measure, x = target["measure"].get((q,)), target["x"].get((q,))
        props = target.qubit_properties[q] if target.qubit_properties else None
        out[q] = (getattr(measure, "error", None), getattr(x, "error", None), getattr(props, "t1", None))
    return out


def _snapshot_errors(entry: Mapping[str, Any], num_qubits: int) -> dict[int, tuple[Any, Any, Any]]:
    """``{q: (readout, x, t1)}`` for the qubits ``"0"…"<num_qubits-1>"`` of a snapshot entry; ``ValueError`` on shape."""
    one = entry.get("one_qubit_error")
    readout, t1 = entry.get("readout_error"), entry.get("t1_s")
    x = one.get("x") if isinstance(one, Mapping) else None
    if not all(isinstance(m, Mapping) for m in (readout, x, t1)):
        raise ValueError("layout: calibration entry needs readout_error, one_qubit_error.x and t1_s mappings")
    return {q: (readout.get(str(q)), x.get(str(q)), t1.get(str(q))) for q in range(num_qubits)}


def _errors_digest(errors: Mapping[int, tuple[Any, Any, Any]]) -> str:
    """Digest of the numbers a ranking used (rankable qubits only), so a record names the calibration it saw."""
    rows = [[q, float(ro), float(x), float(t1)] for q, (ro, x, t1) in sorted(errors.items())
            if _finite_in(ro, 0.0, 1.0) and _finite_in(x, 0.0, 1.0) and _finite_in(t1, 1e-12, math.inf)]
    return canonical_digest({"schema": "nano.qsearch.layout-errors.v1", "qubits": rows})


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


def target_digest(target: Any) -> str:
    """Digest of a qiskit ``Target``'s structure: width and, per operation, the qubit tuples it supports.

    Calibration numbers (errors, durations) are left out: they move with every
    refresh, while the coupling map and instruction set are what an ISA circuit
    must satisfy. A global operation (no qargs restriction) is recorded as null.
    """
    operations = {}
    for name in sorted(target.operation_names):
        qargs = target.qargs_for_operation_name(name)
        operations[name] = None if qargs is None else sorted([int(q) for q in pair] for pair in qargs)
    return canonical_digest({"schema": TARGET_SCHEMA, "num_qubits": int(target.num_qubits), "operations": operations})


def check_isa(isa: Any, target: Any) -> None:
    """``ValueError`` unless every instruction of ``isa`` is supported by ``target`` on its physical qubits."""
    if isa.num_qubits > target.num_qubits:
        raise ValueError("ISA circuit is wider than the target")
    for inst in isa.data:
        name = inst.operation.name
        if name in _UNCHECKED_OPS:
            continue
        qargs = tuple(isa.find_bit(q).index for q in inst.qubits)
        if not target.instruction_supported(name, qargs):
            raise ValueError(f"ISA instruction {name} on {list(qargs)} is not supported by the target")


def _device_transpile(circuit, job: QSearchJob, backend: Any, initial_layout: list[int] | None = None):
    """Transpile against ``backend.target``, verify, and bind backend name + target digest into the ISA."""
    from qiskit import transpile
    target = backend.target
    isa = transpile(circuit, target=target, optimization_level=job.opt_level, seed_transpiler=job.compile_seed,
                    initial_layout=initial_layout)
    check_isa(isa, target)
    digest = backend.target_digest if isinstance(backend, FrozenTarget) else target_digest(target)
    binding = {"backend_name": str(backend.name), "target_digest": digest}
    isa.metadata = {**dict(isa.metadata or {}), TARGET_METADATA_KEY: binding}
    # A fixed name, not the process-global ``circuit-N``: ATLAS fingerprints the raw QPY bytes into the
    # child (and request) identity the operator's authority names, so an identical recompile must match.
    isa.name = QPY_CIRCUIT_NAME
    return isa, f"target:{backend.name}"


def _fez_backend(job: QSearchJob):
    """The FakeFez backend a Fez-register job is transpiled against without a live backend (layout reads it too)."""
    try:
        from qiskit_ibm_runtime.fake_provider import FakeFez
        return FakeFez()
    except Exception as exc:  # noqa: BLE001 - any import or construction failure means the target is absent
        raise TargetUnavailable("qiskit_ibm_runtime FakeFez is not available") from exc


def _transpile(circuit, job: QSearchJob, backend: Any = None, *, fez=None, initial_layout: list[int] | None = None):
    """Return ``(isa_circuit, coupling_map_name)`` for the job's target.

    ``backend`` is a live device backend (device compile); ``fez`` a FakeFez instance to reuse; ``initial_layout``
    applies to the Fez register only (live or FakeFez)."""
    from qiskit import transpile
    if backend is not None:
        return _device_transpile(circuit, job, backend, initial_layout)
    if job.target == "heron_heavy_hex":
        cm = heavy_hex_map(job.n)
        isa = transpile(circuit, coupling_map=cm, basis_gates=BASIS, optimization_level=job.opt_level,
                        seed_transpiler=job.compile_seed)
        return isa, f"heavy_hex_d{_heavy_hex_distance(cm)}"
    fez = _fez_backend(job) if fez is None else fez
    isa = transpile(circuit, backend=fez, optimization_level=job.opt_level, seed_transpiler=job.compile_seed,
                    initial_layout=initial_layout)
    return isa, "fake_fez"


def _basis_prep_program(ground: tuple[int, ...]):
    """``x`` on every 1 bit of ``ground``, then measure every bit into one ``meas`` register (identity mapping)."""
    from qiskit import ClassicalRegister, QuantumCircuit
    n = len(ground)
    circuit = QuantumCircuit(n)
    circuit.add_register(ClassicalRegister(n, "meas"))
    for i, bit in enumerate(ground):
        if bit:
            circuit.x(i)
    circuit.measure(range(n), range(n))
    circuit.metadata = {"schema": "BasisPrepV1", "qaoa_layers": 0, "initialization": "basis_state"}
    return circuit, list(range(n))


def _layout(job: QSearchJob, options: CompileOptions, logical, backend) -> tuple[list[int] | None, dict[str, Any]]:
    """``(physical qubits for logical 0..n-1, record)``; ``None`` leaves placement to the transpiler."""
    if options.layout is None:
        return None, {"mode": "off"}
    info: dict[str, Any] = {"mode": options.layout}
    if job.target not in FEZ_TARGETS:
        return None, {**info, "applied": False, "reason": "target has no device calibration"}
    if _two_qubit_count(logical):
        return None, {**info, "applied": False, "reason": "circuit has two-qubit gates"}
    width = backend.target.num_qubits
    if options.layout == "target":
        errors = _target_errors(backend.target)
        info["errors_digest"] = _errors_digest(errors)        # which calibration the target carried
    else:
        name = options.layout_backend or job.target
        if name not in SNAPSHOT_ENTRIES[job.target]:
            raise ValueError(f"layout: a {job.target} job may not rank qubits from the {name!r} entry")
        entry = options.calibration.get(name)
        if not isinstance(entry, Mapping) or entry.get("num_qubits") != width:
            raise ValueError(f"layout: calibration snapshot has no {width}-qubit entry {name!r}")
        errors = _snapshot_errors(entry, width)
        info.update({"backend": name, "calibration_sha256": options.calibration_sha256,
                     "last_update": entry.get("last_update")})
    physical = rank_qubits(errors, logical.num_qubits)
    return physical, {**info, "applied": True, "physical": physical}


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


def _candidate(job: QSearchJob, variant: str, nuclei: list[list[int]], options: CompileOptions,
               ground: tuple[int, ...] | None = None, backend: Any = None, fez: Any = None):
    if ground is not None:
        logical, mapping = _basis_prep_program(ground)
    else:
        from aether_atlas.qcompiler.nucleus_program import build_nucleus_program
        logical, mapping = build_nucleus_program(
            [list(row) for row in job.q], variant=variant, nuclei=[tuple(v) for v in nuclei],
            gammas=list(job.gammas), betas=list(job.betas), objective_offset=job.offset)
    # The layout ranks the qubits of the target the circuit is transpiled against: the live backend if given.
    if fez is None and backend is None and options.layout is not None and job.target in FEZ_TARGETS:
        fez = _fez_backend(job)
    physical, layout = _layout(job, options, logical, backend if backend is not None else fez)
    isa, cm_name = _transpile(logical, job, backend, fez=fez, initial_layout=physical)
    if physical is not None and [int(q) for q in isa.layout.initial_index_layout(filter_ancillas=True)] != physical:
        raise ValueError("layout: the transpiler moved the calibrated placement")
    metrics = {"logical_2q": _two_qubit_count(logical), "native_2q": _two_qubit_count(isa),
               "native_depth": int(isa.depth(lambda inst: len(inst.qubits) == 2 and inst.operation.name != "barrier"))}
    return logical, isa, list(mapping), metrics, cm_name, layout


def _device_backend(job: QSearchJob, backend: Any) -> Any:
    """The backend to compile against, or None for the FakeFez / heavy-hex path; refuse a mismatched one."""
    if backend is None:
        return None
    if job.target not in DEVICE_TARGETS:
        raise ValueError(f"a live backend applies only to targets {list(DEVICE_TARGETS)}")
    if getattr(backend, "name", None) != job.target or getattr(backend, "target", None) is None:
        raise ValueError("resolved backend name or target differs from the job's target")
    return backend


def compile_program(job: QSearchJob, *, backend: Any = None, options: CompileOptions | None = None,
                    fez: Any = None) -> CompiledProgram:
    """Compile ``job``; with ``backend`` (ibm_fez only) against that live backend's target.

    ``options=None`` reads ``CompileOptions.from_env()`` (all off when the env is unset); ``fez`` is a
    FakeFez instance to reuse instead of building one per transpile (same target, same result)."""
    backend = _device_backend(job, backend)
    options = CompileOptions.from_env() if options is None else options
    ground = diagonal_ground(job.q) if options.basis_prep and job.variant != "joint5" else None
    if ground is not None:
        nuclei, variants = [], ["ordinary"]
    else:
        from aether_atlas.qcompiler.nuclei import find_nuclei
        nuclei = [list(t) for t in find_nuclei([list(row) for row in job.q])] if job.variant in ("auto", "joint5") else []
        if job.variant == "joint5" and not nuclei:
            raise ValueError("joint5 requested but the objective has no edge-disjoint triangle")
        variants = {"auto": ["ordinary"] + (["joint5"] if nuclei else []), "ordinary": ["ordinary"], "joint5": ["joint5"]}[job.variant]
    built = {v: _candidate(job, v, nuclei if v == "joint5" else [], options, ground, backend, fez) for v in variants}
    candidates = {"ordinary": built["ordinary"][3] if "ordinary" in built else None,
                  "joint5": built["joint5"][3] if "joint5" in built else None}
    chosen = min(built, key=lambda v: (built[v][3]["native_2q"], v != "ordinary"))
    logical, isa, mapping, metrics, cm_name, layout = built[chosen]
    chosen_nuclei = nuclei if chosen == "joint5" else []
    measurement_map = [[isa.find_bit(inst.qubits[0]).index, isa.find_bit(inst.clbits[0]).index]
                       for inst in isa.data if inst.operation.name == "measure"]
    record = {"target": job.target, "coupling_map": cm_name, "physical_qubits": isa.num_qubits, "basis": _basis(isa, job),
              "opt_level": job.opt_level, "seed": job.compile_seed, "candidates": candidates}
    device = dict(isa.metadata[TARGET_METADATA_KEY]) if backend is not None else {}
    record.update(device)                  # backend_name, target_digest: device compiles only
    if options.enabled:
        basis_prep = ("applied" if ground is not None else "not_applicable") if options.basis_prep else "off"
        record["rewrites"] = {"basis_prep": basis_prep, "layout": layout}
    # The initial layout without ancillas is stable across identical compiles (only ancilla labels move).
    initial_layout = ([int(q) for q in isa.layout.initial_index_layout(filter_ancillas=True)]
                      if isa.layout is not None else None)
    inspection = {"ops": {k: int(v) for k, v in isa.count_ops().items()}, **metrics,
                  "measurement_map": measurement_map, "initial_layout": initial_layout}
    digest = canonical_digest({"schema": PLAN_SCHEMA,
                               **{k: record[k] for k in ("target", "coupling_map", "physical_qubits", "basis", "opt_level", "seed")},
                               "variant": chosen, "nuclei": chosen_nuclei, "qpy_sha256": _qpy_sha256(isa),
                               "inspection": inspection, **device,
                               **({"rewrites": record["rewrites"]} if "rewrites" in record else {})})
    return CompiledProgram(variant=chosen, nuclei=chosen_nuclei, logical=logical, isa=isa, mapping=mapping,
                           metrics=metrics, candidates=candidates, coupling_map=cm_name,
                           physical_qubits=isa.num_qubits, compiled_plan_digest=digest, compile_record=record,
                           inspection=inspection)


def compile_jobs(jobs: Any, *, backend: Any = None, options: CompileOptions | None = None) -> list[CompiledProgram]:
    """Compile every job of one request against ONE resolved target (J6).

    The live backend (ibm_fez) is frozen once (``FrozenTarget``: one target read, one digest),
    the compile options are read once, and Fez-register jobs compiled without a live backend
    share one FakeFez instance. Each plan is exactly what ``compile_program`` gives the job alone.
    """
    jobs = list(jobs)
    options = CompileOptions.from_env() if options is None else options
    frozen = FrozenTarget.of(backend) if backend is not None else None
    fez = (_fez_backend(jobs[0]) if frozen is None and any(job.target in FEZ_TARGETS for job in jobs) else None)
    return [compile_program(job, backend=frozen, options=options, fez=fez) for job in jobs]


__all__ = ["BASIS", "BASIS_PREP_ENV", "CALIBRATION_PREFIX", "CompileOptions", "CompiledProgram", "DEVICE_TARGETS",
           "FEZ_TARGETS", "FrozenTarget", "compile_jobs", "HEAVY_HEX_DISTANCES", "LAYOUT_BACKEND_ENV", "LAYOUT_ENV", "LAYOUT_MODES", "PLAN_SCHEMA",
           "SNAPSHOT_ENTRIES", "TARGET_METADATA_KEY", "TARGET_SCHEMA", "TargetUnavailable", "check_isa",
           "compile_program", "diagonal_ground", "heavy_hex_map", "rank_qubits", "target_digest"]
