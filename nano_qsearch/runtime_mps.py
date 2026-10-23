"""Statevector and MPS runtimes: spawn the engine child, retry crashes, never raise.

The parent is pure stdlib. Every Aer job runs in a fresh interpreter with
``OMP_NUM_THREADS=1`` (Aer MPS access-violates intermittently on some hosts)
and an environment stripped of key and IBM credential variables. A crash
(nonzero exit, no out file, or an out file that is not a JSON object) is
retried, ``MAX_ATTEMPTS`` runs in all, inside one wall-clock budget per job.
The child gets no pipes: stdout goes to the null device and stderr to a file
in the job's temp directory, whose tail is logged to the service's stderr on a
nonzero exit and never enters a receipt. A deterministic engine error, a
timeout, an OS error or a result that fails the receipt audit becomes a
``NOT_RUN`` receipt whose reason is the exception class name only; ``run``
never raises. ``select_kind`` maps the service default and the job's request
to a concrete runtime kind (``auto`` → statevector for n ≤ 20, else MPS).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .job import QSearchJob
from .receipt import ReceiptAuditError, not_run_receipt, ok_receipt, refused_receipt

ENGINE_PATH = Path(__file__).resolve().parent / "_engine.py"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]      # the directory that contains ``nano_qsearch``
STATEVECTOR_CAP = 20
MAX_ATTEMPTS = 5          # one run plus four retries
DEFAULT_WALL_S = 600
MAX_WALL_S = 3600
MIN_ATTEMPT_S = 1.0       # no child run is started with less than this much of the job's budget left
SERVICE_KINDS = ("auto", "mps", "statevector", "ibm")
STDERR_TAIL_BYTES = 400
# Never handed to the engine child: seal and request keys, and anything that could authorise IBM hardware.
SECRET_ENV_PREFIXES = ("NANO_QSEARCH_KEY", "NANO_QSEARCH_REQUEST_KEY", "AETHER_AQRC_", "QISKIT_IBM", "IBM_", "IBMQ", "QE_TOKEN",
                       "PREDATOR_QSEARCH_")
CHILD_OPTIONS = ("engine_path", "python", "wall_s_override")
IBM_OPTIONS = ("compile_fn", "submit_fn", "environ")


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


def _read_result(out_path: Path) -> dict[str, Any] | None:
    """The child's result object, or None when the file is missing, not JSON or not an object (a crash)."""
    try:
        result = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def _log_child_failure(returncode: int, stderr_path: Path) -> None:
    """One line on the service's stderr: the exit code and the tail of the child's stderr (never a receipt)."""
    try:
        with stderr_path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - STDERR_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        tail = ""
    tail = " | ".join(line.strip() for line in tail.splitlines() if line.strip())
    print(f"nano-qsearch: engine child exited {returncode}: {tail}", file=sys.stderr, flush=True)


def _error_type(result: Mapping[str, Any]) -> str:
    error = result["error"]
    name = error.get("type") if isinstance(error, Mapping) else None
    return name if isinstance(name, str) and name else "EngineError"


