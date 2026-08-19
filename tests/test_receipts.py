"""Run receipts: canonical bytes, executable identity, and drift detection.

These tests are about *bytes*, not about dictionaries. That distinction is the
whole point of the module under test: Python dictionary equality says
``{"approved": True} == {"approved": 1}`` and ``{"x": 0.0} == {"x": -0.0}``,
so every "bit-identical replay" check written as ``a.to_dict() != b.to_dict()``
was passing runs that a byte comparison rejects. Where a test could be written
either way, it is written against ``canonical_bytes``.
"""

import json
import subprocess
import sys
import threading
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest

from nano import __version__ as NANO_VERSION
from nano.bridge import Backtester, Decision, ReplayDivergence as BridgeDivergence
from nano.compiler import compile_module, compile_source
from nano.ir.graph import StrategyGraph
from nano.ir.module import NanoModule
from nano.ir.schema import NANO_IR_VERSION_1_0
from nano.runtime.interpreter import MarketFrame, execute
from nano.runtime.receipt import (
    RECEIPT_VERSION,
    ReceiptError,
    ReplayDivergence,
    build_receipt,
    canonical_bytes,
    differences,
    digest_of,
    frame_digest,
    receipt_digest,
    verify_run,
)
from nano.runtime.vm import run_module

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"


# -- fixtures -----------------------------------------------------------------


def _frame(**signals):
    length = len(next(iter(signals.values())))
    return MarketFrame(timestamps=tuple(i * 60 for i in range(length)), signals=signals)


ZSCORE_SOURCE = (
    "strategy Drift {\n"
    "    input close: series<float>\n"
    "    let z = ZSCORE(close, 3)\n"
    "    every 1m {\n"
    "        if z > 0 {\n"
    "            buy(BTC, 0.7)\n"
    "        } else {\n"
    "            observe()\n"
    "        }\n"
    "    }\n"
    "}\n"
)

PARAM_SOURCE = (
    "strategy Tuned {\n"
    "    param window: int = 3\n"
    "    input close: series<float>\n"
    "    let fast = SMA(close, window)\n"
    "    every 1m {\n"
    "        if close > fast {\n"
    "            buy(BTC)\n"
    "        }\n"
    "    }\n"
    "}\n"
)

REASONING_SOURCE = (
    "tier nano+\n"
    "strategy Ask {\n"
    "    agent Desk { role research }\n"
    "    input close: series<float>\n"
    "    signature Judge {\n"
    "        input price: float\n"
    "        output score: float\n"
    "    }\n"
    "    let verdict = infer(Judge, close)\n"
    "    every 1m {\n"
    "        if verdict.score > 0.5 {\n"
    "            buy(BTC)\n"
    "        } else {\n"
    "            escalate Desk\n"
    "        }\n"
    "    }\n"
    "}\n"
)


class Recorded:
    """A deterministic reasoning provider: a replayed transcript, keyed by bar."""

    def __init__(self, scores):
        self.scores = dict(scores)

    def infer(self, signature, inputs, *, timestamp):
        return {"score": self.scores[timestamp]}


class Drifting:
    """Deliberately nondeterministic: the answer depends on a hidden call count."""

    def __init__(self):
        self.calls = 0

    def infer(self, signature, inputs, *, timestamp):
        self.calls += 1
        # Alternates above/below the rule's 0.5 threshold on an odd period, so
        # the second pass over an odd number of bars lands out of phase.
        return {"score": float(self.calls % 2)}


def _drift_run():
    module = compile_module(ZSCORE_SOURCE)
    frame = _frame(close=(1.0, 3.0, 2.0, 8.0, 5.0))
    return module, frame


# -- the canonical encoder ----------------------------------------------------


def test_canonical_bytes_are_sorted_compact_and_ascii():
    document = {"b": 1, "a": {"z": "é", "y": [1, 2]}}
    assert canonical_bytes(document) == b'{"a":{"y":[1,2],"z":"\\u00e9"},"b":1}'


def test_keys_sort_by_code_point_not_by_escaped_byte():
    """Pins which of two plausible sort rules is the real one.

    `json.dumps(sort_keys=True)` sorts the *unescaped* strings by Unicode code
    point. A rule of "byte-wise over the escaped form" — which this document
    claimed for one revision — would put `é` first, because its escape begins
    with a backslash (0x5C) and `a` is 0x61. Reachable: `frame_digest` uses
    signal names as object keys, and `host` keys are caller-supplied.

    This is also where Nano departs from RFC 8785 / JCS, which sorts by UTF-16
    code unit.
    """
    assert canonical_bytes({"z": 1, "é": 2, "a": 3}) == b'{"a":3,"z":1,"\\u00e9":2}'


def test_canonical_bytes_carry_no_line_terminator():
    # The canonical form is the digested text and nothing else. Framing it as a
    # file or a JSONL record appends exactly one LF, which is not digested.
    blob = canonical_bytes({"a": 1})
    assert b"\n" not in blob and b"\r" not in blob


