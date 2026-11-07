"""Local ideal runtime for native artifacts: classical simulation only, never a provider.

``preflight`` loads and re-verifies every approved QPY in-process before a
request is admitted. ``run`` samples the loaded circuits in a child process
that is killed at the artifact's strictest declared wall bound. ``recover``
cannot reconstruct a lost local outcome, so it returns None (held).
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nano.quantum import RESULT_SCHEMA as OBSERVATION_SCHEMA
from nano.quantum import Sample
from nano.quantum.canonical import canonical_json
from nano.quantum.native import PINNED_QISKIT

from .native_artifact import NativeArtifact, load_native_circuits

LOCAL_RUNTIME_KIND = "local_ideal"
LOCAL_RUNTIME_KINDS = ("local_ideal", "fake_local")   # the only kinds a native result may name


class NativeRuntimeError(RuntimeError):
    """A local execution failure with a stable code; never carries child output."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class LocalNativeRuntime:
    kind = LOCAL_RUNTIME_KIND
    qiskit_version = PINNED_QISKIT

    def preflight(self, artifact: NativeArtifact, *, expected_digest: str) -> None:
        load_native_circuits(artifact, expected_digest=expected_digest)

    def run(self, artifact: NativeArtifact, *, expected_digest: str, record: dict[str, Any]) -> dict[str, Any]:
        deadline = artifact.resources["deadline_s"]
        envelope = {"artifact": artifact.to_payload(), "expected_digest": expected_digest}
        try:
            process = subprocess.run([sys.executable, "-m", "nano_qsearch.native_runtime", "--native-worker"],
                                     input=canonical_json(envelope), capture_output=True, text=True,
                                     encoding="utf-8", timeout=deadline, cwd=str(Path(__file__).resolve().parents[1]))
        except subprocess.TimeoutExpired:
            raise NativeRuntimeError("DEADLINE", "local sampler exceeded the declared wall bound; child killed") from None
        if process.returncode != 0:
            raise NativeRuntimeError("LOCAL_WORKER", "local sampler worker failed")
        try:
            response = json.loads(process.stdout)
        except ValueError:
            raise NativeRuntimeError("LOCAL_WORKER", "local sampler worker returned invalid JSON") from None
        if (not isinstance(response, Mapping) or set(response) != {"qiskit_version", "pub_count", "result"}
                or response["qiskit_version"] != PINNED_QISKIT or response["pub_count"] != len(artifact.pubs)):
            raise NativeRuntimeError("LOCAL_WORKER", "local sampler SDK or PUB inventory differs")
        return response["result"]

    def recover(self, artifact: NativeArtifact, *, record: Mapping[str, Any]) -> dict[str, Any] | None:
        return None


def sample_circuits(artifact: NativeArtifact, circuits: list[Any]) -> dict[str, Any]:
    """Worker-only: sample loaded circuits with their declared seeds, shots and PUB order."""
    from qiskit.primitives import StatevectorSampler
    samples = [o for o in artifact.plan.operations if isinstance(o, Sample)]
    groups: dict[int, list[tuple[Sample, Any]]] = {}
    for operation, circuit in zip(samples, circuits):
        groups.setdefault(operation.seed, []).append((operation, circuit))
    observed: dict[int, dict[str, Any]] = {}
    for seed, members in groups.items():
        results = StatevectorSampler(seed=seed).run([(c, None, o.shots) for o, c in members]).result()
        if len(results) != len(members):
            raise NativeRuntimeError("PUB", "local sampler returned an incomplete PUB inventory")
        for (operation, _), result in zip(members, results):
            register = operation.result["register"]
            observed[operation.result["pub_index"]] = {
                "operation_id": operation.id, "pub_index": operation.result["pub_index"], "register": register,
                "bit_order": "msb_first", "samples": getattr(result.data, register).get_bitstrings()}
    return {"schema": OBSERVATION_SCHEMA, "plan_digest": artifact.plan_digest,
            "pubs": [observed[o.result["pub_index"]] for o in samples],
            "optimization_verdict": "not_applicable", "delivery_verdict": "unverified"}


def _worker() -> None:
    envelope = json.loads(sys.stdin.read())
    artifact, circuits = load_native_circuits(envelope["artifact"], expected_digest=envelope["expected_digest"])
    body = sample_circuits(artifact, circuits)
    sys.stdout.write(canonical_json({"qiskit_version": PINNED_QISKIT, "pub_count": len(circuits), "result": body}))


__all__ = ["LOCAL_RUNTIME_KIND", "LOCAL_RUNTIME_KINDS", "LocalNativeRuntime", "NativeRuntimeError",
           "sample_circuits"]


if __name__ == "__main__":
    if sys.argv[1:] != ["--native-worker"]:
        raise SystemExit(2)
    _worker()
