"""Statevector and MPS runtimes: spawn the engine child, retry crashes, never raise.

The parent is pure stdlib. Every Aer job runs in a fresh interpreter with
``OMP_NUM_THREADS=1`` (Aer MPS access-violates intermittently on some hosts);
a crash (nonzero exit, no out file, or an out file that is not a JSON object)
is retried up to ``MAX_ATTEMPTS`` times. A deterministic engine error, a
timeout or a result that fails the receipt audit becomes a ``NOT_RUN`` receipt
whose reason is the exception class name only. ``select_kind`` maps the
service default and the job's request to a concrete runtime kind (``auto`` →
statevector for n ≤ 20, else MPS).
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

from .job import QSearchJob
from .receipt import ReceiptAuditError, not_run_receipt, ok_receipt, refused_receipt

ENGINE_PATH = Path(__file__).resolve().parent / "_engine.py"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]      # the directory that contains ``nano_qsearch``
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


def _read_result(out_path: Path) -> dict[str, Any] | None:
    """The child's result object, or None when the file is missing, not JSON or not an object (a crash)."""
    try:
        result = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def _error_type(result: Mapping[str, Any]) -> str:
    error = result["error"]
    name = error.get("type") if isinstance(error, Mapping) else None
    return name if isinstance(name, str) and name else "EngineError"


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
        if proc.returncode != 0:
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


def build_runtime(kind: str, **options: Any):
    if kind == "statevector":
        return StatevectorRuntime(**options)
    if kind == "mps":
        return MpsRuntime(**options)
    if kind == "ibm":
        from .runtime_ibm import IbmRuntime
        return IbmRuntime(**options)
    raise ValueError(f"unknown runtime kind {kind!r}")


__all__ = ["DEFAULT_WALL_S", "ENGINE_PATH", "MAX_ATTEMPTS", "MAX_WALL_S", "MpsRuntime", "PACKAGE_ROOT",
           "SERVICE_KINDS", "STATEVECTOR_CAP", "StatevectorRuntime", "build_runtime", "select_kind"]
