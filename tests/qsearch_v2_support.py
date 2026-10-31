"""Shared builders for the v2 request tests (lane HW0): jobs, descriptors, bodies and fake ATLAS receipts.

Nothing here touches IBM, Aer or ATLAS: jobs are the ``job_4var`` fixture re-targeted
and re-signed, and ATLAS request receipts are built by hand in the
``AqrcQSearchRequestReceiptV1`` shape (``campaign_runtime.run_qsearch_request_dry`` /
``submit_qsearch_request``).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from nano_qsearch.canonical import canonical_digest

FIXTURES = Path(__file__).parent / "qsearch_fixtures"
MINIMUM_DISPLAY = "1101"          # x = (1, 0, 1, 1), the job_4var exact minimum (-1.6), msb_first display order
MANIFEST = "sha256:" + "1" * 64
BATCH = "sha256:" + "2" * 64


def job_body(*, target: str = "fake_fez", shots: int = 8, seed: int = 7, max_qpu_s: int = 5,
             kind: str = "ibm") -> dict[str, Any]:
    body = json.loads((FIXTURES / "job_4var.json").read_text(encoding="utf-8"))
    body["compile"]["target"] = target
    body["backend"].update(kind=kind, shots=shots, seed=seed)
    body["limits"]["max_qpu_s"] = max_qpu_s
    unsigned = {k: v for k, v in body.items() if k != "job_digest"}
    return {**unsigned, "job_digest": canonical_digest(unsigned)}


def descriptor_for(jobs: list[dict[str, Any]], *, request_id: str = "req-hw0", target: str = "fake_fez",
                   purpose: str = "qcomms", max_execution_time: int = 10) -> dict[str, Any]:
    blocks = [{"pub_index": i, "job_digest": job["job_digest"],
               "recipients": [{"agent_id": "agent-007", "chunk": i, "slot": 0}],
               "n": len(job["variables"]), "shots": job["backend"]["shots"], "seed": job["backend"]["seed"],
               "classical_min_energy": job["audit"]["classical_min_energy"]} for i, job in enumerate(jobs)]
    return {"schema": "predator.qsearch.request.v2", "request_id": request_id, "target": target, "purpose": purpose,
            "max_execution_time": max_execution_time, "manifest_digest": MANIFEST, "batch_index": 0,
            "batch_digest": BATCH, "blocks": blocks}


def request_body(n: int = 3, *, target: str = "fake_fez", shots: int = 8, max_qpu_s: int = 5,
                 max_execution_time: int = 10, request_id: str = "req-hw0", purpose: str = "qcomms") -> dict[str, Any]:
    jobs = [job_body(target=target, shots=shots, seed=7 + i, max_qpu_s=max_qpu_s) for i in range(n)]
    return {"schema": "predator.qsearch.request.v2",
            "descriptor": descriptor_for(jobs, request_id=request_id, target=target, purpose=purpose,
                                         max_execution_time=max_execution_time),
            "jobs": jobs}


def resign(body: dict[str, Any]) -> dict[str, Any]:
    """Recompute every job digest after a test edited a job, keeping the descriptor in step."""
    body = copy.deepcopy(body)
    for i, job in enumerate(body["jobs"]):
        unsigned = {k: v for k, v in job.items() if k != "job_digest"}
        job["job_digest"] = canonical_digest(unsigned)
        body["descriptor"]["blocks"][i]["job_digest"] = job["job_digest"]
    return body


def child_sha(i: int) -> str:
    return "sha256:" + format(i + 10, "x").rjust(64, "0")


def atlas_request_receipt(request: Any, samples: list[list[str]] | None = None, *, usage: Any = None,
                          job_id: str = "fake-job-1") -> dict[str, Any]:
    """An ``AqrcQSearchRequestReceiptV1`` for ``request`` (a parsed ``QSearchRequest``)."""
    dry = request.target == "fake_fez"
    if samples is None:
        samples = [[MINIMUM_DISPLAY] * (job.shots - 1) + ["0000"] for job in request.jobs]
    children = [child_sha(i) for i in range(request.block_count)]
    envelope = {"schema": "AqrcQSearchRequestV1", "request_id": request.request_id, "purpose": request.purpose,
                "target": request.target, "children": children, "shots": [job.shots for job in request.jobs],
                "pub_count": request.block_count, "max_execution_time": request.max_execution_time,
                "recovery_tag": f"atlas-qsearch-req-{request.request_id}"}
    receipt = {"schema": "AqrcQSearchRequestReceiptV1", "request": {"envelope": envelope, "request_sha256": "x"},
               "purpose": request.purpose, "target": request.target, "backend_name": request.target,
               "dry_run": dry, "job_id": job_id, "status": "DONE", "pub_count": request.block_count,
               "max_execution_time": request.max_execution_time, "job_tags": [envelope["recovery_tag"]],
               "pubs": [{"pub_index": i, "register": "meas", "bit_order": "msb_first", "shots": len(raw),
                         "raw_samples": list(raw), "child_sha256": children[i]} for i, raw in enumerate(samples)]}
    if not dry:
        receipt.update(provider_final_usage_seconds=usage, usage_pending=not (usage and usage > 0),
                       provider_metrics={"usage": {"quantum_seconds": usage}})
    return receipt
