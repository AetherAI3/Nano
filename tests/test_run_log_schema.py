"""The run log contract: docs/run-log.md and docs/run-log.schema.json.

`nano replay --report json` is the ordered audit log of one run. These tests
prove three things about it:

1. **Byte-stable.** Checked-in golden files under `tests/fixtures/run_log/` are
   reproduced byte for byte: in-process, twice, with the wall clock and the
   date-time class replaced by absurd values, and in a subprocess under a
   foreign time zone, locale, and hash seed.
2. **Schema-valid.** Every golden validates against the published JSON Schema,
   and the schema refuses the drifts it claims to close.
3. **Consistent with the receipt.** The `intents`, `escalations`, and `log`
   arrays are the receipt's `run` arrays; the receipt stays the canonical
   artifact (docs/receipts.md) and this document never competes with it.

Regenerate the goldens after an *intentional* change (see docs/run-log.md §6):

    python tests/test_run_log_schema.py
"""

from __future__ import annotations

import datetime as _datetime
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

import pytest

ROOT = Path(__file__).resolve().parent.parent
if __name__ == "__main__":  # running the file puts tests/, not the root, on sys.path
    sys.path.insert(0, str(ROOT))

from nano.cli.commands import Console, encode_run_log  # noqa: E402
from nano.cli.main import build_parser  # noqa: E402
from nano.compiler import compile_module  # noqa: E402
from nano.data import load_frame  # noqa: E402
from nano.runtime.receipt import canonical_bytes  # noqa: E402
from nano.runtime.vm import run_module  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "run_log"
SCHEMA_PATH = ROOT / "docs" / "run-log.schema.json"
DOC_PATH = ROOT / "docs" / "run-log.md"
LIBRARY = ROOT / "nano" / "library"

# name -> (strategy, data, extra CLI arguments). Three real library strategies
# plus one fixture strategy that reaches every event the replay path can emit.
CASES: Dict[str, Tuple[Path, Path, Tuple[str, ...]]] = {
    "consecutive_loss_circuit": (
        LIBRARY / "risk" / "consecutive_loss_circuit.nano",
        FIXTURES / "consecutive_losses.csv",
        (),
    ),
    "zscore_reversion": (
        LIBRARY / "mean_reversion" / "zscore_reversion.nano",
        FIXTURES / "zscore_neg.csv",
        (),
    ),
    "golden_cross_one_date": (
        LIBRARY / "trend" / "golden_cross.nano",
        FIXTURES / "sma_spread_iso.csv",
        ("--date", "2026-01-07"),
    ),
    "run_log_audit": (
        FIXTURES / "run_log_audit.nano",
        FIXTURES / "run_log_audit.csv",
        ("--verify",),
    ),
}

REGENERATE = (
    "regenerate with `python tests/test_run_log_schema.py` ONLY for an "
    "intentional change (docs/run-log.md section 6)"
)


def _argv(name: str, report: str = "json") -> List[str]:
    strategy, data, extra = CASES[name]
    return ["replay", str(strategy), "--data", str(data), "--report", report, *extra]


def _run_cli(argv: List[str]) -> bytes:
    """Run one CLI command in-process behind a platform-default text stdout."""
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="utf-8", write_through=True)
    err = io.StringIO()
    args = build_parser().parse_args(argv)
    code = args.handler(args, Console(out=out, err=err))
    out.flush()
    assert code == 0, err.getvalue()
    return raw.getvalue()


def report_bytes(name: str) -> bytes:
    return _run_cli(_argv(name))


def golden_path(name: str) -> Path:
    return FIXTURES / f"{name}.json"


def golden_bytes(name: str) -> bytes:
    return golden_path(name).read_bytes()


def _schema() -> Dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validator():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


# -- 1. byte stability ---------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_report_reproduces_the_golden_bytes(name):
    assert report_bytes(name) == golden_bytes(name), f"{name}: run log drifted; {REGENERATE}"


@pytest.mark.parametrize("name", sorted(CASES))
def test_two_identical_runs_emit_identical_bytes(name):
    assert report_bytes(name) == report_bytes(name)


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_is_lf_only_ascii_and_reencodes_to_itself(name):
    payload = golden_bytes(name)
    payload.decode("ascii")
    assert b"\r" not in payload and payload.endswith(b"}\n")
    # Parsing and re-encoding with the CLI's encoder must give the same bytes:
    # any change to indent, separators, key sorting or float repr breaks this.
    assert encode_run_log(json.loads(payload)) == payload


class _AbsurdDateTime(_datetime.datetime):
    """A datetime whose clock readings are nonsense; parsing still works."""

    @classmethod
    def now(cls, tz=None):
        return cls(1999, 12, 31, 23, 59, 59, tzinfo=tz)

    @classmethod
    def utcnow(cls):
        return cls(1999, 12, 31, 23, 59, 59)

    @classmethod
    def today(cls):
        return cls(1999, 12, 31)


