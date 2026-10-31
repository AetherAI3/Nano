"""Parse and cross-check a ``predator.qsearch.request.v2`` body (lane HW0, S2 / N4).

The body Predator POSTs to ``/qsearch/v2/submit_request`` is::

    {"schema": "predator.qsearch.request.v2",
     "descriptor": <request descriptor>,          # Predator computes it before submission
     "jobs": [<predator.qsearch.job.v1 body>, ...]}   # same order as descriptor.blocks

``canonical_digest(descriptor)`` is the request digest every v2 receipt binds.
Validation is strict and whole, like ``job.parse_job``: exact key sets, typed
fields, 1..``MAX_BLOCKS`` blocks, no repeated job or ``(agent_id, chunk)``
recipient, and every job must agree with its block (``job_digest``, ``n``,
``shots``, ``seed`` = the job's backend seed, ``classical_min_energy``), be an
``ibm`` job on the descriptor's one ``target``, and (ibm_fez) the jobs'
``limits.max_qpu_s`` must together allow ``descriptor.max_execution_time``.
A body that fails here gets no receipt (the service answers 400).
"""
from __future__ import annotations

import math
import re
import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .canonical import canonical_digest, validate_digest
from .job import JobValidationError, QSearchJob, parse_job

REQUEST_SCHEMA = "predator.qsearch.request.v2"
REQUEST_TARGETS = ("fake_fez", "ibm_fez")
REQUEST_PURPOSES = ("qsearch", "qcomms")
MAX_BLOCKS = 16                      # one Q-stage request: PredatorCampaignV2 stage_pub_caps["Q"]
MAX_EXECUTION_TIME_S = 600           # ATLAS QSEARCH_CAP_SECONDS
REQUEST_BACKEND_KIND = "ibm"
LIMITED_TARGETS = ("ibm_fez",)       # a FakeFez dry run spends nothing, so its jobs need grant no QPU seconds
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_BODY_KEYS = frozenset({"schema", "descriptor", "jobs"})
_DESCRIPTOR_KEYS = frozenset({"schema", "request_id", "target", "purpose", "max_execution_time", "manifest_digest",
                              "batch_index", "batch_digest", "blocks"})
_BLOCK_KEYS = frozenset({"pub_index", "job_digest", "recipients", "n", "shots", "seed", "classical_min_energy"})
_RECIPIENT_KEYS = frozenset({"agent_id", "chunk", "slot"})

Recipient = tuple[str, int, int]


