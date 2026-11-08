"""Issue #84: strict, versioned local fixture runner behind `nano package test`.

Every malformed-fixture test is its own negative control and names the JSON
pointer the rejection must carry. Nothing here weakens a contract clause in
``docs/package-cli.md``; if one of these fails, fix the runner, not the test.
"""
import dataclasses
import hashlib
import inspect
import json
from pathlib import Path

import pytest

import nano.runtime.vm as vm
from nano.cli.package_cases import (
    ACTIONS_EXECUTED,
    ALLOWED_EFFECTS,
    CASES_VERSION,
    MAX_BARS,
    MAX_CASES,
    MAX_CASES_BYTES,
    UNSUPPORTED_OPS,
    Case,
    CaseResult,
    CaseSet,
    FixtureError,
    UnsupportedHandlerError,
    admit_module,
    load_cases,
    run_cases,
)
from nano.compiler import compile_module, compile_package
from nano.ir.module import NanoModule

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "agent-package"
EXAMPLE_CASES = EXAMPLE / "cases.json"


# -- helpers ------------------------------------------------------------------


@pytest.fixture(scope="module")
def example_module():
    return compile_package(EXAMPLE / "nano.json", lock_path=EXAMPLE / "nano.lock").module


def frame(count=2, verified=True, tokens=100, timestamps=(0,)):
    signals = {"evidence_count": [count], "evidence_verified": [verified]}
    if tokens is not None:
        signals["model_tokens"] = [tokens]
    return {"timestamps": list(timestamps), "signals": signals}


def case(name="c", fr=None, expect=None):
    return {
        "name": name,
        "frame": fr if fr is not None else frame(),
        "expect": expect if expect is not None
        else {"proposedIntents": [{"intent": "EXECUTE", "timestamp": 0}]},
    }


def doc(*cases):
    return {"nanoPackageCasesVersion": "1.0.0", "cases": list(cases) or [case()]}


def encode(value):
    return json.dumps(value).encode("utf-8")


def rejected(raw):
    with pytest.raises(FixtureError) as info:
        load_cases(raw)
    err = info.value
    assert err.code == "FIXTURE_ERROR"
    assert err.category == "fixture"
    assert isinstance(err.message, str) and err.message
    return err


def counting_run_module(monkeypatch):
    calls = []
    original = vm.run_module

    def wrapper(module, fr, *args, **kwargs):
        calls.append({"args": args, "kwargs": dict(kwargs)})
        return original(module, fr, *args, **kwargs)

    monkeypatch.setattr(vm, "run_module", wrapper)
    return calls


# -- constants and surface ----------------------------------------------------


def test_public_constants_match_the_frozen_contract():
    assert CASES_VERSION == "1.0.0"
    assert MAX_CASES == 256
    assert MAX_BARS == 4096
    assert MAX_CASES_BYTES == 1048576
    assert ALLOWED_EFFECTS == frozenset({"intent.emit", "log.append"})
    assert ACTIONS_EXECUTED == 0
    assert FixtureError.code == "FIXTURE_ERROR" and FixtureError.category == "fixture"
    assert UnsupportedHandlerError.code == "UNSUPPORTED_HANDLER"
    assert UnsupportedHandlerError.category == "capability"
    assert issubclass(FixtureError, ValueError)
    assert issubclass(UnsupportedHandlerError, ValueError)


def test_unsupported_ops_are_exactly_inference_escalation_route_signature():
    assert UNSUPPORTED_OPS == frozenset(
        {"ai.infer", "ai.signature", "llmre.escalate", "route"}
    )


def test_runner_signature_admits_no_provider_or_handler():
    assert list(inspect.signature(run_cases).parameters) == ["module", "cases"]
    assert list(inspect.signature(load_cases).parameters) == ["raw"]
    assert list(inspect.signature(admit_module).parameters) == ["module"]


# -- 1. frozen example fixture ------------------------------------------------


