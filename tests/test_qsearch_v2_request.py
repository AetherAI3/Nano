"""Lane HW0 N4: v2 request parsing, cross-checks, per-block counts/audit and the v2 seal (no ATLAS, no Aer)."""
import copy
import hashlib
import hmac

import pytest

from nano_qsearch.canonical import canonical_digest, canonical_json
from nano_qsearch.receipt import HARDWARE_SEAL_PREFIX, ReceiptAuditError, SealError, verify_seal
from nano_qsearch.receipt_v2 import (
    HARDWARE_SEAL_PREFIX_V2, SEAL_PREFIX_V2, not_run_v2, request_receipt, results_digest, seal_receipt_v2,
    verify_seal_v2,
)
from nano_qsearch.request import MAX_BLOCKS, RequestValidationError, parse_request
from qsearch_v2_support import MINIMUM_DISPLAY, atlas_request_receipt, request_body, resign

KEY = b"r" * 32


def _code(body):
    with pytest.raises(RequestValidationError) as err:
        parse_request(body)
    return err.value.code


# --- parsing and cross-checks ------------------------------------------------------------------


def test_parses_a_three_block_request_and_digests_the_descriptor():
    body = request_body(3)
    request = parse_request(body)
    assert request.block_count == 3 and request.request_id == "req-hw0" and request.target == "fake_fez"
    assert request.request_digest == canonical_digest(body["descriptor"])
    assert [job.job_digest for job in request.jobs] == [b["job_digest"] for b in body["descriptor"]["blocks"]]
    assert request.blocks[1].recipients == (("agent-007", 1, 0),)


@pytest.mark.parametrize("mutate, code", [
    (lambda b: b.update(schema="predator.qsearch.request.v1"), "SCHEMA"),
    (lambda b: b.update(extra=1), "SCHEMA"),
    (lambda b: b["descriptor"].update(request_id="bad id!"), "DESCRIPTOR"),
    (lambda b: b["descriptor"].update(target="heron_heavy_hex"), "DESCRIPTOR"),
    (lambda b: b["descriptor"].update(purpose="research"), "DESCRIPTOR"),
    (lambda b: b["descriptor"].update(max_execution_time=0), "DESCRIPTOR"),
    (lambda b: b["descriptor"].update(max_execution_time=601), "DESCRIPTOR"),
    (lambda b: b["descriptor"].update(max_execution_time=True), "DESCRIPTOR"),
    (lambda b: b["descriptor"].update(manifest_digest="sha256:XYZ"), "DESCRIPTOR"),
    (lambda b: b["descriptor"]["blocks"][1].update(pub_index=0), "DESCRIPTOR"),
    (lambda b: b["descriptor"]["blocks"][1].update(recipients=[]), "DESCRIPTOR"),
    (lambda b: b["descriptor"]["blocks"][1]["recipients"].append({"agent_id": "x", "chunk": 9, "slot": 0}),
     "DESCRIPTOR"),                                                          # slots must strictly increase
    (lambda b: b["descriptor"]["blocks"][1]["recipients"][0].update(chunk=0), "DESCRIPTOR"),   # duplicate delivery
    (lambda b: b["descriptor"]["blocks"][1].update(classical_min_energy=float("nan")), "DESCRIPTOR"),
    (lambda b: b["descriptor"]["blocks"][1].update(shots=9), "MISMATCH"),
    (lambda b: b["descriptor"]["blocks"][1].update(seed=99), "MISMATCH"),
    (lambda b: b["descriptor"]["blocks"][1].update(n=5), "MISMATCH"),
    (lambda b: b["descriptor"]["blocks"][1].update(classical_min_energy=-1.5), "MISMATCH"),
    (lambda b: b["jobs"].reverse(), "MISMATCH"),                              # reordered jobs
    (lambda b: b["jobs"].pop(), "JOBS"),                                      # dropped job
    (lambda b: b["jobs"][0].update(job_digest="sha256:" + "0" * 64), "JOB_DIGEST"),
])
def test_malformed_or_inconsistent_requests_are_refused(mutate, code):
    body = request_body(3)
    mutate(body)
    assert _code(body) == code


