"""Child-process q-search engine: compile, sample on Qiskit Aer, audit, write JSON.

Run by ``runtime_mps`` in a fresh interpreter (Aer MPS access-violates
intermittently on some hosts) with ``OMP_NUM_THREADS=1``. The child never
raises to its caller: a deterministic failure is written as
``{"error": {"type": "<class name>"}}`` (never the message, which could leak
into a sealed artifact), and a hard crash is a nonzero exit the parent retries.
Samples come from the logical program; the native plan is compiled for its
metrics and digest (``qsearch_compile``).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):          # executed as a script: make ``nano`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nano.loop.qsearch_compile import CompiledProgram, compile_program  # noqa: E402
from nano.loop.qsearch_job import QSearchJob, parse_job                  # noqa: E402

EXACT_CAP = 20
KINDS = ("statevector", "mps")


def _simulator(kind: str, chi_max: int):
    from qiskit_aer import AerSimulator
    options: dict[str, Any] = {"method": kind, "max_parallel_threads": 1}
    if kind == "mps":
        options["method"] = "matrix_product_state"
        options["matrix_product_state_max_bond_dimension"] = chi_max
    return AerSimulator(**options)


def _counts_to_samples(job: QSearchJob, counts: dict[str, int]) -> list[list[Any]]:
    rows = []
    for key, count in counts.items():
        bits = key.replace(" ", "")[::-1]            # clbit i = x[i]; keys are MSB first
        rows.append([bits, int(count), job.energy([int(b) for b in bits])])
    rows.sort(key=lambda r: (r[2], r[0]))
    return rows


def _state_energies(job: QSearchJob):
    """Energies of every x in {0,1}^n, indexed by sum(x[i] << i)."""
    import numpy as np
    n = job.n
    states = ((np.arange(1 << n)[:, None] >> np.arange(n)) & 1).astype(float)   # column i = x[i]
    return np.einsum("si,ij,sj->s", states, np.asarray(job.q), states) + job.offset


def _ideal(job: QSearchJob, compiled: CompiledProgram, samples: list[list[Any]], energies) -> tuple[float, float]:
    import numpy as np
    from qiskit.quantum_info import Statevector
    probs = Statevector(compiled.logical.remove_final_measurements(inplace=False)).probabilities()
    n = job.n
    wires = np.arange(1 << n)
    logical_index = np.zeros_like(wires)
    for i, wire in enumerate(compiled.mapping):
        logical_index |= ((wires >> wire) & 1) << i
    ideal = np.zeros(1 << n)
    np.add.at(ideal, logical_index, probs)
    empirical = np.zeros(1 << n)
    for bits, count, _ in samples:
        empirical[int(bits[::-1], 2)] += count          # bits is x0-first; index is sum(x[i] << i)
    empirical /= empirical.sum()
    return float(ideal @ energies), float(0.5 * np.abs(ideal - empirical).sum())


def _chi_reached(compiled: CompiledProgram, simulator) -> int:
    from qiskit import transpile
    probe = compiled.logical.remove_final_measurements(inplace=False)
    probe.save_matrix_product_state(label="mps")
    data = simulator.run(transpile(probe, simulator)).result().data(0)["mps"]
    return max((len(lam) for lam in data[1]), default=1)


def run_engine(body: Any, kind: str) -> dict[str, Any]:
    """Compile, sample and audit; returns ENGINE_RESULT or ``{"error": {"type": ...}}``."""
    try:
        if kind not in KINDS:
            raise ValueError("unknown engine kind")
        import qiskit_aer
        from qiskit import transpile
        job = parse_job(body)
        started = time.perf_counter()
        compiled = compile_program(job)
        simulator = _simulator(kind, job.chi_max)
        result = simulator.run(transpile(compiled.logical, simulator), shots=job.shots, seed_simulator=job.backend_seed).result()
        if not result.success:
            raise RuntimeError("simulator rejected the circuit")
        samples = _counts_to_samples(job, result.get_counts())
        total = sum(c for _, c, _ in samples)
        chi_reached = _chi_reached(compiled, simulator) if kind == "mps" else None
        exact = mean_ideal = tvd = None
        if job.n <= EXACT_CAP:
            energies = _state_energies(job)
            exact = float(energies.min())
            mean_ideal, tvd = _ideal(job, compiled, samples, energies)
        return {
            "kind": kind, "backend_id": f"qiskit-aer {qiskit_aer.__version__} {kind}",
            "variant": compiled.variant, "nuclei": compiled.nuclei, "compiled_plan_digest": compiled.compiled_plan_digest,
            "compile": compiled.compile_record, "metrics": compiled.metrics,
            "chi_max": job.chi_max if kind == "mps" else None, "chi_reached": chi_reached,
            "truncated": (chi_reached >= job.chi_max) if kind == "mps" else None,
            "shots": job.shots, "seed": job.backend_seed, "wall_s": round(time.perf_counter() - started, 6), "attempts": 1,
            "samples": samples, "best_sample_energy": samples[0][2],
            "mean_energy_sample": sum(c * e for _, c, e in samples) / total,
            "mean_energy_ideal": mean_ideal, "tvd_ideal": tvd, "exact_min_energy": exact,
        }
    except Exception as exc:  # noqa: BLE001 - the child reports, the parent decides
        return {"error": {"type": type(exc).__name__}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--kind", required=True, choices=KINDS)
    args = parser.parse_args(argv)
    try:
        body = json.loads(Path(args.job).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result = {"error": {"type": type(exc).__name__}}
    else:
        result = run_engine(body, args.kind)
    Path(args.out).write_text(json.dumps(result, sort_keys=True, allow_nan=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
