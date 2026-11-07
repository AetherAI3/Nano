"""Issue #85 compatibility: pre-record artifacts keep their exact bytes.

The pins in ``tests/fixtures/records/compat-pins.json`` were frozen from the
base revision before any record change. Every single-source example/library
program, the checked-in scalar package and the Intent/diagnostic/receipt
fixtures must keep the same canonical digest after records ship.
"""
import hashlib
import json
from pathlib import Path

import pytest

from nano.compiler import compile_module, compile_package, compile_to_dict
from nano.ir import load, load_module
from nano.ir.graph import StrategyGraph
from nano.ir.module import NanoModule
from nano.ir.schema import (
    EXECUTABLE_IR_VERSIONS, IRValidationError, NANO_IR_VERSION_1_0,
    NANO_IR_VERSION_1_1, NANO_IR_VERSION_LATEST, SUPPORTED_IR_VERSIONS,
)

ROOT = Path(__file__).resolve().parents[1]
PINS = json.loads((ROOT / "tests/fixtures/records/compat-pins.json").read_text(encoding="utf-8"))["pins"]


def _digest(value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


@pytest.mark.parametrize("key", sorted(k for k in PINS if k.startswith("compile:")))
def test_single_source_programs_keep_exact_ir_bytes(key):
    source = (ROOT / key.split(":", 1)[1]).read_text(encoding="utf-8")
    assert _digest(compile_to_dict(source)) == PINS[key]


def test_scalar_package_keeps_package_and_module_hashes():
    root = ROOT / "examples" / "agent-package"
    built = compile_package(root / "nano.json", lock_path=root / "nano.lock").to_dict()
    assert built["packageHash"] == PINS["package:packageHash"]
    assert built["module"]["moduleHash"] == PINS["package:moduleHash"]
    assert built["module"]["nanoIrVersion"] == NANO_IR_VERSION_1_0
    assert "records" not in built["module"]


@pytest.mark.parametrize("key", sorted(k for k in PINS if k.startswith("json:")))
def test_intent_diagnostic_and_receipt_fixtures_unchanged(key):
    document = json.loads((ROOT / key.split(":", 1)[1]).read_text(encoding="utf-8"))
    assert _digest(document) == PINS[key]


def test_version_constants_keep_their_meaning():
    assert SUPPORTED_IR_VERSIONS == ("0.1.0", "1.0.0")
    assert NANO_IR_VERSION_LATEST == "1.0.0"
    assert NANO_IR_VERSION_1_1 == "1.1.0"
    assert EXECUTABLE_IR_VERSIONS == ("1.0.0", "1.1.0")


def test_v1_modules_still_round_trip_without_a_records_key():
    module = compile_module("strategy A { input x: int every 1m { if x > 1 { execute() } } }")
    document = module.to_dict()
    assert document["nanoIrVersion"] == "1.0.0" and "records" not in document
    assert NanoModule.from_dict(document).to_dict() == document
    assert load_module(document).content_hash() == module.content_hash()


def test_unknown_future_revision_is_rejected_cleanly():
    document = compile_module("strategy A { every 1m { observe() } }").to_dict()
    for version in ("1.2.0", "2.0.0", "1.1", None):
        with pytest.raises(IRValidationError, match="unsupported"):
            NanoModule.from_dict({**document, "nanoIrVersion": version})
        with pytest.raises(IRValidationError, match="unsupported"):
            load({**document, "nanoIrVersion": version})
    with pytest.raises(IRValidationError):
        StrategyGraph.from_dict({**document, "nanoIrVersion": "1.1.0"})