def test_no_wall_clock_reaches_the_report(monkeypatch):
    absurd = 4_102_444_800.123  # 2100-01-01
    for clock in ("time", "monotonic", "perf_counter", "process_time"):
        monkeypatch.setattr(time, clock, lambda: absurd)
    for clock in ("time_ns", "monotonic_ns", "perf_counter_ns"):
        monkeypatch.setattr(time, clock, lambda: int(absurd * 1e9))
    import nano.data.frames as frames

    monkeypatch.setattr(frames, "datetime", _AbsurdDateTime)
    for name in sorted(CASES):
        assert report_bytes(name) == golden_bytes(name), name


_HOSTILE_ENVIRONMENTS = {
    "seed1-kiritimati-de": {
        "PYTHONHASHSEED": "1", "TZ": "Pacific/Kiritimati",
        "LC_ALL": "de_DE.UTF-8", "LANG": "de_DE.UTF-8",
    },
    "seed4242-honolulu-fr": {
        "PYTHONHASHSEED": "4242", "TZ": "Pacific/Honolulu",
        "LC_ALL": "fr_FR.UTF-8", "LANG": "fr_FR.UTF-8",
    },
}


def _subprocess_report(name: str, overrides: Mapping[str, str]) -> bytes:
    env = {**os.environ, **overrides}
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; from nano.cli.main import main; sys.exit(main())",
            *_argv(name),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return completed.stdout


@pytest.mark.parametrize("name", sorted(CASES))
def test_real_stdout_bytes_match_golden_under_a_foreign_environment(name):
    # Real process stdout: on Windows this is the CRLF-translating console
    # wrapper the in-process tests can only simulate.
    payload = _subprocess_report(name, _HOSTILE_ENVIRONMENTS["seed1-kiritimati-de"])
    assert payload == golden_bytes(name)


def test_hash_seed_time_zone_and_locale_do_not_move_bytes():
    outputs = {
        _subprocess_report("run_log_audit", env) for env in _HOSTILE_ENVIRONMENTS.values()
    }
    assert outputs == {golden_bytes("run_log_audit")}


# -- 2. schema -------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_validates_against_the_published_schema(name):
    _validator().validate(json.loads(golden_bytes(name)))


def _audit_document() -> Dict[str, Any]:
    return json.loads(golden_bytes("run_log_audit"))


def _drift(mutate) -> Dict[str, Any]:
    document = _audit_document()
    mutate(document)
    return document


_REJECTED_DRIFTS = {
    "unknown top-level member": lambda d: d.update(runLogVersion=1),
    "missing log": lambda d: d.pop("log"),
    "null sourceHash": lambda d: d.update(sourceHash=None),
    "bare hex moduleHash": lambda d: d.update(moduleHash=d["moduleHash"][7:]),
    "string timestamp": lambda d: d["log"][0].update(timestamp="0"),
    "float timestamp": lambda d: d["intents"][0].update(timestamp=120.5),
    "null asset instead of absent": lambda d: d["intents"][1].update(asset=None),
    "unknown intent member": lambda d: d["intents"][0].update(size=1),
    "unknown action": lambda d: d["intents"][0].update(intent="HOLD"),
    "confidence above one": lambda d: d["intents"][0].update(confidence=1.5),
    "unknown escalation member": lambda d: d["escalations"][0].update(model="x"),
    "integer isAgent": lambda d: d["escalations"][0].update(isAgent=1),
    "unknown log member": lambda d: d["log"][0].update(level="info"),
    "structured detail": lambda d: d["log"][0].update(detail={"k": "v"}),
    "undotted event name": lambda d: d["log"][0].update(event="loaded"),
    "zero bars": lambda d: d.update(bars=0),
    "string replayVerified": lambda d: d.update(replayVerified="true"),
}


@pytest.mark.parametrize("drift", sorted(_REJECTED_DRIFTS))
def test_schema_refuses_a_drifted_document(drift):
    jsonschema = pytest.importorskip("jsonschema")
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(_drift(_REJECTED_DRIFTS[drift]))


def test_schema_accepts_an_unknown_but_well_formed_event_name():
    # The event vocabulary is an open set: adding one is a minor change.
    entry = {"event": "future.thing", "timestamp": 0, "detail": ""}
    _validator().validate(_drift(lambda d: d["log"].append(entry)))


# -- 3. invariants the doc promises -------------------------------------------------


def _intent_key(intent: Mapping[str, Any]) -> Tuple[Any, ...]:
    return (intent["timestamp"], f"{intent['intent']} asset={intent.get('asset')}")


