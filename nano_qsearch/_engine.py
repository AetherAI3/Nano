"""Child-process q-search engine: compile, sample on Qiskit Aer, audit, write JSON.

Run by ``runtime_mps`` in a fresh interpreter (Aer MPS access-violates
intermittently on some hosts) with ``OMP_NUM_THREADS=1``. The child never
raises to its caller: a deterministic failure is written as
``{"error": {"type": "<class name>"}}`` (never the message, which could leak
into a sealed artifact), and a hard crash is a nonzero exit the parent retries.
Samples come from the ATLAS logical program (transpiled for the simulator at
``optimization_level=0``); the native plan is compiled for its metrics and
digest (``nano_qsearch.compile``). The output file is written to ``<out>.tmp``
and moved into place, so the parent never reads a half-written result.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):          # executed as a script: make ``nano_qsearch`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nano_qsearch.compile import CompiledProgram, compile_program  # noqa: E402
from nano_qsearch.job import QSearchJob, parse_job                  # noqa: E402

EXACT_CAP = 20
KINDS = ("statevector", "mps")
_BOND_DIMS = re.compile(r"BD=\[([0-9 ]*)\]")


def _simulator(kind: str, chi_max: int, *, log: bool = False):
    from qiskit_aer import AerSimulator
    options: dict[str, Any] = {"method": kind, "max_parallel_threads": 1}
    if kind == "mps":
        options["method"] = "matrix_product_state"
        options["matrix_product_state_max_bond_dimension"] = chi_max
        if log:
            options["mps_log_data"] = True
    return AerSimulator(**options)


def _for_simulator(circuit, simulator):
    """The ATLAS logical program as the simulator runs it: no optimisation, so it stays that program."""
    from qiskit import transpile
    return transpile(circuit, simulator, optimization_level=0)


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


def _logical_probabilities(probs, mapping: list[int], n: int):
    """Re-index wire-ordered probabilities by logical assignment.

    ``probs[w]`` is the probability of wire bitstring ``w`` (wire k = bit k);
    ``mapping[i]`` is the wire carrying logical variable i. The result is
    indexed by ``sum(x[i] << i)``.
    """
    import numpy as np
    wires = np.arange(1 << n)
    logical_index = np.zeros_like(wires)
    for i, wire in enumerate(mapping):
        logical_index |= ((wires >> wire) & 1) << i
    ideal = np.zeros(1 << n)
    np.add.at(ideal, logical_index, np.asarray(probs, dtype=float))
    return ideal


def _ideal(job: QSearchJob, compiled: CompiledProgram, samples: list[list[Any]], energies) -> tuple[float, float]:
    import numpy as np
    from qiskit.quantum_info import Statevector
    probs = Statevector(compiled.logical.remove_final_measurements(inplace=False)).probabilities()
    n = job.n
    ideal = _logical_probabilities(probs, list(compiled.mapping), n)
    empirical = np.zeros(1 << n)
    for bits, count, _ in samples:
        empirical[int(bits[::-1], 2)] += count          # bits is x0-first; index is sum(x[i] << i)
    empirical /= empirical.sum()
    return float(ideal @ energies), float(0.5 * np.abs(ideal - empirical).sum())


def _mps_snapshot(circuit, simulator) -> tuple[list[int], str | None]:
    """Run ``circuit`` once (shots=1) and return its final bond dimensions and the raw ``MPS_log_data``."""
    probe = circuit.remove_final_measurements(inplace=False)
    probe.save_matrix_product_state(label="mps")
    result = simulator.run(_for_simulator(probe, simulator), shots=1).result()
    if not result.success:
        raise RuntimeError("simulator rejected the bond-dimension probe")
    final = [len(lam) for lam in result.data(0)["mps"][1]]
    log = result.results[0].metadata.get("MPS_log_data")
    return final, log if isinstance(log, str) else None


def _log_body(log: str | None) -> str | None:
    """Strip the ``{...}`` wrapper Aer puts around its accumulated log text."""
    if log is None or not (log.startswith("{") and log.endswith("}")):
        return None
    return log[1:-1]


def _peak_from_log(before: str | None, after: str | None, final: list[int]) -> int | None:
    """Largest bond dimension logged during one run, or None when the log cannot be trusted.

    Aer 0.17 keeps one process-wide MPS log that every logging run appends to
    and reports whole as ``"{" + text + "}"``. Each two-qubit step appends
    ``"I<k>:<gate> on qubits a,b, BD=[d0 d1 ...], "`` (``internal_swap`` steps
    the same, without ``I<k>:``; ``discarded_value=<x>, `` marks a truncation),
    with the bond dimensions after that step. ``before`` is the log returned by
    a run that logs nothing, so this run's text is the suffix after it. The
    last logged bond list must equal the final MPS bonds, else None.
    """
    head, full = _log_body(before), _log_body(after)
    if head is None or full is None or not full.startswith(head):
        return None
    lists = [[int(d) for d in m.split()] for m in _BOND_DIMS.findall(full[len(head):])]
    if not lists or any(not row for row in lists) or lists[-1] != final:
        return None
    return max(max(row) for row in lists)


def _chi_reached(compiled: CompiledProgram, chi_max: int) -> tuple[int, str]:
    """Peak bond dimension of the logical program on MPS at ``chi_max``, and where it came from.

    ``save_matrix_product_state`` gives only the final bonds, which can be
    smaller than a bond reached (and truncated) mid-circuit, so the program is
    run once more with ``mps_log_data=True`` and the per-step bond log is
    parsed: ``(max(peak_from_log, max(final)), "peak_log")``. When the log is
    absent or does not parse, ``(max(final), "final")``. The logged bonds are
    post-truncation, so the result never exceeds ``chi_max``; callers report
    ``truncated = chi_reached >= chi_max``, which is conservative (an exact
    state whose rank is exactly ``chi_max`` is also reported truncated).
    """
    from qiskit import QuantumCircuit
    simulator = _simulator("mps", chi_max, log=True)
    _, before = _mps_snapshot(QuantumCircuit(1), simulator)       # no two-qubit steps: logs nothing
    final, after = _mps_snapshot(compiled.logical, simulator)
    final_max = max(final, default=1)
    peak = _peak_from_log(before, after, final)
    if peak is None:
        return final_max, "final"
    return max(peak, final_max), "peak_log"


def run_engine(body: Any, kind: str) -> dict[str, Any]:
    """Compile, sample and audit; returns ENGINE_RESULT or ``{"error": {"type": ...}}``."""
    try:
        if kind not in KINDS:
            raise ValueError("unknown engine kind")
        import qiskit_aer
        job = parse_job(body)
        started = time.perf_counter()
        compiled = compile_program(job)
        simulator = _simulator(kind, job.chi_max)
        result = simulator.run(_for_simulator(compiled.logical, simulator), shots=job.shots,
                               seed_simulator=job.backend_seed).result()
        if not result.success:
            raise RuntimeError("simulator rejected the circuit")
        samples = _counts_to_samples(job, result.get_counts())
        total = sum(c for _, c, _ in samples)
        chi_reached, chi_source = _chi_reached(compiled, job.chi_max) if kind == "mps" else (None, None)
        exact = mean_ideal = tvd = None
        if job.n <= EXACT_CAP:
            energies = _state_energies(job)
            exact = float(energies.min())
            mean_ideal, tvd = _ideal(job, compiled, samples, energies)
        return {
            "kind": kind, "backend_id": f"qiskit-aer {qiskit_aer.__version__} {kind}",
            "variant": compiled.variant, "nuclei": compiled.nuclei, "compiled_plan_digest": compiled.compiled_plan_digest,
            "compile": compiled.compile_record, "metrics": compiled.metrics,
            "chi_max": job.chi_max if kind == "mps" else None, "chi_reached": chi_reached, "chi_source": chi_source,
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
    out = Path(args.out)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(result, sort_keys=True, allow_nan=False), encoding="utf-8")
    os.replace(tmp, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
