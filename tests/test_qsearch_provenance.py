"""Offline issuer/consumer contract and provenance-loss regression for issue #62."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from nano_qsearch.canonical import canonical_digest, canonical_json
from nano_qsearch.compile_manifest import (
    ManifestValidationError, build_compile_manifest, compile_identity, validate_compile_manifest,
)
from nano_qsearch.receipt import ReceiptAuditError, SealError
from nano_qsearch.receipt_v2 import request_receipt, seal_receipt_v2, verify_seal_v2
from nano_qsearch.receipt_v3 import (
    DOMAIN_V3, SEAL_PREFIX_V3, request_receipt_v3, reseal_usage_v3, seal_receipt_v3, verify_receipt_v3,
)
from nano_qsearch.request import parse_request
from qsearch_v2_support import atlas_request_receipt, child_sha, descriptor_for, job_body, request_body

KEY = bytes(range(32))
FIXTURE_PATH = Path(__file__).parent / "qsearch_fixtures" / "packed_provenance_v3.json"


def manifest_for(request, atlas):
    return build_compile_manifest(request, atlas_children=atlas["request"]["envelope"]["children"],
        compiled_plans=[{"compiled_plan_digest": canonical_digest({"plan": i}),
                         "isa_digest": canonical_digest({"isa": i}), "layout": list(range(block.n))}
                        for i, block in enumerate(request.blocks)],
        target_snapshot_digest=canonical_digest({"snapshot": request.target}), calibration_digest=None,
        profile={"schema": "nano.qsearch.compile-profile.v1", "id": "fixture-default",
                 "digest": canonical_digest({"basis_prep": False, "layout": None, "seed": 11})},
        compiler={"name": "nano-qsearch", "version": "1.1.0", "qiskit_version": "2.3.1",
                  "qiskit_ibm_runtime_version": "0.43.1"})


def mixed_request_body():
    jobs = [job_body(target="ibm_fez", shots=8, seed=7, max_qpu_s=5),
            job_body(target="ibm_fez", shots=5, seed=8, max_qpu_s=5)]
    small = jobs[1]
    small["variables"] = ["c0", "c1", "c2"]
    small["qubo"]["q"] = [[-1.0, 0.0, 0.0], [0.0, 0.5, 0.0], [0.0, 0.0, -0.5]]
    small["audit"]["classical_min_energy"] = -1.5
    small["job_digest"] = canonical_digest({k: v for k, v in small.items() if k != "job_digest"})
    return {"schema": "predator.qsearch.request.v2", "jobs": jobs,
            "descriptor": descriptor_for(jobs, target="ibm_fez", max_execution_time=10)}


def fixture_document():
    source = mixed_request_body()
    request = parse_request(source)
    atlas = atlas_request_receipt(request, [["1101"] * 7 + ["0000"], ["101"] * 4 + ["000"]])
    manifest = manifest_for(request, atlas)
    pending = seal_receipt_v3(request_receipt_v3(request, atlas, expected_manifest=manifest,
                                               purpose=request.purpose), KEY)
    settled = reseal_usage_v3(pending, KEY, expected_manifest=manifest,
        usage={"quantum_seconds": 1.25, "usage_pending": False, "provider_sub_jobs": 1,
               "provider_metrics": {"usage": {"quantum_seconds": 1.25}}},
        atlas_receipt_sha256=canonical_digest({"settled_atlas": "fixture"}))
    return {"contract": "nano-predator.packed-provenance-golden.v1", "key_hex": KEY.hex(),
            "request_body": source, "atlas_receipt": atlas, "expected_manifest": manifest,
            "canonical_manifest": canonical_json(manifest), "compile_identity": compile_identity(manifest),
            "pending_receipt": pending, "settled_receipt": settled}


def test_shared_golden_bytes_and_mixed_widths():
    golden = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert golden == fixture_document()
    assert [pub["n"] for pub in golden["expected_manifest"]["pubs"]] == [4, 3]
    for name in ("pending_receipt", "settled_receipt"):
        verify_receipt_v3(golden[name], bytes.fromhex(golden["key_hex"]), expected_manifest=golden["expected_manifest"])
    assert canonical_json(golden["expected_manifest"]) == golden["canonical_manifest"]


def test_original_one_pub_child_substitution_reproduction_is_regression():
    request = parse_request(request_body(1))
    atlas = atlas_request_receipt(request)
    other = copy.deepcopy(atlas)
    other["request"]["envelope"]["children"][0] = child_sha(99)
    other["pubs"][0]["child_sha256"] = child_sha(99)
    legacy = [seal_receipt_v2(request_receipt(request, item, purpose=request.purpose), KEY) for item in (atlas, other)]
    assert legacy[0] == legacy[1]  # Legacy semantics deliberately remain unchanged.
    verify_seal_v2(legacy[0], KEY)
    manifest = manifest_for(request, atlas)
    with pytest.raises(ReceiptAuditError, match="children"):
        request_receipt_v3(request, other, expected_manifest=manifest, purpose=request.purpose)
    fresh = manifest_for(request, other)
    first = seal_receipt_v3(request_receipt_v3(request, atlas, expected_manifest=manifest, purpose=request.purpose), KEY)
    second = seal_receipt_v3(request_receipt_v3(request, other, expected_manifest=fresh, purpose=request.purpose), KEY)
    assert first["seal"] != second["seal"]
    with pytest.raises(SealError, match="expected manifest"):
        verify_receipt_v3(second, KEY, expected_manifest=manifest)
    with pytest.raises(SealError):
        verify_receipt_v3(legacy[0], KEY, expected_manifest=manifest)
    with pytest.raises(SealError):
        verify_seal_v2(first, KEY)


@pytest.mark.parametrize("field", ["atlas_child_digest", "compiled_plan_digest", "isa_digest", "layout", "profile", "target", "compiler", "order"])
def test_changed_compile_choice_changes_identity_and_fails_expectation(field):
    golden = fixture_document()
    manifest = copy.deepcopy(golden["expected_manifest"])
    if field == "layout":
        manifest["pubs"][0][field] = [3, 2, 1, 0]
    elif field in ("atlas_child_digest", "compiled_plan_digest", "isa_digest"):
        manifest["pubs"][0][field] = canonical_digest({"changed": field})
    elif field == "profile":
        manifest[field]["digest"] = canonical_digest({"changed": field})
    elif field == "target":
        manifest[field]["snapshot_digest"] = canonical_digest({"changed": field})
    elif field == "compiler":
        manifest[field]["qiskit_version"] = "2.2.0"
    else:
        manifest["pubs"].reverse()
        for i, pub in enumerate(manifest["pubs"]):
            pub["pub_index"] = i
    assert compile_identity(manifest) != golden["compile_identity"]
    with pytest.raises(SealError):
        verify_receipt_v3(golden["pending_receipt"], KEY, expected_manifest=manifest)


@pytest.mark.parametrize("mutation", ["partial", "reorder", "duplicate", "wrong_width", "bool_index"])
def test_provider_results_must_be_complete_ordered_and_typed(mutation):
    golden = fixture_document()
    atlas = copy.deepcopy(golden["atlas_receipt"])
    if mutation == "partial":
        atlas["pubs"].pop()
    elif mutation == "reorder":
        atlas["pubs"].reverse()
    elif mutation == "duplicate":
        atlas["pubs"][1] = copy.deepcopy(atlas["pubs"][0])
    elif mutation == "wrong_width":
        atlas["pubs"][1]["raw_samples"][0] = "1101"
    else:
        atlas["pubs"][1]["pub_index"] = True
    request = parse_request(golden["request_body"])
    with pytest.raises(ReceiptAuditError):
        request_receipt_v3(request, atlas, expected_manifest=golden["expected_manifest"], purpose=request.purpose)


def test_positive_settlement_preserves_identity_and_every_old_signed_accounting_revision():
    golden = fixture_document()
    pending, settled = golden["pending_receipt"], golden["settled_receipt"]
    usage = {**settled["usage"], "quantum_seconds": 2.5}
    latest = reseal_usage_v3(settled, KEY, expected_manifest=golden["expected_manifest"], usage=usage)
    verify_receipt_v3(latest, KEY, expected_manifest=golden["expected_manifest"])
    assert latest["compile_identity"] == settled["compile_identity"] == pending["compile_identity"]
    assert latest["compile_manifest"] == pending["compile_manifest"]
    assert latest["accounting_history"][0]["seal"] == pending["seal"]
    assert latest["accounting_history"][0]["usage"] == pending["usage"]
    assert latest["accounting_history"][1]["seal"] == settled["seal"]
    assert latest["seal"] != settled["seal"]
    corrupt = copy.deepcopy(latest)
    corrupt["accounting_history"][0]["usage"]["quantum_seconds"] = 999
    with pytest.raises((ReceiptAuditError, SealError)):
        seal_receipt_v3(corrupt, KEY)
    for seconds in (None, 0, 1.0):
        with pytest.raises(ReceiptAuditError):
            reseal_usage_v3(settled, KEY, expected_manifest=golden["expected_manifest"],
                            usage={**usage, "quantum_seconds": seconds, "usage_pending": not bool(seconds)})


@pytest.mark.parametrize("mutation", ["schema", "extra", "bool", "duplicate", "layout", "digest", "missing_profile"])
def test_manifest_round_trip_strict_malformed_rejection(mutation):
    source = fixture_document()["expected_manifest"]
    assert validate_compile_manifest(json.loads(canonical_json(source))) == source
    bad = copy.deepcopy(source)
    if mutation == "schema":
        bad["schema"] = "nano.qsearch.compile-manifest.v0"
    elif mutation == "extra":
        bad["secret"] = "must not be carried"
    elif mutation == "bool":
        bad["pubs"][0]["seed"] = True
    elif mutation == "duplicate":
        bad["pubs"][1]["job_digest"] = bad["pubs"][0]["job_digest"]
    elif mutation == "layout":
        bad["pubs"][0]["layout"] = [0, 0, 1, 2]
    elif mutation == "digest":
        bad["pubs"][0]["isa_digest"] = "sha256:bad"
    else:
        del bad["profile"]
    with pytest.raises(ManifestValidationError):
        validate_compile_manifest(bad)


def test_detached_manifest_and_receipt_do_not_alias_inputs():
    golden = fixture_document()
    expected = golden["expected_manifest"]
    detached = validate_compile_manifest(expected)
    detached["pubs"][0]["layout"].reverse()
    assert detached != expected
    unsigned = copy.deepcopy(golden["pending_receipt"])
    sealed = seal_receipt_v3(unsigned, KEY)
    unsigned["compile_manifest"]["pubs"][0]["layout"].reverse()
    verify_receipt_v3(sealed, KEY, expected_manifest=expected)


def test_failed_submission_preserves_expected_compile_identity_with_no_result_claim():
    golden = fixture_document()
    atlas = copy.deepcopy(golden["atlas_receipt"])
    atlas.update(status="FAILED", provider_status="CANCELLED", pubs=[])
    request = parse_request(golden["request_body"])
    sealed = seal_receipt_v3(request_receipt_v3(request, atlas, expected_manifest=golden["expected_manifest"],
                                             purpose=request.purpose), KEY)
    assert sealed["status"] == "FAILED" and sealed["blocks"] == []
    verify_receipt_v3(sealed, KEY, expected_manifest=golden["expected_manifest"])


def _raw_sign(payload):
    body = {key: value for key, value in payload.items() if key != "seal"}
    return {**body, "seal": SEAL_PREFIX_V3 + hmac.new(KEY, DOMAIN_V3 + canonical_json(body).encode(), hashlib.sha256).hexdigest()}


@pytest.mark.parametrize("field", ["purpose", "max_execution_time", "classical_min_energy", "job_tags", "register", "provider_job_id", "best_sample_energy"])
def test_independent_expected_source_request_rejects_validly_signed_changed_claim(field):
    golden = fixture_document()
    forged = copy.deepcopy(golden["pending_receipt"])
    if field in ("classical_min_energy", "best_sample_energy"):
        if field == "classical_min_energy":
            forged["blocks"][0]["audit"].update(classical_min_energy=-2.0, agree=False)
        else:
            forged["blocks"][0]["audit"].update(best_sample_energy=-1.0, agree=False)
        from nano_qsearch.receipt_v2 import results_digest
        forged["results_digest"] = results_digest(forged["blocks"])
    elif field == "purpose":
        forged["hardware"][field] = "qsearch"
    elif field == "max_execution_time":
        forged["hardware"][field] = 11
    elif field == "job_tags":
        forged["hardware"][field] = ["another-request"]
    else:
        forged["hardware"][field] = "x" * 257
    forged = _raw_sign(forged)  # A real HMAC must not bypass the independent expected-source check.
    with pytest.raises(SealError):
        verify_receipt_v3(forged, KEY, expected_manifest=golden["expected_manifest"],
                          expected_request=parse_request(golden["request_body"]))


def test_recipient_bool_is_not_integer_even_under_a_valid_signature():
    golden = fixture_document()
    forged = copy.deepcopy(golden["pending_receipt"])
    forged["blocks"][0]["recipients"][0]["slot"] = False
    from nano_qsearch.receipt_v2 import results_digest
    forged["results_digest"] = results_digest(forged["blocks"])
    with pytest.raises(SealError, match="recipient types"):
        verify_receipt_v3(_raw_sign(forged), KEY, expected_manifest=golden["expected_manifest"])
