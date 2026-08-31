"""CLI surface for the sibling Intent frontend."""

from __future__ import annotations

import io
import json

from nano.cli.main import main


def _run(*argv: str):
    import contextlib

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def test_intent_parse_prints_ast_structure():
    code, output, error = _run("intent", "parse", "spy earnings")
    assert code == 0
    assert error == ""
    document = json.loads(output)
    assert document["frame"] == "security_topic_noun_phrase"
    assert [entity["kind"] for entity in document["entities"]] == ["security", "topic"]


def test_intent_compile_json_is_canonical():
    code, output, error = _run("intent", "compile", "when is spy earnings", "--json")
    assert code == 0
    assert error == ""
    assert output.endswith("\n")
    assert "\n" not in output[:-1]
    assert json.loads(output)["nanoIntentVersion"] == "0.1.0"


def test_intent_explain_names_policy_and_reasons():
    code, output, error = _run(
        "intent",
        "explain",
        "describe the predictions of spy earnings over the next 3 quarters",
    )
    assert code == 0
    assert error == ""
    assert "frame: security_topic_forecast" in output
    assert "response: compact_answer (research)" in output
    assert "EXPLICIT_HORIZON" in output


def test_intent_cli_rejects_oversized_input_without_traceback():
    code, output, error = _run("intent", "compile", "x" * 5000)
    assert code == 1
    assert output == ""
    assert "character limit" in error
    assert "Traceback" not in error
