"""IBM hardware runtime: refuse by default, delegate when every gate is open.

No QPU call can happen from this module without ``AETHER_AQRC_HW_GO=1`` and an
authority file named by ``AETHER_AQRC_AUTHORITY_PATH`` (the ATLAS
``campaign_runtime`` conventions), a positive ``limits.max_qpu_s`` in the job
and a Fez target. Compilation reuses ``nano_qsearch.compile.compile_program``;
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

from .job import QSearchJob
from .receipt import refused_receipt

GO_ENV = "AETHER_AQRC_HW_GO"
AUTHORITY_ENV = "AETHER_AQRC_AUTHORITY_PATH"
IBM_TARGETS = ("fake_fez", "ibm_fez")

CompileFn = Callable[[QSearchJob], Any]
SubmitFn = Callable[[QSearchJob, Any], dict[str, Any]]


def default_compile(job: QSearchJob) -> Any:
    from .compile import compile_program
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
