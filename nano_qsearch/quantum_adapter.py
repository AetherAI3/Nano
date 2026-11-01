"""Offline host adapters for checked quantum proposals.

Known bits lower directly to Qiskit circuits. Optimization has an explicit
legacy-job adapter; it is never used to disguise a preparation as a QUBO.
Local observations and decoded records are not signed provider receipts.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from nano.quantum import (RESULT_SCHEMA, OptimizeQubo, Packet, PrepareBits, QuantumPlan,
                          QuantumValidationError, Sample, build_plan, check_evidence_bank,
                          checked_plan, optimize_qubo, parse_plan, parse_result)
from .canonical import canonical_digest, canonical_json, validate_digest
from .job import JOB_SCHEMA, QSearchJob, parse_job

QISKIT_VERSION = "2.3.1"
BRIDGE_SOURCE_SHA = "b6ba6542b2c311d6354948e528bc64f147911178"
REQUEST_SCHEMA = "predator.qsearch.request.v2"


def _fail(code: str, message: str) -> None:
    raise QuantumValidationError(code, message)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class LocalHostPolicy:
    """Deterministic allocation ceilings, checked before circuit allocation."""
    max_qubits: int = 16
    max_pubs: int = 4096
    max_total_shots: int = 100000

    def __post_init__(self) -> None:
        for value in (self.max_qubits, self.max_pubs, self.max_total_shots):
            if type(value) is not int or value < 1:
                _fail("HOST_POLICY", "local host resource caps must be positive integers")


@dataclass(frozen=True)
class LocalPub:
    operation_id: str
    preparation_id: str
    pub_index: int
    register: str
    variables: tuple[str, ...]
    packets: tuple[Packet, ...]
    execution_limits: Mapping[str, int]
    circuit: Any


@dataclass(frozen=True)
class LocalCircuitPlan:
    plan: QuantumPlan
    pubs: tuple[LocalPub, ...]
    policy: LocalHostPolicy = LocalHostPolicy()
    effect: str = "local_compile"
    deadline_owner: str = "run_local_subprocess"


def _qiskit() -> Any:
    import qiskit
    if qiskit.__version__ != QISKIT_VERSION:
        _fail("SDK_VERSION", f"local adapter requires qiskit=={QISKIT_VERSION}")
    return qiskit


def lower_local(plan: QuantumPlan, *, policy: LocalHostPolicy | None = None) -> LocalCircuitPlan:
    """Build local circuits after finite host allocation preflight.

    Declared max_wall_s travels with each PUB and run_local enforces the strictest
    bound in a killable subprocess. No IBM runtime or ATLAS is imported.
    """
    plan = checked_plan(plan)
    if policy is None:
        policy = LocalHostPolicy()
    if not isinstance(policy, LocalHostPolicy):
        _fail("HOST_POLICY", "local host policy must be a LocalHostPolicy")
    policy = LocalHostPolicy(policy.max_qubits, policy.max_pubs, policy.max_total_shots)
    executions = [o for o in plan.operations if isinstance(o, (Sample, OptimizeQubo))]
    if any(isinstance(o, OptimizeQubo) for o in executions):
        _fail("UNSUPPORTED", "local optimization kernel is not implemented; use the explicit legacy adapter")
    if any(o.backend != "local_ideal" or o.limits["max_qpu_s"] != 0 for o in executions):
        _fail("EFFECT", "local adapter admits only local_ideal with zero QPU seconds")
    preparations = {o.id: o for o in plan.operations if isinstance(o, PrepareBits)}
    if (len(executions) > policy.max_pubs or sum(o.shots for o in executions) > policy.max_total_shots
            or any(len(preparations[o.preparation_id].bits) > policy.max_qubits for o in executions)):
        _fail("HOST_LIMIT", "local proposal exceeds the host qubit, PUB or total-shot cap")
    qiskit = _qiskit()
    pubs = []
    for operation in executions:
        preparation = preparations[operation.preparation_id]
        width = len(preparation.bits)
        q = qiskit.QuantumRegister(width, "q")
        c = qiskit.ClassicalRegister(width, operation.result["register"])
        circuit = qiskit.QuantumCircuit(q, c, name="nano.prepare_bits.v1")
        for index, bit in enumerate(preparation.bits):
            if bit == "1":
                circuit.x(q[index])
        circuit.measure(q, c)
        pubs.append(LocalPub(operation.id, preparation.id, operation.result["pub_index"],
                             operation.result["register"], preparation.variables,
                             preparation.packets, operation.limits, circuit))
    return LocalCircuitPlan(plan, tuple(pubs), policy)


def run_local(value: QuantumPlan | LocalCircuitPlan, *, policy: LocalHostPolicy | None = None) -> dict[str, Any]:
    """Sample actual ideal Qiskit circuits with declared seeds, shots and order.

    Rebuild before execution so a mutable circuit or forged public record cannot
    change the checked proposal's behavior. All simulator work is classical.
    Allocation ceilings are enforced before spawning a worker. The strictest
    declared max_wall_s bounds the whole child process, including its startup.
    """
    supplied = value if isinstance(value, LocalCircuitPlan) else None
    # A public bundle is data, never authority to raise host resource ceilings.
    compiled = lower_local(supplied.plan if supplied else value, policy=policy)
    if supplied and (supplied.effect != compiled.effect or supplied.deadline_owner != compiled.deadline_owner
                     or len(supplied.pubs) != len(compiled.pubs)
                     or any(a != b for a, b in zip(supplied.pubs, compiled.pubs))):
        _fail("CIRCUIT", "local circuit bundle differs from its checked proposal")
    operations = [o for o in compiled.plan.operations if isinstance(o, Sample)]
    if not operations:
        return {"schema": RESULT_SCHEMA, "plan_digest": compiled.plan.plan_digest, "pubs": [],
                "optimization_verdict": "not_applicable", "delivery_verdict": "not_applicable"}
    timeout = min(o.limits["max_wall_s"] for o in operations)
    envelope = {"plan": compiled.plan.to_payload(), "policy": {
        "max_qubits": compiled.policy.max_qubits, "max_pubs": compiled.policy.max_pubs,
        "max_total_shots": compiled.policy.max_total_shots}}
    try:
        process = subprocess.run([sys.executable, "-m", "nano_qsearch.quantum_adapter", "--local-worker"],
            input=canonical_json(envelope), capture_output=True, text=True, encoding="utf-8", timeout=timeout,
            cwd=str(Path(__file__).resolve().parents[1]))
    except subprocess.TimeoutExpired:
        _fail("TIMEOUT", "local sampler exceeded the declared wall-time bound; worker was terminated")
    if process.returncode != 0:
        _fail("LOCAL_WORKER", "local sampler worker failed; no result was admitted")
    try:
        response = json.loads(process.stdout)
    except (TypeError, ValueError):
        _fail("LOCAL_WORKER", "local sampler worker returned invalid JSON")
    _exact(response, {"qiskit_version", "pub_count", "result"}, "worker response")
    if (response["qiskit_version"] != QISKIT_VERSION or type(response["pub_count"]) is not int
            or response["pub_count"] != len(compiled.pubs)):
        _fail("LOCAL_WORKER", "local sampler worker SDK or PUB inventory differs")
    parse_result(compiled.plan, response["result"])
    return response["result"]


def _sample_local(compiled: LocalCircuitPlan) -> dict[str, Any]:
    """Worker-only in-process sampler; run_local supplies its hard deadline."""
    from qiskit.primitives import StatevectorSampler
    operations = {o.id: o for o in compiled.plan.operations if isinstance(o, Sample)}
    groups: dict[int, list[LocalPub]] = {}
    for pub in compiled.pubs:
        groups.setdefault(operations[pub.operation_id].seed, []).append(pub)
    observations: dict[int, dict[str, Any]] = {}
    for seed, pubs in groups.items():
        sampler = StatevectorSampler(seed=seed)
        results = sampler.run([(p.circuit, None, operations[p.operation_id].shots) for p in pubs]).result()
        if len(results) != len(pubs):
            _fail("PUB", "local sampler returned an incomplete PUB inventory")
        for pub, result in zip(pubs, results):
            observations[pub.pub_index] = {"operation_id": pub.operation_id, "pub_index": pub.pub_index,
                "register": pub.register, "bit_order": "msb_first",
                "samples": getattr(result.data, pub.register).get_bitstrings()}
    body = {"schema": RESULT_SCHEMA, "plan_digest": compiled.plan.plan_digest,
            "pubs": [observations[p.pub_index] for p in compiled.pubs],
            "optimization_verdict": "not_applicable", "delivery_verdict": "unverified"}
    parse_result(compiled.plan, body)
    return body


@dataclass(frozen=True)
class DecodedRecord:
    operation_id: str
    pub_index: int
    recipient_id: str
    chunk: int
    slot: int
    payload_bits: str
    authentication_bits: str
    authentication_verdict: str
    evidence_reference: str
    evidence_digest: str
    record: Any
    packet: Packet


@dataclass(frozen=True)
class VerifiedResult:
    plan_digest: str
    records: tuple[DecodedRecord, ...]
    optimization_verdict: str
    delivery_verdict: str
    state: str = "verified"


AuthenticationVerifier = Callable[[Packet, str, str], bool]


def decode_verified(plan: QuantumPlan, body: Any, evidence_catalog: Mapping[str, Any], *,
                    authenticate: AuthenticationVerifier | None = None) -> VerifiedResult:
    """Independently verify observations, payloads, evidence and authentication.

    For a declared known-bit preparation every observed shot must match the
    admitted bits. A supplied verifier owns any authentication mechanism; the
    adapter makes no encryption or cryptographic-security claim.
    """
    plan = checked_plan(plan)
    if not isinstance(evidence_catalog, Mapping):
        _fail("TYPE", "evidence catalog must be a caller-supplied mapping")
    result = parse_result(plan, body)
    operations = {o.id: o for o in plan.operations}
    records = []
    deliveries: set[tuple[str, int]] = set()
    optimization = []
    for pub in result.pubs:
        operation = operations[pub.operation_id]
        logical = [s[::-1] for s in pub.samples]
        if isinstance(operation, OptimizeQubo):
            q, offset = operation.qubo["q"], operation.qubo["offset"]
            energies = [offset + sum(q[i][j] * int(bits[i]) * int(bits[j])
                         for i in range(len(bits)) for j in range(len(bits))) for bits in logical]
            floor = operation.audit["classical_min_energy"]
            if min(energies) < floor - 1e-9:
                _fail("AUDIT", "sample energy is below the declared optimization floor")
            optimization.append(abs(min(energies) - floor) <= 1e-9)
            continue
        preparation = operations[operation.preparation_id]
        if any(bits != preparation.bits for bits in logical):
            _fail("PAYLOAD", "sample does not reproduce the admitted known bits")
        position = 0
        for packet in preparation.packets:
            delivery = (packet.recipient_id, packet.chunk)
            if delivery in deliveries:
                _fail("RECIPIENT", "decoded recipient/chunk delivery appears in multiple PUBs")
            deliveries.add(delivery)
            payload = logical[0][position:position + packet.payload_width]
            position += packet.payload_width
            auth = logical[0][position:position + packet.authentication_width]
            position += packet.authentication_width
            bank = check_evidence_bank(packet, evidence_catalog)
            if packet.codec == "evidence_index":
                if not isinstance(bank, Mapping) or set(bank) != {"records"} or not isinstance(bank["records"], list):
                    _fail("CODEC", "evidence_index v1 requires an evidence bank with a records array")
                index = int(payload[::-1] if packet.bit_significance == "lsb_first" else payload, 2)
                if index >= len(bank["records"]):
                    _fail("CODEC", "evidence index is outside the admitted bank")
                record = copy.deepcopy(bank["records"][index])
            elif packet.codec == "raw_bits":
                record = payload
            else:
                _fail("CODEC", "unsupported packet codec")
            auth_verdict = "not_applicable"
            if packet.authentication_width:
                if authenticate is None:
                    _fail("AUTHENTICATION", "authentication overhead requires an explicit verifier")
                try:
                    accepted = authenticate(packet, payload, auth)
                except Exception:
                    _fail("AUTHENTICATION", "authentication verifier failed")
                if accepted is not True:
                    _fail("AUTHENTICATION", "authentication verifier did not accept the packet")
                auth_verdict = "pass"
            records.append(DecodedRecord(operation.id, pub.pub_index, packet.recipient_id, packet.chunk,
                packet.slot, payload, auth, auth_verdict, packet.evidence_reference,
                packet.evidence_digest, _freeze(record), packet))
    verdict = "not_applicable" if not optimization else "pass" if all(optimization) else "fail"
    return VerifiedResult(plan.plan_digest, tuple(records), verdict, "pass" if records else "not_applicable",
                          "failed" if verdict == "fail" else "verified")


@dataclass(frozen=True)
class LegacyCompileProfile:
    """Host-supplied legacy compile fields absent from the operation language."""
    target: str
    backend_kind: str
    chi_max: int
    opt_level: int
    compile_seed: int
    max_variables: int
    compiled_search_digest: str


def to_legacy_job(plan: QuantumPlan, operation_id: str, profile: LegacyCompileProfile) -> QSearchJob:
    """Lower only an actual optimization into the existing strict job reader."""
    plan = checked_plan(plan)
    operation = next((o for o in plan.operations if o.id == operation_id), None)
    if not isinstance(operation, OptimizeQubo):
        _fail("UNSUPPORTED", "only optimize_qubo has a legacy QSearchJob mapping")
    if not isinstance(profile, LegacyCompileProfile):
        _fail("TYPE", "an explicit legacy compile profile is required")
    if operation.backend == "local_ideal" and (profile.backend_kind not in ("statevector", "mps")
            or profile.target not in ("heron_heavy_hex", "fake_fez")):
        _fail("EFFECT", "local optimization requires an explicit local mode and non-live target")
    if operation.backend != "local_ideal" and profile.target != operation.backend:
        _fail("EFFECT", "legacy compile target differs from the operation's declared backend")
    body = {"schema": JOB_SCHEMA, "compiled_search_digest": profile.compiled_search_digest,
        "variables": list(operation.variables), "qubo": _thaw(operation.qubo),
        "compile": {"variant": operation.method["variant"], "p": operation.method["p"],
                    "gammas": list(operation.method["gammas"]), "betas": list(operation.method["betas"]),
                    "opt_level": profile.opt_level, "seed": profile.compile_seed,
                    "target": profile.target, "training": "none"},
        "backend": {"kind": profile.backend_kind, "chi_max": profile.chi_max,
                    "shots": operation.shots, "seed": operation.seed},
        "limits": {"max_variables": profile.max_variables,
                   "max_wall_s": operation.limits["max_wall_s"], "max_qpu_s": operation.limits["max_qpu_s"]},
        "audit": _thaw(operation.audit)}
    body["job_digest"] = canonical_digest(body)
    return parse_job(body)


@dataclass(frozen=True)
class LegacyImport:
    plan: QuantumPlan
    source_payload: Mapping[str, Any]
    metadata: Mapping[str, Any]

    def to_legacy_payload(self) -> dict[str, Any]:
        return _thaw(self.source_payload)


def _exact(value: Any, fields: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail("LEGACY_FIELDS", f"{name} fields differ from the pinned bridge")
    return value


def _int(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _legacy_operation(job: QSearchJob, index: int, total: int) -> dict[str, Any]:
    if job.max_wall_s == 0:
        _fail("LEGACY_LIMIT", "legacy zero wall-time semantics require an explicit migration policy")
    return optimize_qubo(job.variables, [list(row) for row in job.q], offset=job.offset,
        variant=job.variant, p=job.p, gammas=job.gammas, betas=job.betas,
        classical_min_energy=job.classical_min_energy, shots=job.shots, max_circuits=total,
        max_wall_s=job.max_wall_s, max_qpu_s=job.max_qpu_s,
        operation_id=f"legacy-optimize-{index}", backend=job.target, seed=job.backend_seed, pub_index=index)


def read_legacy(body: Any, *, max_pubs: int = 16) -> LegacyImport:
    """Read job.v1 or the pinned request.v2 without submitting or changing them.

    All original fields are retained in source_payload and request metadata.
    The semantic optimization projection does not pretend to be a lossless new
    compile profile. Current-main unsupported target names fail explicitly.
    """
    if not isinstance(body, Mapping):
        _fail("LEGACY_SCHEMA", "legacy document must be an object")
    if body.get("schema") == JOB_SCHEMA:
        job = parse_job(body)
        return LegacyImport(build_plan([_legacy_operation(job, 0, 1)]), _freeze(copy.deepcopy(body)),
                            _freeze({"schema": JOB_SCHEMA, "job_digests": [job.job_digest]}))
    if body.get("schema") != REQUEST_SCHEMA:
        _fail("LEGACY_SCHEMA", "expected predator.qsearch.job.v1 or predator.qsearch.request.v2")
    if not _int(max_pubs, 1) or max_pubs > 4096:
        _fail("LIMITS", "legacy max_pubs must be an integer in 1..4096")
    _exact(body, {"schema", "descriptor", "jobs"}, "request")
    desc = _exact(body["descriptor"], {"schema", "request_id", "target", "purpose", "max_execution_time",
        "manifest_digest", "batch_index", "batch_digest", "blocks"}, "descriptor")
    import re
    if (desc["schema"] != REQUEST_SCHEMA or not isinstance(desc["request_id"], str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", desc["request_id"]) is None
            or desc["target"] not in ("fake_fez", "ibm_fez", "ibm_kingston", "ibm_marrakesh")
            or desc["purpose"] not in ("qsearch", "qcomms") or not _int(desc["batch_index"])
            or not _int(desc["max_execution_time"], 1) or desc["max_execution_time"] > 600):
        _fail("LEGACY_DESCRIPTOR", "invalid descriptor header")
    for field in ("manifest_digest", "batch_digest"):
        try:
            validate_digest(desc[field], field=field)
        except ValueError:
            _fail("DIGEST", "invalid legacy manifest or batch digest")
    blocks = desc["blocks"]
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= max_pubs:
        _fail("PUB", "legacy request exceeds the explicit PUB bound")
    if not isinstance(body["jobs"], list) or len(body["jobs"]) != len(blocks):
        _fail("PUB", "legacy request requires exactly one job per block")
    jobs, digests, deliveries = [], set(), set()
    for index, (block, raw_job) in enumerate(zip(blocks, body["jobs"])):
        _exact(block, {"pub_index", "job_digest", "recipients", "n", "shots", "seed", "classical_min_energy"}, "block")
        job = parse_job(raw_job)
        if (not _int(block["pub_index"]) or block["pub_index"] != index or job.job_digest in digests
                or job.job_digest != block["job_digest"] or type(block["n"]) is not int or job.n != block["n"]
                or type(block["shots"]) is not int or job.shots != block["shots"]
                or type(block["seed"]) is not int or job.backend_seed != block["seed"]
                or type(block["classical_min_energy"]) not in (int, float)
                or job.classical_min_energy != block["classical_min_energy"]
                or job.backend_kind != "ibm" or job.target != desc["target"]):
            _fail("LEGACY_MISMATCH", "legacy job and ordered block differ")
        digests.add(job.job_digest)
        recipients = block["recipients"]
        if not isinstance(recipients, list) or not recipients:
            _fail("RECIPIENT", "legacy block requires recipient metadata")
        previous_slot = -1
        for recipient in recipients:
            _exact(recipient, {"agent_id", "chunk", "slot"}, "recipient")
            if (not isinstance(recipient["agent_id"], str) or not recipient["agent_id"]
                    or not _int(recipient["chunk"]) or not _int(recipient["slot"])
                    or recipient["slot"] <= previous_slot):
                _fail("RECIPIENT", "legacy recipient identity or slot order differs")
            delivery = (recipient["agent_id"], recipient["chunk"])
            if delivery in deliveries:
                _fail("RECIPIENT", "legacy recipient/chunk appears twice")
            deliveries.add(delivery)
            previous_slot = recipient["slot"]
        jobs.append(job)
    if desc["target"] != "fake_fez" and desc["max_execution_time"] > sum(j.max_qpu_s for j in jobs):
        _fail("LIMITS", "legacy execution bound exceeds child QPU-second allowances")
    plan = build_plan([_legacy_operation(job, i, len(jobs)) for i, job in enumerate(jobs)])
    return LegacyImport(plan, _freeze(copy.deepcopy(body)), _freeze({"schema": REQUEST_SCHEMA,
        "bridge_source_sha": BRIDGE_SOURCE_SHA, "request_digest": canonical_digest(desc),
        "descriptor": copy.deepcopy(desc), "job_digests": [j.job_digest for j in jobs]}))


def _local_worker() -> None:
    envelope = json.loads(sys.stdin.read())
    _exact(envelope, {"plan", "policy"}, "worker input")
    policy = _exact(envelope["policy"], {"max_qubits", "max_pubs", "max_total_shots"}, "host policy")
    compiled = lower_local(parse_plan(envelope["plan"]), policy=LocalHostPolicy(**policy))
    body = _sample_local(compiled)
    sys.stdout.write(canonical_json({"qiskit_version": QISKIT_VERSION,
                                   "pub_count": len(compiled.pubs), "result": body}))


if __name__ == "__main__":
    if sys.argv[1:] != ["--local-worker"]:
        raise SystemExit(2)
    _local_worker()
