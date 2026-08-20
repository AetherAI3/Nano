"""Generated adversarial properties for the Nano compiler and IR boundary.

The deterministic campaign is intentionally small in the ordinary pytest run.
``scripts/compiler_fuzz.py`` scales the same generators out across processes and
``PYTHONHASHSEED`` values while preserving every failing source as a reproducer.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from nano.compiler import (
    NanoTypeError,
    check_source,
    compile_module,
    compile_to_dict,
    parse,
)
from nano.fuzzing import (
    audit_catalog_corpus,
    audit_receipt_boundaries,
    audit_risk_boundaries,
    canonical_json,
    execution_summary,
    generate_equivalent_programs,
    generate_invalid_programs,
    generate_valid_programs,
    load_document,
    run_campaign,
)
from nano.ir.module import NanoModule
from nano.ir.schema import IRValidationError, ManifestViolation
from nano.runtime.vm import run_module


def test_catalog_corpus_exercises_contribution_controls_and_both_ir_versions():
    audit = audit_catalog_corpus()

    assert audit.strategy_count == 55
    assert audit.baseline_ir_count == 41
    assert audit.v1_ir_count == 14
    assert audit.watchdog_count == 8
    assert audit.control_cases == 55
    assert audit.defects == ()


def test_receipt_fuzz_covers_replay_nonfinite_and_canonical_limits():
    first = audit_receipt_boundaries(seed=0x5ECE17, cases=24)
    second = audit_receipt_boundaries(seed=0x5ECE17, cases=24)

    assert first.canonical_cases >= 24
    assert first.replay_cases >= 1
    assert first.nonfinite_cases == 3
    assert first.oversized_cases == 6
    assert first.semantic_digest == second.semantic_digest
    assert first.defects == second.defects
    assert first.defects == ()


def test_risk_fuzz_covers_actuating_equality_and_malformed_capacity():
    audit = audit_risk_boundaries(seed=0xA15C, cases=24)

    assert audit.actuating_cases >= 24
    assert audit.equality_cases >= 4
    assert audit.malformed_capacity_cases >= 8
    assert audit.defects == ()

    generated = [
        case
        for case in generate_valid_programs(seed=0xA15C, count=28)
        if case.family == "risk"
    ]
    assert generated
    for case in generated:
        module = compile_module(case.source)
        frame = case.frame()
        assert {"risk.daily_loss", "risk.orders_today"} <= set(frame.signals)
        result = run_module(module, frame)
        assert any(entry.event == "risk.armed" for entry in result.log)
        assert result.intents
        assert {intent.action for intent in result.intents} <= {"BUY", "SELL"}


def test_generated_valid_programs_survive_the_full_frontend_and_ir_round_trip():
    for case in generate_valid_programs(seed=0xA37E, count=36):
        # These are grammar/AST productions, not arbitrary byte strings.
        parsed = case.parse()
        checked = check_source(case.source)
        assert parsed.name == checked.strategy.name

        first = case.compile()
        second = case.compile()
        assert canonical_json(first) == canonical_json(second)

        loaded = load_document(first)
        assert canonical_json(loaded.to_dict()) == canonical_json(first)

        # All source programs, including baseline-shaped ones, must also lower
        # through the canonical v1 executable module and its loader.
        module = compile_module(case.source)
        assert NanoModule.from_dict(module.to_dict()).to_dict() == module.to_dict()


def test_one_mutation_invalid_programs_are_rejected_at_the_mutated_construct():
    cases = generate_invalid_programs(seed=0xBAD5EED, count=42)
    assert {case.family for case in cases} >= {
        "confidence",
        "dynamic_lookahead",
        "interval",
        "negative_lookahead",
        "period",
        "tier_capability",
        "type_operand_order",
        "unknown_action",
    }
    assert len({case.source for case in cases}) == len(cases)
    assert len({case.base_source for case in cases}) == len(cases)

    for case in cases:
        parse(case.base_source)
        check_source(case.base_source)
        compile_to_dict(case.base_source)
        assert case.source != case.base_source
        with pytest.raises(case.expected_error) as excinfo:
            case.compile()
        assert type(excinfo.value) is case.expected_error
        assert (excinfo.value.line, excinfo.value.column) == case.expected_location


def test_type_errors_do_not_become_valid_when_operand_order_changes():
    ordered = [
        case
        for case in generate_invalid_programs(seed=9, count=32)
        if case.family == "type_operand_order"
    ]
    assert len(ordered) >= 2
    assert any('("bad' in case.source and '" + ' in case.source for case in ordered)
    assert any(' + "bad' in case.source for case in ordered)
    for case in ordered:
        with pytest.raises(NanoTypeError):
            check_source(case.source)


def test_semantically_equivalent_source_variants_execute_identically():
    cases = generate_equivalent_programs(seed=0xE011, count=18)
    assert len({case.sources for case in cases}) == len(cases)
    for case in cases:
        summaries = [execution_summary(source, case.frame()) for source in case.sources]
        assert summaries[1:] == summaries[:-1]


def test_generated_baseline_programs_match_reference_interpreter_and_vm():
    baseline_cases = [
        case for case in generate_valid_programs(seed=0xD1FF, count=30) if case.baseline
    ]
    assert baseline_cases
    for case in baseline_cases:
        comparison = case.compare_baseline_runtimes()
        assert comparison.reference == comparison.lifted_vm == comparison.vm


def test_hostile_ir_cannot_reach_runtime_through_lookahead_or_manifest_bypasses():
    source = (
        "strategy Guarded {\n"
        "    input close: series<float>\n"
        "    every 1m {\n"
        "        if close > close[1] {\n"
        "            buy(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    module = compile_module(source)
    frame = generate_valid_programs(seed=1, count=1)[0].frame(length=3)

    index_node = next(node for node in module.nodes if node.op == "series.index")
    negative = replace(index_node, attrs={**index_node.attrs, "offset": -1})
    negative_module = replace(
        module,
        nodes=tuple(
            negative if node.id == negative.id else node for node in module.nodes
        ),
    )
    with pytest.raises(IRValidationError, match="non-negative"):
        run_module(negative_module, frame)

    ungranted = replace(module, effects=("log.append",))
    with pytest.raises(ManifestViolation, match="needs effect"):
        run_module(ungranted, frame)

    malformed = module.to_dict(include_hash=False)
    malformed["effects"] = ["log.append", "shell.exec"]
    with pytest.raises(IRValidationError, match="Unknown effects"):
        NanoModule.from_dict(malformed)


def test_campaign_is_reproducible_and_preserves_every_discovered_defect():
    first = run_campaign(seed=20260819, cases=24)
    second = run_campaign(seed=20260819, cases=24)
    assert first.corpus_digest == second.corpus_digest
    assert first.semantic_digest == second.semantic_digest
    assert first.coverage == second.coverage
    assert first.defects == second.defects
    assert len({defect.id for defect in first.defects}) == len(first.defects)


def test_hash_seed_driver_always_writes_a_deterministic_loopstate(tmp_path):
    root = Path(__file__).resolve().parent.parent

    def run(output: Path) -> tuple[subprocess.CompletedProcess[str], bytes]:
        completed = subprocess.run(
            [
                sys.executable,
                str(root / "scripts" / "compiler_fuzz.py"),
                "--cases",
                "8",
                "--hash-seeds",
                "0,7",
                "--refs",
                "HEAD",
                "--output",
                str(output),
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
        diagnostics = f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        assert completed.returncode in (0, 1), diagnostics
        assert output.is_file(), diagnostics
        return completed, output.read_bytes()

    _, first = run(tmp_path / "first.json")
    _, second = run(tmp_path / "second.json")
    assert first == second
    state = json.loads(first)
    assert "generatedAt" not in state
    assert state["status"] in ("pass", "defects-found")
    assert state["summary"] == {
        "requestedTargets": 1,
        "targets": 1,
        "workers": 2,
        "defects": len(state["defects"]),
    }
    assert state["targets"][0]["stableAcrossHashSeeds"] is True
    worker_coverages = [
        worker["campaign"]["coverage"] for worker in state["targets"][0]["workers"]
    ]
    assert worker_coverages[1:] == worker_coverages[:-1]


def test_hash_seed_driver_is_fetchless_when_origin_main_is_absent(tmp_path):
    root = Path(__file__).resolve().parent.parent
    isolated = tmp_path / "fresh" / "Nano"
    shutil.copytree(
        root,
        isolated,
        ignore=shutil.ignore_patterns(
            ".git", ".pytest_cache", "__pycache__", "*.pyc", "build", "dist"
        ),
    )
    for command in (
        ("init",),
        ("config", "user.name", "G5 Test"),
        ("config", "user.email", "g5@example.invalid"),
        ("add", "."),
        ("commit", "-m", "fresh detached fixture"),
    ):
        subprocess.run(
            ["git", "-C", str(isolated), *command],
            check=True,
            capture_output=True,
            text=True,
        )
    missing_main = subprocess.run(
        ["git", "-C", str(isolated), "rev-parse", "--verify", "origin/main"],
        capture_output=True,
        text=True,
    )
    assert missing_main.returncode != 0

    # Keep one harness file dirty so inserting the loopstate into git's sorted
    # status cannot shift/renormalize the leading status column of another path.
    dirty_script = isolated / "scripts" / "compiler_fuzz.py"
    dirty_script.write_text(
        dirty_script.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
        newline="\n",
    )
    output = isolated / "_loopstate" / "g5-compiler-fuzz.json"
    payloads = []
    for _ in range(2):
        completed = subprocess.run(
            [
                sys.executable,
                str(isolated / "scripts" / "compiler_fuzz.py"),
                "--cases",
                "4",
                "--hash-seeds",
                "0,7",
                "--refs",
                "HEAD",
            ],
            cwd=isolated,
            capture_output=True,
            text=True,
        )
        diagnostics = f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        assert completed.returncode in (0, 1), diagnostics
        assert output.is_file(), diagnostics
        payloads.append(output.read_bytes())
    assert payloads[0] == payloads[1]
    state = json.loads(payloads[0])
    assert state["repository"]["mainMergeBase"] is None
    assert state["repository"]["mainMergeBaseStatus"] == (
        "unavailable: origin/main is absent from local checkout"
    )
    assert state["repository"]["status"] == [" M scripts/compiler_fuzz.py"]
    assert state["summary"] == {
        "requestedTargets": 1,
        "targets": 1,
        "workers": 2,
        "defects": 0,
    }


def test_hash_seed_driver_writes_deterministic_artifact_on_parent_failure(
    tmp_path, monkeypatch
):
    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "compiler_fuzz.py"
    spec = importlib.util.spec_from_file_location("nano_g5_driver_test", script)
    assert spec is not None and spec.loader is not None
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)

    def fail_parent(_args):
        raise RuntimeError("injected parent orchestration failure")

    monkeypatch.setattr(driver, "_parent", fail_parent)
    payloads = []
    for name in ("failure-first.json", "failure-second.json"):
        output = tmp_path / name
        assert (
            driver.main(["--cases", "4", "--refs", "HEAD", "--output", str(output)])
            == 1
        )
        assert output.is_file()
        payloads.append(output.read_bytes())

    assert payloads[0] == payloads[1]
    state = json.loads(payloads[0])
    assert state["status"] == "defects-found"
    assert state["summary"]["defects"] == 1
    assert state["defects"][0]["id"] == "G5-parent-orchestration"
    assert state["defects"][0]["observed"] == (
        "RuntimeError: injected parent orchestration failure"
    )
