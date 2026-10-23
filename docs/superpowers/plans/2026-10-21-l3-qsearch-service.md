# L3 Nano q-search service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Nano-side q-search service that Predator Drive's `NanoRuntimeQSearchBackend` calls: `POST /qsearch/v1/submit` takes a `predator.qsearch.job.v1` body, authenticates and replay-protects the request, compiles the QUBO through ATLAS `build_nucleus_program`, samples it on Qiskit Aer (statevector or MPS) in a crash-isolated child process, and answers with a `predator.qsearch.receipt.v1` sealed with HMAC-SHA256 that Predator's `QSearchReceiptV1.from_payload` verifies. Jobs are durable and idempotent by `job_digest`; a crash between accept and complete is recovered on restart by re-running once with the same seed. An `IbmRuntime` exists and refuses by default.

**Architecture:** The base `aether-nano` package stays pure Python: canonical JSON, job validation, receipt shaping, request auth, the SQLite store and the HTTP service import only the stdlib. Quantum work lives in one child-process entry (`nano/loop/_qsearch_engine.py`, with the compile helpers in `nano/loop/qsearch_compile.py`) that imports numpy, qiskit, qiskit-aer and `aether_atlas`; the parent (`runtime_mps.py`) only spawns it with `OMP_NUM_THREADS=1`, up to 4 retries, and turns any failure into a `NOT_RUN` receipt. The service seals every receipt; the runtime never sees a key. ATLAS gains `qcompiler/nuclei.py::find_nuclei` on its own branch.

**Tech Stack:** Python 3.10+ stdlib (`http.server`, `sqlite3`, `hmac`, `subprocess`), optional extra `aether-nano[quantum]` = `qiskit==2.3.1`, `qiskit-aer==0.17.2`, `numpy>=1.26`; ATLAS `aether_atlas` loaded by path (`NANO_QSEARCH_ATLAS_PATH`); pytest.

---

## Pinned context (read before any task)

- Nano worktree: `<scratchpad>\wt-l3-nano`, branch `feat/v2-l3-qsearch-service` from Nano `origin/main` `e19bf1c`.
- ATLAS worktree: `...\scratchpad\wt-atlas`, branch `feat/qcompiler-find-nuclei` from ATLAS `origin/main`. Only `aether-atlas/aether_atlas/qcompiler/nuclei.py` and `aether-atlas/tests/test_qcompiler_nuclei.py` are committed there.
- Predator-M read-only worktree (detached at `0b949cab`): `...\scratchpad\wt-predator-ro`. Never edited. Used only to generate fixtures and for the integration test (`NANO_QSEARCH_PREDATOR_PATH`).
- Quantum code runs only with system Python `<system Python 3.12>` **from PowerShell** (Git Bash segfaults on qiskit import). Aer MPS access-violates intermittently on this host: every Aer job runs in a child process with up to 4 retries and `OMP_NUM_THREADS=1`.
- Known pre-existing failure on this host: `tests/test_provenance.py::test_decide_returns_inner_decision_unchanged` fails because the installed `aether_protocol_c` rejects floats. Not ours; do not touch.
- Commit messages: conventional type prefix, end with a blank line then `Co-Authored-By: Claude Mythos 5.1 <noreply@anthropic.com>`. Never `--no-verify`.
- Wire contract is frozen (Predator-M `predator_gen3/discovery/qsearch_bridge.py::QSearchJobV1.to_payload()` in, `predator/drive/qsearch_backend.py::QSearchReceiptV1.from_payload` out). If it must change, write the request in the hand-off section at the bottom; do not change it.

### The job body (what arrives), verbatim from `QSearchJobV1._unsigned()` plus `job_digest`

```json
{"schema": "predator.qsearch.job.v1",
 "compiled_search_digest": "sha256:<64 hex>",
 "variables": ["c1", "c2"],
 "qubo": {"q": [[-1.0, 0.5], [0.5, -2.0]], "offset": 0.0, "convention": "symmetric_half"},
 "compile": {"variant": "auto", "p": 1, "gammas": [0.4], "betas": [0.4], "opt_level": 3,
             "seed": 11, "target": "heron_heavy_hex", "training": "none"},
 "backend": {"kind": "auto", "chi_max": 64, "shots": 4096, "seed": 7},
 "limits": {"max_variables": 40, "max_wall_s": 120, "max_qpu_s": 0},
 "audit": {"classical_min_energy": -2.0},
 "job_digest": "sha256:<64 hex>"}
```

`job_digest = canonical_digest(body without job_digest)`. Allowed enums: `compile.variant` ∈ {auto, ordinary, joint5}; `backend.kind` ∈ {auto, mps, statevector, ibm}; `compile.target` ∈ {heron_heavy_hex, fake_fez, ibm_fez}; `compile.training == "none"`; `qubo.convention == "symmetric_half"`. Energy of assignment `x ∈ {0,1}^n` is `E(x) = xᵀ Q x + offset` with `x[i]` the bit for `variables[i]`.

### What `from_payload` checks (the receipt contract)

`schema == "predator.qsearch.receipt.v1"`; `seal` is a string `hmac-sha256:` + 64 lowercase hex, equal to `HMAC-SHA256(key, canonical_json(payload without seal))`; a `local:` seal is refused; `job_digest == expected`; `status ∈ {OK, NOT_RUN, REFUSED, TRUNCATED}`; `backend.kind` is a string; for OK/TRUNCATED: `audit.agree` is a bool, `audit.classical_min_energy` and `audit.best_sample_energy` finite numbers (not bool), `|classical_min_energy − job.classical_min_energy| ≤ 1e-9`, `best ≥ floor − 1e-9`, and `agree == true` only if `|best − floor| ≤ 1e-9`. Canonical JSON = `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` after rejecting non-string keys and non-finite floats.

### Canonical JSON rules to reproduce (Predator `predator_gen3/canonical.py`)

- Mappings: keys must be `str`, else `CanonicalizationError`; values recursed.
- `list`/`tuple` → list. `None`, `str`, `bool`, `int` pass through. `float` must be finite. Anything else → `CanonicalizationError`. (Predator also unwraps `Enum` and dataclasses; Nano does not need those.)
- `canonical_json` = `json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`; `canonical_digest` = `"sha256:" + sha256(utf8)`.

### pair02 fixture (ATLAS frozen six-variable objective, "vuln" pole)

```
Q = [[-0.5477386934673368, -0.5, -0.5, -0.5, -0.5, -0.5],
     [-0.5, -0.40452261306532666, -0.5, -0.5, -0.5, -0.5],
     [-0.5, -0.5, -0.3592964824120603, -0.5, -0.5, -0.5],
     [-0.5, -0.5, -0.5, -0.3165829145728643, -0.5, -0.5],
     [-0.5, -0.5, -0.5, -0.5, -1.0, -0.5],
     [-0.5, -0.5, -0.5, -0.5, -0.5, -0.40452261306532666]]
gammas = [0.2, 0.4]; betas = [0.4, 0.2]; ATLAS frozen nuclei = [[0, 1, 2], [3, 4, 5]] (vertex-disjoint); greedy find_nuclei packs four: [[0,1,2],[0,3,4],[1,3,5],[2,4,5]]
ordinary on fake_fez: logical rzz 30, native cz 144 (spec expects joint5 ≈ 103)
```

---

## File structure

Nano worktree (all new unless marked):

| File | Responsibility | Imports |
|---|---|---|
| `nano/loop/qsearch_canonical.py` | canonical JSON + digest, identical to Predator | stdlib |
| `nano/loop/qsearch_job.py` | parse/validate the job body → `QSearchJob` | stdlib, canonical |
| `nano/loop/qsearch_receipt.py` | receipt shaping (`ok`, `not_run`, `refused`), seal/verify | stdlib, canonical |
| `nano/loop/qsearch_auth.py` | request header sign/verify, nonce + timestamp window | stdlib |
| `nano/loop/qsearch_store.py` | SQLite job + nonce store, recovery query | stdlib |
| `nano/loop/qsearch_compile.py` | ATLAS program build, heavy-hex transpile, metrics, plan digest | numpy, qiskit, aether_atlas (child only) |
| `nano/loop/_qsearch_engine.py` | child-process entry: compile + sample + audit → JSON | numpy, qiskit, qiskit_aer (child only) |
| `nano/loop/runtime_mps.py` | `StatevectorRuntime`, `MpsRuntime`, child spawn with retries, kind selection | stdlib (subprocess) |
| `nano/loop/runtime_ibm.py` | `IbmRuntime`: refuse-by-default gates, ATLAS campaign delegate | stdlib, lazy aether_atlas |
| `nano/loop/qsearch_service.py` | `QSearchService` (auth → parse → store → run → seal), HTTP server, `main()` | stdlib |
| `pyproject.toml` (modify) | extra `quantum`, script `nano-qsearch-serve`, pytest markers | |
| `tests/conftest.py` | path hooks for `NANO_QSEARCH_ATLAS_PATH` / `NANO_QSEARCH_PREDATOR_PATH`, shared fixtures | |
| `tests/qsearch_fixtures/regen.py` | regenerates `job_4var.json`, `job_pair02.json`, `canonical_golden.json` from Predator by path | |
| `tests/test_qsearch_*.py` | one test module per source module | |
| `docs/qsearch-service.md` | operator doc: env vars, auth header, run, recovery | |

ATLAS worktree: `aether-atlas/aether_atlas/qcompiler/nuclei.py`, `aether-atlas/tests/test_qcompiler_nuclei.py`.

Shared vocabulary used by every task:

```python
# nano/loop/qsearch_job.py
@dataclass(frozen=True)
class QSearchJob:
    compiled_search_digest: str
    variables: tuple[str, ...]
    q: tuple[tuple[float, ...], ...]
    offset: float
    variant: str; p: int; gammas: tuple[float, ...]; betas: tuple[float, ...]
    opt_level: int; compile_seed: int; target: str
    backend_kind: str; chi_max: int; shots: int; backend_seed: int
    max_variables: int; max_wall_s: int; max_qpu_s: int
    classical_min_energy: float
    job_digest: str
    payload: Mapping[str, Any]      # the validated body, frozen (MappingProxyType)
    @property
    def n(self) -> int: ...

# runtime protocol (runtime_mps.py, runtime_ibm.py)
class Runtime(Protocol):
    kind: str
    def run(self, job: QSearchJob) -> dict[str, Any]: ...   # UNSIGNED receipt body, never raises
```

---

## Task 0: ATLAS `find_nuclei` (separate repo, do first; engine tests need it)

**Files:**
- Create: `wt-atlas/aether-atlas/aether_atlas/qcompiler/nuclei.py`
- Test: `wt-atlas/aether-atlas/tests/test_qcompiler_nuclei.py`

Work from the ATLAS worktree. Run tests with system Python from PowerShell: `cd <wt-atlas>\aether-atlas; & $py -m pytest tests/test_qcompiler_nuclei.py -q`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qcompiler_nuclei.py
import numpy as np
import pytest

from aether_atlas.qcompiler.nuclei import find_nuclei

PAIR02 = [[-0.5477386934673368, -0.5, -0.5, -0.5, -0.5, -0.5],
          [-0.5, -0.40452261306532666, -0.5, -0.5, -0.5, -0.5],
          [-0.5, -0.5, -0.3592964824120603, -0.5, -0.5, -0.5],
          [-0.5, -0.5, -0.5, -0.3165829145728643, -0.5, -0.5],
          [-0.5, -0.5, -0.5, -0.5, -1.0, -0.5],
          [-0.5, -0.5, -0.5, -0.5, -0.5, -0.40452261306532666]]


def test_dense_pair02_greedy_packs_four_edge_disjoint_triangles():
    # K6: lexicographic greedy claims 12 of 15 edges; (0,5), (1,4), (2,3) remain, no triangle
    assert find_nuclei(PAIR02) == [(0, 1, 2), (0, 3, 4), (1, 3, 5), (2, 4, 5)]


def test_no_couplings_gives_no_nuclei():
    assert find_nuclei(np.diag([1.0, -2.0, 3.0])) == []


def test_single_triangle_in_a_path_plus_chord():
    # edges 0-1, 1-2, 0-2 (triangle) and 2-3 (dangling)
    q = np.zeros((4, 4))
    for a, b in ((0, 1), (1, 2), (0, 2), (2, 3)):
        q[a, b] = q[b, a] = 0.3
    assert find_nuclei(q) == [(0, 1, 2)]


def test_edge_disjoint_but_vertex_sharing_allowed():
    # two triangles sharing vertex 2 only: {0,1,2} and {2,3,4}
    q = np.zeros((5, 5))
    for a, b in ((0, 1), (0, 2), (1, 2), (2, 3), (2, 4), (3, 4)):
        q[a, b] = q[b, a] = -1.0
    assert find_nuclei(q) == [(0, 1, 2), (2, 3, 4)]


def test_second_triangle_sharing_an_edge_is_skipped():
    # {0,1,2} and {0,1,3} share edge 0-1; greedy keeps the lexicographically first
    q = np.zeros((4, 4))
    for a, b in ((0, 1), (0, 2), (1, 2), (0, 3), (1, 3)):
        q[a, b] = q[b, a] = 2.0
    assert find_nuclei(q) == [(0, 1, 2)]


def test_tolerance_hides_tiny_couplings():
    q = np.zeros((3, 3))
    for a, b in ((0, 1), (1, 2)):
        q[a, b] = q[b, a] = 1.0
    q[0, 2] = q[2, 0] = 1e-13   # below tol, so no triangle
    assert find_nuclei(q) == []
    assert find_nuclei(q, tol=1e-14) == [(0, 1, 2)]


def test_asymmetric_input_is_symmetrised_like_qubo_to_ising():
    q = np.zeros((3, 3))
    q[0, 1] = 1.0; q[1, 2] = 1.0; q[2, 0] = 1.0   # upper/lower mixed, each edge once
    assert find_nuclei(q) == [(0, 1, 2)]


def test_result_is_accepted_by_build_nucleus_program():
    pytest.importorskip("qiskit")
    from aether_atlas.qcompiler.nucleus_program import build_nucleus_program
    nuclei = find_nuclei(PAIR02)
    circuit, mapping = build_nucleus_program(PAIR02, variant="joint5", nuclei=nuclei,
                                             gammas=[0.2, 0.4], betas=[0.4, 0.2])
    assert circuit.num_qubits == 6 and sorted(mapping) == list(range(6))


@pytest.mark.parametrize("bad", [np.zeros((2, 3)), np.zeros((0, 0)), [[0.0, float("nan")], [0.0, 0.0]]])
def test_rejects_non_square_or_non_finite(bad):
    with pytest.raises(ValueError):
        find_nuclei(bad)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `& $py -m pytest tests/test_qcompiler_nuclei.py -q` → `ModuleNotFoundError: aether_atlas.qcompiler.nuclei`.

- [ ] **Step 3: Implement**

```python
# aether_atlas/qcompiler/nuclei.py
"""Deterministic nucleus discovery for the joint5 compiler variant.

A nucleus is a triangle (K3) of existing nonzero objective couplings. This
finder is greedy and deterministic: triples are visited in lexicographic order
and a triple is taken when all three edges exist and none is already claimed.
Nuclei may share vertices, never edges, which is exactly what
``nucleus_program.build_nucleus_program`` validates. No edge is invented.
"""
from __future__ import annotations

from itertools import combinations

import numpy as np

from .qaoa_select import qubo_to_ising

DEFAULT_TOLERANCE = 1e-12


def find_nuclei(Q, tol: float = DEFAULT_TOLERANCE) -> list[tuple[int, int, int]]:
    """Return edge-disjoint K3 nuclei of ``Q`` as sorted index triples.

    Couplings are read from the Ising ``J`` of ``qubo_to_ising(Q)`` (which
    symmetrises ``Q``), with ``|J[a, b]| > tol`` meaning an edge exists; the
    same threshold ``nucleus_program`` applies, so every returned nucleus is
    accepted there.
    """
    q = np.asarray(Q, dtype=float)
    if q.ndim != 2 or q.shape[0] != q.shape[1] or not len(q) or not np.isfinite(q).all():
        raise ValueError("Q must be a nonempty finite square matrix")
    if isinstance(tol, bool) or not np.isfinite(float(tol)) or float(tol) < 0:
        raise ValueError("tol must be a finite non-negative number")
    _, j, _ = qubo_to_ising(q)
    n = len(q)
    edge = {(a, b) for a in range(n) for b in range(a + 1, n) if abs(j[a, b]) > tol}
    claimed: set[tuple[int, int]] = set()
    nuclei: list[tuple[int, int, int]] = []
    for a, b, c in combinations(range(n), 3):
        edges = ((a, b), (a, c), (b, c))
        if all(e in edge for e in edges) and not claimed.intersection(edges):
            claimed.update(edges)
            nuclei.append((a, b, c))
    return nuclei


__all__ = ["DEFAULT_TOLERANCE", "find_nuclei"]
```

- [ ] **Step 4: Run tests** → all pass (the `build_nucleus_program` test runs under system Python where qiskit exists).

