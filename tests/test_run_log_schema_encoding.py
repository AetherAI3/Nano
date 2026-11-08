"""Serialisation-layer pins for `nano replay --report json` (docs/run-log.md §4).

The golden files in `tests/fixtures/run_log/` prove whole documents stay put.
This module pins the individual encoding rules those documents rest on, each
with an input chosen to break exactly one of them: the line terminator, key
order, float formatting, integer width, and non-ASCII escaping. A golden that
happens not to contain `0.30000000000000004` cannot catch a float-format change;
these tests can.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from nano.cli.commands import Console, encode_run_log
from nano.cli.main import build_parser
from nano.runtime.effects import Intent, LogEntry
from nano.runtime.vm import Escalation, ModuleResult

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "run_log"
AUDIT_STRATEGY = FIXTURES / "run_log_audit.nano"
AUDIT_DATA = FIXTURES / "run_log_audit.csv"

TOP_LEVEL_ORDER = [
    "strategy", "moduleHash", "sourceHash", "bars", "rowsRead", "rowsFiltered",
    "warmupDeclared", "replayVerified", "intents", "escalations", "log",
    "warmup_bars_skipped",
]


def _replay_through(stream: io.TextIOWrapper, raw: io.BytesIO) -> bytes:
    args = build_parser().parse_args(
        ["replay", str(AUDIT_STRATEGY), "--data", str(AUDIT_DATA), "--report", "json"]
    )
    err = io.StringIO()
    code = args.handler(args, Console(out=stream, err=err))
    stream.flush()
    assert code == 0, err.getvalue()
    return raw.getvalue()


def test_json_report_is_lf_framed_even_behind_a_crlf_translating_stdout():
    # A Windows console's text wrapper rewrites every "\n" to "\r\n". Simulated
    # here with an explicit newline="\r\n" so the check fails on every platform,
    # not only on the one where the defect shows up.
    raw = io.BytesIO()
    crlf_stdout = io.TextIOWrapper(raw, encoding="utf-8", newline="\r\n", write_through=True)
    payload = _replay_through(crlf_stdout, raw)
    assert b"\r" not in payload
    assert payload.endswith(b"}\n") and not payload.endswith(b"\n\n")


def test_json_report_bytes_do_not_depend_on_the_stdout_newline_policy():
    outputs = set()
    for newline in (None, "", "\n", "\r", "\r\n"):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline=newline, write_through=True)
        outputs.add(_replay_through(stream, raw))
    assert len(outputs) == 1


def test_encoder_is_indent_two_ascii_insertion_order_with_one_trailing_lf():
    document = {"b": 1, "a": [], "c": {"z": "\u00e9", "y": None}}
    assert encode_run_log(document) == (
        b'{\n  "b": 1,\n  "a": [],\n  "c": {\n    "z": "\\u00e9",\n    "y": null\n  }\n}\n'
    )


def test_float_formatting_is_shortest_round_trip_repr():
    result = ModuleResult(
        intents=(
            Intent("BUY", 0, "A", 0.1 + 0.2),
            Intent("BUY", 0, "B", 1e-07),
            Intent("BUY", 0, "C", 1.0),
            Intent("BUY", 0, "D", 1),
            Intent("BUY", 0, "E", 5e-324),
        ),
        escalations=(),
        log=(),
    )
    text = encode_run_log(result.to_dict()).decode("ascii")
    for expected in (
        '"confidence": 0.30000000000000004\n',
        '"confidence": 1e-07\n',
        '"confidence": 1.0\n',
        '"confidence": 1\n',
        '"confidence": 5e-324\n',
    ):
        assert expected in text


def test_large_and_negative_integer_timestamps_are_emitted_exactly():
    big = 2**53 + 1
    result = ModuleResult(
        intents=(Intent("PAUSE", big),),
        escalations=(Escalation("Desk", -86400, True, "rule body"),),
        log=(LogEntry("module.loaded", 10**18, "x"),),
    )
    text = encode_run_log(result.to_dict()).decode("ascii")
    assert f'"timestamp": {big}\n' in text
    assert '"timestamp": -86400,' in text
    assert '"timestamp": 1000000000000000000,' in text


def test_record_key_order_is_fixed_by_the_emitters_not_by_sorting():
    result = ModuleResult(
        intents=(Intent("BUY", 1, "BTC", 0.5), Intent("PAUSE", 2)),
        escalations=(Escalation("ops", 3, False, "rule body"),),
        log=(LogEntry("intent.emitted", 1, "BUY asset=BTC"),),
        warmup_bars_skipped=4,
    )
    document = result.to_dict()
    assert list(document) == ["intents", "escalations", "log", "warmup_bars_skipped"]
    assert list(document["intents"][0]) == ["intent", "timestamp", "asset", "confidence"]
    assert list(document["intents"][1]) == ["intent", "timestamp"]
    assert list(document["escalations"][0]) == ["escalate", "timestamp", "isAgent", "reason"]
    assert list(document["log"][0]) == ["event", "timestamp", "detail"]


def test_top_level_key_order_matches_the_documented_order():
    raw = io.BytesIO()
    payload = _replay_through(io.TextIOWrapper(raw, encoding="utf-8", write_through=True), raw)
    assert list(json.loads(payload)) == TOP_LEVEL_ORDER


def test_non_ascii_detail_text_is_escaped_not_emitted_raw():
    # The risk.unenforced detail carries an em dash; it must leave as \u2014.
    raw = io.BytesIO()
    payload = _replay_through(io.TextIOWrapper(raw, encoding="utf-8", write_through=True), raw)
    payload.decode("ascii")  # raises if a raw non-ASCII byte got past the encoder
    assert b"\\u2014" in payload