def test_construction_order_does_not_change_the_bytes():
    forward = {"alpha": 1, "beta": {"x": 1, "y": 2}}
    backward = {"beta": {"y": 2, "x": 1}, "alpha": 1}
    assert forward == backward
    assert canonical_bytes(forward) == canonical_bytes(backward)


def test_array_order_is_preserved_because_it_is_meaning():
    assert canonical_bytes({"log": [2, 1]}) != canonical_bytes({"log": [1, 2]})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_refused(value):
    # JSON has no representation for these; `json.dumps` would happily emit the
    # bare tokens NaN/Infinity, which no conforming parser accepts.
    with pytest.raises(ReceiptError, match="non-finite"):
        canonical_bytes({"confidence": value})


def test_float_formatting_is_pinned():
    assert canonical_bytes({"v": 0.1 + 0.2}) == b'{"v":0.30000000000000004}'
    assert canonical_bytes({"v": 1e308}) == b'{"v":1e+308}'
    assert canonical_bytes({"v": 5e-324}) == b'{"v":5e-324}'
    # An integral float keeps its fractional marker, so `3.0` and `3` do not
    # collide in the bytes even though Python says they are equal.
    assert canonical_bytes({"v": 3.0}) == b'{"v":3.0}'
    assert canonical_bytes({"v": 3}) == b'{"v":3}'


def test_negative_zero_is_distinguishable_in_bytes_but_not_in_dicts():
    assert {"v": -0.0} == {"v": 0.0}
    assert canonical_bytes({"v": -0.0}) != canonical_bytes({"v": 0.0})


def test_booleans_and_integers_are_distinguishable_in_bytes_but_not_in_dicts():
    # This is the exact hole that made `a.to_dict() != b.to_dict()` a weaker
    # check than it claimed to be.
    assert {"approved": True} == {"approved": 1}
    assert canonical_bytes({"approved": True}) != canonical_bytes({"approved": 1})


def test_non_string_object_keys_are_refused():
    with pytest.raises(ReceiptError, match="keys must be strings"):
        canonical_bytes({1: "a"})


def test_unencodable_values_are_refused_by_path():
    with pytest.raises(ReceiptError, match=r"/run/tags"):
        canonical_bytes({"run": {"tags": {"a", "b"}}})


def test_digest_is_over_the_canonical_bytes():
    document = {"b": 2, "a": 1}
    import hashlib

    expected = "sha256:" + hashlib.sha256(canonical_bytes(document)).hexdigest()
    assert receipt_digest(document) == expected


# -- frame hashing ------------------------------------------------------------


def test_frame_hash_ignores_signal_insertion_order():
    a = MarketFrame(timestamps=(0, 60), signals={"close": (1.0, 2.0), "volume": (3.0, 4.0)})
    b = MarketFrame(timestamps=(0, 60), signals={"volume": (3.0, 4.0), "close": (1.0, 2.0)})
    assert frame_digest(a) == frame_digest(b)


def test_frame_hash_changes_when_a_bar_changes():
    a = MarketFrame(timestamps=(0, 60), signals={"close": (1.0, 2.0)})
    b = MarketFrame(timestamps=(0, 60), signals={"close": (1.0, 2.5)})
    assert frame_digest(a) != frame_digest(b)


# -- receipt shape ------------------------------------------------------------


def test_receipt_records_its_own_version():
    module, frame = _drift_run()
    receipt = build_receipt(module, frame, run_module(module, frame))
    assert receipt["receiptVersion"] == RECEIPT_VERSION == 1


def test_receipt_carries_executable_identity():
    module, frame = _drift_run()
    identity = build_receipt(module, frame, run_module(module, frame))["identity"]
    assert identity["nanoVersion"] == NANO_VERSION
    assert identity["irVersion"] == NANO_IR_VERSION_1_0
    assert identity["moduleHash"] == module.content_hash()
    assert identity["compiler"] == {"name": "nnc", "version": "1.0.0"}
    assert identity["tier"] == module.tier
    assert identity["effects"] == list(module.effects)
    assert identity["warmupDeclared"] == module.warmup


def test_provenance_is_kept_out_of_executable_identity():
    # `sourceHash` says where a module came from, not what it does, and nothing
    # about it is authenticated. Filing it under identity would imply otherwise.
    module, frame = _drift_run()
    receipt = build_receipt(module, frame, run_module(module, frame))
    assert receipt["provenance"]["sourceHash"] == module.source_hash
    assert "sourceHash" not in receipt["identity"]
    assert "moduleHash" not in receipt["provenance"]


def test_a_module_without_provenance_has_no_provenance_section():
    module, frame = _drift_run()
    bare = compile_module(ZSCORE_SOURCE).to_dict(include_hash=False)
    bare.pop("provenance", None)
    stripped = type(module).from_dict(bare)
    receipt = build_receipt(stripped, frame, run_module(stripped, frame))
    assert "provenance" not in receipt