- [ ] **Step 5: Commit (ATLAS worktree only these two files)**

```
feat(qcompiler): find_nuclei greedy edge-disjoint triangle finder

Co-Authored-By: Claude Mythos 5.1 <noreply@anthropic.com>
```

---

## Task 1: Canonical JSON + golden vector from Predator

**Files:**
- Create: `nano/loop/qsearch_canonical.py`
- Create: `tests/qsearch_fixtures/regen.py`, `tests/qsearch_fixtures/canonical_golden.json`, `tests/qsearch_fixtures/job_4var.json`, `tests/qsearch_fixtures/job_pair02.json`
- Create: `tests/conftest.py`
- Test: `tests/test_qsearch_canonical.py`

- [ ] **Step 1: conftest with path hooks**

```python
# tests/conftest.py
"""Shared pytest wiring for the q-search tests.

Two optional environment variables point at sibling checkouts that are not
installed packages: ``NANO_QSEARCH_ATLAS_PATH`` (the ``aether-atlas`` directory
that contains the ``aether_atlas`` package) and ``NANO_QSEARCH_PREDATOR_PATH``
(a Predator-M checkout, used only by the integration test). Both are added to
``sys.path`` when set; nothing is imported here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "qsearch_fixtures"

for _var in ("NANO_QSEARCH_ATLAS_PATH", "NANO_QSEARCH_PREDATOR_PATH"):
    _path = os.environ.get(_var)
    if _path and _path not in sys.path:
        sys.path.insert(0, _path)


def pytest_configure(config):
    config.addinivalue_line("markers", "engine: needs qiskit-aer and aether_atlas (system Python, PowerShell)")
    config.addinivalue_line("markers", "integration: needs NANO_QSEARCH_PREDATOR_PATH")


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
```

- [ ] **Step 2: Fixture regeneration script (runs against Predator by path; commit its output)**

```python
# tests/qsearch_fixtures/regen.py
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
    golden_payload = {"z": [1, 2.5, -0.0, True, None, "ünïcode \u2603"], "a": {"nested": {"k": [[]]}, "b": ""},
                      "m": 1e-9, "big": 12345678901234567890, "neg": -3.0}
    golden = {"payload": golden_payload, "digest": canonical_digest(golden_payload),
              "job_4var_digest": canonical_digest({k: v for k, v in json.loads((HERE / "job_4var.json").read_text()).items() if k != "job_digest"})}
    (HERE / "canonical_golden.json").write_text(json.dumps(golden, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
```

Run it once (PowerShell, system Python, `NANO_QSEARCH_PREDATOR_PATH` = the read-only Predator worktree) and commit the three JSON files. Check `job_4var.json` has `"schema": "predator.qsearch.job.v1"` and a `job_digest`.

- [ ] **Step 3: Write the failing tests**

```python
# tests/test_qsearch_canonical.py
import json
import math

import pytest

from nano.loop.qsearch_canonical import (
    CanonicalizationError, canonical_digest, canonical_json, validate_digest,
)


def test_golden_digest_matches_predator(fixtures_dir):
    golden = json.loads((fixtures_dir / "canonical_golden.json").read_text(encoding="utf-8"))
    assert canonical_digest(golden["payload"]) == golden["digest"]


def test_job_digest_of_fixture_matches_predator(fixtures_dir):
    golden = json.loads((fixtures_dir / "canonical_golden.json").read_text(encoding="utf-8"))
    job = json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))
    unsigned = {k: v for k, v in job.items() if k != "job_digest"}
    assert canonical_digest(unsigned) == job["job_digest"] == golden["job_4var_digest"]


def test_sorted_compact_utf8():
    assert canonical_json({"b": 1, "a": [1.5, "é"]}) == '{"a":[1.5,"é"],"b":1}'


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -math.inf])
def test_non_finite_rejected(bad):
    with pytest.raises(CanonicalizationError):
        canonical_json({"x": bad})


def test_non_string_key_rejected():
    with pytest.raises(CanonicalizationError):
        canonical_json({1: "a"})


def test_unsupported_type_rejected():
    with pytest.raises(CanonicalizationError):
        canonical_json({"x": object()})


def test_tuples_become_lists_and_bools_stay_bools():
    assert canonical_json({"t": (1, 2), "b": True}) == '{"b":true,"t":[1,2]}'


def test_validate_digest():
    assert validate_digest("sha256:" + "0" * 64) == "sha256:" + "0" * 64
    for bad in ("sha1:" + "0" * 64, "sha256:" + "0" * 63, "sha256:" + "G" * 64, 5):
        with pytest.raises(ValueError):
            validate_digest(bad)
```

- [ ] **Step 4: Run** → `ModuleNotFoundError`.

- [ ] **Step 5: Implement**

```python
# nano/loop/qsearch_canonical.py
"""Canonical JSON and digests for the q-search wire contract.

This reproduces Predator-M ``predator_gen3/canonical.py`` without importing it:
sorted keys, compact separators, UTF-8 (``ensure_ascii=False``), no NaN or
infinity, no non-string keys. A digest is ``sha256:<64 lowercase hex>`` over
the UTF-8 bytes of the canonical text. Both sides must agree byte for byte or
no receipt seal verifies; ``tests/qsearch_fixtures/canonical_golden.json`` is
the frozen proof that they do.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

_HEX = frozenset("0123456789abcdef")


class CanonicalizationError(ValueError):
    """The value cannot be represented by the canonical JSON contract."""


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("canonical mappings require string keys")
            out[key] = _json_value(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("NaN and infinity are not canonical JSON")
        return value
    raise CanonicalizationError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Sorted, compact, UTF-8-safe JSON text for ``value``."""
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def canonical_digest(value: Any) -> str:
    """``sha256:<hex>`` over the canonical UTF-8 bytes of ``value``."""
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_digest(value: Any, *, field: str = "digest") -> str:
    """Return ``value`` if it is a well-formed ``sha256:<64 lowercase hex>``."""
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError(f"{field} must use the sha256:<hex> form")
    hexadecimal = value[len("sha256:"):]
    if len(hexadecimal) != 64 or any(c not in _HEX for c in hexadecimal):
        raise ValueError(f"{field} must contain 64 lowercase hexadecimal characters")
    return value


__all__ = ["CanonicalizationError", "canonical_digest", "canonical_json", "validate_digest"]
```

- [ ] **Step 6: Run** → pass. **Step 7: Commit** `feat(qsearch): canonical JSON with Predator golden vector`.

---

## Task 2: Job parsing and validation

**Files:** Create `nano/loop/qsearch_job.py`; Test `tests/test_qsearch_job.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_qsearch_job.py
import copy
import json

import pytest

from nano.loop.qsearch_canonical import canonical_digest
from nano.loop.qsearch_job import JobValidationError, QSearchJob, parse_job


@pytest.fixture
def body(fixtures_dir):
    return json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))


def _resigned(body):
    """Return a copy whose job_digest matches its (possibly edited) content."""
    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    return {**unsigned, "job_digest": canonical_digest(unsigned)}


def test_parses_fixture(body):
    job = parse_job(body)
    assert isinstance(job, QSearchJob)
    assert job.n == 4 and job.variables == ("c0", "c1", "c2", "c3")
    assert job.q[0][1] == job.q[1][0] and job.job_digest == body["job_digest"]
    assert job.variant == "auto" and job.backend_kind == "auto" and job.target == "heron_heavy_hex"
    assert job.payload["schema"] == "predator.qsearch.job.v1"
    with pytest.raises(TypeError):
        job.payload["schema"] = "x"          # frozen view


def test_energy_matches_quadratic_form(body):
    job = parse_job(body)
    x = (1, 0, 1, 1)
    expected = sum(job.q[i][j] * x[i] * x[j] for i in range(4) for j in range(4)) + job.offset
    assert abs(job.energy(x) - expected) <= 1e-12
    with pytest.raises(ValueError):
        job.energy((1, 0, 1))


def _bad(body, mutate):
    b = copy.deepcopy(body)
    mutate(b)
    return _resigned(b)


@pytest.mark.parametrize("mutate, code", [
    (lambda b: b.__setitem__("schema", "nope"), "SCHEMA"),
    (lambda b: b.__setitem__("variables", ["a", "a", "b", "c"]), "VARIABLES"),
    (lambda b: b.__setitem__("variables", []), "VARIABLES"),
    (lambda b: b["qubo"]["q"].pop(), "QUBO"),
    (lambda b: b["qubo"]["q"][0].__setitem__(1, 99.0), "QUBO"),            # asymmetric
    (lambda b: b["qubo"]["q"][0].__setitem__(0, True), "QUBO"),            # bool is not a number
    (lambda b: b["qubo"].__setitem__("convention", "upper"), "QUBO"),
    (lambda b: b["qubo"].__setitem__("offset", "0"), "QUBO"),
    (lambda b: b["compile"].__setitem__("variant", "qaoa"), "COMPILE"),
    (lambda b: b["compile"].__setitem__("gammas", [0.1, 0.2]), "COMPILE"),  # p=1 but two angles
    (lambda b: b["compile"].__setitem__("opt_level", 4), "COMPILE"),
    (lambda b: b["compile"].__setitem__("training", "cobyla"), "COMPILE"),
    (lambda b: b["compile"].__setitem__("target", "rigetti"), "COMPILE"),
    (lambda b: b["backend"].__setitem__("kind", "gpu"), "BACKEND"),
    (lambda b: b["backend"].__setitem__("shots", 0), "BACKEND"),
    (lambda b: b["backend"].__setitem__("chi_max", 1.5), "BACKEND"),
    (lambda b: b["limits"].__setitem__("max_variables", 3), "LIMITS"),     # 4 variables > 3
    (lambda b: b["limits"].__setitem__("max_wall_s", -1), "LIMITS"),
    (lambda b: b["audit"].__setitem__("classical_min_energy", "low"), "AUDIT"),
    (lambda b: b.__setitem__("compiled_search_digest", "sha256:short"), "DIGEST"),
    (lambda b: b.__setitem__("extra", 1), "SCHEMA"),                        # unknown top-level key
])
def test_rejections(body, mutate, code):
    with pytest.raises(JobValidationError) as info:
        parse_job(_bad(body, mutate))
    assert info.value.code == code


def test_digest_mismatch_rejected(body):
    body = copy.deepcopy(body)
    body["job_digest"] = "sha256:" + "0" * 64
    with pytest.raises(JobValidationError) as info:
        parse_job(body)
    assert info.value.code == "DIGEST"


def test_non_finite_entries_rejected_before_digest(body):
    body = copy.deepcopy(body)
    body["qubo"]["q"][0][0] = float("nan")
    with pytest.raises(JobValidationError) as info:
        parse_job(body)
    assert info.value.code == "QUBO"


def test_non_mapping_rejected():
    with pytest.raises(JobValidationError):
        parse_job(["not", "a", "job"])
```

- [ ] **Step 2: Run** → fails. **Step 3: Implement**

```python
# nano/loop/qsearch_job.py
"""Parse and validate a ``predator.qsearch.job.v1`` body into a ``QSearchJob``.

The body is Predator-M ``QSearchJobV1.to_payload()``. Validation is strict and
whole: every field is type-checked, the Q matrix must be square, symmetric and
finite, the angle vectors must have exactly ``p`` entries, and ``job_digest``
is recomputed with ``canonical_digest`` and must match. A job that fails here
gets no receipt at all (the service answers 400); a job that is valid but
over the service's own limits gets a REFUSED receipt bound to its digest.
"""
from __future__ import annotations

import math
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .qsearch_canonical import canonical_digest, validate_digest

JOB_SCHEMA = "predator.qsearch.job.v1"
VARIANTS = ("auto", "ordinary", "joint5")
BACKEND_KINDS = ("auto", "mps", "statevector", "ibm")
TARGETS = ("heron_heavy_hex", "fake_fez", "ibm_fez")
CONVENTION = "symmetric_half"
_TOP_KEYS = frozenset({"schema", "compiled_search_digest", "variables", "qubo", "compile",
                       "backend", "limits", "audit", "job_digest"})
_SYMMETRY_TOLERANCE = 0.0   # Predator writes both halves from one value; exact equality is expected


class JobValidationError(ValueError):
    """A job body that is not a valid ``predator.qsearch.job.v1``; ``code`` names the field group."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _mapping(value: Any, code: str, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise JobValidationError(code, f"{name} must be an object")
    return value


def _number(value: Any, code: str, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise JobValidationError(code, f"{name} must be a finite number")
    return float(value)


def _int(value: Any, code: str, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise JobValidationError(code, f"{name} must be an integer >= {minimum}")
    return value


def _choice(value: Any, choices: Sequence[str], code: str, name: str) -> str:
    if value not in choices:
        raise JobValidationError(code, f"{name} must be one of {list(choices)}")
    return value


def _angles(value: Any, p: int, name: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != p:
        raise JobValidationError("COMPILE", f"{name} must list exactly p={p} finite numbers")
    return tuple(_number(v, "COMPILE", name) for v in value)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return types.MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class QSearchJob:
    compiled_search_digest: str
    variables: tuple[str, ...]
    q: tuple[tuple[float, ...], ...]
    offset: float
    variant: str
    p: int
    gammas: tuple[float, ...]
    betas: tuple[float, ...]
    opt_level: int
    compile_seed: int
    target: str
    backend_kind: str
    chi_max: int
    shots: int
    backend_seed: int
    max_variables: int
    max_wall_s: int
    max_qpu_s: int
    classical_min_energy: float
    job_digest: str
    payload: Mapping[str, Any]

    @property
    def n(self) -> int:
        return len(self.variables)

    def energy(self, bits: Sequence[int]) -> float:
        """``xᵀ Q x + offset`` for ``bits`` in variable order."""
        if len(bits) != self.n or any(b not in (0, 1) for b in bits):
            raise ValueError("assignment length or alphabet differs from the job")
        total = self.offset
        for i, xi in enumerate(bits):
            if xi:
                row = self.q[i]
                total += sum(row[j] for j, xj in enumerate(bits) if xj)
        return total


def parse_job(body: Any) -> QSearchJob:
    """Validate ``body`` completely, then return the typed job (never partial)."""
    body = _mapping(body, "SCHEMA", "job")
    if body.get("schema") != JOB_SCHEMA:
        raise JobValidationError("SCHEMA", f"schema must be {JOB_SCHEMA}")
    unknown = set(body) - _TOP_KEYS
    missing = _TOP_KEYS - set(body)
    if unknown or missing:
        raise JobValidationError("SCHEMA", f"unknown keys {sorted(unknown)}, missing keys {sorted(missing)}")
    try:
        compiled_search_digest = validate_digest(body["compiled_search_digest"], field="compiled_search_digest")
    except ValueError as exc:
        raise JobValidationError("DIGEST", str(exc)) from exc

    variables = body["variables"]
    if (not isinstance(variables, list) or not variables
            or any(not isinstance(v, str) or not v for v in variables)
            or len(set(variables)) != len(variables)):
        raise JobValidationError("VARIABLES", "variables must be a nonempty list of unique nonempty strings")
    n = len(variables)

    qubo = _mapping(body["qubo"], "QUBO", "qubo")
    if set(qubo) != {"q", "offset", "convention"}:
        raise JobValidationError("QUBO", "qubo must have exactly q, offset, convention")
    if qubo["convention"] != CONVENTION:
        raise JobValidationError("QUBO", f"qubo.convention must be {CONVENTION}")
    offset = _number(qubo["offset"], "QUBO", "qubo.offset")
    rows = qubo["q"]
    if not isinstance(rows, list) or len(rows) != n or any(not isinstance(r, list) or len(r) != n for r in rows):
        raise JobValidationError("QUBO", "qubo.q must be a square matrix over the variables")
    q = tuple(tuple(_number(v, "QUBO", "qubo.q entry") for v in row) for row in rows)
    for i in range(n):
        for j in range(i + 1, n):
            if abs(q[i][j] - q[j][i]) > _SYMMETRY_TOLERANCE:
                raise JobValidationError("QUBO", f"qubo.q is not symmetric at ({i},{j})")

    comp = _mapping(body["compile"], "COMPILE", "compile")
    if set(comp) != {"variant", "p", "gammas", "betas", "opt_level", "seed", "target", "training"}:
        raise JobValidationError("COMPILE", "compile has unexpected or missing keys")
    variant = _choice(comp["variant"], VARIANTS, "COMPILE", "compile.variant")
    p = _int(comp["p"], "COMPILE", "compile.p", minimum=1)
    gammas = _angles(comp["gammas"], p, "compile.gammas")
    betas = _angles(comp["betas"], p, "compile.betas")
    opt_level = _int(comp["opt_level"], "COMPILE", "compile.opt_level", minimum=0)
    if opt_level > 3:
        raise JobValidationError("COMPILE", "compile.opt_level must be 0..3")
    compile_seed = _int(comp["seed"], "COMPILE", "compile.seed", minimum=0)
    target = _choice(comp["target"], TARGETS, "COMPILE", "compile.target")
    if comp["training"] != "none":
        raise JobValidationError("COMPILE", "only compile.training == 'none' is defined")

    back = _mapping(body["backend"], "BACKEND", "backend")
    if set(back) != {"kind", "chi_max", "shots", "seed"}:
        raise JobValidationError("BACKEND", "backend has unexpected or missing keys")
    backend_kind = _choice(back["kind"], BACKEND_KINDS, "BACKEND", "backend.kind")
    chi_max = _int(back["chi_max"], "BACKEND", "backend.chi_max", minimum=1)
    shots = _int(back["shots"], "BACKEND", "backend.shots", minimum=1)
    backend_seed = _int(back["seed"], "BACKEND", "backend.seed", minimum=0)

    lim = _mapping(body["limits"], "LIMITS", "limits")
    if set(lim) != {"max_variables", "max_wall_s", "max_qpu_s"}:
        raise JobValidationError("LIMITS", "limits has unexpected or missing keys")
    max_variables = _int(lim["max_variables"], "LIMITS", "limits.max_variables", minimum=1)
    max_wall_s = _int(lim["max_wall_s"], "LIMITS", "limits.max_wall_s", minimum=0)
    max_qpu_s = _int(lim["max_qpu_s"], "LIMITS", "limits.max_qpu_s", minimum=0)
    if n > max_variables:
        raise JobValidationError("LIMITS", f"{n} variables exceed limits.max_variables={max_variables}")

    audit = _mapping(body["audit"], "AUDIT", "audit")
    if set(audit) != {"classical_min_energy"}:
        raise JobValidationError("AUDIT", "audit must have exactly classical_min_energy")
    classical_min_energy = _number(audit["classical_min_energy"], "AUDIT", "audit.classical_min_energy")

    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    expected = canonical_digest(unsigned)
    if body["job_digest"] != expected:
        raise JobValidationError("DIGEST", "job_digest does not match the canonical body")

    return QSearchJob(
        compiled_search_digest=compiled_search_digest, variables=tuple(variables), q=q, offset=offset,
        variant=variant, p=p, gammas=gammas, betas=betas, opt_level=opt_level, compile_seed=compile_seed,
        target=target, backend_kind=backend_kind, chi_max=chi_max, shots=shots, backend_seed=backend_seed,
        max_variables=max_variables, max_wall_s=max_wall_s, max_qpu_s=max_qpu_s,
        classical_min_energy=classical_min_energy, job_digest=expected, payload=_deep_freeze(body),
    )


__all__ = ["BACKEND_KINDS", "CONVENTION", "JOB_SCHEMA", "JobValidationError", "QSearchJob", "TARGETS",
           "VARIANTS", "parse_job"]
```

