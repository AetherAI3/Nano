"""Compiler/loader acceptance parity at the source-to-IR trust boundary."""

import sys

import pytest

from nano.compiler import check_source, compile_module, compile_to_dict
from nano.compiler.errors import NanoSyntaxError, NanoTypeError
from nano.ir import IRValidationError, load_module
from nano.ir.schema import MAX_CANONICAL_INTEGER_DIGITS


def _v1_document(*, params=()):
    return {
        "type": "Strategy",
        "nanoIrVersion": "1.0.0",
        "name": "ParamBoundary",
        "effects": ["log.append"],
        "nodes": [
            {
                "id": "n1",
                "op": "schedule",
                "inputs": [],
                "attrs": {"interval": "1m"},
            }
        ],
        "entries": [],
        "params": list(params),
    }


@pytest.mark.parametrize(
    "source",
    [
        "strategy Empty {}",
        "strategy Params { param window: int = 20 }",
        "strategy Inputs { input close: series<float> }",
        (
            "strategy Declarations {\n"
            "    param window: int = 20\n"
            "    input close: series<float>\n"
            "}\n"
        ),
    ],
    ids=("empty", "params-only", "inputs-only", "params-and-inputs-only"),
)
def test_source_that_would_emit_no_nodes_is_rejected_by_every_compile_entry(source):
    with pytest.raises(NanoTypeError, match="produces no IR nodes") as checked:
        check_source(source)
    assert (checked.value.line, checked.value.column) == (1, 1)

    for compile_entry in (compile_to_dict, compile_module):
        with pytest.raises(NanoTypeError, match="produces no IR nodes"):
            compile_entry(source)


def test_empty_risk_block_is_rejected_before_codegen_emits_invalid_ir():
    source = "strategy S {\n    risk {}\n    every 1m {}\n}\n"
    with pytest.raises(NanoTypeError, match="requires at least one limit") as error:
        compile_to_dict(source)
    assert (error.value.line, error.value.column) == (2, 5)


def test_empty_agent_body_is_not_silently_treated_as_a_bare_agent():
    source = "strategy S {\n    agent Desk {}\n}\n"
    with pytest.raises(NanoSyntaxError, match="requires exactly one 'role'") as error:
        compile_to_dict(source)
    assert (error.value.line, error.value.column) == (2, 17)


def test_repeated_agent_role_is_rejected_at_the_second_clause():
    source = (
        "strategy S {\n"
        "    agent Desk { role research\n"
        "        role execution }\n"
        "}\n"
    )
    with pytest.raises(NanoSyntaxError, match="declares 'role' more than once") as error:
        compile_to_dict(source)
    assert (error.value.line, error.value.column) == (3, 9)


@pytest.mark.parametrize(
    "source",
    [
        "strategy ScheduleOnly { every 1m {} }",
        "strategy AgentOnly { agent Desk }",
        "strategy Role { agent Desk { role research } }",
        (
            "strategy Risk {\n"
            "    risk { max_drawdown 0.1 }\n"
            "    every 1m {}\n"
            "}\n"
        ),
    ],
    ids=("empty-schedule", "bare-agent", "role-agent", "nonempty-risk"),
)
def test_positive_source_shapes_compile_and_load_end_to_end(source):
    selected = compile_to_dict(source)
    assert selected["nanoIrVersion"] in ("0.1.0", "1.0.0")
    assert load_module(selected).nodes

    forced_v1 = compile_to_dict(source, ir_version="1.0.0")
    assert forced_v1["nanoIrVersion"] == "1.0.0"
    assert load_module(forced_v1).nodes


@pytest.mark.parametrize(
    ("type_name", "value"),
    [
        ("bool", True),
        ("int", 7),
        ("float", 7),
        ("float", 7.5),
        ("confidence", 0),
        ("confidence", 0.5),
        ("confidence", 1),
        ("string", "desk"),
        ("duration", "5m"),
    ],
)
def test_v1_param_values_match_their_declared_scalar_type(type_name, value):
    module = load_module(
        _v1_document(params=({"name": "p", "type": type_name, "value": value},))
    )
    assert module.params[0].to_dict() == {
        "name": "p",
        "type": type_name,
        "value": value,
    }


