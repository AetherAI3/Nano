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
"""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .job import QSearchJob
from .receipt import ReceiptAuditError, hardware_receipt, not_run_receipt, refused_receipt
from .receipt_v2 import ATLAS_REQUEST_RECEIPT_SCHEMA, not_run_v2, request_receipt
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
PURPOSE_ENV = "NANO_QSEARCH_PURPOSE"
IBM_TARGETS = ("fake_fez", "ibm_fez")
DRY_RUN_TARGETS = ("fake_fez",)
LIVE_TARGETS = ("ibm_fez",)
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
    ledger = _load_ledger(environ)
    if not _has_qsearch_stage(ledger):
        return None
    from aether_atlas.aqrc import campaign_runtime
    return campaign_runtime.resolve_qsearch_context(ledger, service_factory=service_factory, environ=environ)


def check_device_binding(compiled: Any, context: Any) -> None:
    """``ValueError`` unless ``compiled`` was frozen against this context's backend name and target (N2)."""
    from .compile import target_digest
    record = getattr(compiled, "compile_record", None) or {}
    backend = getattr(context, "backend", None)
    name = getattr(context, "backend_name", None)
    if (backend is None or record.get("backend_name") != name or getattr(backend, "name", None) != name
            or record.get("target_digest") != target_digest(backend.target)):
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


def prepare_request_identity(request: QSearchRequest, compiled: list[Any], *,
                             purpose: str) -> tuple[list[tuple[Any, dict[str, Any]]], dict[str, Any]]:
    """``(pairs, prepared)``: the frozen children and the ATLAS request whose ``request_sha256`` the
    operator's plan-scoped authority must name. It depends on the compiled ISA bytes, so for ibm_fez
    it is only known after compiling against the resolved live target (N2)."""
    from aether_atlas.aqrc import campaign_runtime
    pairs = _request_pairs(request, compiled, purpose)
    prepared = campaign_runtime.prepare_qsearch_request(pairs, request_id=request.request_id,
                                                        max_execution_time=request.max_execution_time)
    return pairs, prepared


def _durable_request_receipt(value: Any) -> Mapping[str, Any] | None:
    """An ATLAS request receipt, given itself or a durable ledger row whose ``receipt`` holds it; else None."""
    if isinstance(value, Mapping) and value.get("schema") == ATLAS_REQUEST_RECEIPT_SCHEMA:
        return value
    text = value.get("receipt") if isinstance(value, Mapping) else None
    if not isinstance(text, str):
        return None
    try:
        stored = json.loads(text)
    except ValueError:
        return None
    return stored if isinstance(stored, Mapping) and stored.get("schema") == ATLAS_REQUEST_RECEIPT_SCHEMA else None


def _check_pub_children(atlas_receipt: Mapping[str, Any], pairs: list[tuple[Any, dict[str, Any]]]) -> None:
    pubs = atlas_receipt.get("pubs")
    if (not isinstance(pubs, list) or len(pubs) != len(pairs)
            or any(not isinstance(pub, Mapping) or pub.get("child_sha256") != child["child_sha256"]
                   for pub, (_, child) in zip(pubs, pairs))):
        raise ReceiptAuditError("ATLAS request receipt PUBs differ from the prepared children")


def atlas_request_submit(request: QSearchRequest, compiled: list[Any], *, purpose: str,
                         environ: Mapping[str, str] | None = None, context: Any = None,
                         sampler_factory: Callable[..., Any] | None = None,
                         backend_factory: Callable[..., Any] | None = None) -> Mapping[str, Any]:
    """One ATLAS request over every job of ``request``: one ``sampler.run`` with N PUBs.

    Each compiled job becomes a frozen child (``prepare_qsearch_child``), the
    ordered children one request (``prepare_qsearch_request`` with the
    descriptor's ``max_execution_time``), and the request runs once:
    ``run_qsearch_request_dry`` for FakeFez, ``submit_qsearch_request`` for
    ibm_fez under the environment's authority and ledger and the live context
    (every plan bound to its target, N2). The receipt's PUBs must name the
    prepared children in order.
    """
    from aether_atlas.aqrc import campaign_runtime
    environ = environ if environ is not None else os.environ
    pairs, prepared = prepare_request_identity(request, compiled, purpose=purpose)
    if request.target in DRY_RUN_TARGETS:
        result = campaign_runtime.run_qsearch_request_dry(pairs, prepared, sampler_factory=sampler_factory,
                                                          backend_factory=backend_factory)
    else:
        token, key, ledger = _authority_from_env(environ)
        if context is None or not _has_qsearch_stage(ledger):
            raise RuntimeError(_NO_LIVE_CONTEXT)
        for plan in compiled:
            check_device_binding(plan, context)
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
    return _durable_request_receipt(
        campaign_runtime.recover_qsearch_submission(request.request_id, context=context, ledger=ledger))