def _nulls(value, path=""):
    if value is None:
        return [path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _nulls(v, f"{path}/{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _nulls(v, f"{path}/{i}")]
    return []


def test_absent_values_are_omitted_rather_than_nulled():
    module = compile_module(ZSCORE_SOURCE)
    empty = MarketFrame(timestamps=(), signals={"close": ()})
    receipt = build_receipt(module, empty, run_module(module, empty))
    assert receipt["inputs"]["bars"] == 0
    assert "firstTimestamp" not in receipt["inputs"]
    assert "lastTimestamp" not in receipt["inputs"]
    assert "params" not in receipt["identity"]  # this module declares none
    assert _nulls(receipt) == []


def test_empty_result_sets_are_present_as_empty_arrays():
    module = compile_module(ZSCORE_SOURCE)
    empty = MarketFrame(timestamps=(), signals={"close": ()})
    run = build_receipt(module, empty, run_module(module, empty))["run"]
    assert run["intents"] == []
    assert run["escalations"] == []
    assert run["warmupBarsSkipped"] == 0
    # The log is never empty -- loading the module is itself an event -- but the
    # two result collections are, and they are present rather than omitted.
    assert [e["event"] for e in run["log"]] == ["module.loaded"]


def test_declared_params_travel_with_the_module_not_the_inputs():
    module = compile_module(PARAM_SOURCE)
    frame = _frame(close=(1.0, 2.0, 3.0, 4.0))
    receipt = build_receipt(module, frame, run_module(module, frame))
    assert receipt["identity"]["params"] == [
        {"name": "window", "type": "int", "value": 3}
    ]
    assert "params" not in receipt["inputs"]


def test_warmup_bars_are_reported_as_skipped_not_as_no_signal():
    module, frame = _drift_run()
    run = build_receipt(module, frame, run_module(module, frame))["run"]
    assert run["warmupBarsSkipped"] == 2
    assert [e["event"] for e in run["log"]].count("condition.unwarmed") == 2


def test_multiple_intents_keep_their_emission_order():
    module = compile_module(PARAM_SOURCE)
    frame = _frame(close=(1.0, 2.0, 9.0, 9.5, 10.0))
    run = build_receipt(module, frame, run_module(module, frame))["run"]
    stamps = [i["timestamp"] for i in run["intents"]]
    assert len(stamps) > 1
    assert stamps == sorted(stamps)


def test_escalations_are_recorded_alongside_intents():
    module = compile_module(REASONING_SOURCE)
    frame = _frame(close=(1.0, 2.0))
    provider = Recorded({0: 0.9, 60: 0.1})
    run = build_receipt(module, frame, run_module(module, frame, provider=provider))["run"]
    assert [i["timestamp"] for i in run["intents"]] == [0]
    assert [e["escalate"] for e in run["escalations"]] == ["Desk"]


def test_a_module_that_needs_reasoning_says_so():
    plain = compile_module(ZSCORE_SOURCE)
    asking = compile_module(REASONING_SOURCE)
    frame = _frame(close=(1.0, 2.0))
    assert (
        build_receipt(plain, frame, run_module(plain, frame))["identity"][
            "reasoningRequired"
        ]
        is False
    )
    provider = Recorded({0: 0.9, 60: 0.1})
    assert (
        build_receipt(asking, frame, run_module(asking, frame, provider=provider))[
            "identity"
        ]["reasoningRequired"]
        is True
    )


# -- determinism --------------------------------------------------------------


def test_the_same_run_serialises_to_the_same_bytes():
    module, frame = _drift_run()
    first = build_receipt(module, frame, run_module(module, frame))
    second = build_receipt(module, frame, run_module(module, frame))
    assert canonical_bytes(first) == canonical_bytes(second)


def test_a_receipt_embeds_no_ambient_clock_entropy_or_environment():
    """The receipt path may not reach an ambient source at all.

    Asserting the bytes are equal twice cannot catch a clock coarse enough to
    read the same value on both calls, so this checks the module's namespace
    instead: nothing that can produce a wall clock, entropy, or environment is
    importable from inside it.
    """
    import nano.runtime.receipt as receipt_module

    banned = {"time", "datetime", "random", "os", "secrets", "uuid", "socket"}
    assert banned.isdisjoint(vars(receipt_module))


def test_no_timestamp_field_is_invented_outside_the_data():
    """Every timestamp in a receipt comes from the frame, never from a clock."""
    module, frame = _drift_run()
    receipt = build_receipt(module, frame, run_module(module, frame))
    stamps = {receipt["inputs"]["firstTimestamp"], receipt["inputs"]["lastTimestamp"]}
    stamps |= {e["timestamp"] for e in receipt["run"]["log"]}
    stamps |= {i["timestamp"] for i in receipt["run"]["intents"]}
    assert stamps <= set(frame.timestamps)
    assert "host" not in receipt


def test_host_supplied_context_is_the_only_place_wall_clock_may_live():
    module, frame = _drift_run()
    result = run_module(module, frame)
    plain = build_receipt(module, frame, result)
    stamped = build_receipt(module, frame, result, host={"recordedAt": 1755561600})
    assert stamped["host"] == {"recordedAt": 1755561600}
    assert stamped["identity"] == plain["identity"]
    assert stamped["run"] == plain["run"]
    # Host context is inside the digest: it is part of the record, just not part
    # of the deterministic core.
    assert canonical_bytes(stamped) != canonical_bytes(plain)


def test_verify_run_returns_the_canonical_bytes():
    module, frame = _drift_run()
    blob = verify_run(module, frame)
    assert blob == canonical_bytes(build_receipt(module, frame, run_module(module, frame)))


def test_verify_run_rejects_a_nondeterministic_provider():
    module = compile_module(REASONING_SOURCE)
    frame = _frame(close=(1.0, 2.0, 3.0))
    with pytest.raises(ReplayDivergence, match="Ask"):
        verify_run(module, frame, provider=Drifting())


# -- drift detection ----------------------------------------------------------


def test_mutating_the_ir_changes_the_receipt():
    module, frame = _drift_run()
    before = build_receipt(module, frame, run_module(module, frame))

    document = module.to_dict(include_hash=False)
    for node in document["nodes"]:
        if node["op"] == "intent.emit":
            node["attrs"]["asset"] = "ETH"
    mutated = type(module).from_dict(document)
    after = build_receipt(mutated, frame, run_module(mutated, frame))

    assert receipt_digest(before) != receipt_digest(after)
    assert "/identity/moduleHash" in differences(before, after)


def test_mutating_an_input_changes_the_receipt():
    module, frame = _drift_run()
    before = build_receipt(module, frame, run_module(module, frame))

    nudged = MarketFrame(
        timestamps=frame.timestamps, signals={"close": (1.0, 3.0, 2.0, 8.0, 5.5)}
    )
    after = build_receipt(module, nudged, run_module(module, nudged))

    assert receipt_digest(before) != receipt_digest(after)
    assert "/inputs/frameHash" in differences(before, after)
    assert "/identity/moduleHash" not in differences(before, after)


def test_mutating_a_param_changes_the_receipt():
    module = compile_module(PARAM_SOURCE)
    frame = _frame(close=(1.0, 2.0, 3.0, 4.0, 5.0))
    before = build_receipt(module, frame, run_module(module, frame))

    document = module.to_dict(include_hash=False)
    document["params"][0]["value"] = 2
    for node in document["nodes"]:
        if node["op"] == "indicator":
            node["attrs"]["periods"] = [2]
    retuned = type(module).from_dict(document)
    after = build_receipt(retuned, frame, run_module(retuned, frame))

    assert receipt_digest(before) != receipt_digest(after)
    drift = differences(before, after)
    assert "/identity/moduleHash" in drift
    assert "/identity/params/0/value" in drift


def test_differences_reports_nothing_for_two_identical_receipts():
    module, frame = _drift_run()
    assert differences(
        build_receipt(module, frame, run_module(module, frame)),
        build_receipt(module, frame, run_module(module, frame)),
    ) == ()


def test_differences_sees_a_type_change_dict_equality_would_miss():
    assert differences({"approved": True}, {"approved": 1}) == ("/approved",)
    assert differences({"v": 0.0}, {"v": -0.0}) == ("/v",)


def test_differences_names_added_and_removed_members():
    assert differences({"a": 1}, {"a": 1, "b": 2}) == ("/b",)
    assert differences({"log": [1, 2]}, {"log": [1]}) == ("/log[length]",)


# -- both IR versions ---------------------------------------------------------


BASELINE_SOURCE = (
    "strategy Baseline {\n"
    "    every 1m {\n"
    "        if RSI > 2 {\n"
    "            buy(BTC)\n"
    "        }\n"
    "    }\n"
    "}\n"
)


def _baseline_graph():
    graph = compile_source(BASELINE_SOURCE)
    assert isinstance(graph, StrategyGraph)  # still v0.1.0, not lifted by the compiler
    return graph


def test_a_baseline_graph_and_its_lifted_module_agree_on_the_run():
    graph = _baseline_graph()
    module = graph.to_module()
    frame = _frame(RSI=(1.0, 3.0, 4.0))

    through_interpreter = build_receipt(module, frame, execute(graph, frame))
    through_vm = build_receipt(module, frame, run_module(module, frame))

    assert through_interpreter["identity"] == through_vm["identity"]
    assert through_interpreter["run"]["intents"] == through_vm["run"]["intents"]
    assert through_interpreter["run"]["escalations"] == []


def test_a_baseline_receipt_records_the_baseline_ir_version_of_its_lift():
    graph = _baseline_graph()
    module = graph.to_module()
    frame = _frame(RSI=(1.0, 3.0))
    receipt = build_receipt(module, frame, run_module(module, frame))
    # A lifted baseline graph executes as a v1.0 module, and the receipt says so
    # rather than claiming the document version the host handed in.
    assert receipt["identity"]["irVersion"] == NANO_IR_VERSION_1_0


# -- the backtester's replay check -------------------------------------------


class _TypeDriftingGate:
    """Approves everything, but spells `approved` differently the second time.

    Every *value* in the two reports is equal — the approval counts match, the
    reasons match, the intents match. Only the JSON type of one field changes,
    from `true` to `1`. `True == 1` in Python, so a dictionary comparison calls
    these two backtests identical; they do not serialise to the same bytes, and
    the artifact a host stores is bytes.

    This is not a contrived shape. A gate that reads its answer out of JSON, a
    database driver, or a numpy scalar returns exactly this drift.
    """

    def __init__(self):
        self.runs = 0

    def decide(self, intent, *, frame):
        self.runs += 1
        approved = True if self.runs < 2 else 1
        return Decision(intent=intent, approved=approved, reason="ok")


def test_backtest_replay_check_compares_bytes_not_dict_equality():
    graph = _baseline_graph()
    frames = [_frame(RSI=(1.0, 3.0, 4.0))]

    # First, pin the premise: the two reports really are dict-equal. Without this
    # the test could pass because of an unrelated difference, and would no longer
    # be evidence that byte comparison is what caught the drift.
    gate = _TypeDriftingGate()
    backtester = Backtester(gate)
    first, second = backtester.run(graph, frames).to_dict(), backtester.run(graph, frames).to_dict()
    assert first == second
    assert canonical_bytes(first) != canonical_bytes(second)

    with pytest.raises(BridgeDivergence, match="approved"):
        Backtester(_TypeDriftingGate()).verify_replay(graph, frames)


def test_backtest_replay_check_still_passes_a_deterministic_gate():
    class Approve:
        def decide(self, intent, *, frame):
            return Decision(intent=intent, approved=True, reason="ok")

    graph = _baseline_graph()
    assert Backtester(Approve()).verify_replay(graph, [_frame(RSI=(1.0, 3.0, 4.0))])


# -- the CLI surface ----------------------------------------------------------


CLI_BARS = (
    "timestamp,close\n"
    "2026-01-15T00:00:00Z,100\n"
    "2026-01-15T00:01:00Z,105\n"
    "2026-01-15T00:02:00Z,101\n"
)


def _cli(argv, capsys):
    from nano.cli.main import main

    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture()
def cli_files(tmp_path):
    strategy = tmp_path / "s.nano"
    strategy.write_text(PARAM_SOURCE, encoding="utf-8")
    bars = tmp_path / "bars.csv"
    bars.write_text(CLI_BARS, encoding="utf-8")
    return strategy, bars


def test_cli_replay_emits_exactly_the_canonical_receipt(cli_files, capsys):
    strategy, bars = cli_files
    code, out, _ = _cli(
        ["replay", str(strategy), "--data", str(bars), "--report", "receipt"], capsys
    )
    assert code == 0

    module = compile_module(PARAM_SOURCE)
    frame = MarketFrame(
        timestamps=(1768435200, 1768435260, 1768435320),
        signals={"close": (100.0, 105.0, 101.0)},
    )
    expected = build_receipt(module, frame, run_module(module, frame))
    # One line plus the single framing newline `print` adds -- the newline is not
    # part of the canonical form and not part of the digest.
    assert out.encode("ascii") == canonical_bytes(expected) + b"\n"


def test_cli_receipt_output_is_a_single_json_lines_record(cli_files, capsys):
    strategy, bars = cli_files
    _, out, _ = _cli(
        ["replay", str(strategy), "--data", str(bars), "--report", "receipt"], capsys
    )
    assert out.count("\n") == 1
    assert json.loads(out)["receiptVersion"] == RECEIPT_VERSION


def test_cli_receipt_bytes_are_exact_through_a_real_subprocess(cli_files):
    """The framing promise is about bytes on a pipe, and only a pipe can test it.

    `capsys` captures into an in-memory buffer that performs no newline
    translation, so the two tests above are structurally blind to the bug this
    one exists for: `print` goes through a text wrapper that rewrites `\\n` to
    `os.linesep`, and on Windows the receipt was leaving with `\\r\\n` — two
    framing bytes where `docs/receipts.md` §1 promises one. A consumer digesting
    everything but the last byte got a mismatch.
    """
    strategy, bars = cli_files
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "nano.cli",
            "replay",
            str(strategy),
            "--data",
            str(bars),
            "--report",
            "receipt",
        ],
        cwd=str(ROOT),
        capture_output=True,  # deliberately NOT text=True: raw bytes or nothing
    )
    assert done.returncode == 0, done.stderr.decode()

    module = compile_module(PARAM_SOURCE)
    frame = MarketFrame(
        timestamps=(1768435200, 1768435260, 1768435320),
        signals={"close": (100.0, 105.0, 101.0)},
    )
    expected = canonical_bytes(build_receipt(module, frame, run_module(module, frame)))

    assert done.stdout == expected + b"\n"
    assert not done.stdout.endswith(b"\r\n")
    # The documented consumer recipe: strip the one framing byte, digest the rest.
    assert digest_of(done.stdout[:-1]) == receipt_digest(
        build_receipt(module, frame, run_module(module, frame))
    )


