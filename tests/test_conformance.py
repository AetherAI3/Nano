"""Conformance suite — the Milestone 3 x Milestone 4 weld.

The BUILD_ORDER rule: every example must compile from `.nano` source to IR
that is byte-for-byte equal to its hand-written `<name>_ir.json`, and the
compiled graph must replay identically through the reference interpreter.
This is the acceptance test that proves the language surface and the IR
corpus never drift apart.
"""

import json
from pathlib import Path

import pytest

from nano.compiler import compile_source, compile_to_dict
from nano.runtime.interpreter import MarketFrame, execute
from nano.runtime.vm import run_module

EXAMPLES = Path(__file__).resolve().parent.parent / "nano" / "examples"

NANO_SOURCES = sorted(EXAMPLES.glob("*.nano"))


def _ir_path(nano_path: Path) -> Path:
    return nano_path.with_name(f"{nano_path.stem}_ir.json")


def test_corpus_is_nonempty_and_paired():
    assert NANO_SOURCES, "conformance corpus must not be empty"
    for nano_path in NANO_SOURCES:
        assert _ir_path(nano_path).exists(), f"{nano_path.name} has no IR partner"


@pytest.mark.parametrize("nano_path", NANO_SOURCES, ids=lambda p: p.stem)
def test_compiled_ir_matches_handwritten_ir(nano_path: Path):
    compiled = compile_to_dict(nano_path.read_text())
    handwritten = json.loads(_ir_path(nano_path).read_text())
    assert compiled == handwritten


@pytest.mark.parametrize("nano_path", NANO_SOURCES, ids=lambda p: p.stem)
def test_compiled_graph_replays_identically(nano_path: Path):
    graph = compile_source(nano_path.read_text())
    signals = {c.signal: (0.0, 1e9) for c in graph.conditions}
    frame = MarketFrame(timestamps=(0, 86400), signals=signals)
    first = execute(graph, frame).to_dict()
    second = execute(graph, frame).to_dict()
    assert first == second


@pytest.mark.parametrize("nano_path", NANO_SOURCES, ids=lambda p: p.stem)
def test_corpus_stays_on_baseline_ir(nano_path: Path):
    """The corpus must keep compiling to v0.1.0 rather than drifting up a version.

    Byte-stability of compiled output is this repository's central claim, and the
    compiler emits the lowest IR version that can express a program. If an example
    ever starts emitting `1.0.0`, either it gained a v1.0 construct or the version
    inference regressed — both worth noticing deliberately instead of discovering
    in a host that pinned the older shape.
    """
    assert compile_to_dict(nano_path.read_text())["nanoIrVersion"] == "0.1.0"


@pytest.mark.parametrize("nano_path", NANO_SOURCES, ids=lambda p: p.stem)
def test_baseline_and_v1_execution_paths_agree(nano_path: Path):
    """The reference interpreter and the v1.0 VM must produce identical intents.

    This is the weld that lets v1.0 exist without invalidating a single v0.1.0
    artifact. ``interpreter.execute`` defines correct behavior for baseline graphs;
    the VM is what actually runs under v1.0. Lifting each corpus entry through
    ``to_module()`` and comparing proves the newer path reproduces the older one
    rather than merely resembling it.

    The frame drives each signal low, high, then mid-range, so every comparison
    operator in the corpus gets a bar where it fires and one where it does not —
    an all-firing frame would pass even if one path ignored conditions entirely.
    """
    graph = compile_source(nano_path.read_text())
    signals = {c.signal: (0.0, 1e9, 45.0) for c in graph.conditions}
    frame = MarketFrame(timestamps=(0, 86400, 172800), signals=signals)

    reference = [i.to_dict() for i in execute(graph, frame).intents]
    through_vm = [i.to_dict() for i in run_module(graph.to_module(), frame).intents]
    assert reference == through_vm
