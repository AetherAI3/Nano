"""Reasoning records must satisfy their signature before affecting proposals."""

import pytest

from nano.compiler import compile_module
from nano.runtime.interpreter import MarketFrame
from nano.runtime.vm import run_module


def _source(kind="float", *, bounds="", condition="call.score > 0.5", extra_output=""):
    return f'''tier nano+
strategy S {{
  input close: series<float>
  signature Bias {{
    input price: float
    output score: {kind} {bounds}
    {extra_output}
  }}
  let call = infer(Bias, close)
  every 1m {{
    if {condition} {{ buy(BTC) }} else {{ sell(BTC) }}
    if close > 0 {{ observe() }}
  }}
}}'''


class Recorded:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def infer(self, signature, inputs, *, timestamp):
        self.calls.append((signature, dict(inputs), timestamp))
        response = self.responses[timestamp // 60]
        if isinstance(response, BaseException):
            raise response
        return response


def _run(source, responses):
    provider = Recorded(responses)
    frame = MarketFrame(
        timestamps=tuple(index * 60 for index in range(len(responses))),
        signals={"close": tuple(1.0 for _ in responses)},
    )
    return run_module(compile_module(source), frame, provider=provider), provider


@pytest.mark.parametrize("response", [
    {"score": True}, {"score": "0.9"}, {"score": None},
    {"score": float("inf")}, {"score": -float("inf")}, {"score": float("nan")},
    {"score": 10**1000}, {}, {"score": 0.9, "secret_metadata": "do-not-log"},
    None, [("score", 0.9)], 42,
])
def test_invalid_record_withholds_both_dependent_branches_but_not_unrelated_rule(response):
    result, provider = _run(_source(), [response])
    assert [intent.action for intent in result.intents] == ["OBSERVE"]
    assert len(provider.calls) == 1
    problems = [entry for entry in result.log if entry.event == "infer.invalid"]
    assert len(problems) == 1
    assert "Bias" in problems[0].detail
    assert "do-not-log" not in problems[0].detail
    assert "secret_metadata" not in problems[0].detail
    assert len(problems[0].detail) <= 256
    assert not any(entry.event == "infer.called" for entry in result.log)


@pytest.mark.parametrize("score, action", [(0.9, "BUY"), (0.1, "SELL"), (1, "BUY"), (0, "SELL")])
def test_valid_float_results_and_integer_widening_keep_normal_decisions(score, action):
    result, _ = _run(_source(), [{"score": score}])
    again, _ = _run(_source(), [{"score": score}])
    assert [intent.action for intent in result.intents] == [action, "OBSERVE"]
    assert result.to_dict() == again.to_dict()
    assert not any(entry.event in ("infer.invalid", "infer.failed") for entry in result.log)


@pytest.mark.parametrize("score", [True, 1.0, "1", 10**640])
def test_declared_int_rejects_wrong_types_and_existing_ir_magnitude_limit(score):
    result, _ = _run(_source("int"), [{"score": score}])
    assert [intent.action for intent in result.intents] == ["OBSERVE"]
    assert any(entry.event == "infer.invalid" for entry in result.log)


def test_declared_int_accepts_exact_integer():
    result, _ = _run(_source("int"), [{"score": 1}])
    assert [intent.action for intent in result.intents] == ["BUY", "OBSERVE"]


@pytest.mark.parametrize("score, expected", [(True, "BUY"), (False, "SELL"), (1, None), (0.0, None)])
def test_boolean_output_is_distinct_from_numbers(score, expected):
    result, _ = _run(_source("bool", condition="call.score"), [{"score": score}])
    assert [intent.action for intent in result.intents] == ([expected] if expected else []) + ["OBSERVE"]


@pytest.mark.parametrize("score, accepted", [(0, True), (1.0, True), (0.9, True), (-0.1, False), (1.1, False)])
def test_confidence_output_has_its_intrinsic_range(score, accepted):
    result, _ = _run(_source("confidence"), [{"score": score}])
    assert any(intent.action in ("BUY", "SELL") for intent in result.intents) is accepted
    assert any(entry.event == "infer.invalid" for entry in result.log) is (not accepted)


@pytest.mark.parametrize("score, accepted", [(0.25, True), (0.75, True), (0.2, False), (0.8, False)])
def test_declared_range_is_inclusive_and_enforced(score, accepted):
    result, _ = _run(_source(bounds="range [0.25, 0.75]"), [{"score": score}])
    assert any(intent.action in ("BUY", "SELL") for intent in result.intents) is accepted


def test_all_fields_validate_before_any_part_of_the_record_is_visible():
    result, _ = _run(_source(extra_output="output approved: bool"), [{"score": 0.9, "approved": 1}])
    assert [intent.action for intent in result.intents] == ["OBSERVE"]
    valid, _ = _run(_source(extra_output="output approved: bool"), [{"score": 0.9, "approved": True}])
    assert [intent.action for intent in valid.intents] == ["BUY", "OBSERVE"]


def test_provider_failure_is_distinct_and_does_not_log_exception_contents():
    result, _ = _run(_source(), [RuntimeError("credential=do-not-log")])
    assert [intent.action for intent in result.intents] == ["OBSERVE"]
    assert any(entry.event == "infer.failed" and "RuntimeError" in entry.detail for entry in result.log)
    assert not any("credential" in entry.detail or "do-not-log" in entry.detail for entry in result.log)
    assert not any(entry.event == "infer.invalid" for entry in result.log)


def test_cancellation_signals_are_not_swallowed_as_provider_errors():
    with pytest.raises(KeyboardInterrupt):
        _run(_source(), [KeyboardInterrupt()])


def test_provider_exception_class_cannot_inject_multiline_diagnostics():
    unusual_error = type("Provider\nFailure" + "x" * 400, (Exception,), {})
    result, _ = _run(_source(), [unusual_error("do-not-log")])
    failure = next(entry for entry in result.log if entry.event == "infer.failed")
    assert "\n" not in failure.detail
    assert len(failure.detail) <= 256
    assert "do-not-log" not in failure.detail


def test_unreadable_mapping_is_an_invalid_response_not_a_lane_failure():
    from collections.abc import Mapping

    class BrokenResponse(Mapping):
        def __len__(self):
            return 1

        def __iter__(self):
            return iter(("score",))

        def __getitem__(self, key):
            raise ValueError("do-not-log")

    result, _ = _run(_source(), [BrokenResponse()])
    assert [intent.action for intent in result.intents] == ["OBSERVE"]
    assert any(entry.event == "infer.invalid" and "unreadable_mapping" in entry.detail for entry in result.log)
    assert not any("do-not-log" in entry.detail for entry in result.log)


def test_invalid_bar_does_not_erase_valid_bars():
    result, _ = _run(_source(), [{"score": 0.9}, {"score": True}, {"score": 0.1}])
    assert [(intent.timestamp, intent.action) for intent in result.intents if intent.action != "OBSERVE"] == [(0, "BUY"), (120, "SELL")]
    assert [entry.timestamp for entry in result.log if entry.event == "infer.invalid"] == [60]


@pytest.mark.parametrize("kind", ["string", "duration", "series<float>"])
def test_unimplemented_output_kinds_fail_explicitly_before_provider_call(kind):
    # The grammar accepts these declarations; this numeric VM must not pretend
    # to consume their outputs. The unrelated condition still runs.
    source = _source(extra_output=f"output detail: {kind}")
    result, provider = _run(source, [{"score": 0.9, "detail": "x"}])
    assert provider.calls == []
    assert [intent.action for intent in result.intents] == ["OBSERVE"]
    assert any(entry.event == "infer.invalid" and "unsupported_output_type" in entry.detail for entry in result.log)


def test_missing_provider_keeps_existing_skipped_behavior():
    frame = MarketFrame(timestamps=(0,), signals={"close": (1.0,)})
    result = run_module(compile_module(_source()), frame)
    assert [intent.action for intent in result.intents] == ["OBSERVE"]
    assert any(entry.event == "infer.skipped" for entry in result.log)
    assert not any(entry.event in ("infer.invalid", "infer.failed") for entry in result.log)