def test_cli_verify_compares_bytes_not_dict_equality(cli_files, capsys, monkeypatch):
    """The other half of the bug this branch fixes.

    `verify_replay` in the bridge is locked by its own test; `nano replay
    --verify` was not, and reverting it to `again != receipt` left the suite
    green. The two injected results are dict-equal and differ only in the JSON
    type of one field.
    """
    from nano.cli import commands
    from nano.runtime.effects import Intent
    from nano.runtime.vm import ModuleResult

    first = ModuleResult(
        intents=(Intent(action="BUY", timestamp=0, asset="BTC", confidence=1.0),),
        escalations=(),
        log=(),
    )
    second = ModuleResult(
        intents=(Intent(action="BUY", timestamp=0, asset="BTC", confidence=1),),
        escalations=(),
        log=(),
    )
    assert first.to_dict() == second.to_dict()  # the premise: dict-blind, byte-visible

    results = iter([first, second])
    monkeypatch.setattr(commands, "run_module", lambda *a, **k: next(results))

    strategy, bars = cli_files
    code, _, err = _cli(
        ["replay", str(strategy), "--data", str(bars), "--verify"], capsys
    )
    assert code == 1
    assert "not deterministic" in err
    assert "/run/intents/0/confidence" in err


def test_cli_json_report_still_has_its_original_shape(cli_files, capsys):
    """`--report json` is an existing surface; the receipt is added beside it."""
    strategy, bars = cli_files
    _, out, _ = _cli(
        ["replay", str(strategy), "--data", str(bars), "--report", "json"], capsys
    )
    legacy = json.loads(out)
    assert legacy["moduleHash"].startswith("sha256:")
    assert "receiptVersion" not in legacy