def _child_env() -> dict[str, str]:
    """``os.environ`` minus secrets, single-threaded, with ATLAS and this package on ``PYTHONPATH``."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(SECRET_ENV_PREFIXES)}
    env["OMP_NUM_THREADS"] = "1"
    parts = [str(PACKAGE_ROOT)]
    atlas = env.get("NANO_QSEARCH_ATLAS_PATH")
    if atlas:
        parts.insert(0, atlas)
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


class _ChildRuntime:
    kind = ""

    def __init__(self, *, engine_path: Path = ENGINE_PATH, python: str | None = None,
                 wall_s_override: float | None = None) -> None:
        if wall_s_override is not None and not wall_s_override > 0:
            raise ValueError("wall_s_override must be > 0")
        self._engine = Path(engine_path)
        self._python = python or os.environ.get("NANO_QSEARCH_PYTHON") or sys.executable
        self._wall_override = wall_s_override

    def _timeout(self, job: QSearchJob) -> float:
        if self._wall_override is not None:
            return self._wall_override
        return min(job.max_wall_s or DEFAULT_WALL_S, MAX_WALL_S)

    def _attempt(self, job: QSearchJob, workdir: Path, timeout: float) -> dict[str, Any] | None:
        """One child run: the result dict, or None when the child crashed (retry)."""
        job_path, out_path, err_path = workdir / "job.json", workdir / "out.json", workdir / "stderr.txt"
        job_path.write_text(json.dumps(_thaw(job.payload), sort_keys=True), encoding="utf-8")
        if out_path.exists():
            out_path.unlink()
        cmd = [self._python, str(self._engine), "--job", str(job_path), "--out", str(out_path), "--kind", self.kind]
        # No pipes: a grandchild that outlives the child can never block the wait.
        with err_path.open("wb") as err:
            proc = subprocess.run(cmd, env=_child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=err, timeout=timeout, check=False)
        if proc.returncode != 0:
            _log_child_failure(proc.returncode, err_path)
            return None
        return _read_result(out_path)

    def _receipt(self, job: QSearchJob, result: dict[str, Any], attempt: int) -> dict[str, Any]:
        if "error" in result:
            return not_run_receipt(job, f"engine: {_error_type(result)}")
        try:
            return ok_receipt(job, {**result, "attempts": attempt})
        except (ReceiptAuditError, KeyError, TypeError, ValueError) as exc:    # malformed or unauditable result
            return not_run_receipt(job, f"audit: {type(exc).__name__}")

    def run(self, job: QSearchJob) -> dict[str, Any]:
        try:
            return self._run(job)
        except OSError as exc:          # temp dir creation, job file write, cleanup: never raise
            return not_run_receipt(job, f"engine: {type(exc).__name__}")

    def _run(self, job: QSearchJob) -> dict[str, Any]:
        """Up to ``MAX_ATTEMPTS`` child runs, all inside one wall-clock budget for the job.

        A budget below ``MIN_ATTEMPT_S`` runs nothing, and a retry is started
        only while at least ``MIN_ATTEMPT_S`` of the budget is left. Each run
        is given exactly what is left, so no run outlives the budget.
        """
        budget = self._timeout(job)
        if budget < MIN_ATTEMPT_S:
            return not_run_receipt(job, "engine: TimeoutExpired")
        deadline = time.monotonic() + budget
        with tempfile.TemporaryDirectory(prefix="nano-qsearch-", ignore_cleanup_errors=True) as tmp:
            workdir = Path(tmp)
            for attempt in range(1, MAX_ATTEMPTS + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (attempt > 1 and remaining < MIN_ATTEMPT_S):
                    return not_run_receipt(job, "engine: TimeoutExpired")
                try:
                    result = self._attempt(job, workdir, remaining)
                except subprocess.TimeoutExpired:
                    return not_run_receipt(job, "engine: TimeoutExpired")
                except (OSError, ValueError) as exc:
                    return not_run_receipt(job, f"engine: {type(exc).__name__}")
                if result is None:
                    continue
                return self._receipt(job, result, attempt)
        return not_run_receipt(job, "engine: CalledProcessError")


class StatevectorRuntime(_ChildRuntime):
    kind = "statevector"

    def run(self, job: QSearchJob) -> dict[str, Any]:
        if job.n > STATEVECTOR_CAP:
            return refused_receipt(job, f"statevector: {job.n} variables exceed the {STATEVECTOR_CAP}-qubit cap")
        return super().run(job)


class MpsRuntime(_ChildRuntime):
    kind = "mps"


def _pick(options: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {k: v for k, v in options.items() if k in names}


def build_runtime(kind: str, **options: Any):
    """The runtime for ``kind``; each gets only its own options (child options never reach IBM, nor IBM's the child)."""
    unknown = sorted(set(options) - set(CHILD_OPTIONS) - set(IBM_OPTIONS))
    if unknown:
        raise TypeError(f"unknown runtime options {unknown}")
    if kind == "statevector":
        return StatevectorRuntime(**_pick(options, CHILD_OPTIONS))
    if kind == "mps":
        return MpsRuntime(**_pick(options, CHILD_OPTIONS))
    if kind == "ibm":
        from .runtime_ibm import IbmRuntime
        return IbmRuntime(**_pick(options, IBM_OPTIONS))
    raise ValueError(f"unknown runtime kind {kind!r}")


__all__ = ["CHILD_OPTIONS", "DEFAULT_WALL_S", "ENGINE_PATH", "IBM_OPTIONS", "MAX_ATTEMPTS", "MAX_WALL_S", "MpsRuntime",
           "PACKAGE_ROOT", "SECRET_ENV_PREFIXES", "SERVICE_KINDS", "STATEVECTOR_CAP", "StatevectorRuntime",
           "build_runtime", "select_kind"]