class RequestValidationError(ValueError):
    """A body that is not a valid v2 request; ``code`` names the field group (job codes are ``JOB_<code>``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _is_int(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


@dataclass(frozen=True)
class RequestBlock:
    pub_index: int
    job_digest: str
    recipients: tuple[Recipient, ...]
    n: int
    shots: int
    seed: int
    classical_min_energy: float

    def recipients_payload(self) -> list[dict[str, Any]]:
        return [{"agent_id": a, "chunk": c, "slot": s} for a, c, s in self.recipients]


@dataclass(frozen=True)
class QSearchRequest:
    request_id: str
    target: str
    purpose: str
    max_execution_time: int
    blocks: tuple[RequestBlock, ...]
    jobs: tuple[QSearchJob, ...]
    request_digest: str
    descriptor: Mapping[str, Any]

    @property
    def block_count(self) -> int:
        return len(self.blocks)


def _recipients(raw: Any, position: int) -> tuple[Recipient, ...]:
    if not isinstance(raw, list) or not raw:
        raise RequestValidationError("DESCRIPTOR", f"block {position}: recipients must be a non-empty list")
    out = []
    for item in raw:
        if (not isinstance(item, Mapping) or set(item) != _RECIPIENT_KEYS or not isinstance(item["agent_id"], str)
                or not item["agent_id"] or not _is_int(item["chunk"]) or not _is_int(item["slot"])):
            raise RequestValidationError("DESCRIPTOR", f"block {position}: recipient must be agent_id, chunk, slot")
        out.append((item["agent_id"], item["chunk"], item["slot"]))
    slots = [slot for _, _, slot in out]
    if slots != sorted(set(slots)):
        raise RequestValidationError("DESCRIPTOR", f"block {position}: recipients must be in strictly increasing slot order")
    return tuple(out)


def _block(raw: Any, position: int) -> RequestBlock:
    if not isinstance(raw, Mapping) or set(raw) != _BLOCK_KEYS:
        raise RequestValidationError("DESCRIPTOR", f"block {position}: key set differs from the schema")
    if not _is_int(raw["pub_index"]) or raw["pub_index"] != position:
        raise RequestValidationError("DESCRIPTOR", f"block {position}: pub_index differs from its position")
    try:
        validate_digest(raw["job_digest"], field=f"block {position} job_digest")
    except ValueError as exc:
        raise RequestValidationError("DESCRIPTOR", str(exc)) from None
    if not _is_int(raw["n"], 1) or not _is_int(raw["shots"], 1) or not _is_int(raw["seed"]):
        raise RequestValidationError("DESCRIPTOR", f"block {position}: n, shots must be positive ints, seed an int >= 0")
    if not _finite(raw["classical_min_energy"]):
        raise RequestValidationError("DESCRIPTOR", f"block {position}: classical_min_energy must be a finite number")
    return RequestBlock(pub_index=position, job_digest=raw["job_digest"], recipients=_recipients(raw["recipients"], position),
                        n=raw["n"], shots=raw["shots"], seed=raw["seed"],
                        classical_min_energy=float(raw["classical_min_energy"]))


def _descriptor_header(desc: Mapping[str, Any]) -> None:
    if desc.get("schema") != REQUEST_SCHEMA or set(desc) != _DESCRIPTOR_KEYS:
        raise RequestValidationError("DESCRIPTOR", "descriptor schema or key set differs")
    if not isinstance(desc["request_id"], str) or not _REQUEST_ID.match(desc["request_id"]):
        raise RequestValidationError("DESCRIPTOR", "request_id must be 1..80 of [A-Za-z0-9_-]")
    if desc["target"] not in REQUEST_TARGETS:
        raise RequestValidationError("DESCRIPTOR", f"target must be one of {list(REQUEST_TARGETS)}")
    if desc["purpose"] not in REQUEST_PURPOSES:
        raise RequestValidationError("DESCRIPTOR", f"purpose must be one of {list(REQUEST_PURPOSES)}")
    if not _is_int(desc["max_execution_time"], 1) or desc["max_execution_time"] > MAX_EXECUTION_TIME_S:
        raise RequestValidationError("DESCRIPTOR", f"max_execution_time must be an int in 1..{MAX_EXECUTION_TIME_S}")
    for name in ("manifest_digest", "batch_digest"):
        try:
            validate_digest(desc[name], field=name)
        except ValueError as exc:
            raise RequestValidationError("DESCRIPTOR", str(exc)) from None
    if not _is_int(desc["batch_index"]):
        raise RequestValidationError("DESCRIPTOR", "batch_index must be a non-negative int")


def _blocks(desc: Mapping[str, Any]) -> tuple[RequestBlock, ...]:
    raw = desc["blocks"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_BLOCKS:
        raise RequestValidationError("DESCRIPTOR", f"a request carries 1..{MAX_BLOCKS} blocks")
    blocks = tuple(_block(item, i) for i, item in enumerate(raw))
    if len({b.job_digest for b in blocks}) != len(blocks):
        raise RequestValidationError("DESCRIPTOR", "a job appears twice in the request")
    deliveries = [(a, c) for b in blocks for a, c, _ in b.recipients]
    if len(set(deliveries)) != len(deliveries):
        raise RequestValidationError("DESCRIPTOR", "an (agent_id, chunk) recipient appears twice in the request")
    return blocks


def _jobs(raw: Any, blocks: tuple[RequestBlock, ...], target: str) -> tuple[QSearchJob, ...]:
    if not isinstance(raw, list) or len(raw) != len(blocks):
        raise RequestValidationError("JOBS", "jobs must list one job per descriptor block, in block order")
    jobs = []
    for position, (body, block) in enumerate(zip(raw, blocks)):
        try:
            job = parse_job(body)
        except JobValidationError as exc:
            raise RequestValidationError(f"JOB_{exc.code}", f"job {position}: {exc}") from None
        if (job.job_digest != block.job_digest or job.n != block.n or job.shots != block.shots
                or job.backend_seed != block.seed or job.classical_min_energy != block.classical_min_energy):
            raise RequestValidationError("MISMATCH", f"job {position} differs from descriptor block {position}")
        if job.backend_kind != REQUEST_BACKEND_KIND or job.target != target:
            raise RequestValidationError("MISMATCH", f"job {position} is not an ibm job on the request target")
        jobs.append(job)
    return tuple(jobs)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return types.MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(v) for v in value)
    return value


def parse_request(body: Any) -> QSearchRequest:
    """Validate ``body`` completely, then return the typed request (never partial)."""
    if not isinstance(body, Mapping) or body.get("schema") != REQUEST_SCHEMA or set(body) != _BODY_KEYS:
        raise RequestValidationError("SCHEMA", f"body must be exactly schema={REQUEST_SCHEMA}, descriptor, jobs")
    desc = body["descriptor"]
    if not isinstance(desc, Mapping):
        raise RequestValidationError("DESCRIPTOR", "descriptor must be an object")
    _descriptor_header(desc)
    blocks = _blocks(desc)
    jobs = _jobs(body["jobs"], blocks, desc["target"])
    if desc["target"] in LIMITED_TARGETS and desc["max_execution_time"] > sum(job.max_qpu_s for job in jobs):
        raise RequestValidationError("LIMITS", "max_execution_time exceeds what the jobs' limits.max_qpu_s allow")
    return QSearchRequest(request_id=desc["request_id"], target=desc["target"], purpose=desc["purpose"],
                          max_execution_time=desc["max_execution_time"], blocks=blocks, jobs=jobs,
                          request_digest=canonical_digest(desc), descriptor=_deep_freeze(dict(desc)))


__all__ = ["LIMITED_TARGETS", "MAX_BLOCKS", "MAX_EXECUTION_TIME_S", "QSearchRequest", "REQUEST_BACKEND_KIND", "REQUEST_PURPOSES",
           "REQUEST_SCHEMA", "REQUEST_TARGETS", "RequestBlock", "RequestValidationError", "parse_request"]
