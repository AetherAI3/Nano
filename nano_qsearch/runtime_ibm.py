"""IBM runtime: FakeFez dry runs by default, hardware refused until every gate is open.

Two targets, one code path. ``fake_fez`` is a dry run: the job is compiled with
``nano_qsearch.compile.compile_program`` and the ISA circuit goes through
ATLAS ``campaign_runtime.run_qsearch_dry`` (``SamplerV2`` local testing mode on
Aer); nothing is spent, so no GO, authority or QPU seconds are needed, and the
result is a real ordered-sample receipt sealed with the **hardware seal**
(``receipt.hardware_receipt``). ``ibm_fez`` keeps its gates exactly: no QPU
call can happen without ``AETHER_AQRC_HW_GO=1``, an authority file named by
``AETHER_AQRC_AUTHORITY_PATH``, and a positive ``limits.max_qpu_s``; even then
the delegate reports BLOCKED today, because the frozen campaign protocol has
no reservable q-search stage (lane L4 hand-off). The child purpose
(``qsearch`` or ``qcomms``) rides on the runtime, not on the frozen job body:
``NANO_QSEARCH_PURPOSE`` or ``IbmRuntime(purpose=...)``, and it is recorded in
the receipt's ``hardware.purpose``. Every refusal is a REFUSED receipt; an
exception from a delegate is reported by class name only.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .job import QSearchJob
from .receipt import hardware_receipt, refused_receipt

GO_ENV = "AETHER_AQRC_HW_GO"
AUTHORITY_ENV = "AETHER_AQRC_AUTHORITY_PATH"
PURPOSE_ENV = "NANO_QSEARCH_PURPOSE"
IBM_TARGETS = ("fake_fez", "ibm_fez")
DRY_RUN_TARGETS = ("fake_fez",)
PURPOSES = ("qsearch", "qcomms")
DEFAULT_PURPOSE = "qsearch"

CompileFn = Callable[[QSearchJob], Any]
SubmitFn = Callable[..., Mapping[str, Any]]     # submit_fn(job, compiled, purpose=...) -> ATLAS job receipt


def default_compile(job: QSearchJob) -> Any:
    from .compile import compile_program
    return compile_program(job)


def atlas_campaign_submit(job: QSearchJob, compiled: Any, *, purpose: str = DEFAULT_PURPOSE) -> Mapping[str, Any]:
    """Delegate to ATLAS ``campaign_runtime``: FakeFez dry run, or BLOCKED for ibm_fez.

    Returns the ``AqrcQSearchJobReceiptV1`` for a dry run. For ``ibm_fez`` it
    raises ``RuntimeError`` (reported as ``REFUSED ibm: RuntimeError``): the
    service holds no campaign ledger, no provider context and no plan-scoped
    authority object, and the protocol has no q-search stage to reserve.
    """
    from aether_atlas.aqrc import campaign_runtime
    child = campaign_runtime.prepare_qsearch_child(
        compiled.isa, purpose=purpose, target=job.target, shots=job.shots, job_digest=job.job_digest,
        compiled_plan_digest=compiled.compiled_plan_digest, request_id=job.job_digest[len("sha256:"):][:40])
    if job.target in DRY_RUN_TARGETS:
        return campaign_runtime.run_qsearch_dry(compiled.isa, child)
    raise RuntimeError("BLOCKED: ibm_fez q-search children have no reservable campaign stage; "
                       "run_qsearch_dry covers fake_fez only")


class IbmRuntime:
    kind = "ibm"

    def __init__(self, *, compile_fn: CompileFn = default_compile, submit_fn: SubmitFn = atlas_campaign_submit,
                 environ: Mapping[str, str] | None = None, purpose: str | None = None) -> None:
        self._compile = compile_fn
        self._submit = submit_fn
        self._environ = environ if environ is not None else os.environ
        self._purpose = purpose if purpose is not None else self._environ.get(PURPOSE_ENV, DEFAULT_PURPOSE)

    @property
    def purpose(self) -> str:
        return self._purpose

    def _gate(self, job: QSearchJob) -> str | None:
        if self._purpose not in PURPOSES:
            return f"ibm: purpose must be one of {list(PURPOSES)}"
        if job.target not in IBM_TARGETS:
            return f"ibm: target must be one of {list(IBM_TARGETS)}"
        if job.target in DRY_RUN_TARGETS:
            return None                       # a FakeFez dry run spends nothing: no GO, authority or QPU seconds
        if self._environ.get(GO_ENV) != "1":
            return f"ibm: {GO_ENV}=1 is required"
        authority = self._environ.get(AUTHORITY_ENV)
        if not authority or not Path(authority).is_file():
            return f"ibm: {AUTHORITY_ENV} must name an existing authority file"
        if job.max_qpu_s <= 0:
            return "ibm: job grants no QPU seconds (limits.max_qpu_s)"
        return None

    def run(self, job: QSearchJob) -> dict[str, Any]:
        reason = self._gate(job)
        if reason is not None:
            return refused_receipt(job, reason)
        started = time.monotonic()
        try:
            compiled = self._compile(job)
            atlas_receipt = self._submit(job, compiled, purpose=self._purpose)
            return hardware_receipt(job, compiled, atlas_receipt, purpose=self._purpose,
                                    wall_s=time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001 - class name only, never the message
            return refused_receipt(job, f"ibm: {type(exc).__name__}")


__all__ = ["AUTHORITY_ENV", "DEFAULT_PURPOSE", "DRY_RUN_TARGETS", "GO_ENV", "IBM_TARGETS", "PURPOSES", "PURPOSE_ENV",
           "IbmRuntime", "atlas_campaign_submit", "default_compile"]