def test_frozen_example_fixture_passes_and_is_deterministic(example_module):
    raw = EXAMPLE_CASES.read_bytes()
    cases = load_cases(raw)
    assert isinstance(cases, CaseSet)
    assert cases.version == "1.0.0"
    assert len(cases.cases) == 5
    first = run_cases(example_module, cases)
    second = run_cases(example_module, load_cases(raw))
    assert [r.status for r in first] == ["pass"] * 5
    assert all(r.reason is None for r in first)
    assert [r.to_dict() for r in first] == [r.to_dict() for r in second]
    assert [r.name for r in first] == [c["name"] for c in json.loads(raw)["cases"]]
    assert first[-1].to_dict() == {
        "name": "missing host input is a runtime rejection",
        "observed": {"runtimeError": "Signal 'model_tokens' not present in market frame"},
        "status": "pass",
    }
    assert "reason" not in first[0].to_dict()


def test_loaded_case_set_is_immutable():
    cases = load_cases(EXAMPLE_CASES.read_bytes())
    assert isinstance(cases.cases, tuple)
    one = cases.cases[0]
    assert isinstance(one, Case)
    assert isinstance(one.frame.timestamps, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        one.name = "other"
    with pytest.raises(TypeError):
        one.frame.signals["evidence_count"] = (9,)
    assert isinstance(one.frame.signals["evidence_count"], tuple)
    assert isinstance(one.expected_intents, tuple)
    with pytest.raises(TypeError):
        one.expected_intents[0]["intent"] = "PAUSE"


def test_load_cases_requires_bytes():
    with pytest.raises(TypeError):
        load_cases(EXAMPLE_CASES.read_text(encoding="utf-8"))


# -- 2. intent versus action --------------------------------------------------


def test_execute_is_a_proposed_intent_and_nothing_actuates(example_module, monkeypatch):
    calls = counting_run_module(monkeypatch)
    results = run_cases(example_module, load_cases(EXAMPLE_CASES.read_bytes()))
    assert results[0].observed == {"proposedIntents": [{"intent": "EXECUTE", "timestamp": 0}]}
    assert results[0].to_dict()["observed"] == {
        "proposedIntents": [{"intent": "EXECUTE", "timestamp": 0}]
    }
    assert len(calls) == 5
    for call in calls:
        assert call["args"] == ()
        assert call["kwargs"] == {"provider": None}
    assert ACTIONS_EXECUTED == 0
    import nano.cli.package_cases as runner
    assert "actuator" in runner.__doc__
    assert "EXECUTE" in runner.__doc__


@pytest.mark.parametrize("key", ["provider", "handler", "executed", "skip", "only"])
def test_capability_fields_do_not_exist_at_case_level(key):
    bad = case()
    bad[key] = True if key != "provider" else "openai"
    assert rejected(encode(doc(bad))).pointer == f"/cases/0/{key}"


@pytest.mark.parametrize("key", ["provider", "handler", "executed", "skip", "only"])
def test_capability_fields_do_not_exist_at_root(key):
    value = doc()
    value[key] = True
    assert rejected(encode(value)).pointer == f"/{key}"


@pytest.mark.parametrize("key", ["provider", "handler", "executed"])
def test_capability_fields_do_not_exist_in_frame_or_expect(key):
    fr = frame()
    fr[key] = "x"
    assert rejected(encode(doc(case(fr=fr)))).pointer == f"/cases/0/frame/{key}"
    expect = {"proposedIntents": [], key: "x"}
    assert rejected(encode(doc(case(expect=expect)))).pointer == f"/cases/0/expect/{key}"


# -- 3. malformed fixtures ----------------------------------------------------


def test_non_json_bytes():
    assert rejected(b"{not json").pointer == ""


def test_non_utf8_bytes():
    assert rejected(b"\xff\xfe{}").pointer == ""


def test_root_must_be_an_object():
    assert rejected(b"[]").pointer == ""


def test_pathological_nesting_is_a_fixture_error():
    assert rejected(b"[" * 200000).pointer == ""


def test_duplicate_key_in_case():
    raw = (
        b'{"nanoPackageCasesVersion":"1.0.0","cases":[{"name":"a","name":"b",'
        b'"frame":{"timestamps":[0],"signals":{}},"expect":{"proposedIntents":[]}}]}'
    )
    err = rejected(raw)
    assert err.pointer == "/cases/0/name"
    assert "duplicate" in err.message.lower()


def test_duplicate_key_at_root():
    raw = b'{"nanoPackageCasesVersion":"1.0.0","nanoPackageCasesVersion":"1.0.0","cases":[]}'
    assert rejected(raw).pointer == "/nanoPackageCasesVersion"


@pytest.mark.parametrize("token", [b"NaN", b"Infinity", b"-Infinity", b"1e999"])
def test_non_finite_numbers(token):
    raw = (
        b'{"nanoPackageCasesVersion":"1.0.0","cases":[{"name":"a",'
        b'"frame":{"timestamps":[0],"signals":{"x":[' + token + b']}},'
        b'"expect":{"proposedIntents":[]}}]}'
    )
    assert rejected(raw).pointer == "/cases/0/frame/signals/x/0"


@pytest.mark.parametrize("version", ["1.0.1", "0.1.0", 1, None])
def test_wrong_version(version):
    value = doc()
    value["nanoPackageCasesVersion"] = version
    assert rejected(encode(value)).pointer == "/nanoPackageCasesVersion"


def test_missing_version():
    value = doc()
    del value["nanoPackageCasesVersion"]
    assert rejected(encode(value)).pointer == "/nanoPackageCasesVersion"


def test_missing_cases():
    assert rejected(encode({"nanoPackageCasesVersion": "1.0.0"})).pointer == "/cases"


def test_empty_cases_list_is_a_failure_not_a_pass():
    value = {"nanoPackageCasesVersion": "1.0.0", "cases": []}
    assert rejected(encode(value)).pointer == "/cases"


def test_cases_must_be_a_list():
    value = {"nanoPackageCasesVersion": "1.0.0", "cases": {"a": case()}}
    assert rejected(encode(value)).pointer == "/cases"


def test_too_many_cases_and_the_boundary():
    at_limit = doc(*[case(name=f"c{i}") for i in range(MAX_CASES)])
    assert len(load_cases(encode(at_limit)).cases) == MAX_CASES
    over = doc(*[case(name=f"c{i}") for i in range(MAX_CASES + 1)])
    assert rejected(encode(over)).pointer == "/cases"


def test_case_must_be_an_object():
    value = {"nanoPackageCasesVersion": "1.0.0", "cases": ["x"]}
    assert rejected(encode(value)).pointer == "/cases/0"


@pytest.mark.parametrize("key", ["name", "frame", "expect"])
def test_missing_case_key(key):
    bad = case()
    del bad[key]
    assert rejected(encode(doc(bad))).pointer == f"/cases/0/{key}"


def test_duplicate_case_names():
    assert rejected(encode(doc(case("same"), case("same")))).pointer == "/cases/1/name"


@pytest.mark.parametrize("name", ["", 3, None, ["a"]])
def test_name_must_be_a_non_empty_string(name):
    assert rejected(encode(doc(case(name=name)))).pointer == "/cases/0/name"


def test_frame_must_be_an_object():
    bad = case()
    bad["frame"] = [0]
    assert rejected(encode(doc(bad))).pointer == "/cases/0/frame"


@pytest.mark.parametrize("key", ["timestamps", "signals"])
def test_missing_frame_key(key):
    fr = frame()
    del fr[key]
    assert rejected(encode(doc(case(fr=fr)))).pointer == f"/cases/0/frame/{key}"


def test_timestamps_empty():
    fr = {"timestamps": [], "signals": {}}
    assert rejected(encode(doc(case(fr=fr)))).pointer == "/cases/0/frame/timestamps"


def test_timestamps_not_a_list():
    fr = {"timestamps": 0, "signals": {}}
    assert rejected(encode(doc(case(fr=fr)))).pointer == "/cases/0/frame/timestamps"


@pytest.mark.parametrize("value", [1.5, 1.0, "0", None, [0]])
def test_timestamp_must_be_an_integer(value):
    fr = {"timestamps": [value], "signals": {}}
    assert rejected(encode(doc(case(fr=fr)))).pointer == "/cases/0/frame/timestamps/0"


@pytest.mark.parametrize("value", [True, False])
def test_timestamp_bool_is_not_an_integer(value):
    fr = {"timestamps": [value], "signals": {}}
    assert rejected(encode(doc(case(fr=fr)))).pointer == "/cases/0/frame/timestamps/0"


def test_timestamps_decreasing():
    fr = {"timestamps": [0, 60, 30], "signals": {}}
    assert rejected(encode(doc(case(fr=fr)))).pointer == "/cases/0/frame/timestamps/2"


def test_timestamps_duplicate():
    fr = {"timestamps": [0, 0], "signals": {}}
    assert rejected(encode(doc(case(fr=fr)))).pointer == "/cases/0/frame/timestamps/1"


def test_timestamps_bar_ceiling_and_boundary():
    ok = {"timestamps": list(range(MAX_BARS)), "signals": {"x": [1] * MAX_BARS}}
    loaded = load_cases(encode(doc(case(fr=ok, expect={"proposedIntents": []}))))
    assert len(loaded.cases[0].frame.timestamps) == MAX_BARS
    over = {"timestamps": list(range(MAX_BARS + 1)), "signals": {}}
    assert rejected(encode(doc(case(fr=over)))).pointer == "/cases/0/frame/timestamps"


def test_signals_must_be_an_object():
    fr = {"timestamps": [0], "signals": [[1]]}
    assert rejected(encode(doc(case(fr=fr)))).pointer == "/cases/0/frame/signals"


def test_signal_series_must_be_a_list():
    fr = {"timestamps": [0], "signals": {"x": 1}}
    assert rejected(encode(doc(case(fr=fr)))).pointer == "/cases/0/frame/signals/x"


def test_series_length_mismatch():
    fr = {"timestamps": [0, 60], "signals": {"x": [1, 2], "y": [1]}}
    assert rejected(encode(doc(case(fr=fr)))).pointer == "/cases/0/frame/signals/y"


def test_string_signal_value():
    fr = {"timestamps": [0, 60], "signals": {"x": [1, "2"]}}
    assert rejected(encode(doc(case(fr=fr)))).pointer == "/cases/0/frame/signals/x/1"


def test_null_signal_value():
    fr = {"timestamps": [0], "signals": {"x": [None]}}
    assert rejected(encode(doc(case(fr=fr)))).pointer == "/cases/0/frame/signals/x/0"


@pytest.mark.parametrize("value", [[1], {"v": 1}])
def test_nested_signal_value(value):
    fr = {"timestamps": [0], "signals": {"x": [value]}}
    assert rejected(encode(doc(case(fr=fr)))).pointer == "/cases/0/frame/signals/x/0"


def test_signal_pointer_escapes_json_pointer_metacharacters():
    fr = {"timestamps": [0], "signals": {"a/b~c": [None]}}
    assert rejected(encode(doc(case(fr=fr)))).pointer == "/cases/0/frame/signals/a~1b~0c/0"


def test_numbers_and_booleans_are_accepted_signal_values():
    fr = {"timestamps": [0, 60, 120], "signals": {"x": [1, 2.5, True], "y": [False, -3, 0.0]}}
    loaded = load_cases(encode(doc(case(fr=fr, expect={"proposedIntents": []}))))
    assert loaded.cases[0].frame.signals["x"] == (1, 2.5, True)


def test_expect_with_zero_keys():
    assert rejected(encode(doc(case(expect={})))).pointer == "/cases/0/expect"


def test_expect_with_both_keys():
    expect = {"proposedIntents": [], "runtimeError": "Signal"}
    assert rejected(encode(doc(case(expect=expect)))).pointer == "/cases/0/expect"


def test_expect_must_be_an_object():
    assert rejected(encode(doc(case(expect=["x"])))).pointer == "/cases/0/expect"


def test_unknown_expect_key_alone():
    expect = {"executedActions": []}
    assert rejected(encode(doc(case(expect=expect)))).pointer == "/cases/0/expect/executedActions"


def test_unknown_expect_key_beside_a_valid_one():
    expect = {"proposedIntents": [], "extra": 1}
    assert rejected(encode(doc(case(expect=expect)))).pointer == "/cases/0/expect/extra"


def test_proposed_intents_must_be_a_list():
    expect = {"proposedIntents": {"intent": "EXECUTE", "timestamp": 0}}
    assert rejected(encode(doc(case(expect=expect)))).pointer == "/cases/0/expect/proposedIntents"


@pytest.mark.parametrize(
    "intent, pointer",
    [
        ("EXECUTE", "/cases/0/expect/proposedIntents/0"),
        ({"intent": "EXECUTE"}, "/cases/0/expect/proposedIntents/0/timestamp"),
        ({"timestamp": 0}, "/cases/0/expect/proposedIntents/0/intent"),
        ({"intent": "EXECUTE", "timestamp": 0, "action": "x"},
         "/cases/0/expect/proposedIntents/0/action"),
        ({"intent": "LAUNCH", "timestamp": 0}, "/cases/0/expect/proposedIntents/0/intent"),
        ({"intent": 1, "timestamp": 0}, "/cases/0/expect/proposedIntents/0/intent"),
        ({"intent": "EXECUTE", "timestamp": True}, "/cases/0/expect/proposedIntents/0/timestamp"),
        ({"intent": "EXECUTE", "timestamp": 0.5}, "/cases/0/expect/proposedIntents/0/timestamp"),
        ({"intent": "EXECUTE", "timestamp": 0, "asset": None},
         "/cases/0/expect/proposedIntents/0/asset"),
        ({"intent": "EXECUTE", "timestamp": 0, "asset": ""},
         "/cases/0/expect/proposedIntents/0/asset"),
        ({"intent": "EXECUTE", "timestamp": 0, "confidence": "high"},
         "/cases/0/expect/proposedIntents/0/confidence"),
        ({"intent": "EXECUTE", "timestamp": 0, "confidence": True},
         "/cases/0/expect/proposedIntents/0/confidence"),
        ({"intent": "EXECUTE", "timestamp": 0, "confidence": None},
         "/cases/0/expect/proposedIntents/0/confidence"),
    ],
)
def test_malformed_intent_dict(intent, pointer):
    expect = {"proposedIntents": [intent]}
    assert rejected(encode(doc(case(expect=expect)))).pointer == pointer


def test_well_formed_intent_with_asset_and_confidence_loads():
    expect = {"proposedIntents": [
        {"intent": "BUY", "timestamp": 0, "asset": "BTC", "confidence": 0.7},
    ]}
    loaded = load_cases(encode(doc(case(expect=expect))))
    assert [dict(i) for i in loaded.cases[0].expected_intents] == expect["proposedIntents"]
    assert loaded.cases[0].expected_runtime_error is None


@pytest.mark.parametrize("value", ["", 0, None, ["Signal"]])
def test_runtime_error_must_be_a_non_empty_string(value):
    expect = {"runtimeError": value}
    assert rejected(encode(doc(case(expect=expect)))).pointer == "/cases/0/expect/runtimeError"


def test_file_over_one_mebibyte_and_the_boundary():
    body = encode(doc())
    exact = body + b" " * (MAX_CASES_BYTES - len(body))
    assert len(exact) == MAX_CASES_BYTES
    assert len(load_cases(exact).cases) == 1
    over = exact + b" "
    err = rejected(over)
    assert err.pointer == ""
    assert "1048576" in err.message


def test_fixture_error_string_carries_the_pointer():
    err = rejected(encode(doc(case(name=""))))
    assert str(err).startswith("/cases/0/name")


# -- 4. unsupported handlers --------------------------------------------------

INFER_SOURCE = (
    "tier nano+\n"
    "strategy S {\n"
    "    input close: series<float>\n"
    "    signature Bias {\n"
    "        input price: float\n"
    "        output score: float\n"
    "    }\n"
    "    let call = infer(Bias, close)\n"
    "    every 1m {\n"
    "        if call.score > 0.5 {\n"
    "            execute()\n"
    "        }\n"
    "    }\n"
    "}\n"
)

ESCALATE_SOURCE = (
    "tier nano+\n"
    "strategy S {\n"
    "    input close: series<float>\n"
    "    agent Desk { role research }\n"
    "    every 1m {\n"
    "        if close < 0 {\n"
    "            escalate Desk\n"
    "        }\n"
    "    }\n"
    "}\n"
)

ROUTE_SOURCE = """tier nano+
strategy Reasoned {
  input score: series<float>
  signature Judge {
    input value: float range [0, 1]
    output confidence: confidence range [0, 1]
  }
  let verdict = infer(Judge, score)
  agent Desk { role validation }
  route Gate {
    execute approved when verdict.confidence >= 0.5
    otherwise { escalate Desk }
  }
}
"""

CLOSE_CASES = encode(doc(case(
    fr={"timestamps": [0], "signals": {"close": [1.0], "score": [1.0]}},
    expect={"proposedIntents": []},
)))


@pytest.mark.parametrize(
    "source, op",
    [(INFER_SOURCE, "ai.infer"), (ESCALATE_SOURCE, "llmre.escalate"), (ROUTE_SOURCE, "route")],
)
def test_reasoning_modules_are_refused_before_any_case_runs(source, op, monkeypatch):
    module = compile_module(source)
    assert module.of_op(op)
    calls = counting_run_module(monkeypatch)
    with pytest.raises(UnsupportedHandlerError) as info:
        run_cases(module, load_cases(CLOSE_CASES))
    assert calls == []
    assert info.value.code == "UNSUPPORTED_HANDLER"
    assert info.value.category == "capability"
    assert info.value.message


@pytest.mark.parametrize("op", ["ai.signature", "ai.infer", "llmre.escalate", "route"])
def test_unsupported_node_refused_even_when_tier_and_effects_look_safe(op, monkeypatch):
    # Force tier nano and the baseline effects on an in-process module, so only
    # the node check is left standing between it and the VM.
    module = compile_module(ROUTE_SOURCE)
    assert module.of_op(op)
    keep = {node.id for node in module.nodes if node.op not in UNSUPPORTED_OPS or node.op == op}
    forged = dataclasses.replace(
        module,
        tier="nano",
        effects=("intent.emit", "log.append"),
        nodes=tuple(node for node in module.nodes if node.id in keep),
    )
    assert {node.op for node in forged.nodes} & UNSUPPORTED_OPS == {op}
    calls = counting_run_module(monkeypatch)
    with pytest.raises(UnsupportedHandlerError) as info:
        admit_module(forged)
    assert op in info.value.message
    with pytest.raises(UnsupportedHandlerError):
        run_cases(forged, load_cases(CLOSE_CASES))
    assert calls == []


def test_signature_only_module_is_refused(monkeypatch):
    source = (
        "tier nano+\n"
        "strategy S {\n"
        "    input close: series<float>\n"
        "    signature Bias {\n"
        "        input price: float\n"
        "        output score: float\n"
        "    }\n"
        "    every 1m {\n"
        "        if close > 0 { execute() }\n"
        "    }\n"
        "}\n"
    )
    module = compile_module(source)
    assert module.of_op("ai.signature")
    calls = counting_run_module(monkeypatch)
    with pytest.raises(UnsupportedHandlerError):
        run_cases(module, load_cases(CLOSE_CASES))
    assert calls == []


def _variant(module, **changes):
    data = module.to_dict(include_hash=False)
    data.update(changes)
    return NanoModule.from_dict(data)


@pytest.mark.parametrize(
    "effects",
    [
        ["intent.emit", "llm.call", "log.append"],
        ["intent.emit", "sign.emit", "log.append"],
        ["intent.emit", "llmre.escalate", "log.append"],
    ],
)
def test_effects_outside_the_allowed_set_are_refused(example_module, effects, monkeypatch):
    module = _variant(example_module, effects=effects)
    assert module.tier == "nano"
    assert set(module.effects) == set(effects)
    calls = counting_run_module(monkeypatch)
    with pytest.raises(UnsupportedHandlerError) as info:
        run_cases(module, load_cases(EXAMPLE_CASES.read_bytes()))
    assert calls == []
    extra = (set(effects) - ALLOWED_EFFECTS).pop()
    assert extra in info.value.message


@pytest.mark.parametrize("tier", ["nano+", "nano++"])
def test_higher_tier_is_refused_even_without_reasoning_nodes(example_module, tier, monkeypatch):
    module = _variant(example_module, tier=tier)
    assert module.tier == tier
    assert not {node.op for node in module.nodes} & UNSUPPORTED_OPS
    calls = counting_run_module(monkeypatch)
    with pytest.raises(UnsupportedHandlerError) as info:
        run_cases(module, load_cases(EXAMPLE_CASES.read_bytes()))
    assert calls == []
    assert tier in info.value.message


def test_example_module_is_admitted(example_module):
    assert example_module.tier == "nano"
    assert admit_module(example_module) is None


def test_admit_module_rejects_non_modules():
    with pytest.raises(TypeError):
        admit_module({"tier": "nano"})


def test_reasoning_package_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "pkg"
    root.mkdir()
    entry = (
        "tier nano+\n"
        "strategy Agent {\n"
        "  input value: series<float>\n"
        "  signature Review { input value: float output confidence: float }\n"
        "  let review = infer(Review, value)\n"
        "  every 1m { if review.confidence > 0.5 { execute() } }\n"
        "}\n"
    )
    (root / "agent.nano").write_bytes(entry.encode())
    manifest = {
        "nanoPackageVersion": "0.1.0", "entry": "agent.nano", "modules": {},
        "effects": ["intent.emit", "llm.call", "log.append"],
    }
    (root / "nano.json").write_text(json.dumps(manifest), encoding="utf-8")
    deps = {p.name: "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.glob("*.nano")}
    (root / "nano.lock").write_text(
        json.dumps({"nanoLockVersion": "0.1.0", "dependencies": deps}), encoding="utf-8")
    module = compile_package(root / "nano.json", lock_path=root / "nano.lock").module
    assert module.of_op("ai.infer")
    calls = counting_run_module(monkeypatch)
    cases = encode(doc(case(fr={"timestamps": [0], "signals": {"value": [1.0]}},
                            expect={"proposedIntents": []})))
    with pytest.raises(UnsupportedHandlerError):
        run_cases(module, load_cases(cases))
    assert calls == []


# -- 5. mismatched expectations ----------------------------------------------


def _single(example_module, fr, expect):
    results = run_cases(example_module, load_cases(encode(doc(case(fr=fr, expect=expect)))))
    assert len(results) == 1
    return results[0]


def test_wrong_intent_fails_with_reason(example_module):
    result = _single(example_module, frame(), {"proposedIntents": [{"intent": "PAUSE", "timestamp": 0}]})
    assert result.status == "fail"
    assert result.observed == {"proposedIntents": [{"intent": "EXECUTE", "timestamp": 0}]}
    assert "EXECUTE" in result.reason and "PAUSE" in result.reason
    assert result.to_dict()["reason"] == result.reason


def test_wrong_timestamp_fails(example_module):
    result = _single(example_module, frame(), {"proposedIntents": [{"intent": "EXECUTE", "timestamp": 60}]})
    assert result.status == "fail"


def test_extra_expected_intent_fails(example_module):
    expect = {"proposedIntents": [
        {"intent": "EXECUTE", "timestamp": 0}, {"intent": "EXECUTE", "timestamp": 0},
    ]}
    result = _single(example_module, frame(), expect)
    assert result.status == "fail"
    assert result.reason


def test_missing_expected_intent_fails(example_module):
    result = _single(example_module, frame(), {"proposedIntents": []})
    assert result.status == "fail"
    assert result.observed == {"proposedIntents": [{"intent": "EXECUTE", "timestamp": 0}]}


def test_extra_intent_field_fails(example_module):
    expect = {"proposedIntents": [{"intent": "EXECUTE", "timestamp": 0, "asset": "BTC"}]}
    assert _single(example_module, frame(), expect).status == "fail"


def test_intent_order_matters(example_module):
    fr = {"timestamps": [0, 60], "signals": {
        "evidence_count": [2, 1], "evidence_verified": [True, True], "model_tokens": [100, 100],
    }}
    ordered = [{"intent": "EXECUTE", "timestamp": 0}, {"intent": "PAUSE", "timestamp": 60}]
    assert _single(example_module, fr, {"proposedIntents": ordered}).status == "pass"
    assert _single(example_module, fr, {"proposedIntents": ordered[::-1]}).status == "fail"


def test_expected_runtime_error_but_got_intents_fails(example_module):
    result = _single(example_module, frame(), {"runtimeError": "not present"})
    assert result.status == "fail"
    assert result.observed == {"proposedIntents": [{"intent": "EXECUTE", "timestamp": 0}]}
    assert "not present" in result.reason


def test_expected_intents_but_got_runtime_error_fails(example_module):
    result = _single(example_module, frame(tokens=None),
                     {"proposedIntents": [{"intent": "EXECUTE", "timestamp": 0}]})
    assert result.status == "fail"
    assert result.observed == {"runtimeError": "Signal 'model_tokens' not present in market frame"}
    assert "model_tokens" in result.reason


def test_expected_empty_intents_but_got_runtime_error_fails(example_module):
    result = _single(example_module, frame(tokens=None), {"proposedIntents": []})
    assert result.status == "fail"


def test_runtime_error_substring_mismatch_fails(example_module):
    result = _single(example_module, frame(tokens=None),
                     {"runtimeError": "Signal 'evidence_count' not present"})
    assert result.status == "fail"
    assert result.observed == {"runtimeError": "Signal 'model_tokens' not present in market frame"}
    assert "evidence_count" in result.reason


def test_runtime_error_substring_match_passes(example_module):
    result = _single(example_module, frame(tokens=None), {"runtimeError": "model_tokens"})
    assert result.status == "pass"
    assert result.reason is None


def test_non_runtime_exceptions_propagate(example_module, monkeypatch):
    def boom(module, fr, **kwargs):
        raise ZeroDivisionError("vm bug")

    monkeypatch.setattr(vm, "run_module", boom)
    with pytest.raises(ZeroDivisionError):
        run_cases(example_module, load_cases(EXAMPLE_CASES.read_bytes()))


def test_run_cases_rejects_an_empty_case_set(example_module, monkeypatch):
    calls = counting_run_module(monkeypatch)
    with pytest.raises(FixtureError) as info:
        run_cases(example_module, CaseSet(version="1.0.0", cases=()))
    assert info.value.pointer == "/cases"
    assert calls == []


# -- 6. all-results accounting -----------------------------------------------


def test_every_case_is_reported_without_short_circuit(example_module, monkeypatch):
    calls = counting_run_module(monkeypatch)
    fixture = doc(
        case("fails first", expect={"proposedIntents": [{"intent": "PAUSE", "timestamp": 0}]}),
        case("passes second"),
    )
    results = run_cases(example_module, load_cases(encode(fixture)))
    assert len(calls) == 2
    assert [(r.name, r.status) for r in results] == [("fails first", "fail"), ("passes second", "pass")]
    assert sum(r.status == "pass" for r in results) == 1
    assert sum(r.status == "fail" for r in results) == 1
    assert isinstance(results, tuple)
    assert all(isinstance(r, CaseResult) for r in results)
    assert set(results[0].to_dict()) == {"name", "status", "observed", "reason"}
    assert set(results[1].to_dict()) == {"name", "status", "observed"}


def test_case_result_to_dict_is_a_fresh_plain_copy(example_module):
    result = run_cases(example_module, load_cases(EXAMPLE_CASES.read_bytes()))[0]
    out = result.to_dict()
    out["observed"]["proposedIntents"].append({"intent": "PAUSE", "timestamp": 0})
    assert result.to_dict()["observed"] == {"proposedIntents": [{"intent": "EXECUTE", "timestamp": 0}]}
    json.dumps(result.to_dict(), sort_keys=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "pass"
