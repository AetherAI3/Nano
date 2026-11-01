"""IBM runtime: FakeFez dry runs by default, hardware refused until every gate is open.

Two targets, one code path. ``fake_fez`` is a dry run: the job is compiled with
``nano_qsearch.compile.compile_program`` and the ISA circuit goes through
ATLAS ``campaign_runtime.run_qsearch_dry`` (``SamplerV2`` local testing mode on
Aer); nothing is spent, so no GO, authority or QPU seconds are needed, and the
result is a real ordered-sample receipt sealed with the **hardware seal**
(``receipt.hardware_receipt``). ``ibm_fez`` keeps its gates exactly: no QPU
call can happen without ``AETHER_AQRC_HW_GO=1``, an authority file named by
``AETHER_AQRC_AUTHORITY_PATH``, and a positive ``limits.max_qpu_s``. The
delegate then loads that authority token, the host authority key
(``AETHER_AQRC_AUTHORITY_KEY_PATH``) and the campaign ledger
(``AETHER_AQRC_LEDGER_PATH``) and calls ATLAS ``submit_qsearch_child``, which
re-checks GO, verifies the plan-scoped HMAC authority against the ledger and
the 600 s cap; on a frozen ``PredatorCampaignV1`` ledger it stops BLOCKED (no
reservable q-search stage) and nothing is ever submitted. The child purpose
(``qsearch`` or ``qcomms``) rides on the runtime, not on the frozen job body:
``NANO_QSEARCH_PURPOSE`` or ``IbmRuntime(purpose=...)``, and it is recorded in
the receipt's ``hardware.purpose``. A policy verdict (gate, purpose, target,
audit, BLOCKED) is a REFUSED receipt; a resource or transport failure
(``RESOURCE_ERRORS``: MemoryError, OSError and its socket/URL subclasses,
TimeoutError, urllib errors) is NOT_RUN, so the host may retry or ledger it as
not run rather than as a refusal. Either way the exception is reported by class
name only.

Lane HW0 (contract §3). N1: with a ``PredatorCampaignV2`` ledger (one that has
a reservable q-search stage) the ibm_fez context is ATLAS
``resolve_qsearch_context`` (service, instance, backend name and the live
backend), resolved once per run before compiling; a V1 ledger keeps the
declared-identity context and stops BLOCKED exactly as before. N2: the job is
compiled against that live backend's target, and the delegate refuses a plan
whose bound backend name or target digest differs from the context. N3: the
child's ``max_execution_time`` (provider limit and ledger reservation) is the
job's ``limits.max_qpu_s``. N4: ``run_request`` compiles each job of a v2
request, prepares one ATLAS request over all children and runs it once
(``run_qsearch_request_dry`` on FakeFez, ``submit_qsearch_request`` on
ibm_fez); ``recover_request`` re-attaches a request a previous process handed
to ATLAS through ``recover_qsearch_submission`` and never resubmits. Tests
inject ``service_factory`` / ``sampler_factory`` (passed through to ATLAS);
production passes none, so the real Runtime objects stay behind ATLAS's GO /
LIVE / PACKED gates.

Amendment R3. M3: Nano itself requires ``AETHER_AQRC_HW_GO=1``,
``AETHER_AQRC_QSEARCH_LIVE=1`` and (more than one PUB)
``AETHER_AQRC_QSEARCH_PACKED=1`` before any live context is resolved, so no
``QiskitRuntimeService`` is built when one is missing. H1: once ATLAS has
handed a request or job to the provider (``QSearchSubmittedPending``, or a
ledger row with a job id or an ambiguous submission) nothing is sealed
``NOT_RUN``: the runtime returns None, the service keeps the row accepted
(HTTP 503) and finishes it through ATLAS recovery. A submitted v2 request
whose provider job failed, or whose result does not audit, is ``FAILED``.
L1: v1 ibm_fez jobs re-attach on restart through ``recover_qsearch_submission``
too (``recover_job``), recompiling against the live target and requiring the
same compiled plan digest.

J6: a request's jobs compile against ONE resolved target (``compile.compile_jobs``:
the live backend frozen once, one FakeFez for a dry run, compile options read
once); the compile seconds go to the caller's ``record`` (kept beside the
receipt, never in the sealed body); ``max_pubs`` (the service's
``NANO_QSEARCH_V2_MAX_PUBS``) is passed to ATLAS ``prepare_qsearch_request``
when it takes one.

Accounting (promotion gate): a live (non-dry) v2 request whose ATLAS result is
DONE or FAILED but whose provider usage is not yet final is HELD: the runtime
returns None (the service answers 503 and keeps the request accepted; nothing
is resubmitted) and notes ``accounting: pending`` in the caller's record. Each
later recovery drives ATLAS ``recover_qsearch_submission``'s accounting read;
the stored ATLAS receipt is completed with the ledger's settled usage (ATLAS
``qsearch_receipt_with_settled_usage``, else the row's settled usage; same
provider job only), and only then is the OK or FAILED body returned, with
``usage_pending`` false. Zero or missing usage is never final. FakeFez dry runs
and v1 receipts (which carry no usage) are unchanged.
"""
from __future__ import annotations

import http.client
import inspect
import json
import math
import os
import time
import urllib.error
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .job import QSearchJob
from .receipt import ReceiptAuditError, hardware_receipt, not_run_receipt, refused_receipt
from .receipt_v2 import ATLAS_REQUEST_RECEIPT_SCHEMA, failed_v2, not_run_v2, request_receipt
from .request import QSearchRequest

