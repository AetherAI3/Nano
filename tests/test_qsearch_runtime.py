import json
import textwrap
import time
from dataclasses import replace

import pytest

from nano_qsearch import runtime_mps
from nano_qsearch.job import parse_job
from nano_qsearch.runtime_mps import MAX_ATTEMPTS, MpsRuntime, StatevectorRuntime, build_runtime, select_kind


@pytest.fixture
def job(fixtures_dir):
    return parse_job(json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8")))


def _big(job, n=21):
    return replace(job, variables=tuple(f"v{i}" for i in range(n)), q=tuple(tuple(0.0 for _ in range(n)) for _ in range(n)))


def _fake_engine(tmp_path, body: str):
    path = tmp_path / "fake_engine.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# A well-formed result: one sample at the job's true minimiser (found by enumerating 2^n
# assignments of x^T Q x + offset), reported at the job's classical_min_energy, so it audits.
RESULT_BUILDER = '''
import argparse, json, pathlib
p = argparse.ArgumentParser(); p.add_argument("--job"); p.add_argument("--out"); p.add_argument("--kind")
a = p.parse_args()
job = json.loads(pathlib.Path(a.job).read_text(encoding="utf-8"))
q, off, n = job["qubo"]["q"], job["qubo"]["offset"], len(job["variables"])
def energy(x):
    return sum(x[i] * q[i][j] * x[j] for i in range(n) for j in range(n)) + off
best_x = min((tuple((k >> i) & 1 for i in range(n)) for k in range(1 << n)), key=energy)
floor = job["audit"]["classical_min_energy"]
res = {"kind": a.kind, "backend_id": "fake", "variant": "ordinary", "nuclei": [], "compiled_plan_digest": "sha256:" + "a"*64,
       "compile": {}, "metrics": {"logical_2q": 1, "native_2q": 1, "native_depth": 1},
       "chi_max": None, "chi_reached": None, "truncated": None, "shots": 1, "seed": 7, "wall_s": 0.0, "attempts": 1,
       "samples": [["".join(map(str, best_x)), 1, floor]], "best_sample_energy": floor,
       "mean_energy_sample": floor, "mean_energy_ideal": None, "tvd_ideal": None, "exact_min_energy": None}
'''

OK_ENGINE = RESULT_BUILDER + '''
pathlib.Path(a.out).write_text(json.dumps(res))
'''

CRASH_ENGINE = '''
import os, sys
marker = os.environ["CRASH_MARKER"]
n = int(open(marker).read()) if os.path.exists(marker) else 0
open(marker, "w").write(str(n + 1))
if n < int(os.environ.get("CRASH_TIMES", "99")):
    sys.exit(3)
'''


def test_ok_path(job, tmp_path):
    rt = StatevectorRuntime(engine_path=_fake_engine(tmp_path, OK_ENGINE))
    body = rt.run(job)
    assert body["status"] == "OK" and body["audit"]["agree"] is True and body["backend"]["attempts"] == 1


def test_crash_retries_then_not_run(job, tmp_path, monkeypatch):
    marker = tmp_path / "count"
    monkeypatch.setenv("CRASH_MARKER", str(marker))
    body = MpsRuntime(engine_path=_fake_engine(tmp_path, CRASH_ENGINE)).run(job)
    assert body["status"] == "NOT_RUN" and body["reason"] == "engine: CalledProcessError"
    assert MAX_ATTEMPTS == 5 and int(marker.read_text()) == 5        # one run plus four retries, pinned literally


def test_child_failure_logs_tail_to_service_stderr_not_receipt(job, tmp_path, monkeypatch, capsys):
    marker = tmp_path / "count"
    monkeypatch.setenv("CRASH_MARKER", str(marker))
    engine = _fake_engine(tmp_path, 'import sys\nsys.stderr.write("x" * 2000 + "TAIL-SECRET\\n")\n' + CRASH_ENGINE)
    body = MpsRuntime(engine_path=engine).run(job)
    err = capsys.readouterr().err
    assert body["reason"] == "engine: CalledProcessError" and "TAIL-SECRET" not in json.dumps(body)
    lines = [ln for ln in err.splitlines() if ln.startswith("nano-qsearch: engine child exited 3:")]
    assert len(lines) == 5 and all("TAIL-SECRET" in ln and len(ln) < 500 for ln in lines)


def test_wall_budget_is_per_job_not_per_attempt(job, tmp_path, monkeypatch):
    marker = tmp_path / "count"
    monkeypatch.setenv("CRASH_MARKER", str(marker))
    engine = _fake_engine(tmp_path, "import time\ntime.sleep(0.6)\n" + CRASH_ENGINE)
    started = time.monotonic()
    body = MpsRuntime(engine_path=engine, wall_s_override=1).run(job)
    elapsed = time.monotonic() - started
    assert body["status"] == "NOT_RUN" and body["reason"] == "engine: TimeoutExpired"
    attempts = int(marker.read_text()) if marker.exists() else 0
    assert attempts <= 2 and elapsed < 3.5


def test_os_error_never_raises(job, tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise PermissionError("no temp dir")
    monkeypatch.setattr(runtime_mps.tempfile, "TemporaryDirectory", boom)
    body = MpsRuntime(engine_path=_fake_engine(tmp_path, OK_ENGINE)).run(job)
    assert body["status"] == "NOT_RUN" and body["reason"] == "engine: PermissionError"


LEAK_PROBE = '''
import argparse, json, os, pathlib
p = argparse.ArgumentParser(); p.add_argument("--job"); p.add_argument("--out"); p.add_argument("--kind"); a = p.parse_args()
prefixes = ("NANO_QSEARCH_KEY", "NANO_QSEARCH_REQUEST_KEY", "AETHER_AQRC_", "QISKIT_IBM", "IBM_")
leak = any(k.upper().startswith(prefixes) for k in os.environ)
kept = os.environ.get("NANO_QSEARCH_ATLAS_PATH") == os.environ.get("PROBE_EXPECT_ATLAS")
pathlib.Path(a.out).write_text(json.dumps({"error": {"type": "LEAK" if leak else ("Clean" if kept else "LostAtlas")}}))
'''


def test_child_env_drops_keys_and_ibm_credentials(job, tmp_path, monkeypatch):
    for name in ("NANO_QSEARCH_KEY_PATH", "NANO_QSEARCH_REQUEST_KEY_PATH", "AETHER_AQRC_HW_GO",
                 "AETHER_AQRC_AUTHORITY_PATH", "QISKIT_IBM_TOKEN", "IBM_CLOUD_API_KEY"):
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("NANO_QSEARCH_ATLAS_PATH", str(tmp_path))
    monkeypatch.setenv("PROBE_EXPECT_ATLAS", str(tmp_path))
    assert StatevectorRuntime(engine_path=_fake_engine(tmp_path, LEAK_PROBE)).run(job)["reason"] == "engine: Clean"


def test_child_env_probe_detects_a_leak(job, tmp_path, monkeypatch):
    # positive control: the probe does report LEAK when a secret reaches the child
    monkeypatch.setenv("QISKIT_IBM_TOKEN", "secret")
    monkeypatch.setattr(runtime_mps, "SECRET_ENV_PREFIXES", ("NOTHING_",))
    assert StatevectorRuntime(engine_path=_fake_engine(tmp_path, LEAK_PROBE)).run(job)["reason"] == "engine: LEAK"


def test_crash_twice_then_success_counts_attempts(job, tmp_path, monkeypatch):
    marker = tmp_path / "count"
    monkeypatch.setenv("CRASH_MARKER", str(marker))
    monkeypatch.setenv("CRASH_TIMES", "2")
    body = MpsRuntime(engine_path=_fake_engine(tmp_path, CRASH_ENGINE + "\n" + OK_ENGINE)).run(job)
    assert body["status"] == "OK" and body["backend"]["attempts"] == 3


@pytest.mark.parametrize("garbage", ['{"trunc', "[1, 2]"])
def test_unreadable_out_file_is_a_crash_and_retried(job, tmp_path, monkeypatch, garbage):
    marker = tmp_path / "count"
    monkeypatch.setenv("CRASH_MARKER", str(marker))
    monkeypatch.setenv("GARBAGE", garbage)
    engine = _fake_engine(tmp_path, RESULT_BUILDER + '''
import os
marker = os.environ["CRASH_MARKER"]
n = int(open(marker).read()) if os.path.exists(marker) else 0
open(marker, "w").write(str(n + 1))
pathlib.Path(a.out).write_text(os.environ["GARBAGE"] if n < 2 else json.dumps(res))
''')
    body = MpsRuntime(engine_path=engine).run(job)
    assert body["status"] == "OK" and body["backend"]["attempts"] == 3 and int(marker.read_text()) == 3


def test_result_failing_audit_is_not_run_and_not_retried(job, tmp_path, monkeypatch):
    marker = tmp_path / "count"
    monkeypatch.setenv("CRASH_MARKER", str(marker))
    engine = _fake_engine(tmp_path, RESULT_BUILDER + '''
import os
open(os.environ["CRASH_MARKER"], "a").write("x")
res["best_sample_energy"] = floor - 1.0          # undercuts the job's classical floor
pathlib.Path(a.out).write_text(json.dumps(res))
''')
    body = StatevectorRuntime(engine_path=engine).run(job)
    assert body["status"] == "NOT_RUN" and body["reason"] == "audit: ReceiptAuditError" and marker.read_text() == "x"


def test_result_missing_fields_is_not_run(job, tmp_path):
    engine = _fake_engine(tmp_path, RESULT_BUILDER + '''
del res["compiled_plan_digest"]
pathlib.Path(a.out).write_text(json.dumps(res))
''')
    # ok_receipt wraps a missing copied field as ReceiptAuditError("engine result is missing or malformed fields")
    assert StatevectorRuntime(engine_path=engine).run(job)["reason"] == "audit: ReceiptAuditError"


def test_engine_error_is_not_retried(job, tmp_path, monkeypatch):
    marker = tmp_path / "count"
    monkeypatch.setenv("CRASH_MARKER", str(marker))
    engine = _fake_engine(tmp_path, '''
import argparse, json, pathlib, os
p = argparse.ArgumentParser(); p.add_argument("--job"); p.add_argument("--out"); p.add_argument("--kind"); a = p.parse_args()
open(os.environ["CRASH_MARKER"], "a").write("x")
pathlib.Path(a.out).write_text(json.dumps({"error": {"type": "TargetUnavailable"}}))
''')
    body = StatevectorRuntime(engine_path=engine).run(job)
    assert body["status"] == "NOT_RUN" and body["reason"] == "engine: TargetUnavailable" and marker.read_text() == "x"


def test_timeout_is_not_run(job, tmp_path):
    engine = _fake_engine(tmp_path, "import time\ntime.sleep(30)\n")
    assert StatevectorRuntime(engine_path=engine, wall_s_override=1).run(job)["reason"] == "engine: TimeoutExpired"


def test_statevector_refuses_above_cap(job, tmp_path):
    body = StatevectorRuntime(engine_path=_fake_engine(tmp_path, OK_ENGINE)).run(_big(job))
    assert body["status"] == "REFUSED" and "statevector" in body["reason"]


def test_select_kind(job):
    assert select_kind("auto", job) == "statevector"
    assert select_kind("auto", replace(job, backend_kind="mps")) == "mps"
    assert select_kind("mps", job) == "mps"
    assert select_kind("auto", _big(job)) == "mps"
    assert select_kind("auto", replace(job, backend_kind="ibm")) == "ibm"
    with pytest.raises(ValueError):
        select_kind("gpu", job)


def test_build_runtime_kinds():
    assert isinstance(build_runtime("statevector"), StatevectorRuntime)
    assert isinstance(build_runtime("mps"), MpsRuntime)
    with pytest.raises(ValueError):
        build_runtime("gpu")


def test_build_runtime_gives_each_runtime_only_its_own_options(tmp_path):
    from nano_qsearch.runtime_ibm import IbmRuntime
    assert isinstance(build_runtime("ibm", python="x", engine_path=tmp_path, wall_s_override=1), IbmRuntime)
    sv = build_runtime("statevector", python="x", environ={}, submit_fn=None)
    assert isinstance(sv, StatevectorRuntime) and sv._python == "x"
    with pytest.raises(TypeError):
        build_runtime("mps", pythn="typo")


def test_child_env_has_single_thread_and_job_round_trips(job, tmp_path, monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "8")       # the parent must force 1 whatever it inherits
    engine = _fake_engine(tmp_path, '''
import argparse, json, pathlib, os, sys
p = argparse.ArgumentParser(); p.add_argument("--job"); p.add_argument("--out"); p.add_argument("--kind"); a = p.parse_args()
assert os.environ["OMP_NUM_THREADS"] == "1"
from nano_qsearch.job import parse_job      # PYTHONPATH must make nano_qsearch importable
parse_job(json.loads(pathlib.Path(a.job).read_text(encoding="utf-8")))
pathlib.Path(a.out).write_text(json.dumps({"error": {"type": "Probe"}}))
''')
    assert StatevectorRuntime(engine_path=engine).run(job)["reason"] == "engine: Probe"