Note `_number` rejects NaN before the digest step; the digest step itself must never see NaN (Predator's `canonical_digest` would raise) — that is why validation runs first and the test `test_non_finite_entries_rejected_before_digest` exists.

- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat(qsearch): typed job parsing and validation`.

---

## Task 3: Receipt shaping and seal

**Files:** Create `nano/loop/qsearch_receipt.py`; Test `tests/test_qsearch_receipt.py`.

Receipt bodies (unsigned). `OK`/`TRUNCATED` come from an engine result dict (Task 7 produces it; the shape is fixed here so Task 7 builds to it):

```python
ENGINE_RESULT = {           # produced by _qsearch_engine.py, consumed by receipt.ok_receipt
  "kind": "statevector" | "mps",
  "backend_id": "qiskit-aer 0.17.2 statevector",
  "variant": "ordinary" | "joint5", "nuclei": [[0,1,2]],
  "compiled_plan_digest": "sha256:…",
  "compile": {"target": "heron_heavy_hex", "coupling_map": "heavy_hex_d3", "physical_qubits": 19,
              "basis": ["cz","rz","sx","x"], "opt_level": 3, "seed": 11,
              "candidates": {"ordinary": {"logical_2q": 4, "native_2q": 9, "native_depth": 7},
                             "joint5": {...} | None}},
  "metrics": {"logical_2q": 4, "native_2q": 9, "native_depth": 7},
  "chi_max": None | int, "chi_reached": None | int, "truncated": None | bool,
  "shots": 1024, "seed": 7, "wall_s": 0.41, "attempts": 1,
  "samples": [["0110", 913, -2.41], ...],      # [bits x0-first, count, energy], sorted (energy, bits)
  "best_sample_energy": -2.41, "mean_energy_sample": -1.9,
  "mean_energy_ideal": -1.92 | None, "tvd_ideal": 0.012 | None, "exact_min_energy": -2.41 | None,
}
```

- [ ] **Step 1: Failing tests**

```python
# tests/test_qsearch_receipt.py
import hashlib
import hmac
import json

import pytest

from nano.loop.qsearch_canonical import canonical_json
from nano.loop.qsearch_job import parse_job
from nano.loop.qsearch_receipt import (
    RECEIPT_SCHEMA, SEAL_PREFIX, SealError, not_run_receipt, ok_receipt, refused_receipt,
    seal_receipt, verify_seal,
)

KEY = b"k" * 32


@pytest.fixture
def job(fixtures_dir):
    return parse_job(json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8")))


def _engine_result(job, best):
    return {"kind": "statevector", "backend_id": "qiskit-aer 0.17.2 statevector",
            "variant": "ordinary", "nuclei": [], "compiled_plan_digest": "sha256:" + "1" * 64,
            "compile": {"target": job.target, "coupling_map": "heavy_hex_d3", "physical_qubits": 19,
                        "basis": ["cz", "rz", "sx", "x"], "opt_level": 3, "seed": 11,
                        "candidates": {"ordinary": {"logical_2q": 4, "native_2q": 9, "native_depth": 7}, "joint5": None}},
            "metrics": {"logical_2q": 4, "native_2q": 9, "native_depth": 7},
            "chi_max": None, "chi_reached": None, "truncated": None,
            "shots": 1024, "seed": 7, "wall_s": 0.4, "attempts": 1,
            "samples": [["1100", 600, best], ["0000", 424, 0.0]],
            "best_sample_energy": best, "mean_energy_sample": best * 600 / 1024,
            "mean_energy_ideal": best * 0.6, "tvd_ideal": 0.01, "exact_min_energy": job.classical_min_energy}


def test_seal_round_trip_and_independent_hmac(job):
    body = not_run_receipt(job, "engine: TimeoutExpired")
    sealed = seal_receipt(body, KEY)
    assert sealed["seal"].startswith(SEAL_PREFIX)
    expected = hmac.new(KEY, canonical_json({k: v for k, v in sealed.items() if k != "seal"}).encode("utf-8"),
                        hashlib.sha256).hexdigest()
    assert sealed["seal"] == SEAL_PREFIX + expected
    verify_seal(sealed, KEY)      # no raise
    with pytest.raises(SealError):
        verify_seal({**sealed, "status": "OK"}, KEY)
    with pytest.raises(SealError):
        verify_seal(sealed, b"x" * 32)
    with pytest.raises(ValueError):
        seal_receipt(body, b"short")


def test_not_run_and_refused_shape(job):
    nr = not_run_receipt(job, "engine: OSError")
    assert nr == {"schema": RECEIPT_SCHEMA, "job_digest": job.job_digest, "status": "NOT_RUN",
                  "backend": {"kind": "none"}, "reason": "engine: OSError"}
    rf = refused_receipt(job, "limits: 41 variables exceed service cap 40")
    assert rf["status"] == "REFUSED" and rf["backend"] == {"kind": "none"} and rf["job_digest"] == job.job_digest


def test_ok_receipt_agree_within_tolerance(job):
    body = ok_receipt(job, _engine_result(job, job.classical_min_energy + 5e-10))
    assert body["status"] == "OK" and body["audit"]["agree"] is True
    assert body["audit"]["classical_min_energy"] == job.classical_min_energy
    assert body["audit"]["energy_gap_best"] == pytest.approx(5e-10 / abs(job.classical_min_energy), abs=1e-12)
    assert body["backend"]["kind"] == "statevector" and body["backend"]["chi_max"] is None
    assert body["samples"]["top"][0] == {"bits": "1100", "count": 600, "energy": job.classical_min_energy + 5e-10}
    assert body["samples"]["bit_order"] == "x0_first"
    assert body["samples"]["distribution_digest"].startswith("sha256:")
    assert body["variant"] == "ordinary" and body["metrics"]["native_2q"] == 9
    assert "seal" not in body


def test_ok_receipt_disagree_and_truncated(job):
    res = _engine_result(job, job.classical_min_energy + 0.5)
    res.update(kind="mps", chi_max=8, chi_reached=8, truncated=True)
    body = ok_receipt(job, res)
    assert body["status"] == "TRUNCATED" and body["audit"]["agree"] is False
    assert body["backend"]["chi_reached"] == 8 and body["backend"]["truncated"] is True


def test_ok_receipt_top_is_capped_at_eight(job):
    res = _engine_result(job, -1.0)
    res["samples"] = [[format(i, "04b"), 10, -1.0 + i] for i in range(12)]
    assert len(ok_receipt(job, res)["samples"]["top"]) == 8


def test_zero_classical_min_gives_zero_gap(job, fixtures_dir):
    body = json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))
    # build a job whose classical min is exactly 0 by editing and re-signing
    from nano.loop.qsearch_canonical import canonical_digest
    body["audit"]["classical_min_energy"] = 0.0
    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    zero_job = parse_job({**unsigned, "job_digest": canonical_digest(unsigned)})
    assert ok_receipt(zero_job, _engine_result(zero_job, 0.0))["audit"]["energy_gap_best"] == 0.0
```

- [ ] **Step 2: Run** → fails. **Step 3: Implement**

```python
# nano/loop/qsearch_receipt.py
"""Shape and seal ``predator.qsearch.receipt.v1`` payloads.

Every receipt the service emits goes through ``seal_receipt``: HMAC-SHA256
over the canonical JSON of the unsigned body, carried as ``seal =
"hmac-sha256:<hex>"``. That is exactly what Predator-M
``QSearchReceiptV1.from_payload`` recomputes, so a receipt that verifies here
verifies there. Energies are compared with ``ENERGY_TOLERANCE``, never ``==``.
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

from .qsearch_canonical import canonical_digest, canonical_json
from .qsearch_job import QSearchJob

RECEIPT_SCHEMA = "predator.qsearch.receipt.v1"
SEAL_PREFIX = "hmac-sha256:"
ENERGY_TOLERANCE = 1e-9
MIN_KEY_BYTES = 16
TOP_SAMPLES = 8
_HEX = frozenset("0123456789abcdef")
SERVICE_NAME = "nano-qsearch"


class SealError(ValueError):
    """The receipt seal is missing, malformed or does not verify."""


def _check_key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise ValueError(f"seal key must be at least {MIN_KEY_BYTES} bytes")
    return bytes(key)


def seal_receipt(unsigned: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    key = _check_key(key)
    body = {k: v for k, v in unsigned.items() if k != "seal"}
    mac = hmac.new(key, canonical_json(body).encode("utf-8"), hashlib.sha256).hexdigest()
    return {**body, "seal": SEAL_PREFIX + mac}


def verify_seal(payload: Mapping[str, Any], key: bytes) -> None:
    key = _check_key(key)
    seal = payload.get("seal")
    if not isinstance(seal, str) or not seal.startswith(SEAL_PREFIX):
        raise SealError("receipt carries no hmac-sha256 seal")
    suffix = seal[len(SEAL_PREFIX):]
    if len(suffix) != 64 or any(c not in _HEX for c in suffix):
        raise SealError("receipt seal is malformed")
    body = {k: v for k, v in payload.items() if k != "seal"}
    expected = hmac.new(key, canonical_json(body).encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(suffix, expected):
        raise SealError("receipt seal does not verify")


def _base(job: QSearchJob, status: str) -> dict[str, Any]:
    return {"schema": RECEIPT_SCHEMA, "job_digest": job.job_digest, "status": status}


def not_run_receipt(job: QSearchJob, reason: str) -> dict[str, Any]:
    return {**_base(job, "NOT_RUN"), "backend": {"kind": "none"}, "reason": reason}


def refused_receipt(job: QSearchJob, reason: str) -> dict[str, Any]:
    return {**_base(job, "REFUSED"), "backend": {"kind": "none"}, "reason": reason}


def energy_gap(best: float, floor: float) -> float:
    return (best - floor) / abs(floor) if abs(floor) > 0.0 else 0.0


def ok_receipt(job: QSearchJob, result: Mapping[str, Any]) -> dict[str, Any]:
    """Build the OK (or TRUNCATED) unsigned body from an engine result (see plan Task 3)."""
    floor = job.classical_min_energy
    best = float(result["best_sample_energy"])
    agree = abs(best - floor) <= ENERGY_TOLERANCE
    samples = [list(row) for row in result["samples"]]
    status = "TRUNCATED" if result.get("truncated") is True else "OK"
    return {
        **_base(job, status),
        "compiled_plan_digest": result["compiled_plan_digest"],
        "variant": result["variant"],
        "nuclei": [list(nucleus) for nucleus in result["nuclei"]],
        "compile": dict(result["compile"]),
        "metrics": dict(result["metrics"]),
        "backend": {
            "kind": result["kind"], "id": result["backend_id"],
            "chi_max": result["chi_max"], "chi_reached": result["chi_reached"], "truncated": result["truncated"],
            "shots": result["shots"], "seed": result["seed"], "wall_s": result["wall_s"],
            "attempts": result["attempts"],
        },
        "samples": {
            "bit_order": "x0_first",
            "distribution_digest": canonical_digest([[bits, count] for bits, count, _ in samples]),
            "top": [{"bits": bits, "count": count, "energy": energy} for bits, count, energy in samples[:TOP_SAMPLES]],
        },
        "audit": {
            "classical_min_energy": floor, "best_sample_energy": best, "agree": agree,
            "energy_gap_best": energy_gap(best, floor),
            "mean_energy_sample": result["mean_energy_sample"],
            "mean_energy_ideal": result["mean_energy_ideal"],
            "tvd_ideal": result["tvd_ideal"],
            "exact_min_energy": result["exact_min_energy"],
        },
        "service": {"name": SERVICE_NAME, "engine": "nano.loop._qsearch_engine"},
    }


__all__ = ["ENERGY_TOLERANCE", "RECEIPT_SCHEMA", "SEAL_PREFIX", "SealError", "energy_gap",
           "not_run_receipt", "ok_receipt", "refused_receipt", "seal_receipt", "verify_seal"]
```

- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat(qsearch): receipt shaping and HMAC seal`.

---

## Task 4: Request authentication and replay protection

**Files:** Create `nano/loop/qsearch_auth.py`; Test `tests/test_qsearch_auth.py`.

Header: `X-Aether-Qsearch-Auth: v1 ts=<unix int> nonce=<16..64 hex> mac=<64 hex>` where
`mac = HMAC-SHA256(request_key, "POST\n/qsearch/v1/submit\n<sha256(body) hex>\n<ts>\n<nonce>")`.
Window: `|now − ts| ≤ 120`. A nonce is accepted once; `NonceStore.remember(nonce, ts) -> bool` returns False on replay. The request key is a separate file from the receipt key.

- [ ] **Step 1: Failing tests**

```python
# tests/test_qsearch_auth.py
import pytest

from nano.loop.qsearch_auth import (
    AUTH_HEADER, WINDOW_S, AuthError, MemoryNonceStore, sign_request, verify_request,
)

KEY = b"r" * 32
BODY = b'{"schema":"predator.qsearch.job.v1"}'


def test_sign_then_verify():
    header = sign_request(KEY, "POST", "/qsearch/v1/submit", BODY, ts=1_000_000, nonce="ab" * 16)
    assert header.startswith("v1 ts=1000000 nonce=")
    store = MemoryNonceStore()
    verify_request(KEY, header, "POST", "/qsearch/v1/submit", BODY, now=1_000_010, nonce_store=store)


@pytest.mark.parametrize("tamper", [
    lambda h: h.replace("ts=1000000", "ts=1000001"),
    lambda h: h[:-1] + ("0" if h[-1] != "0" else "1"),
    lambda h: h.replace("nonce=" + "ab" * 16, "nonce=" + "cd" * 16),
    lambda h: "v2 " + h[3:],
    lambda h: h.replace("ts=", "time="),
    lambda h: "",
])
def test_tampered_header_rejected(tamper):
    header = sign_request(KEY, "POST", "/qsearch/v1/submit", BODY, ts=1_000_000, nonce="ab" * 16)
    with pytest.raises(AuthError):
        verify_request(KEY, tamper(header), "POST", "/qsearch/v1/submit", BODY, now=1_000_000, nonce_store=MemoryNonceStore())


def test_body_method_path_and_key_are_bound():
    header = sign_request(KEY, "POST", "/qsearch/v1/submit", BODY, ts=5, nonce="ab" * 16)
    for args in ((KEY, "POST", "/qsearch/v1/submit", BODY + b" "), (KEY, "GET", "/qsearch/v1/submit", BODY),
                 (KEY, "POST", "/qsearch/v1/other", BODY), (b"z" * 32, "POST", "/qsearch/v1/submit", BODY)):
        key, method, path, body = args
        with pytest.raises(AuthError):
            verify_request(key, header, method, path, body, now=5, nonce_store=MemoryNonceStore())


def test_timestamp_window():
    header = sign_request(KEY, "POST", "/qsearch/v1/submit", BODY, ts=10_000, nonce="ab" * 16)
    verify_request(KEY, header, "POST", "/qsearch/v1/submit", BODY, now=10_000 + WINDOW_S, nonce_store=MemoryNonceStore())
    verify_request(KEY, header, "POST", "/qsearch/v1/submit", BODY, now=10_000 - WINDOW_S, nonce_store=MemoryNonceStore())
    for now in (10_000 + WINDOW_S + 1, 10_000 - WINDOW_S - 1):
        with pytest.raises(AuthError) as info:
            verify_request(KEY, header, "POST", "/qsearch/v1/submit", BODY, now=now, nonce_store=MemoryNonceStore())
        assert info.value.code == "STALE"


def test_replay_rejected_only_after_mac_passes():
    store = MemoryNonceStore()
    header = sign_request(KEY, "POST", "/qsearch/v1/submit", BODY, ts=50, nonce="ab" * 16)
    verify_request(KEY, header, "POST", "/qsearch/v1/submit", BODY, now=50, nonce_store=store)
    with pytest.raises(AuthError) as info:
        verify_request(KEY, header, "POST", "/qsearch/v1/submit", BODY, now=51, nonce_store=store)
    assert info.value.code == "REPLAY"
    # a forged mac with a fresh nonce must not consume the nonce
    bad = sign_request(b"q" * 32, "POST", "/qsearch/v1/submit", BODY, ts=50, nonce="cd" * 16)
    with pytest.raises(AuthError):
        verify_request(KEY, bad, "POST", "/qsearch/v1/submit", BODY, now=50, nonce_store=store)
    assert store.remember("cd" * 16, 50) is True


def test_bad_nonce_shape_rejected():
    for nonce in ("", "xyz", "a" * 15, "a" * 65, "AB" * 16):
        with pytest.raises(ValueError):
            sign_request(KEY, "POST", "/qsearch/v1/submit", BODY, ts=1, nonce=nonce)


def test_short_key_rejected():
    with pytest.raises(ValueError):
        sign_request(b"short", "POST", "/qsearch/v1/submit", BODY, ts=1, nonce="ab" * 16)
    assert AUTH_HEADER == "X-Aether-Qsearch-Auth"
```

- [ ] **Step 2: Run** → fails. **Step 3: Implement**

```python
# nano/loop/qsearch_auth.py
"""Request authentication for ``POST /qsearch/v1/submit``.

