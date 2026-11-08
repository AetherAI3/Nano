"""Issue #85 / RFC 0085: strict host admission of record inputs and VM ceilings."""
import copy
import dataclasses
import math

import pytest

from nano.ir.records import (
    MAX_RECORD_INT, MAX_RECORD_STRING_BYTES, MAX_RECORD_VALUES, RecordValue, admit_record_inputs,
)
from nano.runtime import vm as vm_module
from nano.runtime.interpreter import MarketFrame, RuntimeError_
from nano.runtime.vm import RecordInputError, run_module

from records_support import (
    GOLDEN, build, envelope, golden_evidence, golden_frame, golden_ids, golden_package,
    simple_package,
)

ROW = {"count": 2, "score": 0.5, "origin": "probe", "verified": True}
_DELETE = object()


@pytest.fixture(scope="module")
def golden_module(tmp_path_factory):
    return build(golden_package(tmp_path_factory.mktemp("golden"))).module


def _with(path, value):
    """A valid golden envelope with one entry replaced or deleted."""
    root = golden_evidence(ROW)
    target = root
    for key in path[:-1]:
        target = target[key]
    if value is _DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return root


def _cyclic():
    value = golden_evidence(ROW)
    value["fields"]["source"] = value
    return value


CASES = [
    ("not-a-mapping", lambda: 7, "RECORD_INPUT_SCHEMA", "evidence/0"),
    ("bare-fields-without-envelope", lambda: dict(ROW), "RECORD_INPUT_SCHEMA", "evidence/0"),
    ("extra-envelope-key", lambda: {**golden_evidence(ROW), "debug": True}, "RECORD_INPUT_SCHEMA", "evidence/0"),
    ("fields-not-mapping", lambda: _with(("fields",), [1, 2]), "RECORD_INPUT_SCHEMA", "evidence/0"),
    ("stale-nominal-id", lambda: _with(("recordType",), "record:sha256:" + "1" * 64), "RECORD_INPUT_STALE_ID", "evidence/0"),
    ("nested-stale-id", lambda: _with(("fields", "source", "recordType"), golden_ids()["Evidence"]),
     "RECORD_INPUT_STALE_ID", "evidence/0/source"),
    ("hidden-field", lambda: _with(("fields", "secret"), "x"), "RECORD_INPUT_UNKNOWN_FIELD", "evidence/0"),
    ("non-string-key", lambda: _with(("fields", 1), "x"), "RECORD_INPUT_UNKNOWN_FIELD", "evidence/0"),
    ("missing-field", lambda: _with(("fields", "score"), _DELETE), "RECORD_INPUT_MISSING_FIELD", "evidence/0/score"),
    ("int-for-float", lambda: _with(("fields", "score"), 1), "RECORD_INPUT_TYPE", "evidence/0/score"),
    ("float-for-int", lambda: _with(("fields", "count"), 2.0), "RECORD_INPUT_TYPE", "evidence/0/count"),
    ("numeric-string", lambda: _with(("fields", "count"), "2"), "RECORD_INPUT_TYPE", "evidence/0/count"),
    ("bool-as-int", lambda: _with(("fields", "count"), True), "RECORD_INPUT_TYPE", "evidence/0/count"),
    ("int-as-bool", lambda: _with(("fields", "source", "fields", "verified"), 1),
     "RECORD_INPUT_TYPE", "evidence/0/source/verified"),
    ("nan", lambda: _with(("fields", "score"), math.nan), "RECORD_INPUT_NONFINITE", "evidence/0/score"),
    ("infinity", lambda: _with(("fields", "score"), -math.inf), "RECORD_INPUT_NONFINITE", "evidence/0/score"),
    ("int-range", lambda: _with(("fields", "count"), MAX_RECORD_INT + 1), "RECORD_INPUT_RANGE", "evidence/0/count"),
    ("huge-int", lambda: _with(("fields", "count"), 10**5000), "RECORD_INPUT_RANGE", "evidence/0/count"),
    ("lone-surrogate", lambda: _with(("fields", "source", "fields", "origin"), "\ud800"),
     "RECORD_INPUT_TYPE", "evidence/0/source/origin"),
    ("cyclic-value", _cyclic, "RECORD_INPUT_RECURSIVE", "evidence/0/source"),
]


@pytest.mark.parametrize("name,make,code,path", CASES, ids=[c[0] for c in CASES])
def test_host_rejects_invalid_records_before_any_node_runs(golden_module, monkeypatch, name, make, code, path):
    monkeypatch.setattr(vm_module._Machine, "derive", lambda self: pytest.fail("rules ran"))
    frame = golden_frame({"timestamps": [0], "model_tokens": [100]}, evidence=[make()])
    with pytest.raises(RecordInputError) as err:
        run_module(golden_module, frame)
    assert (err.value.code, err.value.path) == (code, path)
    assert isinstance(err.value, RuntimeError_)


def test_rejection_never_echoes_host_values(golden_module):
    frame = golden_frame({"timestamps": [0], "model_tokens": [100]},
                         evidence=[_with(("fields", "source", "fields", "origin"), 12345678)])
    with pytest.raises(RecordInputError) as err:
        run_module(golden_module, frame)
    assert "12345678" not in str(err.value)


def test_missing_record_input_is_rejected(golden_module):
    frame = MarketFrame(timestamps=(0,), signals={"model_tokens": (100,)})
    with pytest.raises(RecordInputError) as err:
        run_module(golden_module, frame)
    assert (err.value.code, err.value.path) == ("RECORD_INPUT_MISSING", "evidence")