@pytest.mark.parametrize(
    ("type_name", "value"),
    [
        ("bool", 1),
        ("int", True),
        ("int", 1.5),
        ("float", True),
        ("float", float("nan")),
        ("float", float("inf")),
        ("confidence", -0.1),
        ("confidence", 1.1),
        ("string", 1),
        ("duration", "minute"),
        ("duration", "١m"),
        ("duration", 5),
    ],
)
def test_v1_param_type_mismatches_are_stable_load_errors(type_name, value):
    with pytest.raises(IRValidationError, match="does not match declared scalar type"):
        load_module(
            _v1_document(
                params=({"name": "p", "type": type_name, "value": value},)
            )
        )


@pytest.mark.parametrize("type_name", ["series<float>", "record<X>", "number"])
def test_v1_params_reject_non_scalar_or_unknown_declared_types(type_name):
    with pytest.raises(IRValidationError, match="type must be a scalar type"):
        load_module(
            _v1_document(
                params=({"name": "p", "type": type_name, "value": 1},)
            )
        )


@pytest.mark.parametrize("value", [10**640 - 1, -(10**640 - 1)])
def test_canonical_integer_boundary_accepts_640_decimal_digits(value):
    module = load_module(
        _v1_document(params=({"name": "p", "type": "int", "value": value},))
    )
    assert module.params[0].value == value


@pytest.mark.parametrize("value", [10**640, -(10**640)])
def test_canonical_integer_boundary_rejects_641_decimal_digits(value):
    with pytest.raises(
        IRValidationError,
        match=f"at most {MAX_CANONICAL_INTEGER_DIGITS} decimal digits",
    ):
        load_module(
            _v1_document(params=({"name": "p", "type": "int", "value": value},))
        )


def test_source_param_uses_the_same_canonical_integer_boundary():
    accepted = "9" * MAX_CANONICAL_INTEGER_DIGITS
    source = f"strategy S {{ param p: int = {accepted} agent Desk }}"
    assert compile_module(source).params[0].value == int(accepted)

    rejected = "1" + ("0" * MAX_CANONICAL_INTEGER_DIGITS)
    with pytest.raises(
        NanoTypeError,
        match=f"at most {MAX_CANONICAL_INTEGER_DIGITS} decimal digits",
    ):
        compile_module(f"strategy S {{ param p: int = {rejected} agent Desk }}")


def test_source_integer_boundary_is_independent_of_cpython_digit_guard():
    positive = "9" * MAX_CANONICAL_INTEGER_DIGITS
    negative = "-" + positive
    oversized_positive = "1" + ("0" * MAX_CANONICAL_INTEGER_DIGITS)
    oversized_negative = "-" + oversized_positive
    padded_one = ("0" * (MAX_CANONICAL_INTEGER_DIGITS + 100)) + "1"

    settings = [None]
    if hasattr(sys, "get_int_max_str_digits"):
        settings.extend((640, 0))

    original = sys.get_int_max_str_digits() if hasattr(sys, "get_int_max_str_digits") else None
    try:
        for setting in settings:
            if setting is not None:
                sys.set_int_max_str_digits(setting)

            for literal in (positive, negative, padded_one):
                module = compile_module(
                    f"strategy S {{ param p: int = {literal} agent Desk }}"
                )
                expected = (
                    -int(positive)
                    if literal == negative
                    else 1
                    if literal == padded_one
                    else int(positive)
                )
                assert module.params[0].value == expected

            for literal in (oversized_positive, oversized_negative):
                with pytest.raises(
                    NanoTypeError,
                    match=f"at most {MAX_CANONICAL_INTEGER_DIGITS} decimal digits",
                ):
                    compile_module(
                        f"strategy S {{ param p: int = {literal} agent Desk }}"
                    )
    finally:
        if original is not None:
            sys.set_int_max_str_digits(original)


def test_integer_validation_never_mutates_the_process_wide_digit_guard(monkeypatch):
    if not hasattr(sys, "set_int_max_str_digits"):
        pytest.skip("interpreter has no integer digit guard")

    def forbidden(_value):
        raise AssertionError("validation must not mutate sys.int_max_str_digits")

    monkeypatch.setattr(sys, "set_int_max_str_digits", forbidden)
    load_module(
        _v1_document(
            params=({"name": "p", "type": "int", "value": 10**639},)
        )
    )
    with pytest.raises(IRValidationError, match="at most 640 decimal digits"):
        load_module(
            _v1_document(
                params=({"name": "p", "type": "int", "value": 10**640},)
            )
        )