A caller proves possession of the request key (a file separate from the
receipt-seal key) with ``X-Aether-Qsearch-Auth: v1 ts=<unix> nonce=<hex>
mac=<hex>``; ``mac`` is HMAC-SHA256 over method, path, body digest, timestamp
and nonce joined by newlines. The timestamp must be within ``WINDOW_S`` of the
server clock and the nonce is accepted once (``NonceStore``). Verification
runs the MAC check before touching the nonce store so a forged request cannot
burn a nonce. The receipt seal is a different key and a different concern.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import threading
from typing import Protocol

AUTH_HEADER = "X-Aether-Qsearch-Auth"
WINDOW_S = 120
MIN_KEY_BYTES = 16
_NONCE = re.compile(r"[0-9a-f]{16,64}\Z")
_HEADER = re.compile(r"v1 ts=(?P<ts>-?\d{1,12}) nonce=(?P<nonce>[0-9a-f]{16,64}) mac=(?P<mac>[0-9a-f]{64})\Z")


class AuthError(Exception):
    """Authentication failed; ``code`` ∈ {MALFORMED, STALE, MAC, REPLAY}. Never carries the body."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class NonceStore(Protocol):
    def remember(self, nonce: str, ts: int) -> bool:
        """Record ``nonce``; return False if it was already recorded."""


class MemoryNonceStore:
    def __init__(self) -> None:
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()

    def remember(self, nonce: str, ts: int) -> bool:
        with self._lock:
            if nonce in self._seen:
                return False
            self._seen[nonce] = ts
            return True


def _check_key(key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise ValueError(f"request key must be at least {MIN_KEY_BYTES} bytes")
    return bytes(key)


def _message(method: str, path: str, body: bytes, ts: int, nonce: str) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return "\n".join((method.upper(), path, digest, str(ts), nonce)).encode("utf-8")


def sign_request(key: bytes, method: str, path: str, body: bytes, *, ts: int, nonce: str) -> str:
    key = _check_key(key)
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise ValueError("ts must be an integer unix time")
    if not isinstance(nonce, str) or not _NONCE.match(nonce):
        raise ValueError("nonce must be 16..64 lowercase hex characters")
    mac = hmac.new(key, _message(method, path, body, ts, nonce), hashlib.sha256).hexdigest()
    return f"v1 ts={ts} nonce={nonce} mac={mac}"


def verify_request(key: bytes, header: str | None, method: str, path: str, body: bytes, *,
                   now: int, nonce_store: NonceStore) -> None:
    key = _check_key(key)
    match = _HEADER.match(header or "")
    if match is None:
        raise AuthError("MALFORMED", "missing or malformed auth header")
    ts, nonce, mac = int(match["ts"]), match["nonce"], match["mac"]
    if abs(now - ts) > WINDOW_S:
        raise AuthError("STALE", "timestamp outside the accepted window")
    expected = hmac.new(key, _message(method, path, body, ts, nonce), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise AuthError("MAC", "request signature does not verify")
    if not nonce_store.remember(nonce, ts):
        raise AuthError("REPLAY", "nonce already used")


__all__ = ["AUTH_HEADER", "AuthError", "MemoryNonceStore", "NonceStore", "WINDOW_S", "sign_request", "verify_request"]
```

- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat(qsearch): request HMAC authentication with nonce replay protection`.

---

## Task 5: Durable SQLite store

**Files:** Create `nano/loop/qsearch_store.py`; Test `tests/test_qsearch_store.py`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_qsearch_store.py
import json
import threading

import pytest

from nano.loop.qsearch_store import JobRecord, JobStore

D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "qsearch.sqlite3")
    yield s
    s.close()


def test_begin_complete_get(store):
    assert store.get(D1) is None
    rec = store.begin(D1, '{"job":1}')
    assert isinstance(rec, JobRecord) and rec.state == "ACCEPTED" and rec.receipt_json is None
    assert store.begin(D1, '{"job":1}').state == "ACCEPTED"      # idempotent, same row
    store.complete(D1, '{"receipt":1}')
    got = store.get(D1)
    assert got.state == "DONE" and json.loads(got.receipt_json) == {"receipt": 1} and got.completed_at >= got.accepted_at


def test_complete_unknown_or_done_rejected(store):
    with pytest.raises(KeyError):
        store.complete(D1, "{}")
    store.begin(D1, "{}")
    store.complete(D1, "{}")
    with pytest.raises(ValueError):
        store.complete(D1, '{"second":true}')


def test_pending_lists_only_accepted(store):
    store.begin(D1, '{"a":1}')
    store.begin(D2, '{"b":2}')
    store.complete(D2, "{}")
    assert [r.job_digest for r in store.pending()] == [D1]


def test_survives_reopen(tmp_path):
    path = tmp_path / "db.sqlite3"
    s = JobStore(path); s.begin(D1, "{}"); s.close()
    s2 = JobStore(path)
    assert s2.get(D1).state == "ACCEPTED" and [r.job_digest for r in s2.pending()] == [D1]
    s2.close()


def test_nonces(store):
    assert store.remember("ab" * 16, 100) is True
    assert store.remember("ab" * 16, 101) is False
    store.prune_nonces(before_ts=150)
    assert store.remember("ab" * 16, 160) is True     # pruned, acceptable again (window has passed)


def test_thread_safe(store):
    def work(i):
        d = "sha256:" + format(i, "064x")
        store.begin(d, "{}"); store.complete(d, "{}")
    threads = [threading.Thread(target=work, args=(i,)) for i in range(16)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(store.pending()) == 0 and store.get("sha256:" + format(3, "064x")).state == "DONE"


def test_attempts_increment_on_begin_after_accept(store):
    store.begin(D1, "{}")
    assert store.mark_attempt(D1) == 1
    assert store.mark_attempt(D1) == 2
```

- [ ] **Step 2: Run** → fails. **Step 3: Implement**

```python
# nano/loop/qsearch_store.py
"""Durable job and nonce store for the q-search service (SQLite, stdlib).