def test_a_repeated_job_is_refused():
    body = request_body(2)
    body["jobs"][1] = copy.deepcopy(body["jobs"][0])
    body["descriptor"]["blocks"][1]["job_digest"] = body["jobs"][0]["job_digest"]
    assert _code(body) == "DESCRIPTOR"


def test_block_count_bounds():
    assert parse_request(request_body(MAX_BLOCKS)).block_count == MAX_BLOCKS
    assert _code(request_body(MAX_BLOCKS + 1)) == "DESCRIPTOR"
    body = request_body(1)
    body["descriptor"]["blocks"], body["jobs"] = [], []
    assert _code(body) == "DESCRIPTOR"


def test_every_job_must_be_ibm_on_the_one_target():
    body = request_body(2)
    body["jobs"][1]["backend"]["kind"] = "mps"
    assert _code(resign(body)) == "MISMATCH"
    body = request_body(2)
    body["jobs"][1]["compile"]["target"] = "ibm_fez"
    assert _code(resign(body)) == "MISMATCH"


def test_ibm_fez_limit_must_fit_the_jobs_max_qpu_s():
    assert parse_request(request_body(3, target="ibm_fez", max_qpu_s=5, max_execution_time=15)).max_execution_time == 15
    assert _code(request_body(3, target="ibm_fez", max_qpu_s=5, max_execution_time=16)) == "LIMITS"
    assert _code(request_body(2, target="ibm_fez", max_qpu_s=0, max_execution_time=1)) == "LIMITS"
    # A FakeFez dry run spends nothing: its jobs need grant no QPU seconds.
    assert parse_request(request_body(2, max_qpu_s=0, max_execution_time=10)).block_count == 2


# --- receipt body: counts, audit, hardware, usage ------------------------------------------------


def test_ok_body_keeps_raw_display_counts_in_pub_order_with_v1_audit():
    request = parse_request(request_body(3))
    samples = [[MINIMUM_DISPLAY] * 7 + ["0000"], ["0000"] * 8, [MINIMUM_DISPLAY, "1000"] * 4]
    body = request_receipt(request, atlas_request_receipt(request, samples), purpose="qcomms")
    assert [b["pub_index"] for b in body["blocks"]] == [0, 1, 2] == [b["block_index"] for b in body["blocks"]]
    first, second, third = body["blocks"]
    assert first["counts"] == {"1101": 7, "0000": 1} and third["counts"] == {"1101": 4, "1000": 4}
    assert first["counts_digest"] == canonical_digest(first["counts"])
    assert first["audit"] == {"classical_min_energy": -1.6, "best_sample_energy": pytest.approx(-1.6), "agree": True}
    assert second["audit"]["agree"] is False and second["audit"]["best_sample_energy"] == 0.0
    assert third["audit"]["agree"] is True
    assert body["results_digest"] == results_digest(body["blocks"])
    assert body["backend"] == {"kind": "ibm", "id": "fake_fez", "dry_run": True}
    assert body["hardware"] == {"backend_name": "fake_fez", "provider_job_id": "fake-job-1", "bit_order": "msb_first",
                                "register": "meas", "purpose": "qcomms", "dry_run": True, "max_execution_time": 10,
                                "job_tags": ["atlas-qsearch-req-req-hw0"]}
    assert body["usage"] == {"quantum_seconds": None, "usage_pending": True, "provider_sub_jobs": None,
                             "provider_metrics": None}
    assert set(body) == {"schema", "receipt_version", "status", "request", "backend", "hardware", "blocks",
                         "results_digest", "usage"}
    assert body["request"] == {"request_digest": request.request_digest, "block_count": 3}


@pytest.mark.parametrize("usage, seconds, pending", [(2.5, 2.5, False), (0, 0.0, True), (None, None, True),
                                                     (float("nan"), None, True)])