def test_admitted_values_are_immutable_copies(golden_module):
    host = golden_evidence(ROW)
    admitted = admit_record_inputs(golden_module.records, golden_module.inputs, {"evidence": [host]})
    value = admitted["evidence"][0]
    host["fields"]["count"] = 99
    host["fields"]["source"]["fields"]["verified"] = False
    assert value.get("count") == 2 and value.get("source").get("verified") is True
    assert isinstance(value.get("source"), RecordValue)
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.record_id = "x"


def test_deeply_nested_hostile_value_is_rejected_without_recursion_error(golden_module):
    hostile = 0
    for _ in range(100_000):
        hostile = {"recordType": golden_ids()["Source"], "fields": {"origin": "x", "verified": hostile}}
    frame = golden_frame({"timestamps": [0], "model_tokens": [100]},
                         evidence=[_with(("fields", "source"), hostile)])
    with pytest.raises(RecordInputError) as err:
        run_module(golden_module, frame)
    assert err.value.code == "RECORD_INPUT_TYPE"


# -- ceilings: materialized values and string bytes ------------------------

FLAT = "module Evidence {\n  record Quad { a: int b: int c: int d: int }\n}\n"
FLAT_AGENT = "import Evidence\nstrategy Agent {\n  input q: Quad\n  every 1m { if q.a >= 1 { execute() } }\n}\n"


@pytest.fixture(scope="module")
def flat_module(tmp_path_factory):
    return build(simple_package(tmp_path_factory.mktemp("flat"), evidence=FLAT, agent=FLAT_AGENT)).module


def _quad_frame(module, bars, absent=0):
    rid = module.records[0].id
    rows = [None] * absent + [envelope(rid, {"a": 1, "b": 2, "c": 3, "d": 4})] * bars
    return MarketFrame(timestamps=tuple(range(0, 60 * len(rows), 60)), signals={"q": rows})


def test_materialization_boundary_is_admitted(flat_module):
    assert MAX_RECORD_VALUES == 4096
    result = run_module(flat_module, _quad_frame(flat_module, 1024))
    assert len(result.intents) == 1024


def test_materialization_boundary_plus_one_is_rejected_before_walking(flat_module, monkeypatch):
    from nano.ir import records as records_module
    monkeypatch.setattr(records_module, "_validate", lambda *a: pytest.fail("walked before counting"))
    with pytest.raises(RecordInputError) as err:
        run_module(flat_module, _quad_frame(flat_module, 1025))
    assert (err.value.code, err.value.path) == ("RECORD_INPUT_BUDGET", "q")


def test_absent_bars_do_not_count_toward_materialization(flat_module):
    assert len(run_module(flat_module, _quad_frame(flat_module, 1024, absent=10)).intents) == 1024


TEXT = "module Evidence {\n  record Note { text: string }\n}\n"
TEXT_AGENT = 'import Evidence\nstrategy Agent {\n  input n: Note\n  every 1m { if n.text != "" { execute() } }\n}\n'


@pytest.fixture(scope="module")
def text_module(tmp_path_factory):
    return build(simple_package(tmp_path_factory.mktemp("text"), evidence=TEXT, agent=TEXT_AGENT)).module


def _notes(module, *texts):
    rid = module.records[0].id
    rows = [envelope(rid, {"text": t}) for t in texts]
    return MarketFrame(timestamps=tuple(range(0, 60 * len(rows), 60)), signals={"n": rows})


@pytest.mark.parametrize("texts,ok", [
    (("x" * MAX_RECORD_STRING_BYTES,), True),
    (("x" * (MAX_RECORD_STRING_BYTES + 1),), False),
    (("é" * (MAX_RECORD_STRING_BYTES // 2),), True),           # bytes, not characters
    (("é" * (MAX_RECORD_STRING_BYTES // 2) + "a",), False),
    (("x" * 131072, "y" * 131072), True),                             # cumulative across bars
    (("x" * 131072, "y" * 131073), False),
])
def test_string_byte_budget_boundaries(text_module, texts, ok):
    from nano.compiler.package import MAX_BYTES
    assert MAX_RECORD_STRING_BYTES == MAX_BYTES == 262144
    frame = _notes(text_module, *texts)
    if ok:
        assert len(run_module(text_module, frame).intents) == len(texts)
        return
    with pytest.raises(RecordInputError) as err:
        run_module(text_module, frame)
    assert err.value.code == "RECORD_INPUT_BUDGET"
    assert err.value.path == f"n/{len(texts) - 1}/text"


def test_depth_eight_values_are_admitted_and_read(tmp_path):
    lines = ["  record R1 { leaf: int }"] + [f"  record R{i} {{ inner: R{i - 1} }}" for i in range(2, 9)]
    evidence = "module Evidence {\n" + "\n".join(lines) + "\n}\n"
    agent = ("import Evidence\nstrategy Agent {\n  input r: R8\n  every 1m { if r."
             + ".".join(["inner"] * 7) + ".leaf >= 1 { execute() } }\n}\n")
    module = build(simple_package(tmp_path, evidence=evidence, agent=agent)).module
    ids = [decl.id for decl in module.records]
    value = envelope(ids[0], {"leaf": 1})
    for rid in ids[1:]:
        value = envelope(rid, {"inner": value})
    result = run_module(module, MarketFrame(timestamps=(0,), signals={"r": [value]}))
    assert [i.action for i in result.intents] == ["EXECUTE"]


def test_golden_frames_replay_identically(golden_module):
    for spec in GOLDEN["frames"]:
        frame = golden_frame(spec)
        first, second = run_module(golden_module, frame), run_module(golden_module, copy.deepcopy(frame))
        assert first.to_dict() == second.to_dict()