A job row is keyed by ``job_digest`` and moves ACCEPTED → DONE exactly once.
``pending()`` lists ACCEPTED rows so a restarted service can re-run what it
accepted before a crash; ``complete`` refuses to overwrite a DONE row so a
second divergent result can never replace the first. Nonces live in the same
file so replay protection survives a restart. One connection, one lock.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_digest TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('ACCEPTED', 'DONE')),
  job_json TEXT NOT NULL,
  receipt_json TEXT,
  accepted_at REAL NOT NULL,
  completed_at REAL,
  attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS nonces (
  nonce TEXT PRIMARY KEY,
  ts INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class JobRecord:
    job_digest: str
    state: str
    job_json: str
    receipt_json: str | None
    accepted_at: float
    completed_at: float | None
    attempts: int


class JobStore:
    _COLS = "job_digest, state, job_json, receipt_json, accepted_at, completed_at, attempts"

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self._path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _row(row) -> JobRecord | None:
        return None if row is None else JobRecord(*row)

    def get(self, job_digest: str) -> JobRecord | None:
        with self._lock:
            return self._row(self._db.execute(f"SELECT {self._COLS} FROM jobs WHERE job_digest=?", (job_digest,)).fetchone())

    def begin(self, job_digest: str, job_json: str) -> JobRecord:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO jobs (job_digest, state, job_json, accepted_at) VALUES (?, 'ACCEPTED', ?, ?)",
                             (job_digest, job_json, time.time()))
            return self._row(self._db.execute(f"SELECT {self._COLS} FROM jobs WHERE job_digest=?", (job_digest,)).fetchone())

    def mark_attempt(self, job_digest: str) -> int:
        with self._lock:
            self._db.execute("UPDATE jobs SET attempts = attempts + 1 WHERE job_digest=?", (job_digest,))
            return int(self._db.execute("SELECT attempts FROM jobs WHERE job_digest=?", (job_digest,)).fetchone()[0])

    def complete(self, job_digest: str, receipt_json: str) -> None:
        with self._lock:
            row = self._db.execute("SELECT state FROM jobs WHERE job_digest=?", (job_digest,)).fetchone()
            if row is None:
                raise KeyError(job_digest)
            if row[0] == "DONE":
                raise ValueError("job already completed; a second result is never stored")
            self._db.execute("UPDATE jobs SET state='DONE', receipt_json=?, completed_at=? WHERE job_digest=? AND state='ACCEPTED'",
                             (receipt_json, time.time(), job_digest))

    def pending(self) -> list[JobRecord]:
        with self._lock:
            rows = self._db.execute(f"SELECT {self._COLS} FROM jobs WHERE state='ACCEPTED' ORDER BY accepted_at").fetchall()
        return [self._row(r) for r in rows]

    def remember(self, nonce: str, ts: int) -> bool:
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO nonces (nonce, ts) VALUES (?, ?)", (nonce, ts))
            return cur.rowcount == 1

    def prune_nonces(self, *, before_ts: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM nonces WHERE ts < ?", (before_ts,))


__all__ = ["JobRecord", "JobStore"]
```

- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat(qsearch): durable SQLite job and nonce store`.

---

## Task 6: Compile helpers (child-side, qiskit + ATLAS)

**Files:** Create `nano/loop/qsearch_compile.py`; Test `tests/test_qsearch_compile.py` (marked `engine`, uses `importorskip`).

Run these tests only from PowerShell with system Python and `NANO_QSEARCH_ATLAS_PATH=<wt-atlas>\aether-atlas`.

Rules: heavy-hex map = smallest odd distance `d ∈ {3,5,7,9,11}` with `CouplingMap.from_heavy_hex(d).size() ≥ n` (19, 57, 115, 193, …); basis `["cz","rz","sx","x"]`; `transpile(circuit, coupling_map=cm, basis_gates=BASIS, optimization_level=opt_level, seed_transpiler=seed)`. For targets `fake_fez` and `ibm_fez`, compile against `qiskit_ibm_runtime.fake_provider.FakeFez()` when importable (`transpile(circuit, backend=FakeFez(), optimization_level=…, seed_transpiler=…)`), reported as `coupling_map: "fake_fez"`; if the import fails raise `TargetUnavailable`. `native_2q` = number of 2-qubit gate instances in the ISA circuit; `native_depth` = `isa.depth(lambda inst: len(inst.qubits) == 2)`; `logical_2q` = 2-qubit gate count of the logical circuit. `variant == "auto"`: compile `ordinary`; if `find_nuclei(Q)` returns ≥ 1 nucleus also compile `joint5` with those nuclei; keep the smaller `native_2q` (tie → ordinary). `variant == "joint5"` with no nuclei → `ValueError`. `compiled_plan_digest` = `canonical_digest` of `{"schema": "nano.qsearch.compiled-plan.v1", "target", "coupling_map", "physical_qubits", "basis", "opt_level", "seed", "variant", "nuclei", "qpy_sha256": sha256(qpy bytes of the ISA circuit), "inspection": {"ops": dict(count_ops), "logical_2q", "native_2q", "native_depth", "measurement_map": [[physical_qubit, clbit], ...]}}`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_qsearch_compile.py
import json
from dataclasses import replace

import pytest

pytestmark = pytest.mark.engine
pytest.importorskip("qiskit")
pytest.importorskip("aether_atlas")

from nano.loop.qsearch_canonical import canonical_digest        # noqa: E402
from nano.loop.qsearch_job import parse_job                      # noqa: E402
from nano.loop.qsearch_compile import BASIS, compile_program, heavy_hex_map   # noqa: E402


@pytest.fixture
def pair02(fixtures_dir):
    return parse_job(json.loads((fixtures_dir / "job_pair02.json").read_text(encoding="utf-8")))


@pytest.fixture
def four(fixtures_dir):
    return parse_job(json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8")))


def test_heavy_hex_map_sizes():
    assert heavy_hex_map(4).size() == 19 and heavy_hex_map(19).size() == 19
    assert heavy_hex_map(20).size() == 57 and heavy_hex_map(58).size() == 115


def test_auto_compiles_both_and_keeps_smaller(pair02):
    plan = compile_program(pair02)
    cands = plan.candidates
    assert set(cands) == {"ordinary", "joint5"} and cands["ordinary"]["logical_2q"] == 30
    assert plan.nuclei == [[0, 1, 2], [0, 3, 4], [1, 3, 5], [2, 4, 5]] or plan.variant == "ordinary"
    chosen = min(cands, key=lambda k: (cands[k]["native_2q"], k != "ordinary"))
    assert plan.variant == chosen and plan.metrics == cands[chosen]
    assert plan.compiled_plan_digest.startswith("sha256:") and plan.coupling_map == "heavy_hex_d3"
    assert set(plan.isa.count_ops()) <= set(BASIS) | {"measure", "barrier"}
    assert plan.logical.num_qubits == 6 and sorted(plan.mapping) == list(range(6))


def test_explicit_ordinary_skips_joint5(pair02):
    plan = compile_program(replace(pair02, variant="ordinary"))
    assert plan.variant == "ordinary" and plan.candidates["joint5"] is None and plan.nuclei == []


def test_joint5_without_nuclei_is_an_error(fixtures_dir):
    body = json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8"))
    n = len(body["variables"])
    for i in range(n):
        for j in range(n):
            if i != j:
                body["qubo"]["q"][i][j] = 0.0        # strip all couplings → no triangle
    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    job = parse_job({**unsigned, "job_digest": canonical_digest(unsigned)})
    with pytest.raises(ValueError):
        compile_program(replace(job, variant="joint5"))
    plan = compile_program(job)              # auto → ordinary, no 2q gates at all
    assert plan.variant == "ordinary" and plan.metrics["native_2q"] == 0


def test_digest_is_deterministic(four):
    assert compile_program(four).compiled_plan_digest == compile_program(four).compiled_plan_digest
```

- [ ] **Step 2: Run** (PowerShell) → fails. **Step 3: Implement**

```python
# nano/loop/qsearch_compile.py
"""Compile a q-search job into logical and native (Heron-basis) programs.

Child-process code: imports numpy, qiskit and ``aether_atlas`` at call time.
The logical program comes from ATLAS ``build_nucleus_program`` (ordinary QAOA
or joint5 with ``find_nuclei`` nuclei); the native program is the same circuit
transpiled to ``{cz, rz, sx, x}`` on a heavy-hex coupling map. ``variant ==
"auto"`` compiles both candidates and keeps the smaller native 2Q count.
``compiled_plan_digest`` binds the ISA bytes and inspection so a receipt names
one specific compiled program.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from typing import Any

from .qsearch_canonical import canonical_digest
from .qsearch_job import QSearchJob

BASIS = ["cz", "rz", "sx", "x"]
HEAVY_HEX_DISTANCES = (3, 5, 7, 9, 11)
PLAN_SCHEMA = "nano.qsearch.compiled-plan.v1"


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


def heavy_hex_map(n: int):
    """Smallest heavy-hex coupling map (distance 3, 5, 7, ...) with at least ``n`` qubits."""
    from qiskit.transpiler import CouplingMap
    for d in HEAVY_HEX_DISTANCES:
        cm = CouplingMap.from_heavy_hex(d)
        if cm.size() >= n:
            cm.distance = d  # type: ignore[attr-defined] - carried for naming only
            return cm
    raise ValueError(f"no heavy-hex distance in {HEAVY_HEX_DISTANCES} fits {n} qubits")


def _two_qubit_count(circuit) -> int:
    return sum(1 for inst in circuit.data if len(inst.qubits) == 2 and inst.operation.name != "barrier")


def _transpile(circuit, job: QSearchJob):
    """Return ``(isa_circuit, coupling_map_name)`` for the job's target."""
    from qiskit import transpile
    if job.target == "heron_heavy_hex":
        cm = heavy_hex_map(job.n)
        isa = transpile(circuit, coupling_map=cm, basis_gates=BASIS, optimization_level=job.opt_level,
                        seed_transpiler=job.compile_seed)
        return isa, f"heavy_hex_d{cm.distance}"
    try:
        from qiskit_ibm_runtime.fake_provider import FakeFez
    except ImportError as exc:
        raise TargetUnavailable("qiskit_ibm_runtime fake provider is not importable") from exc
    isa = transpile(circuit, backend=FakeFez(), optimization_level=job.opt_level, seed_transpiler=job.compile_seed)
    return isa, "fake_fez"


def _qpy_sha256(circuit) -> str:
    from qiskit import qpy
    buffer = io.BytesIO()
    qpy.dump(circuit, buffer)
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
    record = {"target": job.target, "coupling_map": cm_name, "physical_qubits": isa.num_qubits, "basis": list(BASIS),
              "opt_level": job.opt_level, "seed": job.compile_seed, "candidates": candidates}
    digest = canonical_digest({"schema": PLAN_SCHEMA,
                               **{k: record[k] for k in ("target", "coupling_map", "physical_qubits", "basis", "opt_level", "seed")},
                               "variant": chosen, "nuclei": chosen_nuclei, "qpy_sha256": _qpy_sha256(isa),
                               "inspection": {"ops": {k: int(v) for k, v in isa.count_ops().items()}, **metrics,
                                              "measurement_map": measurement_map}})
    return CompiledProgram(variant=chosen, nuclei=chosen_nuclei, logical=logical, isa=isa, mapping=mapping,
                           metrics=metrics, candidates=candidates, coupling_map=cm_name,
                           physical_qubits=isa.num_qubits, compiled_plan_digest=digest, compile_record=record)


__all__ = ["BASIS", "CompiledProgram", "HEAVY_HEX_DISTANCES", "PLAN_SCHEMA", "TargetUnavailable",
           "compile_program", "heavy_hex_map"]
```

If `CouplingMap` refuses the `distance` attribute, keep a module-level helper `_heavy_hex_distance(cm) -> int` that matches `cm.size()` against the distances instead; `heavy_hex_map` must return a plain `CouplingMap`.

- [ ] **Step 4: Run (PowerShell)** → pass. Note the measured `native_2q` for ordinary vs joint5 on pair02 in the commit body. **Step 5: Commit** `feat(qsearch): ATLAS compile with heavy-hex transpile and plan digest`.

---

## Task 7: Engine child process (compile + sample + audit)

**Files:** Create `nano/loop/_qsearch_engine.py`; Test `tests/test_qsearch_engine.py` (marked `engine`).

CLI: `python _qsearch_engine.py --job <job.json> --out <out.json> --kind statevector|mps`. Reads the job body, `parse_job`, `compile_program`, samples, writes either the ENGINE_RESULT dict (Task 3) or `{"error": {"type": "<ExceptionClassName>"}}` (no message text; class name only) and exits 0 in both cases. A hard crash (access violation) is a nonzero exit with no out file — the parent retries.

Sampling: transpile the **logical** circuit for `AerSimulator(method=kind, matrix_product_state_max_bond_dimension=chi_max (mps only), max_parallel_threads=1)`; `run(..., shots=job.shots, seed_simulator=job.backend_seed)`. Counts keys are classical-bit strings MSB-first; classical bit `i` holds logical `x[i]` (`build_nucleus_program` measures `mapping` into `range(n)`), so `bits_x0_first = key.replace(" ", "")[::-1]`. Energy via `job.energy`. `samples` sorted by `(energy, bits)`. For `n ≤ 20`: `exact_min_energy` by enumeration (numpy, vectorised over all 2^n states), `mean_energy_ideal` and `tvd_ideal` from `Statevector(logical.remove_final_measurements(inplace=False)).probabilities()` mapped from wire index to logical index through `mapping` (`x[i] = (s >> mapping[i]) & 1`). For `n > 20` those three are `None`. MPS: after sampling, run the unmeasured logical circuit with `save_matrix_product_state(label="mps")` on the same simulator; `chi_reached = max((len(lam) for lam in data["mps"][1]), default=1)`; `truncated = chi_reached >= chi_max`. Statevector: `chi_max = chi_reached = truncated = None`. `backend_id = f"qiskit-aer {qiskit_aer.__version__} {kind}"`; `wall_s` = perf_counter around compile+run; `attempts` is filled by the parent (write 1 here).

- [ ] **Step 1: Failing tests**

```python
# tests/test_qsearch_engine.py
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.engine
pytest.importorskip("qiskit_aer")
pytest.importorskip("aether_atlas")

from nano.loop.qsearch_canonical import canonical_digest           # noqa: E402
from nano.loop.qsearch_job import parse_job                        # noqa: E402
from nano.loop._qsearch_engine import run_engine                    # noqa: E402

ENGINE = Path(__file__).resolve().parents[1] / "nano" / "loop" / "_qsearch_engine.py"


def _job(fixtures_dir, name):
    body = json.loads((fixtures_dir / name).read_text(encoding="utf-8"))
    return body, parse_job(body)


def _child_env():
    parts = [str(ENGINE.parents[2])]
    if os.environ.get("NANO_QSEARCH_ATLAS_PATH"):
        parts.insert(0, os.environ["NANO_QSEARCH_ATLAS_PATH"])
    if os.environ.get("PYTHONPATH"):
        parts.append(os.environ["PYTHONPATH"])
    return {**os.environ, "OMP_NUM_THREADS": "1", "PYTHONPATH": os.pathsep.join(parts)}


@pytest.mark.parametrize("kind", ["statevector", "mps"])
def test_four_variable_fixture(fixtures_dir, kind):
    body, job = _job(fixtures_dir, "job_4var.json")
    res = run_engine(body, kind)
    assert "error" not in res and res["kind"] == kind
    assert res["best_sample_energy"] >= job.classical_min_energy - 1e-9
    assert abs(res["exact_min_energy"] - job.classical_min_energy) <= 1e-9
    assert sum(c for _, c, _ in res["samples"]) == job.shots
    for bits, _, energy in res["samples"]:
        assert len(bits) == 4 and abs(job.energy([int(b) for b in bits]) - energy) <= 1e-9
    assert res["samples"] == sorted(res["samples"], key=lambda r: (r[2], r[0]))
    assert 0.0 <= res["tvd_ideal"] <= 1.0 and math.isfinite(res["mean_energy_ideal"])
    if kind == "mps":
        assert res["chi_reached"] >= 1 and res["chi_max"] == job.chi_max and res["truncated"] is False
    else:
        assert res["chi_reached"] is None and res["truncated"] is None


def test_pair02_statevector_and_mps_agree_on_distribution(fixtures_dir):
    body, job = _job(fixtures_dir, "job_pair02.json")
    sv, mps = run_engine(body, "statevector"), run_engine(body, "mps")
    assert sv["variant"] == mps["variant"] and sv["compiled_plan_digest"] == mps["compiled_plan_digest"]
    # pair02 at chi 64 is exact: MPS TVD to ideal within 0.01 of the statevector sampling TVD (spec B7)
    assert abs(sv["tvd_ideal"] - mps["tvd_ideal"]) <= 0.01
    assert abs(sv["mean_energy_sample"] - sv["mean_energy_ideal"]) <= 0.1


def test_deterministic_for_same_seed(fixtures_dir):
    body, _ = _job(fixtures_dir, "job_4var.json")
    a, b = run_engine(body, "statevector"), run_engine(body, "statevector")
    assert a["samples"] == b["samples"]


def test_truncation_reported_at_tiny_chi(fixtures_dir):
    body, _ = _job(fixtures_dir, "job_pair02.json")
    body["backend"]["chi_max"] = 1
    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    body = {**unsigned, "job_digest": canonical_digest(unsigned)}
    res = run_engine(body, "mps")
    assert res["chi_max"] == 1 and res["truncated"] is True


def test_cli_writes_result_file(fixtures_dir, tmp_path):
    body, _ = _job(fixtures_dir, "job_4var.json")
    job_path, out_path = tmp_path / "job.json", tmp_path / "out.json"
    job_path.write_text(json.dumps(body), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ENGINE), "--job", str(job_path), "--out", str(out_path), "--kind", "statevector"],
                          env=_child_env(), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out_path.read_text(encoding="utf-8"))["kind"] == "statevector"


def test_cli_reports_error_class_not_message(tmp_path):
    job_path, out_path = tmp_path / "job.json", tmp_path / "out.json"
    job_path.write_text('{"schema": "nope"}', encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ENGINE), "--job", str(job_path), "--out", str(out_path), "--kind", "mps"],
                          env=_child_env(), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0
    assert json.loads(out_path.read_text(encoding="utf-8")) == {"error": {"type": "JobValidationError"}}
```

- [ ] **Step 2: Run** → fails. **Step 3: Implement**

```python
# nano/loop/_qsearch_engine.py
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
```

- [ ] **Step 4: Run (PowerShell)** → pass. If `pair02` MPS/statevector TVD differ by more than 0.01, do not loosen: report DONE_WITH_CONCERNS with the two numbers. **Step 5: Commit** `feat(qsearch): child-process engine with exact audit`.

---

## Task 8: Runtimes (parent side) with retries and NOT_RUN

**Files:** Create `nano/loop/runtime_mps.py`; Test `tests/test_qsearch_runtime.py`.

Parent logic: write the job body to a temp file in a private temp dir; spawn `[python, engine_path, --job, --out, --kind]` with env `OMP_NUM_THREADS=1`, `PYTHONPATH` = nano package parent + existing, `NANO_QSEARCH_ATLAS_PATH` passed through **and** prepended to `PYTHONPATH` when set; timeout = `job.max_wall_s` if > 0 else `DEFAULT_WALL_S = 600`, capped by `MAX_WALL_S = 3600`. Up to `MAX_ATTEMPTS = 5` (= 4 retries) while the child exits nonzero or leaves no out file. Outcomes: out file with `error` → `not_run_receipt(job, f"engine: {type}")` (no retry); timeout → `"engine: TimeoutExpired"`; retries exhausted → `"engine: CalledProcessError"`; success → `ok_receipt(job, {**result, "attempts": attempts})`. `StatevectorRuntime.run` refuses `job.n > 20` with `refused_receipt(job, "statevector: ...")`. `python` defaults to `sys.executable`, overridable by `NANO_QSEARCH_PYTHON`. Also expose `select_kind(service_kind, job) -> str` (`"auto"` → job's kind → `"statevector"` if `n ≤ 20` else `"mps"`; a non-auto service kind wins) and `build_runtime(kind) -> Runtime` (ibm → Task 9's `IbmRuntime`).

- [ ] **Step 1: Failing tests** (no qiskit needed; the engine is faked by `engine_path` injection)

```python
# tests/test_qsearch_runtime.py
import json
import textwrap
from dataclasses import replace

import pytest

from nano.loop.qsearch_job import parse_job
from nano.loop.runtime_mps import MAX_ATTEMPTS, MpsRuntime, StatevectorRuntime, select_kind


