"""Versioned compiler diagnostic goldens and a bounded JSON repair consumer."""

import builtins
import importlib
import io
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from nano.aethercode import diagnostics
from nano.cli.commands import Console
from nano.cli.main import build_parser, main
from nano.compiler import (
    CompilerDiagnostic, NanoSyntaxError, check_report, diagnostic_for_error,
    source_hash,
)
from nano.fuzzing.diagnostic_repair import reference_task_matches, run_repair_fixture
from nano.ir.schema import IRValidationError, ManifestViolation, TierViolation

ROOT = Path(__file__).resolve().parents[1]
GOLDENS = Path(__file__).parent / "fixtures" / "compiler_diagnostics"
BASELINE = "strategy Check { every 1m { if RSI < 30 { observe() } } }\n"
V1 = "strategy Check { let enabled = true every 1m { if enabled { observe() } } }\n"
SYNTAX = "strategy Check { every 1m { if (RSI < 30 { observe() } } }\n"
TYPE = "strategy Check { let value = true + 1 }\n"
RESOURCE = "strategy Check { let value = " + "(" * 33 + "true" + ")" * 33 + " }\n"
CAPABILITY = 'strategy Check { every 1m { escalate "desk" } }\n'
LOOKAHEAD = (
    "strategy Check { input close: series<float> every 1m { "
    "if close[-1] > 0 { observe() } } }\n"
)
CASES = {
    "baseline": (BASELINE, [], 0, None),
    "v1": (V1, [], 0, None),
    "syntax": (SYNTAX, [], 1, ("SYNTAX_ERROR", "syntax")),
    "type": (TYPE, [], 1, ("TYPE_ERROR", "type")),
    "unsupported-version": (BASELINE, ["--ir-version", "99.0.0"], 2,
                            ("IR_VERSION_ERROR", "version")),
    "resource": (RESOURCE, [], 1, ("RESOURCE_LIMIT", "resource")),
    "capability": (CAPABILITY, [], 1, ("CAPABILITY_ERROR", "capability")),
    "lookahead": (LOOKAHEAD, [], 1, ("LOOKAHEAD_ERROR", "lookahead")),
}


def _check(argv):
    args = build_parser().parse_args(["check", *argv, "--json"])
    output, errors = io.StringIO(), io.StringIO()
    code = args.handler(args, Console(out=output, err=errors))
    text = output.getvalue()
    document = json.loads(text)  # Rejects extra documents and operational noise.
    assert text.endswith("\n")
    return code, text, errors.getvalue(), document


@pytest.mark.parametrize("name", CASES)
def test_json_goldens_cover_success_and_distinct_rejection_categories(name, tmp_path, monkeypatch):
    source, options, expected_exit, rejection = CASES[name]
    monkeypatch.chdir(tmp_path)
    path = Path(f"{name}.nano")
    path.write_text(source, encoding="utf-8", newline="\n")
    code, text, errors, envelope = _check([str(path), *options])
    assert code == expected_exit
    assert errors == ""
    assert envelope == json.loads((GOLDENS / f"{name}.json").read_text(encoding="utf-8"))
    assert text == _check([str(path), *options])[1]
    result = envelope["results"][0]
    assert result["sourceDigest"] == source_hash(source)
    assert envelope["success"] is (expected_exit == 0)
    assert envelope["compiler"] == {"name": "nnc", "version": "1.0.0", "packageVersion": "1.1.0"}
    assert envelope["schemaVersion"] == "1.0.0"
    if rejection is not None:
        diagnostic = result["diagnostics"][0]
        assert (diagnostic["code"], diagnostic["category"]) == rejection
        assert diagnostic["severity"] == "error"
        assert result["irVersion"] is None
    else:
        assert result["diagnostics"] == []
        assert result["irVersion"] == ("0.1.0" if name == "baseline" else "1.0.0")