def test_live_usage_is_reported_and_zero_is_pending_never_free(usage, seconds, pending):
    request = parse_request(request_body(2, target="ibm_fez"))
    body = request_receipt(request, atlas_request_receipt(request, usage=usage), purpose="qcomms")
    assert body["usage"]["quantum_seconds"] == seconds and body["usage"]["usage_pending"] is pending
    assert body["backend"]["dry_run"] is False and body["hardware"]["dry_run"] is False


@pytest.mark.parametrize("mutate", [
    lambda r: r["pubs"].pop(),                                                   # fewer PUBs than blocks
    lambda r: r["pubs"].reverse(),                                               # reordered PUBs
    lambda r: r["pubs"].__setitem__(1, dict(r["pubs"][0])),                      # duplicated PUB
    lambda r: r["pubs"][0]["raw_samples"].pop(),                                 # shots short
    lambda r: r["pubs"][0].update(bit_order="x0_first"),
    lambda r: r["pubs"][0].update(raw_samples=["00111"] * 8),                    # wrong width
    lambda r: r["pubs"][1].update(register="c"),                                 # two registers
    lambda r: r["pubs"][1].update(child_sha256="sha256:" + "f" * 64),            # another child
    lambda r: r["request"]["envelope"].update(request_id="other"),
    lambda r: r["request"]["envelope"].update(max_execution_time=11),
    lambda r: r.update(dry_run=False),
    lambda r: r.update(job_id=""),
    lambda r: r.update(schema="AqrcQSearchJobReceiptV1"),
])
def test_a_delegate_receipt_that_does_not_match_the_request_is_refused_whole(mutate):
    request = parse_request(request_body(3))
    atlas = atlas_request_receipt(request)
    mutate(atlas)
    with pytest.raises(ReceiptAuditError):
        request_receipt(request, atlas, purpose="qcomms")


def test_a_block_below_its_classical_floor_refuses_the_whole_request():
    body = request_body(2)
    for block, job in zip(body["descriptor"]["blocks"], body["jobs"]):
        job["audit"]["classical_min_energy"] = 0.0
        block["classical_min_energy"] = 0.0
    request = parse_request(resign(body))
    with pytest.raises(ReceiptAuditError, match="below the classical floor"):
        request_receipt(request, atlas_request_receipt(request), purpose="qcomms")


# --- seal domains ---------------------------------------------------------------------------------


def _reference_mac(domain, body):
    return hmac.new(KEY, domain + canonical_json(body).encode("utf-8"), hashlib.sha256).hexdigest()


def test_v2_seal_matches_the_bench_spec_domains_and_never_crosses_v1():
    request = parse_request(request_body(2))
    body = request_receipt(request, atlas_request_receipt(request), purpose="qcomms")
    sealed = seal_receipt_v2(body, KEY)
    assert sealed["seal"] == HARDWARE_SEAL_PREFIX_V2 + _reference_mac(b"predator.qsearch.hardware-receipt.v2\n", body)
    verify_seal_v2(sealed, KEY)
    unsigned = not_run_v2(request, "ibm: MemoryError")
    not_run = seal_receipt_v2(unsigned, KEY)
    assert not_run["seal"] == SEAL_PREFIX_V2 + _reference_mac(b"predator.qsearch.receipt.v2\n", unsigned)
    assert not_run["blocks"] == [] and not_run["backend"] == {"kind": "none"} and not_run["request"]["block_count"] == 0
    # A v1 hardware seal over the same body never verifies as v2, and a v2 seal never verifies as v1.
    v1_style = {**body, "seal": HARDWARE_SEAL_PREFIX + _reference_mac(b"predator.qsearch.hardware-receipt.v1\n", body)}
    with pytest.raises(SealError):
        verify_seal_v2(v1_style, KEY)
    with pytest.raises(SealError):
        verify_seal(sealed, KEY)
    tampered = copy.deepcopy(sealed)
    tampered["blocks"][0]["counts"] = {"1101": 8}
    with pytest.raises(SealError):
        verify_seal_v2(tampered, KEY)
    with pytest.raises(ValueError):
        seal_receipt_v2(body, b"short")