@pytest.fixture
def job(fixtures_dir):
    return parse_job(json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8")))


def _big(job, n=21):
    return replace(job, variables=tuple(f"v{i}" for i in range(n)), q=tuple(tuple(0.0 for _ in range(n)) for _ in range(n)))


def _fake_engine(tmp_path, body: str):
    path = tmp_path / "fake_engine.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


OK_ENGINE = '''
import argparse, json, pathlib
p = argparse.ArgumentParser(); p.add_argument("--job"); p.add_argument("--out"); p.add_argument("--kind")
a = p.parse_args()
job = json.loads(pathlib.Path(a.job).read_text())
res = {"kind": a.kind, "backend_id": "fake", "variant": "ordinary", "nuclei": [], "compiled_plan_digest": "sha256:" + "a"*64,
       "compile": {}, "metrics": {"logical_2q": 1, "native_2q": 1, "native_depth": 1},
       "chi_max": None, "chi_reached": None, "truncated": None, "shots": 1, "seed": 7, "wall_s": 0.0, "attempts": 1,
       "samples": [["0000", 1, job["audit"]["classical_min_energy"]]], "best_sample_energy": job["audit"]["classical_min_energy"],
       "mean_energy_sample": 0.0, "mean_energy_ideal": None, "tvd_ideal": None, "exact_min_energy": None}
pathlib.Path(a.out).write_text(json.dumps(res))
'''

CRASH_ENGINE = '''
import os, sys
marker = os.environ["CRASH_MARKER"]
n = int(open(marker).read()) if os.path.exists(marker) else 0
open(marker, "w").write(str(n + 1))
if n < int(os.environ.get("CRASH_TIMES", "99")):
    sys.exit(3)
'''


def test_ok_path(job, tmp_path):
    rt = StatevectorRuntime(engine_path=_fake_engine(tmp_path, OK_ENGINE))
    body = rt.run(job)
    assert body["status"] == "OK" and body["audit"]["agree"] is True and body["backend"]["attempts"] == 1


def test_crash_retries_then_not_run(job, tmp_path, monkeypatch):
    marker = tmp_path / "count"
    monkeypatch.setenv("CRASH_MARKER", str(marker))
    body = MpsRuntime(engine_path=_fake_engine(tmp_path, CRASH_ENGINE)).run(job)
    assert body["status"] == "NOT_RUN" and body["reason"] == "engine: CalledProcessError"
    assert int(marker.read_text()) == MAX_ATTEMPTS


def test_crash_twice_then_success_counts_attempts(job, tmp_path, monkeypatch):
    marker = tmp_path / "count"
    monkeypatch.setenv("CRASH_MARKER", str(marker))
    monkeypatch.setenv("CRASH_TIMES", "2")
    body = MpsRuntime(engine_path=_fake_engine(tmp_path, CRASH_ENGINE + "\n" + OK_ENGINE)).run(job)
    assert body["status"] == "OK" and body["backend"]["attempts"] == 3


def test_engine_error_is_not_retried(job, tmp_path, monkeypatch):
    marker = tmp_path / "count"
    monkeypatch.setenv("CRASH_MARKER", str(marker))
    engine = _fake_engine(tmp_path, '''
import argparse, json, pathlib, os
p = argparse.ArgumentParser(); p.add_argument("--job"); p.add_argument("--out"); p.add_argument("--kind"); a = p.parse_args()
open(os.environ["CRASH_MARKER"], "a").write("x")
pathlib.Path(a.out).write_text(json.dumps({"error": {"type": "TargetUnavailable"}}))
''')
    body = StatevectorRuntime(engine_path=engine).run(job)
    assert body["status"] == "NOT_RUN" and body["reason"] == "engine: TargetUnavailable" and marker.read_text() == "x"


def test_timeout_is_not_run(job, tmp_path):
    engine = _fake_engine(tmp_path, "import time\ntime.sleep(30)\n")
    assert StatevectorRuntime(engine_path=engine, wall_s_override=1).run(job)["reason"] == "engine: TimeoutExpired"


def test_statevector_refuses_above_cap(job, tmp_path):
    body = StatevectorRuntime(engine_path=_fake_engine(tmp_path, OK_ENGINE)).run(_big(job))
    assert body["status"] == "REFUSED" and "statevector" in body["reason"]


def test_select_kind(job):
    assert select_kind("auto", job) == "statevector"
    assert select_kind("auto", replace(job, backend_kind="mps")) == "mps"
    assert select_kind("mps", job) == "mps"
    assert select_kind("auto", _big(job)) == "mps"
    assert select_kind("auto", replace(job, backend_kind="ibm")) == "ibm"
    with pytest.raises(ValueError):
        select_kind("gpu", job)


def test_child_env_has_single_thread_and_job_round_trips(job, tmp_path):
    engine = _fake_engine(tmp_path, '''
import argparse, json, pathlib, os, sys
p = argparse.ArgumentParser(); p.add_argument("--job"); p.add_argument("--out"); p.add_argument("--kind"); a = p.parse_args()
assert os.environ["OMP_NUM_THREADS"] == "1"
from nano.loop.qsearch_job import parse_job      # PYTHONPATH must make nano importable
parse_job(json.loads(pathlib.Path(a.job).read_text(encoding="utf-8")))
pathlib.Path(a.out).write_text(json.dumps({"error": {"type": "Probe"}}))
''')
    assert StatevectorRuntime(engine_path=engine).run(job)["reason"] == "engine: Probe"
```

- [ ] **Step 2: Run** → fails. **Step 3: Implement**

```python
# nano/loop/runtime_mps.py
"""Statevector and MPS runtimes: spawn the engine child, retry crashes, never raise.

The parent is pure stdlib. Every Aer job runs in a fresh interpreter with
``OMP_NUM_THREADS=1`` (Aer MPS access-violates intermittently on some hosts);
a crash is retried up to ``MAX_ATTEMPTS`` times, a deterministic engine error
or a timeout becomes a ``NOT_RUN`` receipt whose reason is the exception class
name only. ``select_kind`` maps the service default and the job's request to a
concrete runtime kind (``auto`` → statevector for n ≤ 20, else MPS).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .qsearch_job import QSearchJob
from .qsearch_receipt import not_run_receipt, ok_receipt, refused_receipt

ENGINE_PATH = Path(__file__).resolve().parent / "_qsearch_engine.py"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
STATEVECTOR_CAP = 20
MAX_ATTEMPTS = 5          # one run plus four retries
DEFAULT_WALL_S = 600
MAX_WALL_S = 3600
SERVICE_KINDS = ("auto", "mps", "statevector", "ibm")


def select_kind(service_kind: str, job: QSearchJob) -> str:
    if service_kind not in SERVICE_KINDS:
        raise ValueError(f"service backend must be one of {SERVICE_KINDS}")
    if service_kind != "auto":
        return service_kind
    if job.backend_kind != "auto":
        return job.backend_kind
    return "statevector" if job.n <= STATEVECTOR_CAP else "mps"


def _thaw(value: Any) -> Any:
    """Turn the frozen job payload (MappingProxyType / tuples) back into plain JSON types."""
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    return value


class _ChildRuntime:
    kind = ""

    def __init__(self, *, engine_path: Path = ENGINE_PATH, python: str | None = None,
                 wall_s_override: int | None = None) -> None:
        self._engine = Path(engine_path)
        self._python = python or os.environ.get("NANO_QSEARCH_PYTHON") or sys.executable
        self._wall_override = wall_s_override

    def _timeout(self, job: QSearchJob) -> int:
        if self._wall_override is not None:
            return self._wall_override
        return min(job.max_wall_s or DEFAULT_WALL_S, MAX_WALL_S)

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["OMP_NUM_THREADS"] = "1"
        parts = [str(PACKAGE_ROOT)]
        atlas = env.get("NANO_QSEARCH_ATLAS_PATH")
        if atlas:
            parts.insert(0, atlas)
        if env.get("PYTHONPATH"):
            parts.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(parts)
        return env

    def _attempt(self, job: QSearchJob, workdir: Path) -> dict[str, Any] | None:
        """One child run: the result dict, or None when the child crashed (retry)."""
        job_path, out_path = workdir / "job.json", workdir / "out.json"
        job_path.write_text(json.dumps(_thaw(job.payload), sort_keys=True), encoding="utf-8")
        if out_path.exists():
            out_path.unlink()
        cmd = [self._python, str(self._engine), "--job", str(job_path), "--out", str(out_path), "--kind", self.kind]
        proc = subprocess.run(cmd, env=self._env(), capture_output=True, timeout=self._timeout(job), check=False)
        if proc.returncode != 0 or not out_path.exists():
            return None
        return json.loads(out_path.read_text(encoding="utf-8"))

    def run(self, job: QSearchJob) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="nano-qsearch-") as tmp:
            workdir = Path(tmp)
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    result = self._attempt(job, workdir)
                except subprocess.TimeoutExpired:
                    return not_run_receipt(job, "engine: TimeoutExpired")
                except (OSError, ValueError) as exc:
                    return not_run_receipt(job, f"engine: {type(exc).__name__}")
                if result is None:
                    continue
                if "error" in result:
                    return not_run_receipt(job, f"engine: {result['error'].get('type', 'EngineError')}")
                return ok_receipt(job, {**result, "attempts": attempt})
        return not_run_receipt(job, "engine: CalledProcessError")


class StatevectorRuntime(_ChildRuntime):
    kind = "statevector"

    def run(self, job: QSearchJob) -> dict[str, Any]:
        if job.n > STATEVECTOR_CAP:
            return refused_receipt(job, f"statevector: {job.n} variables exceed the {STATEVECTOR_CAP}-qubit cap")
        return super().run(job)


class MpsRuntime(_ChildRuntime):
    kind = "mps"


def build_runtime(kind: str, **options: Any):
    if kind == "statevector":
        return StatevectorRuntime(**options)
    if kind == "mps":
        return MpsRuntime(**options)
    if kind == "ibm":
        from .runtime_ibm import IbmRuntime
        return IbmRuntime(**options)
    raise ValueError(f"unknown runtime kind {kind!r}")


__all__ = ["DEFAULT_WALL_S", "ENGINE_PATH", "MAX_ATTEMPTS", "MAX_WALL_S", "MpsRuntime", "SERVICE_KINDS",
           "STATEVECTOR_CAP", "StatevectorRuntime", "build_runtime", "select_kind"]
```

- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat(qsearch): statevector and MPS runtimes with crash retries`.

---

## Task 9: IBM runtime (refuse by default)

**Files:** Create `nano/loop/runtime_ibm.py`; Test `tests/test_qsearch_ibm.py`.

Gates in order, each producing a `REFUSED` receipt (never NOT_RUN, never an exception): `AETHER_AQRC_HW_GO != "1"` → `"ibm: AETHER_AQRC_HW_GO=1 is required"`; `AETHER_AQRC_AUTHORITY_PATH` unset or not an existing file → `"ibm: AETHER_AQRC_AUTHORITY_PATH must name an existing authority file"`; `job.max_qpu_s <= 0` → `"ibm: job grants no QPU seconds (limits.max_qpu_s)"`; `job.target not in ("fake_fez", "ibm_fez")` → `"ibm: target must be one of [...]"`. Only when all pass: `compile_fn(job)` (default: `qsearch_compile.compile_program` imported lazily — the same compile code as the simulators), then `submit_fn(job, compiled)`. The default `submit_fn` is `atlas_campaign_submit`, which imports `aether_atlas.aqrc.campaign_runtime` (proving the delegate is wired) and returns `refused_receipt(job, "ibm: ATLAS campaign_runtime admits only compiler_validation children; q-search purpose is not qualified (hand-off)")` — because `campaign_runtime.prepare_child` today requires a four-PUB two-pole compiler-validation child and no q-search stage purpose exists (see hand-off notes). No test exercises the post-gate path with real ATLAS; tests inject `compile_fn`/`submit_fn` fakes and assert the fakes are **not** called when a gate refuses.

- [ ] **Step 1: Failing tests**

```python
# tests/test_qsearch_ibm.py
import json
from dataclasses import replace

import pytest

from nano.loop.qsearch_job import parse_job
from nano.loop.runtime_ibm import IbmRuntime


@pytest.fixture
def job(fixtures_dir):
    j = parse_job(json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8")))
    return replace(j, target="fake_fez", max_qpu_s=5)


@pytest.fixture
def spy():
    calls = []
    def compile_fn(job):
        calls.append(("compile", job.job_digest)); return "compiled"
    def submit_fn(job, compiled):
        calls.append(("submit", compiled)); return {"schema": "x", "status": "OK"}
    return calls, compile_fn, submit_fn


def test_refuses_without_go_flag(job, spy, monkeypatch, tmp_path):
    calls, c, s = spy
    monkeypatch.delenv("AETHER_AQRC_HW_GO", raising=False)
    auth = tmp_path / "authority.json"; auth.write_text("{}")
    monkeypatch.setenv("AETHER_AQRC_AUTHORITY_PATH", str(auth))
    body = IbmRuntime(compile_fn=c, submit_fn=s).run(job)
    assert body["status"] == "REFUSED" and "AETHER_AQRC_HW_GO" in body["reason"] and body["backend"] == {"kind": "none"}
    assert calls == []


def test_refuses_without_authority_file(job, spy, monkeypatch, tmp_path):
    calls, c, s = spy
    monkeypatch.setenv("AETHER_AQRC_HW_GO", "1")
    monkeypatch.setenv("AETHER_AQRC_AUTHORITY_PATH", str(tmp_path / "missing.json"))
    assert "AUTHORITY_PATH" in IbmRuntime(compile_fn=c, submit_fn=s).run(job)["reason"]
    monkeypatch.delenv("AETHER_AQRC_AUTHORITY_PATH")
    assert "AUTHORITY_PATH" in IbmRuntime(compile_fn=c, submit_fn=s).run(job)["reason"]
    assert calls == []


def test_refuses_zero_qpu_budget_and_wrong_target(job, spy, monkeypatch, tmp_path):
    calls, c, s = spy
    monkeypatch.setenv("AETHER_AQRC_HW_GO", "1")
    auth = tmp_path / "authority.json"; auth.write_text("{}")
    monkeypatch.setenv("AETHER_AQRC_AUTHORITY_PATH", str(auth))
    assert "max_qpu_s" in IbmRuntime(compile_fn=c, submit_fn=s).run(replace(job, max_qpu_s=0))["reason"]
    assert "target" in IbmRuntime(compile_fn=c, submit_fn=s).run(replace(job, target="heron_heavy_hex"))["reason"]
    assert calls == []


def test_gates_open_then_delegates(job, spy, monkeypatch, tmp_path):
    calls, c, s = spy
    monkeypatch.setenv("AETHER_AQRC_HW_GO", "1")
    auth = tmp_path / "authority.json"; auth.write_text("{}")
    monkeypatch.setenv("AETHER_AQRC_AUTHORITY_PATH", str(auth))
    assert IbmRuntime(compile_fn=c, submit_fn=s).run(job) == {"schema": "x", "status": "OK"}
    assert calls == [("compile", job.job_digest), ("submit", "compiled")]


def test_delegate_exception_becomes_refused(job, monkeypatch, tmp_path):
    monkeypatch.setenv("AETHER_AQRC_HW_GO", "1")
    auth = tmp_path / "authority.json"; auth.write_text("{}")
    monkeypatch.setenv("AETHER_AQRC_AUTHORITY_PATH", str(auth))
    def boom(job): raise RuntimeError("secret detail")
    body = IbmRuntime(compile_fn=boom, submit_fn=lambda j, c: None).run(job)
    assert body["status"] == "REFUSED" and body["reason"] == "ibm: RuntimeError" and "secret" not in json.dumps(body)


def test_default_submit_refuses_unqualified_purpose(job):
    pytest.importorskip("aether_atlas")
    from nano.loop.runtime_ibm import atlas_campaign_submit
    body = atlas_campaign_submit(job, None)
    assert body["status"] == "REFUSED" and "campaign_runtime" in body["reason"]
```

- [ ] **Step 2: Run** → fails. **Step 3: Implement**

```python
# nano/loop/runtime_ibm.py
"""IBM hardware runtime: refuse by default, delegate when every gate is open.

No QPU call can happen from this module without ``AETHER_AQRC_HW_GO=1`` and an
authority file named by ``AETHER_AQRC_AUTHORITY_PATH`` (the ATLAS
``campaign_runtime`` conventions), a positive ``limits.max_qpu_s`` in the job
and a Fez target. Compilation reuses ``qsearch_compile.compile_program``;
submission is delegated to ATLAS ``aqrc.campaign_runtime``, which today admits
only compiler-validation children, so the delegate refuses with that reason
until a q-search stage purpose is qualified there. Every refusal is a REFUSED
receipt; an exception from a delegate is reported by class name only.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .qsearch_job import QSearchJob
from .qsearch_receipt import refused_receipt

GO_ENV = "AETHER_AQRC_HW_GO"
AUTHORITY_ENV = "AETHER_AQRC_AUTHORITY_PATH"
IBM_TARGETS = ("fake_fez", "ibm_fez")

CompileFn = Callable[[QSearchJob], Any]
SubmitFn = Callable[[QSearchJob, Any], dict[str, Any]]


def default_compile(job: QSearchJob) -> Any:
    from .qsearch_compile import compile_program
    return compile_program(job)


def atlas_campaign_submit(job: QSearchJob, compiled: Any) -> dict[str, Any]:
    """Delegate to ATLAS ``campaign_runtime``; refuses until a q-search purpose is qualified there."""
    from aether_atlas.aqrc import campaign_runtime  # noqa: F401 - the wiring is the point
    del compiled
    return refused_receipt(job, "ibm: ATLAS campaign_runtime admits only compiler_validation children; "
                                "q-search purpose is not qualified (hand-off)")


class IbmRuntime:
    kind = "ibm"

    def __init__(self, *, compile_fn: CompileFn = default_compile, submit_fn: SubmitFn = atlas_campaign_submit,
                 environ: Mapping[str, str] | None = None) -> None:
        self._compile = compile_fn
        self._submit = submit_fn
        self._environ = environ if environ is not None else os.environ

    def _gate(self, job: QSearchJob) -> str | None:
        if self._environ.get(GO_ENV) != "1":
            return f"ibm: {GO_ENV}=1 is required"
        authority = self._environ.get(AUTHORITY_ENV)
        if not authority or not Path(authority).is_file():
            return f"ibm: {AUTHORITY_ENV} must name an existing authority file"
        if job.max_qpu_s <= 0:
            return "ibm: job grants no QPU seconds (limits.max_qpu_s)"
        if job.target not in IBM_TARGETS:
            return f"ibm: target must be one of {list(IBM_TARGETS)}"
        return None

    def run(self, job: QSearchJob) -> dict[str, Any]:
        reason = self._gate(job)
        if reason is not None:
            return refused_receipt(job, reason)
        try:
            compiled = self._compile(job)
            return self._submit(job, compiled)
        except Exception as exc:  # noqa: BLE001 - class name only, never the message
            return refused_receipt(job, f"ibm: {type(exc).__name__}")


__all__ = ["AUTHORITY_ENV", "GO_ENV", "IBM_TARGETS", "IbmRuntime", "atlas_campaign_submit", "default_compile"]
```

- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat(qsearch): IbmRuntime that refuses without GO flag and authority`.

---

## Task 10: Service core (auth → parse → store → run → seal), recovery, single-flight

**Files:** Create `nano/loop/qsearch_service.py` (service class + HTTP + `main`); Test `tests/test_qsearch_service.py`.

Config (`ServiceConfig.from_env()`): `NANO_QSEARCH_BIND` (default `127.0.0.1:8798`), `NANO_QSEARCH_BACKEND` (`auto`), `NANO_QSEARCH_DATA_DIR` (required), `NANO_QSEARCH_KEY_PATH` (receipt key file, ≥16 raw bytes, required), `NANO_QSEARCH_REQUEST_KEY_PATH` (required, must differ from the receipt key), `NANO_QSEARCH_MAX_VARIABLES` (`40`), `NANO_QSEARCH_MAX_BODY_BYTES` (`1048576`). CLI flags `--bind --backend --data-dir --key-path --request-key-path --verbose` override env.

`QSearchService.submit(method, path, body: bytes, headers: Mapping[str,str], *, now) -> tuple[int, dict]`:
1. `verify_request` → on `AuthError` return `(401, {"error": "unauthorized", "code": e.code})` — no receipt, nothing stored.
2. `len(body) > max_body` → `(413, {"error": "body too large"})`.
3. `json.loads(body, parse_constant=reject)` + `parse_job` → on failure `(400, {"error": "invalid job", "code": ...})`.
4. `store.get(job_digest)` DONE → `(200, stored receipt)` without recomputing.
5. Single-flight: if another thread is computing this digest, wait for its event, then return the stored receipt.
6. `store.begin`; `kind = select_kind(config.backend, job)`; if `job.n > config.max_variables` → `refused_receipt(job, "limits: ... exceed the service cap ...")`, else `runtime.run(job)`.
7. `seal_receipt(body, receipt_key)`; `store.complete`; `(200, sealed)`.

`QSearchService.recover() -> list[str]`: for each `store.pending()` record, `parse_job(json.loads(job_json))`, `mark_attempt`, run + seal + complete; returns the digests recovered. Called by `main()` before serving.

HTTP: `ThreadingHTTPServer` + `BaseHTTPRequestHandler`; only `POST /qsearch/v1/submit` (anything else 404); reads `Content-Length` bytes (413 if over cap); passes `self.headers.get(AUTH_HEADER)`; writes JSON with `Content-Type: application/json`; `log_message` is silenced unless `--verbose`. Also `prune_nonces(before_ts=now − 2*WINDOW_S)` once per authenticated request.

- [ ] **Step 1: Failing tests**

```python
# tests/test_qsearch_service.py
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from nano.loop.qsearch_auth import AUTH_HEADER, sign_request
from nano.loop.qsearch_receipt import ok_receipt, verify_seal
from nano.loop.qsearch_service import QSearchService, ServiceConfig, serve_in_thread
from nano.loop.qsearch_store import JobStore

RECEIPT_KEY = b"receipt-key-" + b"0" * 20
REQUEST_KEY = b"request-key-" + b"1" * 20
PATH = "/qsearch/v1/submit"


class FakeRuntime:
    kind = "statevector"

    def __init__(self):
        self.calls = 0
        self.slow = threading.Event(); self.slow.set()

    def run(self, job):
        self.calls += 1
        self.slow.wait()
        return ok_receipt(job, {"kind": "statevector", "backend_id": "fake", "variant": "ordinary", "nuclei": [],
                                "compiled_plan_digest": "sha256:" + "a" * 64, "compile": {}, "metrics": {"logical_2q": 0, "native_2q": 0, "native_depth": 0},
                                "chi_max": None, "chi_reached": None, "truncated": None, "shots": 1, "seed": 7, "wall_s": 0.0, "attempts": 1,
                                "samples": [["0000", 1, job.classical_min_energy]], "best_sample_energy": job.classical_min_energy,
                                "mean_energy_sample": 0.0, "mean_energy_ideal": None, "tvd_ideal": None, "exact_min_energy": None})


def _config(tmp_path, **over):
    base = dict(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path, receipt_key=RECEIPT_KEY, request_key=REQUEST_KEY)
    return ServiceConfig(**{**base, **over})


@pytest.fixture
def body_bytes(fixtures_dir):
    return (fixtures_dir / "job_4var.json").read_bytes()


@pytest.fixture
def service(tmp_path):
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)
    yield svc, rt
    svc.close()


def _headers(body, ts, nonce="ab" * 16, key=REQUEST_KEY):
    return {AUTH_HEADER: sign_request(key, "POST", PATH, body, ts=ts, nonce=nonce)}


def test_round_trip_sealed_ok(service, body_bytes):
    svc, rt = service
    status, receipt = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
    assert status == 200 and receipt["status"] == "OK"
    verify_seal(receipt, RECEIPT_KEY)
    assert receipt["job_digest"] == json.loads(body_bytes)["job_digest"] and rt.calls == 1


def test_unauthenticated_gets_401_and_no_receipt(service, body_bytes):
    svc, rt = service
    status, out = svc.submit("POST", PATH, body_bytes, {}, now=1000)
    assert status == 401 and "seal" not in out and "job_digest" not in out and rt.calls == 0
    status, out = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, key=b"wrong-key-" + b"9" * 22), now=1000)
    assert status == 401 and rt.calls == 0
    assert svc.store.get(json.loads(body_bytes)["job_digest"]) is None


def test_replay_rejected(service, body_bytes):
    svc, rt = service
    h = _headers(body_bytes, 1000)
    assert svc.submit("POST", PATH, body_bytes, h, now=1000)[0] == 200
    status, out = svc.submit("POST", PATH, body_bytes, h, now=1001)
    assert status == 401 and out["code"] == "REPLAY" and rt.calls == 1


def test_stale_timestamp_rejected(service, body_bytes):
    svc, rt = service
    assert svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1300)[0] == 401 and rt.calls == 0


def test_identical_resubmission_returns_stored_receipt(service, body_bytes):
    svc, rt = service
    _, first = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce="aa" * 16), now=1000)
    _, second = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1001, nonce="bb" * 16), now=1001)
    assert first == second and rt.calls == 1


def test_invalid_job_is_400_without_receipt(service, body_bytes):
    svc, rt = service
    bad = body_bytes.replace(b'"predator.qsearch.job.v1"', b'"nope"')
    status, out = svc.submit("POST", PATH, bad, _headers(bad, 1000), now=1000)
    assert status == 400 and "seal" not in out and rt.calls == 0
    status, out = svc.submit("POST", PATH, b"{nope", _headers(b"{nope", 1000, nonce="cc" * 16), now=1000)
    assert status == 400


def test_over_service_cap_is_refused_receipt(tmp_path, body_bytes):
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path, max_variables=3), runtime_factory=lambda kind: rt)
    try:
        status, receipt = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
        assert status == 200 and receipt["status"] == "REFUSED" and rt.calls == 0
        verify_seal(receipt, RECEIPT_KEY)
    finally:
        svc.close()


def test_single_flight_concurrent_identical_jobs(service, body_bytes):
    svc, rt = service
    rt.slow.clear()
    results = []
    def go(nonce):
        results.append(svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000, nonce=nonce), now=1000))
    threads = [threading.Thread(target=go, args=(f"{i:02d}" * 16,)) for i in range(4)]
    for t in threads: t.start()
    time.sleep(0.2); rt.slow.set()
    for t in threads: t.join()
    assert rt.calls == 1 and len({json.dumps(r[1], sort_keys=True) for r in results}) == 1


def test_crash_recovery_reruns_once_with_same_digest(tmp_path, body_bytes):
    digest = json.loads(body_bytes)["job_digest"]
    # simulate: accepted, then the process died before complete
    store = JobStore(tmp_path / "qsearch.sqlite3"); store.begin(digest, body_bytes.decode("utf-8")); store.close()
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)
    try:
        assert svc.recover() == [digest] and rt.calls == 1
        rec = svc.store.get(digest)
        assert rec.state == "DONE" and rec.attempts == 1
        stored = json.loads(rec.receipt_json)
        verify_seal(stored, RECEIPT_KEY)
        # a later identical submission returns the recovered receipt, no second run
        _, again = svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
        assert again == stored and rt.calls == 1
        assert svc.recover() == [] and rt.calls == 1
    finally:
        svc.close()


def test_runtime_selection_uses_service_default(tmp_path, body_bytes):
    kinds = []
    class RT(FakeRuntime):
        def __init__(self, kind): super().__init__(); self.kind = kind
    svc = QSearchService(_config(tmp_path, backend="mps"), runtime_factory=lambda kind: (kinds.append(kind), RT(kind))[1])
    try:
        svc.submit("POST", PATH, body_bytes, _headers(body_bytes, 1000), now=1000)
        assert kinds == ["mps"]
    finally:
        svc.close()


def test_same_key_for_both_purposes_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        _config(tmp_path, request_key=RECEIPT_KEY)


def test_http_round_trip_over_localhost(tmp_path, body_bytes):
    rt = FakeRuntime()
    svc = QSearchService(_config(tmp_path), runtime_factory=lambda kind: rt)
    server, thread = serve_in_thread(svc)
    try:
        url = f"http://127.0.0.1:{server.server_port}{PATH}"
        now = int(time.time())
        req = urllib.request.Request(url, data=body_bytes, method="POST",
                                     headers={"Content-Type": "application/json", **_headers(body_bytes, now)})
        with urllib.request.urlopen(req, timeout=10) as resp:
            receipt = json.loads(resp.read())
        verify_seal(receipt, RECEIPT_KEY)
        assert receipt["status"] == "OK"
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(urllib.request.Request(url, data=body_bytes, method="POST"), timeout=10)
        assert info.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(urllib.request.Request(url.replace("submit", "x"), data=b"{}", method="POST"), timeout=10)
        assert info.value.code == 404
    finally:
        server.shutdown(); thread.join(); svc.close()
```

- [ ] **Step 2: Run** → fails. **Step 3: Implement**

```python
# nano/loop/qsearch_service.py
"""The q-search HTTP service: ``POST /qsearch/v1/submit``.

Order of operations for one request: authenticate (``qsearch_auth``; a
failure is 401 with no receipt and nothing stored), parse the job
(``qsearch_job``; a failure is 400 with no receipt), look the ``job_digest``
up in the durable store (a DONE job returns its stored receipt without
recomputing), run the selected runtime exactly once per digest (single-flight
across threads), seal the receipt (``qsearch_receipt``) and store it. On
startup ``recover()`` re-runs every job that was accepted but never completed,
once, with the same seed, so a crash never yields a second divergent result.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .qsearch_auth import AUTH_HEADER, WINDOW_S, AuthError, verify_request
from .qsearch_canonical import canonical_json
from .qsearch_job import JobValidationError, QSearchJob, parse_job
from .qsearch_receipt import MIN_KEY_BYTES, refused_receipt, seal_receipt
from .qsearch_store import JobStore
from .runtime_mps import SERVICE_KINDS, build_runtime, select_kind

SUBMIT_PATH = "/qsearch/v1/submit"
DEFAULT_BIND = "127.0.0.1:8798"
DEFAULT_MAX_VARIABLES = 40
DEFAULT_MAX_BODY_BYTES = 1_048_576
STORE_FILE = "qsearch.sqlite3"


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite JSON constant {name}")


def _read_key(path: str, name: str) -> bytes:
    data = Path(path).read_bytes()
    if len(data) < MIN_KEY_BYTES:
        raise ValueError(f"{name} must hold at least {MIN_KEY_BYTES} bytes")
    return data


@dataclass(frozen=True)
class ServiceConfig:
    bind: str
    backend: str
    data_dir: Path
    receipt_key: bytes
    request_key: bytes
    max_variables: int = DEFAULT_MAX_VARIABLES
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    verbose: bool = False

    def __post_init__(self) -> None:
        if self.backend not in SERVICE_KINDS:
            raise ValueError(f"backend must be one of {SERVICE_KINDS}")
        for name, key in (("receipt_key", self.receipt_key), ("request_key", self.request_key)):
            if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
                raise ValueError(f"{name} must be at least {MIN_KEY_BYTES} bytes")
        if bytes(self.receipt_key) == bytes(self.request_key):
            raise ValueError("receipt key and request key must differ")
        host, _, port = self.bind.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError("bind must be host:port")

    @property
    def host_port(self) -> tuple[str, int]:
        host, _, port = self.bind.rpartition(":")
        return host, int(port)

    @classmethod
    def from_env(cls, args: argparse.Namespace | None = None) -> "ServiceConfig":
        env = os.environ

        def pick(flag: str, var: str, default: str | None = None) -> str | None:
            value = getattr(args, flag, None) if args is not None else None
            return value if value else env.get(var, default)

        data_dir = pick("data_dir", "NANO_QSEARCH_DATA_DIR")
        key_path = pick("key_path", "NANO_QSEARCH_KEY_PATH")
        request_key_path = pick("request_key_path", "NANO_QSEARCH_REQUEST_KEY_PATH")
        if not data_dir or not key_path or not request_key_path:
            raise ValueError("NANO_QSEARCH_DATA_DIR, NANO_QSEARCH_KEY_PATH and NANO_QSEARCH_REQUEST_KEY_PATH are required")
        return cls(bind=pick("bind", "NANO_QSEARCH_BIND", DEFAULT_BIND), backend=pick("backend", "NANO_QSEARCH_BACKEND", "auto"),
                   data_dir=Path(data_dir), receipt_key=_read_key(key_path, "NANO_QSEARCH_KEY_PATH"),
                   request_key=_read_key(request_key_path, "NANO_QSEARCH_REQUEST_KEY_PATH"),
                   max_variables=int(env.get("NANO_QSEARCH_MAX_VARIABLES", DEFAULT_MAX_VARIABLES)),
                   max_body_bytes=int(env.get("NANO_QSEARCH_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)),
                   verbose=bool(getattr(args, "verbose", False)))


RuntimeFactory = Callable[[str], Any]


class QSearchService:
    def __init__(self, config: ServiceConfig, *, runtime_factory: RuntimeFactory = build_runtime) -> None:
        self.config = config
        self.store = JobStore(config.data_dir / STORE_FILE)
        self._runtime_factory = runtime_factory
        self._runtimes: dict[str, Any] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self.store.close()

    def _runtime(self, kind: str):
        with self._lock:
            if kind not in self._runtimes:
                self._runtimes[kind] = self._runtime_factory(kind)
            return self._runtimes[kind]

    def _execute(self, job: QSearchJob) -> dict[str, Any]:
        """Run (or refuse) one job and return the SEALED receipt; never raises."""
        if job.n > self.config.max_variables:
            unsigned = refused_receipt(job, f"limits: {job.n} variables exceed the service cap of {self.config.max_variables}")
        else:
            unsigned = self._runtime(select_kind(self.config.backend, job)).run(job)
        return seal_receipt(unsigned, self.config.receipt_key)

    def _run_once(self, job: QSearchJob) -> dict[str, Any]:
        """Single-flight by digest: compute once, store, and share the stored receipt."""
        with self._lock:
            record = self.store.get(job.job_digest)
            if record is not None and record.state == "DONE":
                return json.loads(record.receipt_json)
            waiter = self._inflight.get(job.job_digest)
            owner = waiter is None
            if owner:
                waiter = self._inflight[job.job_digest] = threading.Event()
        if not owner:
            waiter.wait()
            return json.loads(self.store.get(job.job_digest).receipt_json)
        try:
            self.store.begin(job.job_digest, canonical_json(job.payload))
            sealed = self._execute(job)
            self.store.complete(job.job_digest, json.dumps(sealed, sort_keys=True, allow_nan=False))
            return sealed
        finally:
            with self._lock:
                self._inflight.pop(job.job_digest, None)
            waiter.set()

    def submit(self, method: str, path: str, body: bytes, headers: Mapping[str, str | None], *,
               now: int | None = None) -> tuple[int, dict[str, Any]]:
        now = int(time.time()) if now is None else now
        try:
            verify_request(self.config.request_key, headers.get(AUTH_HEADER), method, path, body, now=now, nonce_store=self.store)
        except AuthError as exc:
            return 401, {"error": "unauthorized", "code": exc.code}
        self.store.prune_nonces(before_ts=now - 2 * WINDOW_S)
        if len(body) > self.config.max_body_bytes:
            return 413, {"error": "body too large"}
        try:
            job = parse_job(json.loads(body.decode("utf-8"), parse_constant=_reject_constant))
        except JobValidationError as exc:
            return 400, {"error": "invalid job", "code": exc.code}
        except (ValueError, UnicodeDecodeError, RecursionError):
            return 400, {"error": "invalid job", "code": "JSON"}
        return 200, self._run_once(job)

    def recover(self) -> list[str]:
        recovered = []
        for record in self.store.pending():
            job = parse_job(json.loads(record.job_json))
            self.store.mark_attempt(job.job_digest)
            sealed = self._execute(job)
            self.store.complete(job.job_digest, json.dumps(sealed, sort_keys=True, allow_nan=False))
            recovered.append(job.job_digest)
        return recovered


class _Handler(BaseHTTPRequestHandler):
    service: QSearchService  # set per server by make_server

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path != SUBMIT_PATH:
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"error": "bad content length"})
            return
        if length > self.service.config.max_body_bytes:
            self._send(413, {"error": "body too large"})
            return
        body = self.rfile.read(length)
        status, payload = self.service.submit("POST", self.path, body, {AUTH_HEADER: self.headers.get(AUTH_HEADER)})
        self._send(status, payload)

    def do_GET(self) -> None:  # noqa: N802
        self._send(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.service.config.verbose:
            super().log_message(fmt, *args)


def make_server(service: QSearchService) -> ThreadingHTTPServer:
    handler = type("QSearchHandler", (_Handler,), {"service": service})
    server = ThreadingHTTPServer(service.config.host_port, handler)
    server.daemon_threads = True
    return server


def serve_in_thread(service: QSearchService) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = make_server(service)
    thread = threading.Thread(target=server.serve_forever, name="nano-qsearch-http", daemon=True)
    thread.start()
    return server, thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nano-qsearch-serve", description="Serve POST /qsearch/v1/submit for Predator Drive.")
    parser.add_argument("--bind")
    parser.add_argument("--backend", choices=SERVICE_KINDS)
    parser.add_argument("--data-dir", dest="data_dir")
    parser.add_argument("--key-path", dest="key_path")
    parser.add_argument("--request-key-path", dest="request_key_path")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = ServiceConfig.from_env(args)
    except (OSError, ValueError) as exc:
        print(f"nano-qsearch-serve: {exc}", file=sys.stderr)
        return 2
    service = QSearchService(config)
    recovered = service.recover()
    server = make_server(service)
    print(f"nano-qsearch-serve: backend={config.backend} bind={server.server_address[0]}:{server.server_port} "
          f"recovered={len(recovered)}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()
    return 0


__all__ = ["DEFAULT_BIND", "QSearchService", "SUBMIT_PATH", "ServiceConfig", "main", "make_server", "serve_in_thread"]
```

- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat(qsearch): HTTP service with auth, durable store, single-flight and recovery`.

---

## Task 11: Packaging, CLI entry, docs

**Files:** Modify `pyproject.toml`; Create `docs/qsearch-service.md`; Test `tests/test_qsearch_packaging.py`.

- [ ] **Step 1: Test**

```python
# tests/test_qsearch_packaging.py
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_quantum_extra_and_script_declared():
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] == []
    extra = data["project"]["optional-dependencies"]["quantum"]
    assert any(d.startswith("qiskit==2.3.1") for d in extra) and any(d.startswith("qiskit-aer==0.17.2") for d in extra)
    assert data["project"]["scripts"]["nano-qsearch-serve"] == "nano.loop.qsearch_service:main"


