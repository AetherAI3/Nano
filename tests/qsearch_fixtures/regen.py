"""Regenerate the q-search fixtures from a Predator-M checkout.

Usage (PowerShell, system Python):
    $env:NANO_QSEARCH_PREDATOR_PATH = "<predator checkout>"
    python tests/qsearch_fixtures/regen.py

Writes job_4var.json, job_pair02.json (real ``QSearchJobV1.to_payload()``
bodies) and canonical_golden.json (a payload and the digest Predator's
``canonical_digest`` produced for it). Nano never imports Predator at runtime;
these files are the frozen evidence that the two canonical encoders agree.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, os.environ["NANO_QSEARCH_PREDATOR_PATH"])

from predator_gen3.canonical import canonical_digest  # noqa: E402
from predator_gen3.discovery.compiler import CompiledSearchProblemV1  # noqa: E402
from predator_gen3.discovery.qsearch_bridge import QSearchPolicyV1, build_qsearch_job  # noqa: E402

PAIR02_Q = [[-0.5477386934673368, -0.5, -0.5, -0.5, -0.5, -0.5],
            [-0.5, -0.40452261306532666, -0.5, -0.5, -0.5, -0.5],
            [-0.5, -0.5, -0.3592964824120603, -0.5, -0.5, -0.5],
            [-0.5, -0.5, -0.5, -0.3165829145728643, -0.5, -0.5],
            [-0.5, -0.5, -0.5, -0.5, -1.0, -0.5],
            [-0.5, -0.5, -0.5, -0.5, -0.5, -0.40452261306532666]]


def _problem(ids, linear, quadratic):
    lineage = tuple({"candidate_id": c, "prerequisites": [], "exclusions": []} for c in ids)
    return CompiledSearchProblemV1(
        proposal_digest="sha256:" + "ab" * 32, candidate_ids=tuple(ids),
        linear=tuple(linear), quadratic=tuple(quadratic), penalty=1.0, lineage=lineage)


def _job(problem, policy):
    _, energy = problem.classical_minimum()
    return build_qsearch_job(problem, classical_min_energy=energy, policy=policy).to_payload()


def main() -> None:
    # 4 variables: linear utilities and three couplings, one triangle (0,1,2).
    four = _problem(["c0", "c1", "c2", "c3"], [-1.0, -0.8, -0.6, -0.4],
                    [(0, 1, 0.9), (0, 2, 0.7), (1, 2, 0.5), (2, 3, -0.3)])
    (HERE / "job_4var.json").write_text(json.dumps(_job(four, QSearchPolicyV1(shots=1024)), indent=1), encoding="utf-8")
    # pair02 shape: Q[i][i] = linear[i]; Q[i][j] = quad/2 so quad = 2*Q[i][j] = -1.0 everywhere.
    n = 6
    quadratic = [(a, b, 2.0 * PAIR02_Q[a][b]) for a in range(n) for b in range(a + 1, n)]
    pair02 = _problem([f"v{i}" for i in range(n)], [PAIR02_Q[i][i] for i in range(n)], quadratic)
    policy = QSearchPolicyV1(p=2, gammas=(0.2, 0.4), betas=(0.4, 0.2), shots=4096, target="heron_heavy_hex")
    (HERE / "job_pair02.json").write_text(json.dumps(_job(pair02, policy), indent=1), encoding="utf-8")
    golden_payload = {"z": [1, 2.5, -0.0, True, None, "ünïcode ☃"], "a": {"nested": {"k": [[]]}, "b": ""},
                      "m": 1e-9, "big": 12345678901234567890, "neg": -3.0}
    golden = {"payload": golden_payload, "digest": canonical_digest(golden_payload),
              "job_4var_digest": canonical_digest({k: v for k, v in json.loads((HERE / "job_4var.json").read_text(encoding="utf-8")).items() if k != "job_digest"})}
    (HERE / "canonical_golden.json").write_text(json.dumps(golden, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