@pytest.mark.parametrize("name", sorted(CASES))
def test_documented_invariants_hold(name):
    document = json.loads(golden_bytes(name))
    log = document["log"]
    assert log[0]["event"] == "module.loaded"
    assert document["rowsRead"] == document["bars"] + document["rowsFiltered"]
    unwarmed = [e for e in log if e["event"] == "condition.unwarmed"]
    assert document["warmup_bars_skipped"] == len(unwarmed)
    emitted = [(e["timestamp"], e["detail"]) for e in log if e["event"] == "intent.emitted"]
    assert emitted == [_intent_key(i) for i in document["intents"]]
    escalated = [(e["timestamp"], e["detail"]) for e in log if e["event"] == "llmre.escalated"]
    assert escalated == [
        (x["timestamp"], f"{x['escalate']}: {x['reason']}") for x in document["escalations"]
    ]


def test_arrays_are_in_emission_order_not_timestamp_order():
    # Rules run one after another, each over the whole frame, then routes. A
    # consumer that assumes sorted timestamps is wrong; the fixture proves it.
    document = _audit_document()
    log_times = [e["timestamp"] for e in document["log"]]
    assert log_times != sorted(log_times)
    escalation_times = [x["timestamp"] for x in document["escalations"]]
    assert escalation_times != sorted(escalation_times)


def test_verify_changes_only_replay_verified():
    verified = json.loads(report_bytes("run_log_audit"))
    plain = json.loads(_run_cli([a for a in _argv("run_log_audit") if a != "--verify"]))
    assert verified.pop("replayVerified") is True
    assert plain.pop("replayVerified") is False
    assert verified == plain


@pytest.mark.parametrize("name", sorted(CASES))
def test_run_arrays_are_the_receipt_run_arrays(name):
    report = json.loads(golden_bytes(name))
    receipt = json.loads(_run_cli(_argv(name, report="receipt")))
    for member in ("intents", "escalations", "log"):
        # Byte comparison, so True/1 and 1/1.0 cannot pass as equal.
        assert canonical_bytes(report[member]) == canonical_bytes(receipt["run"][member])
    assert report["warmup_bars_skipped"] == receipt["run"]["warmupBarsSkipped"]
    assert report["moduleHash"] == receipt["identity"]["moduleHash"]
    assert report["sourceHash"] == receipt["provenance"]["sourceHash"]
    assert report["warmupDeclared"] == receipt["identity"]["warmupDeclared"]
    assert report["bars"] == receipt["inputs"]["bars"]


# -- 4. the event vocabulary stays documented -----------------------------------------


_EVENT_LITERAL = re.compile(r'event="([a-z_.]+)"')


def _emitted_event_names() -> set:
    names = set()
    for relative in ("nano/runtime/vm.py", "nano/runtime/risk.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        names |= set(_EVENT_LITERAL.findall(source))
    return names


def _documented_event_names() -> set:
    text = DOC_PATH.read_text(encoding="utf-8")
    section = text.split("<!-- run-log-events:start -->", 1)[1]
    section = section.split("<!-- run-log-events:end -->", 1)[0]
    return set(re.findall(r"^\| `([a-z_.]+)` \|", section, re.MULTILINE))


def test_known_events_equal_the_vm_emitters_the_schema_and_the_doc():
    known = set(_schema()["$defs"]["knownEvent"]["enum"])
    assert _emitted_event_names() == known
    assert _documented_event_names() == known


def test_goldens_reach_every_event_the_cli_can_emit():
    seen = {e["event"] for name in CASES for e in json.loads(golden_bytes(name))["log"]}
    provider_only = {"infer.called", "infer.invalid", "infer.failed"}
    assert seen == set(_schema()["$defs"]["knownEvent"]["enum"]) - provider_only


class _ScriptedProvider:
    """Fails at t=0, answers off-contract at t=60, and validly afterwards."""

    def infer(self, signature: str, inputs: Mapping[str, Any], *, timestamp: int):
        if timestamp == 0:
            raise ValueError("synthetic provider failure")
        if timestamp == 60:
            return {"unexpected": 1}
        return {"confidence": 0.9}


def test_provider_only_events_fit_the_same_schema():
    # `nano replay` never supplies a provider, but ModuleResult.to_dict() is the
    # same log a Python host gets from run_module(..., provider=...).
    strategy, data, _ = CASES["run_log_audit"]
    module = compile_module(strategy.read_text(encoding="utf-8"))
    result = run_module(module, load_frame(data).frame, provider=_ScriptedProvider())
    document = {**_audit_document(), **result.to_dict()}
    _validator().validate(document)
    events = {e["event"] for e in document["log"]}
    assert {"infer.called", "infer.invalid", "infer.failed"} <= events
    assert "infer.skipped" not in events


def _regenerate() -> None:
    for name in sorted(CASES):
        path = golden_path(name)
        payload = report_bytes(name)
        changed = not path.exists() or path.read_bytes() != payload
        path.write_bytes(payload)
        sys.stdout.write(f"{'wrote' if changed else 'same '}  {path.name}  ({len(payload)} bytes)\n")


if __name__ == "__main__":
    _regenerate()