def test_base_modules_import_without_quantum_packages():
    for name in ("qiskit", "qiskit_aer", "numpy", "aether_atlas"):
        sys.modules.pop(name, None)
    import nano.loop.qsearch_service  # noqa: F401
    import nano.loop.runtime_ibm  # noqa: F401
    import nano.loop.runtime_mps  # noqa: F401
    assert "qiskit" not in sys.modules and "aether_atlas" not in sys.modules


def test_docs_mention_every_env_var():
    text = (ROOT / "docs" / "qsearch-service.md").read_text(encoding="utf-8")
    for var in ("NANO_QSEARCH_BIND", "NANO_QSEARCH_BACKEND", "NANO_QSEARCH_DATA_DIR", "NANO_QSEARCH_KEY_PATH",
                "NANO_QSEARCH_REQUEST_KEY_PATH", "NANO_QSEARCH_ATLAS_PATH", "NANO_QSEARCH_PYTHON", "X-Aether-Qsearch-Auth",
                "AETHER_AQRC_HW_GO", "AETHER_AQRC_AUTHORITY_PATH"):
        assert var in text, var
```

- [ ] **Step 2: pyproject edits**

```toml
[project.scripts]
nano = "nano.cli.main:main"
nano-qsearch-serve = "nano.loop.qsearch_service:main"