# -- refusals that must name what they refused --------------------------------


def test_a_foreign_scalar_is_named_by_module_and_class():
    """`numpy.bool_` reports `bool` as its class name.

    Naming it unqualified produced "bool is not canonically encodable (allowed:
    ... boolean ...)", which reads as a bug in the encoder rather than a report
    about the caller's value.
    """
    NumpyLikeBool = type("bool", (), {})
    with pytest.raises(ReceiptError, match=r"test_receipts\.bool"):
        canonical_bytes({"approved": NumpyLikeBool()})


def test_a_decimal_is_refused_rather_than_silently_encoded():
    """Any NUMERIC-column database driver hands back one of these."""
    with pytest.raises(ReceiptError, match=r"decimal\.Decimal"):
        canonical_bytes({"price": Decimal("1.5")})


def test_a_mapping_that_is_not_a_dict_is_refused_with_a_path():
    # `json.dumps` rejects this too, but with a bare TypeError pointing at
    # nothing, which is useless inside a large `host` structure.
    with pytest.raises(ReceiptError, match=r"/host/limits"):
        canonical_bytes({"host": {"limits": MappingProxyType({"a": 1})}})


def test_a_self_referential_document_is_refused_rather_than_recursing():
    looped: dict = {"host": {}}
    looped["host"]["self"] = looped
    with pytest.raises(ReceiptError, match="contains itself"):
        canonical_bytes(looped)