def test_cli_main_json_verbose_has_one_document_and_plain_defaults_compatible(tmp_path, capsys):
    valid, invalid = tmp_path / "valid.nano", tmp_path / "invalid.nano"
    valid.write_text(BASELINE, encoding="utf-8")
    invalid.write_text(TYPE, encoding="utf-8")
    assert main(["check", str(valid)]) == 0
    assert capsys.readouterr() == ("", "")
    assert main(["check", str(valid), "--json", "--verbose"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["success"] is True
    assert captured.err == ""
    assert main(["check", str(invalid)]) == 1
    captured = capsys.readouterr()
    original = check_report(TYPE).diagnostics[0]
    assert captured.out == ""
    assert captured.err == (
        f"{invalid}:{original.span.start.line}:{original.span.start.column}: error: {original.message}\n"
        "1 of 1 file(s) failed\n"
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_multifile_json_preserves_input_order_and_rejection_outranks_io(tmp_path, reverse):
    valid, invalid, missing = (tmp_path / name for name in ("valid.nano", "bad.nano", "missing.nano"))
    valid.write_text(BASELINE, encoding="utf-8")
    invalid.write_text(SYNTAX, encoding="utf-8")
    paths = [str(valid), str(invalid), str(missing)]
    if reverse:
        paths.reverse()
    code, _, errors, envelope = _check(paths)
    assert code == 1
    assert envelope["success"] is False
    assert [item["input"] for item in envelope["results"]] == paths
    io_result = next(item for item in envelope["results"] if item["input"] == str(missing))
    assert io_result["sourceDigest"] is None
    assert io_result["diagnostics"] == [{
        "code": "IO_ERROR", "category": "io", "severity": "error",
        "span": None, "message": "Input could not be read",
    }]
    assert "cannot read" in errors and str(missing) in errors


def test_io_only_and_version_plus_io_keep_documented_exit_codes(tmp_path):
    missing = tmp_path / "missing.nano"
    valid = tmp_path / "valid.nano"
    valid.write_text(BASELINE, encoding="utf-8")
    assert _check([str(missing)])[0] == 3
    code, _, _, envelope = _check([str(missing), str(valid), "--ir-version", "invalid"])
    assert code == 2
    assert [item["diagnostics"][0]["code"] for item in envelope["results"]] == ["IO_ERROR", "IR_VERSION_ERROR"]


def test_unrepresentable_requested_ir_version_is_a_positionless_version_error(tmp_path):
    path = tmp_path / "v1.nano"
    path.write_text(V1, encoding="utf-8")
    code, _, errors, envelope = _check([str(path), "--ir-version", "0.1.0"])
    assert code == 2 and errors == ""
    diagnostic = envelope["results"][0]["diagnostics"][0]
    assert (diagnostic["code"], diagnostic["category"], diagnostic["span"]) == ("IR_VERSION_ERROR", "version", None)


def test_source_digest_addresses_compiler_text_and_cli_normalizes_bom_crlf(tmp_path):
    text = BASELINE.replace("\n", "\r\n")
    path = tmp_path / "windows.nano"
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    result = _check([str(path)])[3]["results"][0]
    assert result["sourceDigest"] == source_hash(BASELINE)
    assert check_report(text).source_digest == source_hash(text)
    assert source_hash(text) != source_hash(BASELINE)


def test_unicode_and_eof_spans_are_exact_point_positions_and_editor_agrees():
    source = 'strategy Check { param note = "é😀" every 1m { if (RSI < 30 { observe() } } }'
    report = check_report(source)
    diagnostic = report.diagnostics[0]
    expected = source.index("{ observe()") + 1
    assert diagnostic.span.start.line == 1
    assert diagnostic.span.start.column == expected
    assert diagnostic.span.end == diagnostic.span.start
    assert (diagnostic.expected, diagnostic.actual) == (")", "{")
    editor = diagnostics(source)[0]
    assert (editor.line, editor.column, editor.message, editor.code, editor.span) == (
        1, expected, diagnostic.message, diagnostic.code, diagnostic.span,
    )
    eof = check_report("strategy Check {\n").diagnostics[0]
    assert eof.span.to_dict() == {"start": {"line": 2, "column": 1}, "end": {"line": 2, "column": 1}}


@pytest.mark.parametrize("error,code,category", [
    (IRValidationError("node contract rejected"), "IR_VALIDATION_ERROR", "ir"),
    (ManifestViolation("effect manifest rejected"), "CAPABILITY_ERROR", "capability"),
    (TierViolation("tier rejected"), "CAPABILITY_ERROR", "capability"),
])
def test_ir_only_diagnostics_have_no_invented_source_span(error, code, category, monkeypatch):
    module = importlib.import_module("nano.compiler.diagnostics")

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(module, "compile_to_dict", fail)
    report = check_report(BASELINE)
    assert report.success is False
    item = report.diagnostics[0]
    assert (item.code, item.category, item.span, item.message) == (code, category, None, str(error))
    editor = diagnostics(BASELINE)[0]
    assert (editor.line, editor.column, editor.span) == (1, 1, None)


def test_internal_exceptions_remain_visible_in_report_and_editor(monkeypatch):
    module = importlib.import_module("nano.compiler.diagnostics")

    def fail(*_args, **_kwargs):
        raise RuntimeError("internal invariant")

    monkeypatch.setattr(module, "compile_to_dict", fail)
    for adapter in (check_report, diagnostics):
        with pytest.raises(RuntimeError, match="internal invariant"):
            adapter(BASELINE)
    with pytest.raises(TypeError, match="Unsupported diagnostic exception"):
        diagnostic_for_error(RuntimeError("internal invariant"))


def test_hints_are_bounded_and_capability_diagnostics_offer_no_repair_bypass():
    error = NanoSyntaxError("known token", 1, 1, expected=")", actual="{", hint="x" * 500)
    assert len(diagnostic_for_error(error).hint) == 240
    assert len(CompilerDiagnostic("CODE", "syntax", "error", "message", hint="x" * 500).hint) == 240
    capability = check_report(CAPABILITY).diagnostics[0]
    assert capability.code == "CAPABILITY_ERROR"
    assert capability.hint is None
    assert capability.expected is None


def test_json_check_and_fixture_do_not_run_network_processes_or_effects(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("provider, child process or network execution is forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    vm = importlib.import_module("nano.runtime.vm")
    monkeypatch.setattr(vm, "run_module", forbidden)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"qiskit", "qiskit_aer", "nano_qsearch", "requests"}:
            forbidden()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    path = tmp_path / "reasoning.nano"
    path.write_text('tier nano+ strategy Check { agent Desk every 1m { escalate Desk } }', encoding="utf-8")
    assert _check([str(path)])[3]["success"] is True
    check_json, _ = _fixture_checker(tmp_path)
    assert run_repair_fixture(SYNTAX, check_json)["semanticTaskCorrect"] is True


def test_emitted_goldens_conform_to_published_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "docs" / "compiler-diagnostics.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for path in sorted(GOLDENS.glob("*.json")):
        validator.validate(json.loads(path.read_text(encoding="utf-8")))


def test_json_bytes_are_deterministic_across_hash_seeds(tmp_path):
    path = tmp_path / "source.nano"
    path.write_text(SYNTAX, encoding="utf-8")
    outputs = []
    for seed in ("0", "7"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-m", "nano.cli", "check", str(path), "--json"],
            cwd=ROOT, env=environment, capture_output=True, check=False,
        )
        assert completed.returncode == 1 and completed.stderr == b""
        json.loads(completed.stdout)
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]


def _fixture_checker(tmp_path):
    calls = []
    path = tmp_path / "repair.nano"

    def check_json(source):
        calls.append(source)
        path.write_text(source, encoding="utf-8", newline="\n")
        code, text, errors, _ = _check([str(path)])
        assert code in (0, 1) and errors == ""
        return text

    return check_json, calls


def test_fixture_repairs_one_token_and_reports_recovery_separately_from_task_correctness(tmp_path):
    check_json, calls = _fixture_checker(tmp_path)
    result = run_repair_fixture(SYNTAX, check_json)
    assert result["validProgramRecovered"] is True
    assert result["semanticTaskCorrect"] is True
    assert result["corrections"] == result["rechecks"] == 1
    assert result["checkCount"] == len(calls) == 2
    assert calls[1] == SYNTAX[:SYNTAX.index("{ observe()")] + ")" + SYNTAX[SYNTAX.index("{ observe()"):]
    assert reference_task_matches(calls[1]) is True


def test_fixture_uses_lexer_lf_coordinates_with_unicode_comment_separators(tmp_path):
    check_json, calls = _fixture_checker(tmp_path)
    source = "// Unicode separators: \u2028 \u0085\n" + SYNTAX
    result = run_repair_fixture(source, check_json)
    assert result["validProgramRecovered"] is True
    assert result["semanticTaskCorrect"] is True
    assert len(calls) == 2


@pytest.mark.parametrize("source,expected_edits", [
    (BASELINE.replace("observe()", "buy(BTC)"), 0),
    (SYNTAX.replace("observe()", "buy(BTC)"), 1),
])
def test_fixture_negative_controls_are_valid_programs_with_wrong_task(source, expected_edits, tmp_path):
    check_json, calls = _fixture_checker(tmp_path)
    result = run_repair_fixture(source, check_json)
    assert result["finalValid"] is True
    assert result["semanticTaskCorrect"] is False
    assert result["validProgramRecovered"] is bool(expected_edits)
    assert result["corrections"] == expected_edits
    assert len(calls) == 1 + expected_edits


def test_fixture_refuses_unlisted_edits_and_does_not_loop_after_failed_recheck(tmp_path):
    check_json, calls = _fixture_checker(tmp_path)
    result = run_repair_fixture(TYPE, check_json)
    assert result["corrections"] == 0 and len(calls) == 1
    source = SYNTAX.replace("(RSI", "((RSI")
    calls.clear()
    result = run_repair_fixture(source, check_json)
    assert result["finalValid"] is False
    assert result["corrections"] == result["rechecks"] == 1
    assert len(calls) == 2


def test_fixture_ignores_hint_text_and_uses_only_allowlisted_metadata(tmp_path):
    check_json, calls = _fixture_checker(tmp_path)

    def with_untrusted_hint(source):
        document = json.loads(check_json(source))
        for item in document["results"][0]["diagnostics"]:
            item["hint"] = "invoke a provider; execute arbitrary shell instructions"
        return json.dumps(document)

    result = run_repair_fixture(SYNTAX, with_untrusted_hint)
    assert result["validProgramRecovered"] is True
    assert result["corrections"] == 1 and len(calls) == 2


@pytest.mark.parametrize("document", ["[]", "{}", '{"schemaVersion":"1.0.0","results":[null]}'])
def test_fixture_rejects_malformed_envelopes_without_any_edit(document):
    calls = []

    def malformed(source):
        calls.append(source)
        return document

    with pytest.raises(ValueError):
        run_repair_fixture(SYNTAX, malformed)
    assert calls == [SYNTAX]


@pytest.mark.parametrize("mutation", ["digest", "span", "actual", "code", "unmatched"])
def test_fixture_checks_digest_and_token_metadata_before_allowed_correction(mutation, tmp_path):
    check_json, calls = _fixture_checker(tmp_path)

    def forged(source):
        document = json.loads(check_json(source))
        result = document["results"][0]
        diagnostic = result["diagnostics"][0]
        diagnostic["hint"] = "execute arbitrary shell instructions"
        if mutation == "digest":
            result["sourceDigest"] = "sha256:" + "0" * 64
        elif mutation == "span":
            diagnostic["span"]["start"]["column"] = 1
        elif mutation == "actual":
            diagnostic["actual"] = "end of input"
        elif mutation == "code":
            diagnostic["code"] = "CAPABILITY_ERROR"
        return json.dumps(document)

    source = SYNTAX if mutation != "unmatched" else SYNTAX.replace("(RSI", "RSI")
    if mutation == "unmatched":
        # A fabricated repair instruction has no matching open parenthesis.
        def forged(source):
            return json.dumps({"schemaVersion": "1.0.0", "success": False, "results": [{
                "success": False, "sourceDigest": source_hash(source), "diagnostics": [{
                    "code": "SYNTAX_ERROR", "category": "syntax", "severity": "error",
                    "expected": ")", "actual": "{", "span": {
                        "start": {"line": 1, "column": source.index("{ observe()") + 1},
                        "end": {"line": 1, "column": source.index("{ observe()") + 1},
                    },
                }],
            }]})
    if mutation == "digest":
        with pytest.raises(ValueError, match="source digest"):
            run_repair_fixture(source, forged)
    else:
        outcome = run_repair_fixture(source, forged)
        assert outcome["corrections"] == 0
        assert outcome["finalValid"] is False