[project.optional-dependencies]
provenance = ["aether-protocol-c"]
# Q-search service engines (Qiskit Aer statevector / MPS). The service itself is
# stdlib; only the child-process engine imports these. ATLAS is loaded by path
# (NANO_QSEARCH_ATLAS_PATH) and is not on PyPI.
quantum = ["qiskit==2.3.1", "qiskit-aer==0.17.2", "numpy>=1.26"]
dev = ["pytest>=7"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
  "engine: needs qiskit-aer and aether_atlas (system Python, run from PowerShell on Windows)",
  "integration: needs NANO_QSEARCH_PREDATOR_PATH pointing at a Predator-M checkout",
]
```

- [ ] **Step 3: docs/qsearch-service.md** — sections: What it is (one paragraph, the wire contract); Install (`pip install -e ".[quantum]"`, `NANO_QSEARCH_ATLAS_PATH`); Keys (two files, ≥16 random bytes, `python -c "import os; open('receipt.key','wb').write(os.urandom(32))"`, never the same bytes); Run (`nano-qsearch-serve --data-dir ... --key-path ... --request-key-path ...`, default bind `127.0.0.1:8798`, `NANO_QSEARCH_BACKEND=auto|mps|statevector|ibm`); Request auth (header format, message layout, ±120 s, nonce once; note that Predator's `NanoRuntimeQSearchBackend` opener does not send it yet — see hand-off); Responses (200 receipt / 401 / 400 / 413 / 404; REFUSED vs NOT_RUN); Durability and recovery (SQLite, `job_digest` idempotency, restart re-runs ACCEPTED once); Engine isolation (`NANO_QSEARCH_PYTHON`, `OMP_NUM_THREADS=1`, 4 retries, Windows/PowerShell note); IBM (`AETHER_AQRC_HW_GO`, `AETHER_AQRC_AUTHORITY_PATH`, refuses today, why); Testing (the pytest invocations below).

- [ ] **Step 4: Run full suite (plain, from Git Bash is fine for non-engine tests)**: `python -m pytest -q -m "not engine and not integration"`. Then PowerShell system Python with `NANO_QSEARCH_ATLAS_PATH` set: `& $py -m pytest tests -q -m engine`. **Step 5: Commit** `feat(qsearch): quantum extra, nano-qsearch-serve entry and operator doc`.

---

## Task 12: Predator integration test (real `from_payload` on a real receipt)

**Files:** Test `tests/test_qsearch_predator_integration.py`.

Marked `integration` **and** `engine`. Skips unless `NANO_QSEARCH_PREDATOR_PATH` is set and `qiskit_aer` + `aether_atlas` import. Starts a real `QSearchService` with `build_runtime` (real engine child), serves on an ephemeral localhost port with fake keys, submits `job_pair02.json` **through Predator's own** `NanoRuntimeQSearchBackend` (with an `opener` that adds the auth header, since Predator's default opener does not), and asserts `QSearchReceiptV1.from_payload` accepted it: `receipt.status == "OK"`, `receipt.backend_kind in ("statevector", "mps")`, `receipt.agree is not None`, `receipt.job_digest == job.job_digest`. Also asserts a second submission through the backend returns the identical payload digest (store hit) and that an unauthenticated opener yields `status == "NOT_RUN"` with reason starting `transport:`.

```python
# tests/test_qsearch_predator_integration.py
import json
import os
import secrets
import time
import urllib.request

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.engine]
if not os.environ.get("NANO_QSEARCH_PREDATOR_PATH"):
    pytest.skip("NANO_QSEARCH_PREDATOR_PATH not set", allow_module_level=True)
pytest.importorskip("qiskit_aer")
pytest.importorskip("aether_atlas")
qsearch_backend = pytest.importorskip("predator.drive.qsearch_backend")
qsearch_bridge = pytest.importorskip("predator_gen3.discovery.qsearch_bridge")

from nano.loop.qsearch_auth import AUTH_HEADER, sign_request          # noqa: E402
from nano.loop.qsearch_service import QSearchService, ServiceConfig, serve_in_thread   # noqa: E402

RECEIPT_KEY = secrets.token_bytes(32)
REQUEST_KEY = secrets.token_bytes(32)


def _job_from_fixture(fixtures_dir):
    body = json.loads((fixtures_dir / "job_pair02.json").read_text(encoding="utf-8"))
    c, b, l = body["compile"], body["backend"], body["limits"]
    policy = qsearch_bridge.QSearchPolicyV1(
        variant=c["variant"], p=c["p"], gammas=tuple(c["gammas"]), betas=tuple(c["betas"]), opt_level=c["opt_level"],
        seed=c["seed"], target=c["target"], training=c["training"], backend_kind=b["kind"], chi_max=b["chi_max"],
        shots=b["shots"], backend_seed=b["seed"], max_variables=l["max_variables"], max_wall_s=l["max_wall_s"],
        max_qpu_s=l["max_qpu_s"])
    job = qsearch_bridge.QSearchJobV1(compiled_search_digest=body["compiled_search_digest"], variables=tuple(body["variables"]),
                                      qubo=tuple(tuple(r) for r in body["qubo"]["q"]), offset=body["qubo"]["offset"], policy=policy,
                                      classical_min_energy=body["audit"]["classical_min_energy"])
    assert job.job_digest == body["job_digest"]
    return job


def _signing_opener(key):
    def opener(url, data, timeout):
        header = sign_request(key, "POST", "/qsearch/v1/submit", data, ts=int(time.time()), nonce=secrets.token_hex(16))
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", AUTH_HEADER: header})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    return opener


def test_predator_backend_accepts_sealed_receipt(fixtures_dir, tmp_path):
    cfg = ServiceConfig(bind="127.0.0.1:0", backend="auto", data_dir=tmp_path, receipt_key=RECEIPT_KEY, request_key=REQUEST_KEY)
    svc = QSearchService(cfg)
    server, thread = serve_in_thread(svc)
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}"
        job = _job_from_fixture(fixtures_dir)
        backend = qsearch_backend.NanoRuntimeQSearchBackend(endpoint, key=RECEIPT_KEY, timeout_s=900, opener=_signing_opener(REQUEST_KEY))
        receipt = backend.submit(job)
        assert receipt.status == "OK" and receipt.backend_kind in ("statevector", "mps") and receipt.agree is not None
        assert receipt.job_digest == job.job_digest and receipt.payload["variant"] in ("ordinary", "joint5")
        again = backend.submit(job)
        assert again.receipt_digest == receipt.receipt_digest
        plain = qsearch_backend.NanoRuntimeQSearchBackend(endpoint, key=RECEIPT_KEY, timeout_s=60)
        not_run = plain.submit(job)
        assert not_run.status == "NOT_RUN" and not_run.reason.startswith("transport:")
    finally:
        server.shutdown(); thread.join(); svc.close()
```

- [ ] Run (PowerShell, system Python, `NANO_QSEARCH_ATLAS_PATH` and `NANO_QSEARCH_PREDATOR_PATH` set) → pass. Commit `test(qsearch): Predator from_payload integration over localhost`.

---

## Task 13: Live service proof and hand-off notes

- [ ] Generate two key files in the scratchpad, start `nano-qsearch-serve` (PowerShell, `python -m nano.loop.qsearch_service --data-dir ... --key-path ... --request-key-path ... --bind 127.0.0.1:8798 --backend auto`, `NANO_QSEARCH_ATLAS_PATH` set), POST `job_pair02.json` with a signed header from a second PowerShell, verify the returned seal with Predator's `from_payload` from the read-only worktree, then kill the server mid-job once (start a job, kill the process, restart, observe `recovered=1` on the banner and a DONE row). Record the exact commands and output in this plan under "Evidence". Stop the server.
- [ ] Fill in the hand-off section below, commit `docs(qsearch): L3 evidence and hand-off notes`.

---

## Self-review (writing-plans checklist, done 2026-10-21)

- Spec coverage: deliverables 1–7 map to Tasks 2, 1, 6–8, 9, 10–11, 0, and the test modules in each task; the integration proof is Task 12; `variant auto` both-compile + smaller native count: Task 6; `backend.kind auto` rule: Task 8 `select_kind`; tolerance 1e-9: Task 3; child failure → NOT_RUN class name: Task 8; auth/replay/401: Tasks 4, 10; seal prefix + `from_payload` compatibility: Tasks 3, 12; durable store, identical resubmission, crash recovery: Tasks 5, 10; CLI entry: Task 11; `find_nuclei` + test: Task 0.
- Placeholders: none.
- Type consistency: `QSearchJob` field names (`compile_seed`, `backend_seed`, `backend_kind`, `max_qpu_s`) are used identically in Tasks 2, 6, 7, 8, 9, 10; ENGINE_RESULT keys in Tasks 3, 7, 8, 10 match; `Runtime.run` returns the unsigned body everywhere and the service seals.

## Deviations from this plan as executed

- **Package location.** The service lives in the top-level package `nano_qsearch/`, not `nano/loop/`. `tests/test_determinism_guards.py` forbids clock, environment, subprocess, network and third-party imports anywhere under `nano/`, and the service needs all of them. Module names dropped the `qsearch_` prefix (`nano_qsearch/job.py`, `receipt.py`, `auth.py`, `store.py`, `compile.py`, `_engine.py`, `runtime_mps.py`, `runtime_ibm.py`, `service.py`). Code blocks above still show the original paths.
- **pair02 nuclei.** `find_nuclei` on the dense K6 pair02 objective packs four edge-disjoint triangles, `[(0,1,2),(0,3,4),(1,3,5),(2,4,5)]`, not the hand-picked vertex-disjoint pair from the Fez campaign.
- **Compiled-plan digest.** QPY stores a process-wide circuit name and the transpile layout, both nondeterministic across processes; the digest hashes a layout-free copy under a fixed name and records `initial_layout` and `measurement_map` in its inspection block.
- **Bond dimension.** `chi_reached` is the peak bond dimension parsed from Aer's `MPS_log_data` (`chi_source: peak_log`); the final-state bonds under-report (pair02: final 6, peak 8). If the log cannot be trusted the receipt says `chi_source: final` and marks the run truncated; programs with no two-qubit gate report `no_2q`.
- **IBM submission.** `atlas_campaign_submit` imports ATLAS `aqrc.campaign_runtime` but does not call it; it returns REFUSED because no q-search stage purpose is qualified there (see Hand-off).
- **Sampling.** Samples come from the logical ATLAS program; the native ISA program is compiled for metrics and the digest only (equivalent for noiseless simulation).
- **Hardening beyond the plan** (from two-stage review): engine output is audited before a receipt is built (`ReceiptAuditError` → NOT_RUN); service caps on shots, chi and seeds return sealed REFUSED receipts; per-job wall budget across retries; least-privilege child environment; socket idle timeout; exclusive data-dir lock; bind before recovery; at most two attempts per job, an attempt counted only once a concurrency slot is held; recovery uses the runtime kind recorded at accept time and never runs hardware; generic 401 body.

## Evidence

All runs on 2026-10-21/22, Windows 10, system Python 3.12.10 (`<system Python 3.12>`), qiskit 2.3.1, qiskit-aer 0.17.2, numpy 1.26.4, SQLite 3.49.1, ATLAS at `feat/qcompiler-find-nuclei`, Predator-M read-only at `origin/main` 17ee3368 (wire-contract files unchanged since 0b949cab).

**Test suites** (from the worktree root, `NANO_QSEARCH_ATLAS_PATH`, `NANO_QSEARCH_PREDATOR_PATH`, `OMP_NUM_THREADS=1` set):

```
python -m pytest -q -W ignore::DeprecationWarning --ignore=tests/test_provenance.py
```

Result: `1468 passed, 1 skipped` (the skip is the unrelated README test-count check in `tests/test_docs.py`).

`tests/test_provenance.py` has 7 pre-existing failures on this host: the installed `aether_protocol_c` rejects float fields (`CommitmentError: ... float values are not canonically reproducible`). The file and `nano/bridge` are untouched by this branch. A plain venv with only `pytest` and `pip install -e .` (no qiskit, no ATLAS) collects everything and skips the engine tests.

**Live service** (`python -m nano_qsearch --data-dir ... --key-path ... --request-key-path ... --bind 127.0.0.1:8798`, two fresh 32-byte key files; client builds the job with Predator's own `QSearchJobV1`, signs the request and verifies the reply with Predator's `QSearchReceiptV1.from_payload`):

| Step | Result |
|---|---|
| Signed POST of the pair02 job (backend auto) | HTTP 200, `from_payload` accepted, status OK, backend statevector, variant joint5 (4 nuclei), native 2Q 87 / depth 66, agree true, best −18.032663316582912 vs floor −18.032663316582916, tvd_ideal 0.0288, 4.6 s |
| Identical resubmit | same `receipt_digest` `sha256:ae1173…5a61` in 0.11 s; store row DONE, attempts 1 |
| Unsigned POST | 401 `{"error": "unauthorized"}` |
| MPS server, job submitted, server and engine child killed with `taskkill /T /F` 2.5 s in | row ACCEPTED, attempts 1, kind mps, no receipt; client connection reset |
| Restart on the same data dir with default backend statevector | banner `recovered=1` after 6 s; row DONE, attempts 2, kind mps (recorded kind used), chi_reached 8 of 64, chi_source peak_log |
| Resubmit after recovery | HTTP 200, `from_payload` accepted, backend mps, agree true, tvd_ideal 0.0339, 0.56 s; attempts still 2 (no recompute) |
| Second instance on the same data dir | exit 2, `data directory is already in use by another nano-qsearch-serve` |

## Hand-off notes

- **Request for the orchestrator (Predator side; wire contract unchanged).** Predator's `NanoRuntimeQSearchBackend` default opener (`predator/drive/qsearch_backend.py::_urllib_opener`) sends no `X-Aether-Qsearch-Auth` header, so against this service it always yields `NOT_RUN transport: HTTPError` (401). Predator needs an opener that signs requests with a request key distinct from `PREDATOR_QSEARCH_KEY_PATH`, plus a config variable for that key path. The header format and a 10-line stdlib signer are in `docs/qsearch-service.md` ("Predator side"); the integration test proves Predator's `from_payload` accepts the sealed receipts once requests are signed. This is L2/L4 territory; nothing in Predator was changed here.
- **Timeouts.** Predator's client timeout (`PREDATOR_QSEARCH_TIMEOUT_S`, default 130 s) is below the service's default job budget (600 s when `limits.max_wall_s` is 0). A timed-out Predator call followed by a resubmit returns the stored receipt; recommend `PREDATOR_QSEARCH_TIMEOUT_S ≥ max_wall_s`.
- **`compiled_plan_digest`.** It is a Nano manifest digest (`nano.qsearch.compiled-plan.v1`: target, coupling map, basis, opt level, seed, variant, nuclei, layout-free QPY sha256, inspection), not an ATLAS `CompiledPlanV1` digest, which needs an IBM Target and `SamplerOptions`. Spec B3 calls it "the ATLAS CompiledPlanV1 digest"; a hardware lane (L4) that produces a real `CompiledPlanV1` should fill it from there.
- **IBM.** `IbmRuntime` refuses unless `AETHER_AQRC_HW_GO=1`, `AETHER_AQRC_AUTHORITY_PATH` names an existing file, `limits.max_qpu_s > 0` and the target is Fez. Even with every gate open, the ATLAS `campaign_runtime` delegate refuses: `prepare_child` admits only four-PUB two-pole compiler-validation children, and no q-search stage purpose exists there. Qualifying one is L4's work. No IBM or paid call was made.
- **Classical simulation only.** Every OK receipt here is statevector or MPS simulation on Qiskit Aer, labelled by `backend.kind`.
- **Not blocked.** No new credential was needed.