# Transient failures of the machine or the wire, never a verdict about the job:
# the receipt is NOT_RUN so Predator can retry or ledger it as not run.
# OSError covers socket, timeout and file errors; urllib and http errors ride on it.
RESOURCE_ERRORS: tuple[type[BaseException], ...] = (
    MemoryError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException,
)

GO_ENV = "AETHER_AQRC_HW_GO"
AUTHORITY_ENV = "AETHER_AQRC_AUTHORITY_PATH"
AUTHORITY_KEY_ENV = "AETHER_AQRC_AUTHORITY_KEY_PATH"
LEDGER_ENV = "AETHER_AQRC_LEDGER_PATH"
LIVE_ENV = "AETHER_AQRC_QSEARCH_LIVE"
PACKED_ENV = "AETHER_AQRC_QSEARCH_PACKED"
ATLAS_JOB_RECEIPT_SCHEMA = "AqrcQSearchJobReceiptV1"
SUBMITTED_PENDING = "QSearchSubmittedPending"      # ATLAS: a provider job id is attached, the outcome is not known yet
SUBMITTED_STATES = ("submitted", "ambiguous_submission")
ACCOUNTED_STATUSES = ("OK", "FAILED")   # v2 statuses that name a provider job and report its usage
SETTLED_RECEIPT_HELPER = "qsearch_receipt_with_settled_usage"   # ATLAS #184; absent on older pins
RECOVERY_NOT_RUN = "recovery: hardware jobs are never run from recovery"
JOB_NEVER_SUBMITTED = "recovery: job was never submitted"
PURPOSE_ENV = "NANO_QSEARCH_PURPOSE"
# J6 amendment A2: ibm_kingston and ibm_marrakesh are live targets with exactly ibm_fez's gates and
# semantics (live context, device compile against that backend, never a dry run); fake_fez stays the
# only dry-run target.
IBM_TARGETS = ("fake_fez", "ibm_fez", "ibm_kingston", "ibm_marrakesh")
DRY_RUN_TARGETS = ("fake_fez",)
LIVE_TARGETS = ("ibm_fez", "ibm_kingston", "ibm_marrakesh")
PURPOSES = ("qsearch", "qcomms")
DEFAULT_PURPOSE = "qsearch"
REFUSED_PREFIX = "refused: "         # v2 has no REFUSED status: a verdict is a NOT_RUN with this reason prefix
NEVER_SUBMITTED = "recovery: request was never submitted"
_NO_LIVE_CONTEXT = "BLOCKED: a q-search request needs a PredatorCampaignV2 ledger and a live context"

CompileFn = Callable[..., Any]                  # compile_fn(job) or compile_fn(job, backend=<live backend>)
SubmitFn = Callable[..., Mapping[str, Any]]     # submit_fn(job, compiled, purpose=..., environ=...) -> ATLAS receipt
ContextFn = Callable[..., Any]                  # context_fn(target, environ=..., service_factory=...) -> context | None
RequestSubmitFn = Callable[..., Mapping[str, Any]]   # (request, compiled_list, purpose=, environ=, context=, ...)
RequestRecoverFn = Callable[..., Any]                # (request, environ=, service_factory=) -> ATLAS receipt | None
RequestSettledFn = Callable[..., Any]                # (request, environ=) -> ATLAS receipt | None; ledger only
JobRecoverFn = Callable[..., Any]                    # (job, environ=, service_factory=) -> (ATLAS receipt | None, context)
SubmittedProbe = Callable[..., bool]                 # (request_id, environ=) -> handed to the provider?
_AUTO = object()


class NeverSubmitted(RuntimeError):
    """Recovery found no ledger row for the request: ATLAS never reserved or submitted it."""


def default_compile(job: QSearchJob, *, backend: Any = None) -> Any:
    from .compile import compile_program
    return compile_program(job, backend=backend)


def _required_path(environ: Mapping[str, str], name: str) -> Path:
    value = environ.get(name)
    if not value or not Path(value).is_file():
        raise RuntimeError(f"BLOCKED: {name} must name an existing file for an ibm_fez child")
    return Path(value)


def _load_ledger(environ: Mapping[str, str]) -> Any:
    from aether_atlas.aqrc.campaign import CampaignLedger
    return CampaignLedger(_required_path(environ, LEDGER_ENV))


def _has_qsearch_stage(ledger: Any) -> bool:
    from aether_atlas.aqrc.campaign import qsearch_stage
    return qsearch_stage(ledger.configuration()["protocol"]) is not None


def _authority_from_env(environ: Mapping[str, str]) -> tuple[dict[str, Any], bytes, Any]:
    """The operator's plan-scoped authority token, host key and campaign ledger, all by path."""
    token = json.loads(_required_path(environ, AUTHORITY_ENV).read_text(encoding="utf-8"))
    if not isinstance(token, dict) or not isinstance(token.get("body"), dict):
        raise ValueError("authority file is not an execution authorization token")
    key = _required_path(environ, AUTHORITY_KEY_ENV).read_bytes().strip()
    return token, key, _load_ledger(environ)


def _child_request_id(job: QSearchJob) -> str:
    return job.job_digest[len("sha256:"):][:40]


def live_flags_reason(environ: Mapping[str, str], pub_count: int = 1) -> str | None:
    """Why a live (ibm_fez) context may not be resolved yet, or None (amendment R3 M3)."""
    if environ.get(GO_ENV) != "1":
        return f"ibm: {GO_ENV}=1 is required"
    if environ.get(LIVE_ENV) != "1":
        return f"ibm: {LIVE_ENV}=1 is required"
    if pub_count > 1 and environ.get(PACKED_ENV) != "1":
        return f"ibm: {PACKED_ENV}=1 is required for a packed request"
    return None