def test_a_lone_surrogate_is_refused():
    # `ensure_ascii` would turn this into a `\\udcff` escape that no consumer can
    # decode, quietly breaking the "survives any transport" promise.
    with pytest.raises(ReceiptError, match="surrogate"):
        canonical_bytes({"detail": "bad \udcff byte"})


def test_valid_non_ascii_text_still_encodes():
    # The surrogate guard must not become a ban on international text.
    assert canonical_bytes({"n": "stratégie ✓"}) == b'{"n":"strat\\u00e9gie \\u2713"}'


# -- conventions the artifact promises ----------------------------------------


def test_signal_names_are_sorted_not_left_in_frame_order():
    """Otherwise the artifact inherits the column order of whatever CSV it read."""
    frame = MarketFrame(
        timestamps=(0, 60), signals={"zeta": (1.0, 2.0), "alpha": (3.0, 4.0)}
    )
    module = compile_module(ZSCORE_SOURCE)
    receipt = build_receipt(module, frame, run_module(module, _frame(close=(1.0, 2.0))))
    assert receipt["inputs"]["signals"] == ["alpha", "zeta"]
    assert receipt["inputs"]["signals"] != list(frame.signals)


def test_the_effect_manifest_keeps_declared_order_not_sorted_order():
    """`moduleHash` is taken over the manifest as spelled.

    Emitting a sorted copy beside that hash would make the receipt disagree with
    the number printed next to it. `sign.emit` before `log.append` is canonical
    `EFFECT_ORDER` and is *not* sorted order, which is what makes the two
    distinguishable at all.
    """
    document = compile_module(ZSCORE_SOURCE).to_dict(include_hash=False)
    document["effects"] = ["intent.emit", "sign.emit", "log.append"]
    module = NanoModule.from_dict(document)
    frame = _frame(close=(1.0, 3.0, 2.0, 8.0, 5.0))

    effects = build_receipt(module, frame, run_module(module, frame))["identity"][
        "effects"
    ]
    assert effects == ["intent.emit", "sign.emit", "log.append"]
    assert effects != sorted(effects)


