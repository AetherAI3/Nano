"""Lifecycle adapter tests use a provider double; no SDK, credentials or hardware."""
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from nano_qsearch import runtime_ibm as ibm
from nano_qsearch.request import parse_request
from qsearch_v2_support import request_body


class Job:
    instance = "instance-a"

    def __init__(self, states=("QUEUED",), *, cancel_error=None):
        self.states = list(states)
        self.cancels = self.reads = 0
        self.cancel_error = cancel_error

    def job_id(self):
        return "job-a"

    def backend(self):
        return SimpleNamespace(name="ibm_fez")

    def status(self):
        self.reads += 1
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def cancel(self):
        self.cancels += 1
        if self.cancel_error:
            raise self.cancel_error

    def result(self, *args, **kwargs):
        pytest.fail("lifecycle observation must never wait for a result")


def adapter(monkeypatch, job, *, attached=True, matches=None, digest=None):
    request = parse_request(request_body(1, target="ibm_fez", max_qpu_s=10))
    envelope = {"request_id": request.request_id, "target": request.target, "purpose": request.purpose,
                "client_request_digest": request.request_digest if digest is None else digest}
    stored = {"schema": "AqrcQSearchSubmissionV1", "request": {"envelope": envelope}, "job_tags": ["tag-a"]}
    row = {"receipt": json.dumps(stored), "job_id": "job-a" if attached else None,
           "usage": None, "cap": 10}
    ledger = SimpleNamespace(get=lambda rid: row, reserved_at=lambda rid: 100)
    lookups, checks = [], []

    def jobs(**kwargs):
        lookups.append(kwargs)
        return [job] if matches is None else matches

    service = SimpleNamespace(job=lambda jid: job, jobs=jobs)
    context = SimpleNamespace(service=service, instance="instance-a", backend_name="ibm_fez")
    runtime = SimpleNamespace(
        _verify_qsearch_recovery_context=lambda *args: checks.append("context"),
        _verify_qsearch_submission=lambda *args: (envelope, ["child"], ["tag-a"]),
        _verify_qsearch_provider_job=lambda *args: checks.append("job"))
    atlas, aqrc = ModuleType("aether_atlas"), ModuleType("aether_atlas.aqrc")
    aqrc.campaign_runtime = runtime
    atlas.aqrc = aqrc
    monkeypatch.setitem(sys.modules, "aether_atlas", atlas)
    monkeypatch.setitem(sys.modules, "aether_atlas.aqrc", aqrc)
    monkeypatch.setattr(ibm, "_load_ledger", lambda env: ledger)
    monkeypatch.setattr(ibm, "atlas_resolve_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(ibm, "_verify_cancel_authority", lambda *args: None)
    env = {ibm.GO_ENV: "1", ibm.LIVE_ENV: "1"}
    return request, env, row, lookups, checks


def test_observe_reads_status_without_waiting_or_budget_mutation(monkeypatch):
    job = Job()
    request, env, row, _, checks = adapter(monkeypatch, job)
    original = dict(row)
    observed = ibm.atlas_request_observe(request, environ=env)
    assert observed == {"provider_job_id": "job-a", "provider_status": "QUEUED",
                        "held_qpu_s": 10, "settled_qpu_s": None}
    assert row == original
    assert checks == ["context", "job"]
    assert (job.reads, job.cancels) == (1, 0)


def test_lost_submission_ack_uses_one_bounded_identity_lookup(monkeypatch):
    job = Job()
    request, env, _, lookups, checks = adapter(monkeypatch, job, attached=False)
    assert ibm.atlas_request_observe(request, environ=env)["provider_job_id"] == "job-a"
    assert lookups == [{"instance": "instance-a", "backend_name": "ibm_fez", "job_tags": ["tag-a"], "limit": 2}]
    assert checks == ["context", "job"]


@pytest.mark.parametrize("matches", [[], [Job(), Job()]])
def test_ambiguous_submission_stays_held_without_cancellation(monkeypatch, matches):
    job = Job()
    request, env, row, _, _ = adapter(monkeypatch, job, attached=False, matches=matches)
    with pytest.raises(ibm.LifecycleAdapterError, match="PROVIDER_SUBMISSION_AMBIGUOUS"):
        ibm.atlas_request_cancel(request, environ=env)
    assert row["usage"] is None
    assert job.cancels == 0


@pytest.mark.parametrize("states,cancels,confirmed", [
    (("QUEUED", "QUEUED"), 1, False),
    (("QUEUED", "CANCELLED"), 1, True),
    (("RUNNING", "DONE"), 1, False),
    (("DONE",), 0, False),
    (("CANCELLED",), 0, True),
])
def test_cancel_requires_terminal_provider_confirmation(monkeypatch, states, cancels, confirmed):
    job = Job(states)
    request, env, row, _, _ = adapter(monkeypatch, job)
    observed = ibm.atlas_request_cancel(request, environ=env)
    assert observed["cancellation_confirmed"] is confirmed
    assert job.cancels == cancels
    assert row["usage"] is None
    assert observed["settled_qpu_s"] is None


def test_lost_cancel_ack_can_confirm_terminal_status_without_inventing_settlement(monkeypatch):
    job = Job(("QUEUED", "CANCELLED"), cancel_error=TimeoutError("secret-token"))
    request, env, row, _, _ = adapter(monkeypatch, job)
    observed = ibm.atlas_request_cancel(request, environ=env)
    assert observed["cancellation_confirmed"] is True
    assert observed["cancel_acknowledged"] is False
    assert observed["cancel_error_code"] == "CANCEL_ACKNOWLEDGEMENT_UNKNOWN"
    assert "secret-token" not in json.dumps(observed)
    assert row["usage"] is None


def test_wrong_digest_refuses_before_provider_lookup(monkeypatch):
    job = Job()
    request, env, _, _, checks = adapter(monkeypatch, job, digest="sha256:" + "f" * 64)
    with pytest.raises(ibm.LifecycleAdapterError, match="PROVIDER_IDENTITY_MISMATCH"):
        ibm.atlas_request_cancel(request, environ=env)
    assert checks == []
    assert (job.cancels, job.reads) == (0, 0)


def test_default_off_live_gates_prevent_lookup(monkeypatch):
    request = parse_request(request_body(1, target="ibm_fez", max_qpu_s=10))
    monkeypatch.setattr(ibm, "_load_ledger", lambda env: pytest.fail("default-off must not touch ledger"))
    with pytest.raises(ibm.LifecycleAdapterError, match="LIVE_CONTEXT_REQUIRED"):
        ibm.atlas_request_cancel(request, environ={})


@pytest.mark.parametrize("check", ["_verify_qsearch_recovery_context", "_verify_qsearch_provider_job"])
def test_atlas_identity_denial_cannot_cancel_or_leak_provider_error(monkeypatch, check):
    job = Job()
    request, env, row, _, _ = adapter(monkeypatch, job)

    def deny(*args):
        raise ValueError("wrong-instance secret-token")

    monkeypatch.setattr(sys.modules["aether_atlas.aqrc"].campaign_runtime, check, deny)
    runtime = ibm.IbmRuntime(environ=env)
    with pytest.raises(ibm.LifecycleAdapterError) as raised:
        runtime.cancel_request(request, record={"cancellation_requested_at": 100})
    assert raised.value.code == "PROVIDER_IDENTITY_MISMATCH"
    assert raised.value.retryable is False
    assert raised.value.next_action == "MANUAL_RECONCILIATION"
    assert "secret-token" not in str(raised.value)
    assert row["usage"] is None
    assert job.cancels == 0


def test_unknown_provider_status_does_not_request_cancel(monkeypatch):
    job = Job(("something-new",))
    request, env, row, _, _ = adapter(monkeypatch, job)
    with pytest.raises(ibm.LifecycleAdapterError, match="PROVIDER_STATUS_UNKNOWN"):
        ibm.atlas_request_cancel(request, environ=env)
    assert row["usage"] is None
    assert job.cancels == 0


def test_unauthorized_cancel_stops_before_any_provider_lookup(monkeypatch):
    job = Job()
    request, env, row, _, checks = adapter(monkeypatch, job)

    def deny(*args):
        raise ibm.LifecycleAdapterError("CANCELLATION_NOT_AUTHORIZED", field="authority")

    monkeypatch.setattr(ibm, "_verify_cancel_authority", deny)
    with pytest.raises(ibm.LifecycleAdapterError, match="CANCELLATION_NOT_AUTHORIZED"):
        ibm.atlas_request_cancel(request, environ=env)
    assert checks == []
    assert (job.reads, job.cancels) == (0, 0)
    assert row["usage"] is None


def test_dispatch_event_is_after_validation_and_before_cancel_io(monkeypatch):
    job = Job(("QUEUED", "CANCELLED"))
    request, env, _, _, checks = adapter(monkeypatch, job)
    events = []

    def dispatch():
        assert checks == ["context", "job"]
        assert job.cancels == 0
        events.append("durable-dispatch")

    ibm.atlas_request_cancel(request, environ=env, before_cancel=dispatch)
    assert events == ["durable-dispatch"]
    assert job.cancels == 1


def test_failed_durable_dispatch_event_prevents_cancel_rpc(monkeypatch):
    job = Job()
    request, env, _, _, _ = adapter(monkeypatch, job)

    def dispatch():
        raise OSError("store unavailable")

    with pytest.raises(OSError, match="store unavailable"):
        ibm.atlas_request_cancel(request, environ=env, before_cancel=dispatch)
    assert job.cancels == 0


@pytest.mark.parametrize("state,usage,held,settled", [
    ("awaiting_accounting", 0, 10, None),
    ("cancelled", 0, 10, None),
    ("submitted", 2, 10, None),
    ("done", 2, 0, 2),
])
def test_only_terminal_positive_ledger_settlement_is_exposed(monkeypatch, state, usage, held, settled):
    request, env, row, _, _ = adapter(monkeypatch, Job(("DONE",)))
    row.update(state=state, usage=usage)
    observed = ibm.atlas_request_observe(request, environ=env)
    assert observed["held_qpu_s"] == held
    assert observed["settled_qpu_s"] == settled


def test_runtime_requires_durable_cancel_fence_and_reports_safe_errors():
    request = parse_request(request_body(1, target="ibm_fez", max_qpu_s=10))
    calls = []

    def cancel(*args, **kwargs):
        calls.append(1)
        raise OSError("credential secret-token")

    runtime = ibm.IbmRuntime(request_cancel_fn=cancel)
    with pytest.raises(ibm.LifecycleAdapterError, match="CANCELLATION_FENCE_REQUIRED"):
        runtime.cancel_request(request, record={})
    assert calls == []
    with pytest.raises(ibm.LifecycleAdapterError) as raised:
        runtime.cancel_request(request, record={"cancellation_requested_at": 100.0})
    assert raised.value.code == "PROVIDER_UNAVAILABLE"
    assert raised.value.retryable is True
    assert raised.value.next_action == "RECONCILE"
    assert "secret-token" not in str(raised.value)


def test_runtime_observation_preserves_accounting_and_confirms_only_cancelled():
    request = parse_request(request_body(1, target="ibm_fez", max_qpu_s=10))
    runtime = ibm.IbmRuntime(request_observe_fn=lambda *args, **kwargs: {
        "provider_status": "CANCELLED", "settled_qpu_s": None, "held_qpu_s": 10,
        "credential": "secret-token"})
    record = {"accounting": "pending"}
    runtime.observe_request(request, record=record)
    assert record == {"accounting": "pending", "provider_status": "CANCELLED", "settled_qpu_s": None,
                      "held_qpu_s": 10, "cancellation_confirmed": True}