def _require_live_flags(environ: Mapping[str, str], pub_count: int = 1) -> None:
    reason = live_flags_reason(environ, pub_count)
    if reason is not None:
        raise RuntimeError("BLOCKED: " + reason)


def atlas_submitted(request_id: str, *, environ: Mapping[str, str] | None = None) -> bool:
    """True when the ATLAS ledger shows ``request_id`` handed to the provider (job id attached, or ambiguous)."""
    environ = environ if environ is not None else os.environ
    row = _load_ledger(environ).get(request_id)
    return row is not None and (bool(row.get("job_id")) or row.get("state") in SUBMITTED_STATES)


def _accepts(fn: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _durable(value: Any, schema: str) -> Mapping[str, Any] | None:
    """An ATLAS receipt of ``schema``, given itself or a durable ledger row whose ``receipt`` holds it; else None."""
    if isinstance(value, Mapping) and value.get("schema") == schema:
        return value
    text = value.get("receipt") if isinstance(value, Mapping) else None
    if not isinstance(text, str):
        return None
    try:
        stored = json.loads(text)
    except ValueError:
        return None
    return stored if isinstance(stored, Mapping) and stored.get("schema") == schema else None


def atlas_resolve_context(target: str, *, environ: Mapping[str, str] | None = None,
                          service_factory: Callable[..., Any] | None = None) -> Any:
    """The live ATLAS context for an ibm_fez run on a PredatorCampaignV2 ledger, else None.

    FakeFez needs no context. A V1 ledger has no q-search stage, so ATLAS stops
    BLOCKED before any reservation and no provider context is built (the
    declared identity is enough). Otherwise ATLAS ``resolve_qsearch_context``
    builds it; ATLAS constructs a real ``QiskitRuntimeService`` only behind
    ``AETHER_AQRC_HW_GO=1`` and ``AETHER_AQRC_QSEARCH_LIVE=1``, and tests pass
    ``service_factory``.
    """
    if target not in LIVE_TARGETS:
        return None
    environ = environ if environ is not None else os.environ
    _require_live_flags(environ)                       # R3 M3: never build a live service without both flags
    ledger = _load_ledger(environ)
    if not _has_qsearch_stage(ledger):
        return None
    from aether_atlas.aqrc import campaign_runtime
    return campaign_runtime.resolve_qsearch_context(ledger, service_factory=service_factory, environ=environ)


def check_device_binding(compiled: Any, context: Any, *, live_digest: str | None = None) -> None:
    """``ValueError`` unless ``compiled`` was frozen against this context's backend name and target (N2).

    ``live_digest`` is the context target's digest when the caller already computed it (one per request).
    """
    from .compile import target_digest
    record = getattr(compiled, "compile_record", None) or {}
    backend = getattr(context, "backend", None)
    name = getattr(context, "backend_name", None)
    if backend is None or record.get("backend_name") != name or getattr(backend, "name", None) != name:
        raise ValueError("compiled plan is not bound to the resolved live backend's target")
    if record.get("target_digest") != (live_digest if live_digest is not None else target_digest(backend.target)):
        raise ValueError("compiled plan is not bound to the resolved live backend's target")


def atlas_campaign_submit(job: QSearchJob, compiled: Any, *, purpose: str = DEFAULT_PURPOSE,
                          environ: Mapping[str, str] | None = None, context: Any = None,
                          sampler_factory: Callable[..., Any] | None = None) -> Mapping[str, Any]:
    """Delegate to ATLAS ``campaign_runtime``: FakeFez dry run, or the gated ibm_fez path.

    Returns the ``AqrcQSearchJobReceiptV1`` for a dry run. For ``ibm_fez`` it
    loads the authority token, host key and ledger from the environment and
    calls ``submit_qsearch_child``, which verifies GO, the HMAC authority and
    the 600 s cap. On a V1 ledger it then raises ``RuntimeError("BLOCKED:
    ...")`` because the protocol has no q-search stage to reserve (child and
    declared-identity context exactly as before); the runtime reports that as
    ``REFUSED ibm: RuntimeError``. On a PredatorCampaignV2 ledger the context
    is the live one (``context``, else resolved here), the compiled plan must
    be bound to its backend and target (N2), and the child's
    ``max_execution_time`` is the job's ``limits.max_qpu_s`` (N3). A
    verification failure is a ``ValueError``.
    """
    from aether_atlas.aqrc import campaign_runtime
    environ = environ if environ is not None else os.environ

    def child_for(**limit: int) -> dict[str, Any]:
        return campaign_runtime.prepare_qsearch_child(
            compiled.isa, purpose=purpose, target=job.target, shots=job.shots, job_digest=job.job_digest,
            compiled_plan_digest=compiled.compiled_plan_digest, request_id=_child_request_id(job), **limit)
    if job.target in DRY_RUN_TARGETS:
        return campaign_runtime.run_qsearch_dry(compiled.isa, child_for())
    token, key, ledger = _authority_from_env(environ)
    if not _has_qsearch_stage(ledger):
        # V1: the gate needs only the declared backend identity; a live provider
        # context is never built because no reservation or submission follows.
        context = SimpleNamespace(backend_name=job.target, instance=ledger.configuration()["protocol"]["instance"])
        return campaign_runtime.submit_qsearch_child(compiled.isa, child_for(), context=context, ledger=ledger,
                                                     authority=token, authority_key=key)
    if context is None:
        context = campaign_runtime.resolve_qsearch_context(ledger, environ=environ)
    check_device_binding(compiled, context)
    return campaign_runtime.submit_qsearch_child(compiled.isa, child_for(max_execution_time=job.max_qpu_s),
                                                 context=context, ledger=ledger, authority=token, authority_key=key,
                                                 sampler_factory=sampler_factory)


def _request_pairs(request: QSearchRequest, compiled: list[Any], purpose: str) -> list[tuple[Any, dict[str, Any]]]:
    """``(isa, frozen child)`` per job, in descriptor order; every child carries the request's limit."""
    from aether_atlas.aqrc import campaign_runtime
    return [(plan.isa, campaign_runtime.prepare_qsearch_child(
                plan.isa, purpose=purpose, target=request.target, shots=job.shots, job_digest=job.job_digest,
                compiled_plan_digest=plan.compiled_plan_digest, request_id=_child_request_id(job),
                max_execution_time=request.max_execution_time))
            for job, plan in zip(request.jobs, compiled)]


def prepare_request_identity(request: QSearchRequest, compiled: list[Any], *, purpose: str,
                             max_pubs: int | None = None) -> tuple[list[tuple[Any, dict[str, Any]]], dict[str, Any]]:
    """``(pairs, prepared)``: the frozen children and the ATLAS request whose ``request_sha256`` the
    operator's plan-scoped authority must name. It depends on the compiled ISA bytes, so for ibm_fez
    it is only known after compiling against the resolved live target (N2)."""
    from aether_atlas.aqrc import campaign_runtime
    pairs = _request_pairs(request, compiled, purpose)
    extra = ({"client_request_digest": request.request_digest}
             if _accepts(campaign_runtime.prepare_qsearch_request, "client_request_digest") else {})
    if max_pubs is not None and _accepts(campaign_runtime.prepare_qsearch_request, "max_pubs"):
        extra["max_pubs"] = max_pubs               # J6: ATLAS's request PUB cap follows the configured limit
    prepared = campaign_runtime.prepare_qsearch_request(pairs, request_id=request.request_id,
                                                        max_execution_time=request.max_execution_time, **extra)
    return pairs, prepared


def _durable_request_receipt(value: Any) -> Mapping[str, Any] | None:
    return _durable(value, ATLAS_REQUEST_RECEIPT_SCHEMA)


def _positive_finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except OverflowError:
        return False


def with_settled_usage(receipt: Mapping[str, Any], row: Any) -> Mapping[str, Any]:
    """``receipt`` with the ledger row's settled usage when the receipt still reads pending.

    ATLAS can finalize a row's ``usage`` after the receipt it stored said ``usage_pending``; the
    row is the accounting truth. Merged only for the same provider job and a positive finite
    usage: zero or missing usage stays pending (never a free run), and nothing else changes.
    """
    if not isinstance(receipt, Mapping):
        return receipt
    if receipt.get("usage_pending") is False and _positive_finite(receipt.get("provider_final_usage_seconds")):
        return receipt
    if not isinstance(row, Mapping) or row.get("job_id") != receipt.get("job_id") \
            or not _positive_finite(row.get("usage")):
        return receipt
    return {**receipt, "provider_final_usage_seconds": float(row["usage"]), "usage_pending": False}


def _check_pub_children(atlas_receipt: Mapping[str, Any], pairs: list[tuple[Any, dict[str, Any]]]) -> None:
    if atlas_receipt.get("status") == "FAILED":
        return                                         # a provider-terminal failure carries no PUBs
    pubs = atlas_receipt.get("pubs")
    if (not isinstance(pubs, list) or len(pubs) != len(pairs)
            or any(not isinstance(pub, Mapping) or pub.get("child_sha256") != child["child_sha256"]
                   for pub, (_, child) in zip(pubs, pairs))):
        raise ReceiptAuditError("ATLAS request receipt PUBs differ from the prepared children")


def atlas_request_submit(request: QSearchRequest, compiled: list[Any], *, purpose: str,
                         environ: Mapping[str, str] | None = None, context: Any = None,
                         sampler_factory: Callable[..., Any] | None = None,
                         backend_factory: Callable[..., Any] | None = None,
                         max_pubs: int | None = None,
                         prepared_identity: Any = None) -> Mapping[str, Any]:
    """One ATLAS request over every job of ``request``: one ``sampler.run`` with N PUBs.

    Each compiled job becomes a frozen child (``prepare_qsearch_child``), the
    ordered children one request (``prepare_qsearch_request`` with the
    descriptor's ``max_execution_time``), and the request runs once:
    ``run_qsearch_request_dry`` for FakeFez, ``submit_qsearch_request`` for
    ibm_fez under the environment's authority and ledger and the live context
    (every plan bound to its target, N2). The receipt's PUBs must name the
    prepared children in order.

    J6: the request PUB bound passed to ATLAS is the ledger's Q-stage PUB cap
    (``qsearch_max_request_pubs``) for ibm_fez, and ``max_pubs`` (the service's
    ``NANO_QSEARCH_V2_MAX_PUBS``) for a FakeFez dry run, which has no ledger.
    """
    from aether_atlas.aqrc import campaign_runtime
    environ = environ if environ is not None else os.environ
    live = request.target not in DRY_RUN_TARGETS
    if live:
        token, key, ledger = _authority_from_env(environ)
        if context is None or not _has_qsearch_stage(ledger):
            raise RuntimeError(_NO_LIVE_CONTEXT)
        ledger_cap = getattr(campaign_runtime, "qsearch_max_request_pubs", None)
        if ledger_cap is not None:
            max_pubs = ledger_cap(ledger)
    if prepared_identity is None:
        pairs, prepared = prepare_request_identity(request, compiled, purpose=purpose, max_pubs=max_pubs)
    else:
        pairs, prepared = prepared_identity
        if len(pairs) != request.block_count or (max_pubs is not None and len(pairs) > max_pubs):
            raise ValueError("frozen request exceeds the current permitted PUB cap")
    if not live:
        result = campaign_runtime.run_qsearch_request_dry(pairs, prepared, sampler_factory=sampler_factory,
                                                          backend_factory=backend_factory)
    else:
        from .compile import target_digest
        live_digest = target_digest(context.backend.target)          # once per request, not per plan
        for plan in compiled:
            check_device_binding(plan, context, live_digest=live_digest)
        result = campaign_runtime.submit_qsearch_request(pairs, prepared, context=context, ledger=ledger,
                                                         authority=token, authority_key=key,
                                                         sampler_factory=sampler_factory)
    receipt = _durable_request_receipt(result)
    if receipt is None:
        raise RuntimeError("BLOCKED: ATLAS returned a held request row, not a receipt")
    _check_pub_children(receipt, pairs)
    return receipt


def atlas_request_recover(request: QSearchRequest, *, environ: Mapping[str, str] | None = None,
                          service_factory: Callable[..., Any] | None = None) -> Mapping[str, Any] | None:
    """Re-attach a request a previous process handed to ATLAS; never resubmits.

    ``NeverSubmitted`` when the ledger has no row for the request (nothing was
    reserved, so nothing can be running). Otherwise ATLAS
    ``recover_qsearch_submission`` looks the provider job up by its stored job
    id or by the request's job tag; the receipt comes back when the job is
    done, else None and the reservation stays held.
    """
    environ = environ if environ is not None else os.environ
    ledger = _load_ledger(environ)
    if ledger.get(request.request_id) is None:
        raise NeverSubmitted(NEVER_SUBMITTED)
    from aether_atlas.aqrc import campaign_runtime
    context = atlas_resolve_context(request.target, environ=environ, service_factory=service_factory)
    if context is None:
        raise RuntimeError(_NO_LIVE_CONTEXT)
    receipt = _durable_request_receipt(
        campaign_runtime.recover_qsearch_submission(request.request_id, context=context, ledger=ledger))
    if receipt is None:
        return None
    # The recovery call above performed ATLAS's accounting read; the ledger now holds what is settled.
    return settled_request_receipt(campaign_runtime, ledger, request.request_id, receipt)


def atlas_request_settled(request: QSearchRequest, *,
                          environ: Mapping[str, str] | None = None) -> Mapping[str, Any] | None:
    """The stored final ATLAS receipt with the ledger's settled usage, read from the ledger alone.

    No provider call, no live context, no flags: ATLAS ``qsearch_receipt_with_settled_usage``
    (or, on older pins, the row's settled usage). None while the row has no final receipt;
    ``NeverSubmitted`` when the ledger has no row. Its usage may still read pending.
    """
    environ = environ if environ is not None else os.environ
    ledger = _load_ledger(environ)
    row = ledger.get(request.request_id)
    if row is None:
        raise NeverSubmitted(NEVER_SUBMITTED)
    from aether_atlas.aqrc import campaign_runtime
    helper = getattr(campaign_runtime, SETTLED_RECEIPT_HELPER, None)
    if helper is not None:
        return _durable_request_receipt(helper(ledger, request.request_id))
    receipt = _durable_request_receipt(row)
    return None if receipt is None else with_settled_usage(receipt, row)


def settled_request_receipt(campaign_runtime: Any, ledger: Any, request_id: str,
                            receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    """The client view of a recovered ATLAS receipt with the ledger's settled usage.

    ATLAS ``qsearch_receipt_with_settled_usage`` (AETHER-ATLAS #184) when present: the stored final
    receipt, completed once and durably with the row's settled usage (still ``usage_pending`` while
    unsettled). It is used only for the same provider job; otherwise the recovered receipt stands and
    stays pending. An ATLAS pin without the helper falls back to ``with_settled_usage`` over the row.
    """
    helper = getattr(campaign_runtime, SETTLED_RECEIPT_HELPER, None)
    if helper is None:
        return with_settled_usage(receipt, ledger.get(request_id))
    settled = _durable_request_receipt(helper(ledger, request_id))
    if settled is None or settled.get("job_id") != receipt.get("job_id"):
        return receipt
    return settled


def atlas_job_recover(job: QSearchJob, *, environ: Mapping[str, str] | None = None,
                      service_factory: Callable[..., Any] | None = None) -> tuple[Mapping[str, Any] | None, Any]:
    """Re-attach a v1 ibm_fez job a previous process handed to ATLAS (L1); never resubmits.

    Returns ``(receipt or None, live context)``; ``NeverSubmitted`` when the ledger has no row.
    """
    environ = environ if environ is not None else os.environ
    ledger = _load_ledger(environ)
    request_id = _child_request_id(job)
    if ledger.get(request_id) is None:
        raise NeverSubmitted(JOB_NEVER_SUBMITTED)
    from aether_atlas.aqrc import campaign_runtime
    context = atlas_resolve_context(job.target, environ=environ, service_factory=service_factory)
    if context is None:
        raise RuntimeError(_NO_LIVE_CONTEXT)
    found = campaign_runtime.recover_qsearch_submission(request_id, context=context, ledger=ledger)
    return _durable(found, ATLAS_JOB_RECEIPT_SCHEMA), context


class IbmRuntime:
    kind = "ibm"

    def __init__(self, *, compile_fn: CompileFn = default_compile, submit_fn: SubmitFn = atlas_campaign_submit,
                 environ: Mapping[str, str] | None = None, purpose: str | None = None,
                 context_fn: Any = _AUTO, request_submit_fn: RequestSubmitFn = atlas_request_submit,
                 request_recover_fn: RequestRecoverFn = atlas_request_recover,
                 request_settled_fn: Any = _AUTO,
                 job_recover_fn: JobRecoverFn = atlas_job_recover,
                 submitted_probe: SubmittedProbe = atlas_submitted,
                 service_factory: Callable[..., Any] | None = None,
                 sampler_factory: Callable[..., Any] | None = None,
                 backend_factory: Callable[..., Any] | None = None) -> None:
        self._compile = compile_fn
        self._submit = submit_fn
        self._environ = environ if environ is not None else os.environ
        self._purpose = purpose if purpose is not None else self._environ.get(PURPOSE_ENV, DEFAULT_PURPOSE)
        self._context_fn = context_fn
        self._request_submit = request_submit_fn
        self._request_recover = request_recover_fn
        # The ledger-only settled view belongs with ATLAS's own recovery; an injected recovery owns its own.
        self._request_settled = ((atlas_request_settled if request_recover_fn is atlas_request_recover else None)
                                 if request_settled_fn is _AUTO else request_settled_fn)
        self._job_recover = job_recover_fn
        self._submitted_probe = submitted_probe
        self._service_factory = service_factory
        self._sampler_factory = sampler_factory
        self._backend_factory = backend_factory

    @property
    def purpose(self) -> str:
        return self._purpose

    def _context(self, target: str, *, default_delegate: bool, pub_count: int = 1) -> Any:
        """The live context for ``target``, or None.

        Unless ``context_fn`` is given, only ATLAS's own delegates get one: an
        injected ``submit_fn`` owns its context, so the lookup runs only for the defaults.
        For ibm_fez the GO / LIVE / PACKED flags are checked first (R3 M3).
        """
        fn = self._context_fn
        if fn is _AUTO:
            fn = atlas_resolve_context if default_delegate else None
        if fn is None:
            return None
        if target in LIVE_TARGETS:
            _require_live_flags(self._environ, pub_count)
        return fn(target, environ=self._environ, service_factory=self._service_factory)

    def _handed_over(self, target: str, request_id: str, exc: BaseException) -> bool:
        """Keep live submissions pending when hand-over is known or cannot be ruled out (R3 H1)."""
        if target not in LIVE_TARGETS:
            return False
        return type(exc).__name__ == SUBMITTED_PENDING or self._probe(request_id) is not False

    def _probe(self, request_id: str) -> bool | None:
        """Submission evidence, or None when the ledger cannot be read.

        An unreadable ledger is not evidence that submission never happened.
        Keep the accepted service row pending so recovery can reconcile it.
        """
        try:
            return bool(self._submitted_probe(request_id, environ=self._environ))
        except Exception:  # noqa: BLE001 - unknown outcome; retain it for recovery
            return None

    def _compiled(self, job: QSearchJob, context: Any) -> Any:
        return self._compile(job) if context is None else self._compile(job, backend=context.backend)

    def _spend_gate(self, target: str, qpu_seconds: list[int]) -> str | None:
        """Nano's own gates for anything that is not a dry run: GO, authority file, QPU seconds."""
        if target in DRY_RUN_TARGETS:
            return None                       # a FakeFez dry run spends nothing: no GO, authority or QPU seconds
        if self._environ.get(GO_ENV) != "1":
            return f"ibm: {GO_ENV}=1 is required"
        authority = self._environ.get(AUTHORITY_ENV)
        if not authority or not Path(authority).is_file():
            return f"ibm: {AUTHORITY_ENV} must name an existing authority file"
        if any(seconds <= 0 for seconds in qpu_seconds):
            return "ibm: job grants no QPU seconds (limits.max_qpu_s)"
        return None

    def _gate(self, job: QSearchJob) -> str | None:
        if self._purpose not in PURPOSES:
            return f"ibm: purpose must be one of {list(PURPOSES)}"
        if job.target not in IBM_TARGETS:
            return f"ibm: target must be one of {list(IBM_TARGETS)}"
        return self._spend_gate(job.target, [job.max_qpu_s])

    def run(self, job: QSearchJob) -> dict[str, Any] | None:
        """The unsigned v1 body, or None when the job reached the provider and is still pending (R3 H1)."""
        reason = self._gate(job)
        if reason is not None:
            return refused_receipt(job, reason)
        started = time.monotonic()
        submission_attempted = False
        try:
            context = self._context(job.target, default_delegate=self._submit is atlas_campaign_submit)
            compiled = self._compiled(job, context)
            extra: dict[str, Any] = {} if context is None else {"context": context}
            if self._sampler_factory is not None:
                extra["sampler_factory"] = self._sampler_factory
            submission_attempted = True
            result = self._submit(job, compiled, purpose=self._purpose, environ=self._environ, **extra)
        except RESOURCE_ERRORS as exc:          # the run did not happen; class name only
            if submission_attempted and self._handed_over(job.target, _child_request_id(job), exc):
                return None
            return not_run_receipt(job, f"ibm: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - a verdict (BLOCKED, authority, audit); class name only
            if submission_attempted and self._handed_over(job.target, _child_request_id(job), exc):
                return None
            return refused_receipt(job, f"ibm: {type(exc).__name__}")
        if job.target in LIVE_TARGETS and not (isinstance(result, Mapping) and "pubs" in result):
            durable = _durable(result, ATLAS_JOB_RECEIPT_SCHEMA)
            if durable is None and self._probe(_child_request_id(job)) is not False:
                return None                     # a held ledger row: submitted earlier, outcome not known yet
            result = durable if durable is not None else result
        return self._job_body(job, compiled, result, purpose=self._purpose, wall_s=time.monotonic() - started)

    @staticmethod
    def _job_body(job: QSearchJob, compiled: Any, atlas_receipt: Any, *, purpose: str,
                  wall_s: float) -> dict[str, Any]:
        """v1 body for a returned ATLAS result: OK, or REFUSED (a provider failure or a result that does not audit)."""
        if isinstance(atlas_receipt, Mapping) and atlas_receipt.get("status") == "FAILED":
            return refused_receipt(job, "ibm: provider job failed")
        try:
            return hardware_receipt(job, compiled, atlas_receipt, purpose=purpose, wall_s=wall_s)
        except Exception as exc:  # noqa: BLE001 - a verdict on a returned result; class name only
            return refused_receipt(job, f"ibm: {type(exc).__name__}")

    def recover_job(self, job: QSearchJob) -> dict[str, Any] | None:
        """The unsigned v1 body for a job a previous process accepted, or None while it is still pending (L1).

        FakeFez is never re-run (NOT_RUN). ibm_fez: no ledger row means it was never submitted
        (NOT_RUN); otherwise ATLAS re-attaches the provider job. The ISA is recompiled against
        the live target and must reproduce the child's compiled plan digest, else REFUSED.
        """
        if job.target not in LIVE_TARGETS:
            return not_run_receipt(job, RECOVERY_NOT_RUN)
        try:
            atlas_receipt, context = self._job_recover(job, environ=self._environ,
                                                       service_factory=self._service_factory)
        except NeverSubmitted:
            return not_run_receipt(job, JOB_NEVER_SUBMITTED)
        except Exception:  # noqa: BLE001 - the reservation stays held; a later recovery tries again
            return None
        if atlas_receipt is None:
            return None
        purpose = atlas_receipt.get("purpose") if atlas_receipt.get("purpose") in PURPOSES else self._purpose
        if atlas_receipt.get("status") == "FAILED":
            return refused_receipt(job, "ibm: provider job failed")
        try:
            compiled = self._compiled(job, context)
            child = atlas_receipt.get("child") or {}
            if child.get("envelope", {}).get("compiled_plan_digest") != compiled.compiled_plan_digest:
                raise ReceiptAuditError("recompiled plan differs from the submitted child")
        except Exception as exc:  # noqa: BLE001 - the recovered job cannot be bound to this plan
            return refused_receipt(job, f"recovery: {type(exc).__name__}")
        return self._job_body(job, compiled, atlas_receipt, purpose=purpose, wall_s=0.0)

    def _request_gate(self, request: QSearchRequest) -> str | None:
        if request.purpose not in PURPOSES or request.target not in IBM_TARGETS:
            return "ibm: purpose or target not admitted"
        reason = self._spend_gate(request.target, [job.max_qpu_s for job in request.jobs])
        if reason is None and request.target in LIVE_TARGETS:
            reason = live_flags_reason(self._environ, request.block_count)      # R3 M3, before any context
        return reason

    def _request_body(self, request: QSearchRequest, atlas_receipt: Any) -> dict[str, Any] | None:
        """The v2 body for a returned ATLAS result. A submitted (ibm_fez) request is never NOT_RUN (R3 H1):
        a result that does not audit is FAILED, and one too malformed even for that stays pending (None)."""
        try:
            return request_receipt(request, atlas_receipt, purpose=request.purpose)
        except Exception as exc:  # noqa: BLE001 - a verdict on a returned result; class name only
            reason = f"{REFUSED_PREFIX}ibm: {type(exc).__name__}"
            if request.target not in LIVE_TARGETS:
                return not_run_v2(request, reason)
        try:
            return failed_v2(request, atlas_receipt, purpose=request.purpose, reason=reason)
        except Exception:  # noqa: BLE001 - cannot even name the provider job: leave it for the operator
            return None

    def _compile_all(self, jobs: Any, context: Any) -> list[Any]:
        """Every job against one resolved target: the batch compile for the default compiler (J6),
        else the injected compiler per job with the one resolved backend."""
        if self._compile is default_compile:
            from .compile import compile_jobs
            return compile_jobs(jobs, backend=None if context is None else context.backend)
        return [self._compiled(job, context) for job in jobs]

    def run_prepared_request(self, artifact: Any, *, expected_artifact_digest: str,
                             current_snapshot: Any = None, max_pubs: int | None = None,
                             record: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Explicit opt-in adapter for an approved offline artifact; existing routes stay v2."""
        from .prepared_runtime import run_prepared_request
        return run_prepared_request(self, artifact, expected_artifact_digest=expected_artifact_digest,
                                    current_snapshot=current_snapshot, max_pubs=max_pubs, record=record)

    def run_request(self, request: QSearchRequest, *, max_pubs: int | None = None,
                    record: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Run one v2 request once: the unsigned v2 body (OK, FAILED or NOT_RUN), or None when it reached
        the provider and its outcome is not known yet (the service then answers 503 and recovers it).

        ``record`` (optional) receives ``compile_s``, ``compiled_jobs`` and ``target_resolutions``: kept
        beside the receipt by the service, never in the sealed body. ``max_pubs`` goes to ATLAS."""
        reason = self._request_gate(request)
        if reason is not None:
            return not_run_v2(request, REFUSED_PREFIX + reason)
        record = {} if record is None else record
        submission_attempted = False
        try:
            context = self._context(request.target, pub_count=request.block_count,
                                    default_delegate=self._request_submit is atlas_request_submit)
            record["target_resolutions"] = 0 if context is None else 1
            started = time.perf_counter()
            compiled = self._compile_all(request.jobs, context)
            record.update(compile_s=round(time.perf_counter() - started, 3), compiled_jobs=len(compiled))
            submission_attempted = True
            atlas_receipt = self._request_submit(request, compiled, purpose=request.purpose, environ=self._environ,
                                                 context=context, sampler_factory=self._sampler_factory,
                                                 backend_factory=self._backend_factory, max_pubs=max_pubs)
        except RESOURCE_ERRORS as exc:          # the run did not happen; class name only
            if submission_attempted and self._handed_over(request.target, request.request_id, exc):
                return self._held_unknown(exc, record)
            return not_run_v2(request, f"ibm: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - a verdict (BLOCKED, authority, audit); class name only
            if submission_attempted and self._handed_over(request.target, request.request_id, exc):
                return self._held_unknown(exc, record)
            return not_run_v2(request, f"{REFUSED_PREFIX}ibm: {type(exc).__name__}")
        return self._final(request, self._request_body(request, atlas_receipt), record, atlas_receipt)

    @staticmethod
    def _held_unknown(exc: BaseException, record: dict[str, Any] | None) -> None:
        """Submitted, outcome not known: remember the provider job id when ATLAS names it (503 detail)."""
        job_id = getattr(exc, "job_id", None)
        if record is not None and isinstance(job_id, str) and job_id:
            record["provider_job_id"] = job_id
        return None

    @staticmethod
    def _final(request: QSearchRequest, body: dict[str, Any] | None, record: dict[str, Any] | None,
               atlas_receipt: Any = None) -> dict[str, Any] | None:
        """``body``, or None while a live result's provider usage is not final (accounting gate).

        The record keeps what a client needs while the result is held: the provider job id, the
        result (OK / FAILED), the provider's terminal status and, for FAILED, the reason. A settled
        record notes that ``provider_metrics`` is the read at job completion (it may show 0 there).
        """
        if body is None or request.target not in LIVE_TARGETS or body.get("status") not in ACCOUNTED_STATUSES:
            return body
        pending = body["usage"]["usage_pending"]
        if record is not None:
            status = atlas_receipt.get("provider_status") if isinstance(atlas_receipt, Mapping) else None
            record.update(accounting="pending" if pending else "settled",
                          provider_job_id=body["hardware"]["provider_job_id"], result=body["status"],
                          provider_status=status if isinstance(status, str) and status else
                          ("DONE" if body["status"] == "OK" else None),
                          reason=body.get("reason"))
            if not pending:
                record["quantum_seconds"] = body["usage"]["quantum_seconds"]
                record["usage_note"] = ("quantum_seconds is the ledger's settled usage; usage.provider_metrics is "
                                        "the provider's read at job completion and may predate it")
                if isinstance(atlas_receipt, Mapping) and "provider_usage_at_receipt" in atlas_receipt:
                    record["provider_usage_at_receipt"] = atlas_receipt["provider_usage_at_receipt"]
        return None if pending else body

    def recover_request(self, request: QSearchRequest, *,
                        record: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """The unsigned v2 body for a request a previous process accepted, or None while it is still pending.

        Never runs anything: a FakeFez request is NOT_RUN (dry runs are not
        repeated from recovery, as in v1), an ibm_fez request with no ledger row
        is NOT_RUN (never submitted), and a submitted one is re-attached through
        ATLAS: OK, or FAILED for a provider-terminal failure; a job that is not
        done yet, or a lookup that fails, stays pending.
        """
        if request.target not in LIVE_TARGETS:
            return not_run_v2(request, "recovery: hardware requests are never run from recovery")
        if self._request_settled is not None:
            # Ledger only: a row already settled needs no provider call, no live context and no flags.
            try:
                settled = self._request_settled(request, environ=self._environ)
            except NeverSubmitted:
                return not_run_v2(request, NEVER_SUBMITTED)
            except Exception:  # noqa: BLE001 - unreadable here; provider recovery decides below
                settled = None
            if settled is not None:
                body = self._final(request, self._request_body(request, settled), record, settled)
                if body is not None:
                    return body
        # ATLAS's own recovery resolves a live context, which needs the flags; an injected one owns its context.
        missing = live_flags_reason(self._environ) if self._request_recover is atlas_request_recover else None
        if missing is not None:                 # the accounting read needs the provider: say so, keep holding
            if record is not None:
                record["accounting_blocker"] = "needs_live_context"
            return None
        if record is not None:
            record["accounting_blocker"] = None
        try:
            atlas_receipt = self._request_recover(request, environ=self._environ,
                                                  service_factory=self._service_factory)
        except NeverSubmitted:
            return not_run_v2(request, NEVER_SUBMITTED)
        except Exception as exc:  # noqa: BLE001 - the reservation stays held; a later recovery tries again
            return self._held_unknown(exc, record)
        if atlas_receipt is None:
            return None
        return self._final(request, self._request_body(request, atlas_receipt), record, atlas_receipt)


__all__ = ["ATLAS_JOB_RECEIPT_SCHEMA", "AUTHORITY_ENV", "AUTHORITY_KEY_ENV", "DEFAULT_PURPOSE", "DRY_RUN_TARGETS",
           "GO_ENV", "IBM_TARGETS", "JOB_NEVER_SUBMITTED", "LEDGER_ENV", "LIVE_ENV", "LIVE_TARGETS",
           "NEVER_SUBMITTED", "PACKED_ENV", "PURPOSES", "PURPOSE_ENV", "RECOVERY_NOT_RUN", "REFUSED_PREFIX",
           "RESOURCE_ERRORS", "SUBMITTED_PENDING", "IbmRuntime", "NeverSubmitted", "atlas_campaign_submit",
           "atlas_job_recover", "atlas_request_recover", "atlas_request_submit", "atlas_resolve_context",
           "atlas_submitted", "check_device_binding", "default_compile", "live_flags_reason",
           "prepare_request_identity", "settled_request_receipt", "with_settled_usage", "ACCOUNTED_STATUSES",
           "atlas_request_settled",
           "SETTLED_RECEIPT_HELPER"]