def test_an_uncopyable_host_value_is_refused_with_a_path():
    """Validation has to run BEFORE the defensive copy, not after.

    `host` is the one section where arbitrary caller data lands, so it is the
    one place the path-naming validator matters most. Copying first inverts
    that: `copy.deepcopy` on a lock raises a bare `TypeError` naming nothing,
    and `_check` never gets to run. That regression is invisible to every other
    test, because every other section is built from canonical types already.
    """
    module, frame = _drift_run()
    result = run_module(module, frame)

    with pytest.raises(ReceiptError, match=r"/host/lock"):
        build_receipt(module, frame, result, host={"lock": threading.Lock()})

    # Uncopyable and un-encodable are different faults; both must name a path.
    with pytest.raises(ReceiptError, match=r"/host/cfg"):
        build_receipt(module, frame, result, host={"cfg": MappingProxyType({"a": 1})})

    # The same value at a non-host path was always reported correctly -- that
    # asymmetry is exactly what made the regression easy to miss.
    with pytest.raises(ReceiptError, match=r"/run/cfg"):
        canonical_bytes({"run": {"cfg": MappingProxyType({"a": 1})}})


def test_host_context_is_deep_copied_out_of_the_caller():
    """A receipt is a record; it must not keep aliasing a live caller structure."""
    module, frame = _drift_run()
    live = {"deployment": {"id": "a"}}
    receipt = build_receipt(module, frame, run_module(module, frame), host=live)
    live["deployment"]["id"] = "b"
    assert receipt["host"] == {"deployment": {"id": "a"}}


def test_differences_reports_a_length_change_and_the_elements_too():
    """A truncated log whose surviving entries also changed has two problems.

    Reporting only the length would send a reader looking for one.
    """
    drift = differences({"log": [1, 2, 3]}, {"log": [9, 2]})
    assert "/log[length]" in drift
    assert "/log/0" in drift


RECEIPT_SHAPE = {
    "": {"receiptVersion", "identity", "inputs", "run", "provenance", "host"},
    "identity": {
        "nanoVersion",
        "irVersion",
        "compiler",
        "module",
        "moduleHash",
        "tier",
        "effects",
        "warmupDeclared",
        "reasoningRequired",
        "params",
    },
    "inputs": {"bars", "signals", "frameHash", "firstTimestamp", "lastTimestamp"},
    "run": {"intents", "escalations", "log", "warmupBarsSkipped"},
    "provenance": {"sourceHash"},
}


def test_the_receipt_shape_is_pinned_to_this_version():
    """EDITING `RECEIPT_SHAPE` MEANS BUMPING `RECEIPT_VERSION`.

    The golden files pin the bytes, but they are regenerated as a matter of
    routine — on every Nano version bump, because `identity.nanoVersion` is part
    of the artifact. That makes them the wrong guard against a *shape* change,
    which could ride along in the same regeneration unnoticed. This pins the
    member names independently of any value in them.
    """
    module = compile_module(PARAM_SOURCE)
    frame = _frame(close=(1.0, 2.0, 9.0, 9.5, 10.0))
    receipt = build_receipt(
        module, frame, run_module(module, frame), host={"operator": "ci"}
    )
    # A maximal receipt: every optional section present.
    assert set(receipt) == RECEIPT_SHAPE[""]
    for section in ("identity", "inputs", "run", "provenance"):
        assert set(receipt[section]) == RECEIPT_SHAPE[section], section


def test_optional_members_are_the_only_ones_a_minimal_receipt_omits():
    """The pin above is maximal; this fixes exactly which members may be absent."""
    module = compile_module(ZSCORE_SOURCE)
    empty = MarketFrame(timestamps=(), signals={"close": ()})
    receipt = build_receipt(module, empty, run_module(module, empty))
    assert RECEIPT_SHAPE[""] - set(receipt) == {"host"}
    assert RECEIPT_SHAPE["identity"] - set(receipt["identity"]) == {"params"}
    assert RECEIPT_SHAPE["inputs"] - set(receipt["inputs"]) == {
        "firstTimestamp",
        "lastTimestamp",
    }


