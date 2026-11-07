"""Deterministic nesting admission across the source compiler frontends."""

import sys

import pytest

from nano.aethercode.diagnostics import diagnostics
from nano.cli.main import main
from nano.compiler import (
    CompilerLimits,
    DEFAULT_COMPILER_LIMITS,
    MAX_COMPILER_NESTING,
    NanoResourceLimitError,
    NanoSyntaxError,
    check_source,
    compile_module,
    compile_source,
    compile_to_dict,
    parse,
)


def expression_source(expression):
    return f"strategy Deep {{ let x = {expression} every 1m {{ observe() }} }}"


def conditional_source(count, guard="true"):
    return (
        "strategy Deep { every 1m { "
        + "if true { " * (count - 1)
        + f"if {guard} {{ observe() }}"
        + " }" * (count - 1)
        + " } }"
    )


EXPRESSION_FAMILIES = {
    "parentheses": lambda n: "(" * n + "true" + ")" * n,
    "not": lambda n: "not " * n + "true",
    "negation": lambda n: "- " * n + "1",
    "and": lambda n: "true and " * n + "true",
    "or": lambda n: "true or " * n + "true",
    "additive": lambda n: "1 + " * n + "1",
    "multiplicative": lambda n: "1 * " * n + "1",
    "calls": lambda n: "f(" * n + "1" + ")" * n,
    "index_offsets": lambda n: "x[" * n + "0" + "]" * n,
    "index_chain": lambda n: "x" + "[0]" * n,
    "member_chain": lambda n: "x" + ".field" * n,
}


@pytest.mark.parametrize("limit", [4, MAX_COMPILER_NESTING])
@pytest.mark.parametrize("family", EXPRESSION_FAMILIES)
def test_expression_exact_limit_and_one_over(limit, family):
    limits = CompilerLimits(max_nesting=limit)
    make = EXPRESSION_FAMILIES[family]
    parse(expression_source(make(limit)), limits=limits)
    with pytest.raises(NanoResourceLimitError) as caught:
        parse(expression_source(make(limit + 1)), limits=limits)
    assert caught.value.code == "RESOURCE_LIMIT"
    assert caught.value.limit == limit
    assert caught.value.line == 1
    assert caught.value.column > 1
    assert "simplify or split" in caught.value.message


@pytest.mark.parametrize("limit", [4, MAX_COMPILER_NESTING])
def test_conditionals_exact_limit_and_one_over(limit):
    limits = CompilerLimits(max_nesting=limit)
    compile_module(conditional_source(limit), limits=limits)
    with pytest.raises(NanoResourceLimitError) as caught:
        compile_module(conditional_source(limit + 1), limits=limits)
    assert caught.value.resource == "syntax nesting"
    assert caught.value.column == len("strategy Deep { every 1m { ") + 10 * limit + 1


@pytest.mark.parametrize("family", ["parentheses", "not", "negation", "and", "or", "additive", "multiplicative"])
def test_admitted_expressions_reach_v1_codegen_and_loader(family):
    source = expression_source(EXPRESSION_FAMILIES[family](MAX_COMPILER_NESTING))
    assert check_source(source).strategy.name == "Deep"
    assert compile_to_dict(source)["nanoIrVersion"] == "1.0.0"
    assert compile_module(source).name == "Deep"


def test_syntax_scopes_are_shared_and_expression_budget_covers_mixed_operators():
    limit = MAX_COMPILER_NESTING
    guard = "(" * (limit - 1) + "true" + ")" * (limit - 1)
    compile_module(conditional_source(1, guard))
    with pytest.raises(NanoResourceLimitError):
        parse(conditional_source(1, "(" + guard + ")"))

    # All statement scopes plus the deepest admitted operator tree: downstream
    # checking, baseline selection, v1 lowering and loader also stay bounded.
    compile_module(conditional_source(limit, "true and " * limit + "true"))
    mixed = expression_source("not " * 16 + "(" + "true and " * 16 + "true)")
    compile_module(mixed)
    with pytest.raises(NanoResourceLimitError):
        compile_module(mixed.replace("not ", "not not ", 1))


@pytest.mark.parametrize("api", [parse, check_source, compile_source, compile_to_dict, compile_module])
def test_public_source_entrypoints_preserve_position_and_error_code(api):
    source = "strategy Deep {\n let x = " + "(" * 33 + "true" + ")" * 33 + "\n every 1m { observe() }\n}"
    with pytest.raises(NanoResourceLimitError) as caught:
        api(source)
    error = caught.value
    assert (error.line, error.column) == (2, 42)
    assert isinstance(error, NanoSyntaxError)  # existing editor/host catch contract
    assert source.splitlines()[error.line - 1][error.column - 1] == "("
    assert error.code == "RESOURCE_LIMIT"


def test_original_report_and_editor_agree_with_compiler():
    source = "strategy Deep { every 1m { if " + "(" * 100 + "RSI < 30" + ")" * 100 + " { observe() } } }"
    with pytest.raises(NanoResourceLimitError) as caught:
        compile_to_dict(source)
    diagnostic, = diagnostics(source)
    assert (diagnostic.line, diagnostic.column, diagnostic.message) == (
        caught.value.line, caught.value.column, caught.value.message
    )


def test_cli_and_editor_report_identical_resource_limit(tmp_path, capsys):
    source = expression_source("(" * 33 + "true" + ")" * 33)
    path = tmp_path / "deep.nano"
    path.write_text(source, encoding="utf-8")
    diagnostic, = diagnostics(source)
    assert main(["check", str(path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert f"{diagnostic.line}:{diagnostic.column}" in output.err
    assert diagnostic.message in output.err


@pytest.mark.parametrize("family", EXPRESSION_FAMILIES)
def test_large_generated_expression_rejects_without_recursion_error(family):
    before = sys.getrecursionlimit()
    source = expression_source(EXPRESSION_FAMILIES[family](10_000))
    with pytest.raises(NanoResourceLimitError):
        compile_to_dict(source)
    assert sys.getrecursionlimit() == before


def test_large_conditional_rejects_without_recursion_error():
    with pytest.raises(NanoResourceLimitError):
        compile_module(conditional_source(10_000))


def test_lower_host_limit_is_explicit_and_does_not_change_default():
    source = expression_source("((((true))))")
    for api in (parse, check_source, compile_module, compile_to_dict, compile_source):
        with pytest.raises(NanoResourceLimitError) as caught:
            api(source, limits=CompilerLimits(max_nesting=3))
        assert caught.value.limit == 3
    compile_module(source)
    assert DEFAULT_COMPILER_LIMITS.max_nesting == MAX_COMPILER_NESTING == 32


@pytest.mark.parametrize("value", [0, -1, 33, True, 1.5, None])
def test_unsafe_host_limit_is_rejected(value):
    with pytest.raises(ValueError, match="max_nesting"):
        CompilerLimits(max_nesting=value)


def test_existing_generic_type_grammar_does_not_recurse():
    with pytest.raises(NanoSyntaxError, match="Expected '>'"):
        parse("strategy Types { input x: series<series<float>> }")
