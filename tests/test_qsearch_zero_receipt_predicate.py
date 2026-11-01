"""Pure confirmed-zero identity predicate; HMAC authentication is a separate caller gate."""
import copy

import pytest

from nano_qsearch.receipt import SealError
from nano_qsearch.receipt_v2 import (confirmed_zero_receipt, request_receipt, seal_receipt_v2,
                                     verify_seal_v2)
from nano_qsearch.request import parse_request
from qsearch_v2_support import request_body
from test_qsearch_confirmed_zero import confirmed_zero

KEY = b"zero-predicate-test-key-" + b"x" * 32


def _sealed(status="CANCELLED"):
    request = parse_request(request_body(3, target="ibm_fez"))
    body = request_receipt(request, confirmed_zero(request, status), purpose=request.purpose)
    return request, seal_receipt_v2(body, KEY)


@pytest.mark.parametrize("status", ["CANCELLED", "ERROR"])
def test_authenticated_terminal_zero_has_one_shared_identity_predicate(status):
    request, receipt = _sealed(status)
    verify_seal_v2(receipt, KEY)
    assert confirmed_zero_receipt(request, receipt) is True
    assert receipt["status"] == "FAILED" and receipt["reason"] == "provider: " + status


@pytest.mark.parametrize("field,value", [
    ("schema", "future"), ("provider_api_version", "2025-05-01"), ("usage_status", "pending"),
    ("job_id", "wrong-job"), ("request_id", "other-request"), ("backend_name", "ibm_other"),
    ("client_request_digest", "sha256:" + "0" * 64), ("request_sha256", "0" * 64),
    ("plan_sha", "0" * 64), ("instance", ""), ("ledger_id", "b" * 32),
    ("job_tags", []), ("provider_status", "DONE"), ("evidence_sha256", "missing"),
    ("checked_at", " "), ("future_marker", True),
])
def test_even_an_authenticated_receipt_cannot_relax_proof_identity(field, value):
    request, receipt = _sealed()
    receipt["usage"]["zero_usage_evidence"][field] = value
    receipt = seal_receipt_v2(receipt, KEY)
    verify_seal_v2(receipt, KEY)
    assert confirmed_zero_receipt(request, receipt) is False


@pytest.mark.parametrize("path,value", [
    (("status",), "OK"), (("reason",), "cancelled"), (("blocks",), [{}]),
    (("request", "request_digest"), "sha256:" + "0" * 64), (("request", "block_count"), False),
    (("backend", "kind"), "none"), (("backend", "id"), "ibm_other"), (("backend", "dry_run"), True),
    (("hardware", "provider_job_id"), "other-job"), (("hardware", "backend_name"), "ibm_other"),
    (("hardware", "dry_run"), True), (("hardware", "purpose"), "other"),
    (("hardware", "max_execution_time"), True), (("hardware", "job_tags"), []),
    (("usage", "quantum_seconds"), False), (("usage", "quantum_seconds"), 2.5),
    (("usage", "usage_pending"), True), (("usage", "zero_usage_evidence"), None),
])
def test_wrong_terminal_hardware_usage_or_request_body_is_not_confirmed_zero(path, value):
    request, receipt = _sealed()
    target = receipt
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    receipt = seal_receipt_v2(receipt, KEY)
    verify_seal_v2(receipt, KEY)
    assert confirmed_zero_receipt(request, receipt) is False


def test_unknown_zero_and_a_different_request_stay_held():
    request, receipt = _sealed()
    other = parse_request(request_body(3, target="ibm_fez", request_id="another"))
    assert confirmed_zero_receipt(other, receipt) is False
    del receipt["usage"]["zero_usage_evidence"]
    receipt["usage"].update(usage_pending=False, quantum_seconds=0)
    assert confirmed_zero_receipt(request, seal_receipt_v2(receipt, KEY)) is False


def test_callers_must_authenticate_the_hardware_hmac_before_the_pure_predicate():
    request, receipt = _sealed()
    forged = copy.deepcopy(receipt)
    forged["seal"] = forged["seal"][:-1] + ("0" if forged["seal"][-1] != "0" else "1")
    # The pure predicate has no key: it is explicitly not a MAC authenticator.
    assert confirmed_zero_receipt(request, forged) is True
    with pytest.raises(SealError):
        verify_seal_v2(forged, KEY)
    unsigned = {key: value for key, value in receipt.items() if key != "seal"}
    assert confirmed_zero_receipt(request, unsigned) is False