# -- pinned hashes ------------------------------------------------------------


def test_the_module_hash_of_a_fixed_source_is_pinned():
    """`moduleHash` is a published content address, so its value is part of the API.

    Nothing else in the suite pins an exact digest: reordering `to_dict`, changing
    the separators, or letting `provenance` back into the hashed document would
    silently renumber every module a host has ever recorded, and every existing
    test would still pass.

    If this goes red, the question is not "what is the new number" but "did we
    mean to invalidate every stored hash". Update it only alongside a deliberate,
    documented IR change.
    """
    module = compile_module(ZSCORE_SOURCE)
    assert module.content_hash() == (
        "sha256:f43458398231b977af7a1114b0e0e7760cd18a09910c9e32d178f7d8d1e45c24"
    )
    assert module.source_hash == (
        "sha256:308da8d305e125c2e5d35a97bd7e2cd32ab3f255635c5c67116cfc4c09c10bea"
    )


def test_a_pinned_frame_has_a_pinned_frame_hash():
    """`frameHash` addresses the injected data, so it must not move with Nano."""
    _, frame = _drift_run()
    assert frame_digest(frame) == (
        "sha256:173b1650bc76a5efe8f882e3695d2b03047fc813ac4ddd51eaafbbd576dd7f8f"
    )


def test_a_receipt_digest_addresses_exactly_the_stored_artifact():
    """The digest is over the bytes on disk, with no framing newline included."""
    module, frame = _drift_run()
    receipt = build_receipt(module, frame, run_module(module, frame))
    stored = (GOLDEN / "receipt_drift.json").read_bytes()
    assert receipt_digest(receipt) == digest_of(stored)


# -- golden files -------------------------------------------------------------


def golden_cases():
    """Named runs whose exact bytes are checked into the repository."""
    plain = compile_module(ZSCORE_SOURCE)
    plain_frame = _frame(close=(1.0, 3.0, 2.0, 8.0, 5.0))

    tuned = compile_module(PARAM_SOURCE)
    tuned_frame = _frame(close=(1.0, 2.0, 9.0, 9.5, 10.0))

    asking = compile_module(REASONING_SOURCE)
    asking_frame = _frame(close=(1.0, 2.0))

    empty = MarketFrame(timestamps=(), signals={"close": ()})

    return {
        "drift": build_receipt(plain, plain_frame, run_module(plain, plain_frame)),
        "tuned": build_receipt(tuned, tuned_frame, run_module(tuned, tuned_frame)),
        "empty": build_receipt(plain, empty, run_module(plain, empty)),
        "reasoning": build_receipt(
            asking,
            asking_frame,
            run_module(asking, asking_frame, provider=Recorded({0: 0.9, 60: 0.1})),
        ),
    }


@pytest.mark.parametrize("name", sorted(golden_cases()))
def test_golden_receipts_are_byte_for_byte_unchanged(name):
    """The receipt format is a published artifact; changing it must be deliberate.

    Regenerate with `py -3.11 tests/regen_goldens.py` after an *intentional*
    change, and bump ``RECEIPT_VERSION`` in the same commit if the shape moved.

    Note that a Nano version bump legitimately moves every one of these, because
    `identity.nanoVersion` is part of the artifact. The failure message says so
    rather than making you work it out.
    """
    path = GOLDEN / f"receipt_{name}.json"
    assert path.exists(), f"missing golden file {path.name}"

    current = golden_cases()[name]
    stored_bytes = path.read_bytes()
    if stored_bytes == canonical_bytes(current):
        return

    drift = differences(json.loads(stored_bytes), current)
    hint = (
        "only the Nano version moved — regenerate the goldens"
        if drift == ("/identity/nanoVersion",)
        else "the receipt content or format changed"
    )
    pytest.fail(
        f"{path.name} no longer matches: {hint}.\n"
        f"  drifted at: {', '.join(drift) or '(byte-level only)'}\n"
        f"  regenerate: py -3.11 tests/regen_goldens.py"
    )


def test_golden_files_parse_as_json_and_declare_the_current_version():
    for name, receipt in golden_cases().items():
        stored = json.loads((GOLDEN / f"receipt_{name}.json").read_bytes())
        assert stored["receiptVersion"] == RECEIPT_VERSION
        assert stored == json.loads(canonical_bytes(receipt))


def test_the_golden_corpus_covers_more_than_one_shape():
    """A guard on the guard: an empty case list would make the checks vacuous."""
    cases = golden_cases()
    assert len(cases) >= 4
    assert {len(c["run"]["intents"]) for c in cases.values()} >= {0, 1}
    assert any(c["run"]["escalations"] for c in cases.values())