class IbmRuntime:
    kind = "ibm"

    def __init__(self, *, compile_fn: CompileFn = default_compile, submit_fn: SubmitFn = atlas_campaign_submit,
                 environ: Mapping[str, str] | None = None, purpose: str | None = None,
                 context_fn: Any = _AUTO, request_submit_fn: RequestSubmitFn = atlas_request_submit,
                 request_recover_fn: RequestRecoverFn = atlas_request_recover,
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
        self._service_factory = service_factory
        self._sampler_factory = sampler_factory
        self._backend_factory = backend_factory

    @property
    def purpose(self) -> str:
        return self._purpose

    def _context(self, target: str, *, default_delegate: bool) -> Any:
        """The live context for ``target``, or None.

        Unless ``context_fn`` is given, only ATLAS's own delegates get one: an
        injected ``submit_fn`` owns its context, so the lookup runs only for the defaults.
        """
        fn = self._context_fn
        if fn is _AUTO:
            fn = atlas_resolve_context if default_delegate else None
        if fn is None:
            return None
        return fn(target, environ=self._environ, service_factory=self._service_factory)

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

    def run(self, job: QSearchJob) -> dict[str, Any]:
        reason = self._gate(job)
        if reason is not None:
            return refused_receipt(job, reason)
        started = time.monotonic()
        try:
            context = self._context(job.target, default_delegate=self._submit is atlas_campaign_submit)
            compiled = self._compiled(job, context)
            extra: dict[str, Any] = {} if context is None else {"context": context}
            if self._sampler_factory is not None:
                extra["sampler_factory"] = self._sampler_factory
            atlas_receipt = self._submit(job, compiled, purpose=self._purpose, environ=self._environ, **extra)
            return hardware_receipt(job, compiled, atlas_receipt, purpose=self._purpose,
                                    wall_s=time.monotonic() - started)
        except RESOURCE_ERRORS as exc:          # the run did not happen; class name only
            return not_run_receipt(job, f"ibm: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - a verdict (BLOCKED, authority, audit); class name only
            return refused_receipt(job, f"ibm: {type(exc).__name__}")

    def run_request(self, request: QSearchRequest) -> dict[str, Any]:
        """Run one v2 request once; the unsigned v2 body (OK with every block, or NOT_RUN). Never raises."""
        if request.purpose not in PURPOSES or request.target not in IBM_TARGETS:
            return not_run_v2(request, REFUSED_PREFIX + "ibm: purpose or target not admitted")
        reason = self._spend_gate(request.target, [job.max_qpu_s for job in request.jobs])
        if reason is not None:
            return not_run_v2(request, REFUSED_PREFIX + reason)
        try:
            context = self._context(request.target, default_delegate=self._request_submit is atlas_request_submit)
            compiled = [self._compiled(job, context) for job in request.jobs]
            atlas_receipt = self._request_submit(request, compiled, purpose=request.purpose, environ=self._environ,
                                                 context=context, sampler_factory=self._sampler_factory,
                                                 backend_factory=self._backend_factory)
            return request_receipt(request, atlas_receipt, purpose=request.purpose)
        except RESOURCE_ERRORS as exc:          # the run did not happen; class name only
            return not_run_v2(request, f"ibm: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - a verdict (BLOCKED, authority, audit); class name only
            return not_run_v2(request, f"{REFUSED_PREFIX}ibm: {type(exc).__name__}")

    def recover_request(self, request: QSearchRequest) -> dict[str, Any] | None:
        """The unsigned v2 body for a request a previous process accepted, or None while it is still pending.

        Never runs anything: a FakeFez request is NOT_RUN (dry runs are not
        repeated from recovery, as in v1), an ibm_fez request with no ledger row
        is NOT_RUN (never submitted), and a submitted one is re-attached through
        ATLAS; a job that is not done yet, or a lookup that fails, stays pending.
        """
        if request.target not in LIVE_TARGETS:
            return not_run_v2(request, "recovery: hardware requests are never run from recovery")
        try:
            atlas_receipt = self._request_recover(request, environ=self._environ,
                                                  service_factory=self._service_factory)
        except NeverSubmitted:
            return not_run_v2(request, NEVER_SUBMITTED)
        except Exception:  # noqa: BLE001 - the reservation stays held; a later recovery tries again
            return None
        if atlas_receipt is None:
            return None
        try:
            return request_receipt(request, atlas_receipt, purpose=request.purpose)
        except Exception as exc:  # noqa: BLE001 - a receipt that does not audit is a verdict, class name only
            return not_run_v2(request, f"{REFUSED_PREFIX}recovery: {type(exc).__name__}")


__all__ = ["AUTHORITY_ENV", "AUTHORITY_KEY_ENV", "DEFAULT_PURPOSE", "DRY_RUN_TARGETS", "GO_ENV", "IBM_TARGETS",
           "LEDGER_ENV", "LIVE_TARGETS", "NEVER_SUBMITTED", "PURPOSES", "PURPOSE_ENV", "REFUSED_PREFIX",
           "RESOURCE_ERRORS", "IbmRuntime", "NeverSubmitted", "atlas_campaign_submit", "atlas_request_recover",
           "atlas_request_submit", "atlas_resolve_context", "check_device_binding", "default_compile",
           "prepare_request_identity"]
