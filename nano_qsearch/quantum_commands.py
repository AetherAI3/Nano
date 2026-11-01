"""Explicit local host binding for quantum command proposals; no submission path."""
from collections.abc import Mapping
from typing import Any

from nano.intent.quantum import QuantumCommandIR, structured_reply, validate_quantum_document
from nano.quantum import OptimizeQubo, PrepareBits, QuantumPlan, QuantumValidationError, Sample, checked_plan
from .quantum_adapter import decode_verified, lower_local, run_local


def run_local_command(command: QuantumCommandIR, *, catalog: Mapping[str, Any],
                      plans: Mapping[str, QuantumPlan], evidence_catalog: Mapping[str, Any],
                      authenticate: Any = None,
                      backend_bindings: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Resolve a checked plan explicitly and perform only the requested local work.

    Catalog names are proposal references. Lowering additionally requires a
    content-bound typed plan supplied by the host. Prepared-artifact manifests,
    signed receipts, authorization and hardware work remain separate contracts.
    """
    if not isinstance(command, QuantumCommandIR):
        raise QuantumValidationError("COMMAND", "expected a quantum command")
    command = validate_quantum_document(command.to_dict(), catalog=catalog)
    reply = structured_reply(command, catalog=catalog)
    if command.diagnostics or command.operation in ("inspect_status", "explain_receipt", "prepare_submission"):
        return reply
    reference = command.args["plan_id"]
    if reference not in plans:
        raise QuantumValidationError("REFERENCE", "host has not supplied the referenced typed plan")
    plan = checked_plan(plans[reference])
    binding = command.catalog_snapshot["resources"]["plan"]
    if binding.get("operation_digest") != plan.plan_digest:
        raise QuantumValidationError("BINDING", "lowering requires an exact typed-plan digest in the catalog")
    operation = next((o for o in plan.operations if o.id == binding["operation_ref"]), None)
    if operation is None or operation.op != binding["operation_kind"]:
        raise QuantumValidationError("BINDING", "catalog operation reference differs from the typed plan")
    if isinstance(operation, Sample) and operation.preparation_id != binding["admitted_preparation_ref"]:
        raise QuantumValidationError("BINDING", "catalog preparation differs from the admitted operation")
    executions = [o for o in plan.operations if isinstance(o, (Sample, OptimizeQubo))]
    if len(executions) > binding["max_circuits"] or any(o.shots > binding["max_shots"] for o in executions):
        raise QuantumValidationError("BOUNDS", "typed plan exceeds the catalog's declared bounds")
    bank = command.catalog_snapshot["resources"].get("bank")
    if bank is not None:
        packets = [p for o in plan.operations if isinstance(o, PrepareBits) for p in o.packets]
        if not packets or any(p.evidence_reference != bank["id"] or p.evidence_digest != bank["digest"] for p in packets):
            raise QuantumValidationError("EVIDENCE", "selected bank differs from the typed packet evidence")
    reply.update(typed_plan_ref=plan.plan_digest, observed_from="local_host")
    if command.operation == "check":
        return {**reply, "validation": "checked"}
    if not executions:
        raise QuantumValidationError("UNSUPPORTED", "local circuit compilation requires an admitted sampling operation")
    backend = command.catalog_snapshot["resources"]["backend"]
    if backend["kind"] != "local":
        raise QuantumValidationError("EFFECT", "local command adapter requires a local backend")
    if command.operation == "simulate" and (
        len(executions) != command.args["circuits"]
        or any(o.shots != command.args["shots"] for o in executions)
        or command.args["qpu_seconds"] != 0
    ):
        raise QuantumValidationError("BOUNDS", "requested quantities differ from the checked execution bounds")
    if backend_bindings is None or backend_bindings.get(backend["id"]) != "local_ideal":
        raise QuantumValidationError("BINDING", "host must explicitly bind the selected backend to local_ideal")
    if command.operation == "simulate" and not isinstance(operation, Sample):
        raise QuantumValidationError("UNSUPPORTED", "local sampling requires an admitted sample operation")
    compiled = lower_local(plan)
    if command.operation == "compile":
        return {**reply, "state": "compiled", "local_circuit_count": len(compiled.pubs)}
    observations = run_local(compiled)
    verified = decode_verified(plan, observations, evidence_catalog, authenticate=authenticate)
    return {**reply, "state": verified.state, "observed_from": "local_ideal",
            "sample_count": sum(len(pub["samples"]) for pub in observations["pubs"]),
            "optimization_verdict": verified.optimization_verdict,
            "delivery_verdict": verified.delivery_verdict,
            "decoded_records": len(verified.records)}
