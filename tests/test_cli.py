"""The `nano` CLI and the data adapters behind `replay`.

Commands are driven through ``main()`` with argv lists and a captured console
rather than a subprocess: it is faster, and it asserts the exit codes CI actually
branches on.

Exit codes are treated as interface. `nano check` returning 1 on a type error is
what makes it usable in a pre-commit hook, and a command that printed a diagnostic
while exiting 0 would be worse than one that crashed.
"""

import io
import json

import pytest

import nano

from nano.cli.commands import (
    EXIT_DIAGNOSTICS,
    EXIT_IO,
    EXIT_OK,
    EXIT_USAGE,
    Console,
)
from nano.cli.main import build_parser, main
from nano.cli.render import render
from nano.compiler import compile_module
from nano.data import FeedError, load_frame, parse_date, parse_timestamp

VALID = (
    "strategy Momentum {\n"
    "    input close: series<float>\n"
    "    let fast = SMA(close, 2)\n"
    "    every 1m {\n"
    "        if close > fast {\n"
    "            buy(BTCUSD, 0.85)\n"
    "        } else {\n"
    "            observe()\n"
    "        }\n"
    "    }\n"
    "}\n"
)

LEGACY = (
    "strategy Legacy {\n"
    "    every 15m {\n"
    "        if RSI(14) < 30 {\n"
    "            buy(BTCUSD, 0.85)\n"
    "        }\n"
    "    }\n"
    "}\n"
)

BARS_CSV = (
    "timestamp,close\n"
    "2026-01-15T00:00:00Z,100\n"
    "2026-01-15T00:01:00Z,105\n"
    "2026-01-15T00:02:00Z,101\n"
    "2026-01-16T00:00:00Z,150\n"
)


def _run(argv, capsys):
    """Run the CLI and return (code, stdout, stderr)."""
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def strategy(tmp_path):
    path = tmp_path / "momentum.nano"
    path.write_text(VALID, encoding="utf-8")
    return path


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "legacy.nano"
    path.write_text(LEGACY, encoding="utf-8")
    return path


@pytest.fixture
def bars(tmp_path):
    path = tmp_path / "bars.csv"
    path.write_text(BARS_CSV, encoding="utf-8")
    return path


# -- timestamps and dates -----------------------------------------------------


def test_epoch_and_iso_timestamps_agree():
    # A naive ISO timestamp is read as UTC. Reading it as local time would make the
    # same file replay differently on two machines.
    assert parse_timestamp(1767225600) == 1767225600
    assert parse_timestamp("2026-01-01T00:00:00Z") == parse_timestamp(
        "2026-01-01T00:00:00"
    )


def test_bad_timestamp_and_bad_date_are_rejected():
    with pytest.raises(FeedError, match="neither epoch seconds nor ISO-8601"):
        parse_timestamp("not-a-time")
    with pytest.raises(FeedError, match="ISO-8601"):
        parse_date("15/01/2026")


# -- loading ------------------------------------------------------------------


def test_csv_loads_and_date_filters_on_utc(bars):
    loaded = load_frame(bars, on_date=parse_date("2026-01-15"))
    assert (loaded.rows_read, loaded.rows_kept, loaded.rows_filtered) == (4, 3, 1)
    assert loaded.signal_names == ("close",)


def test_blank_cells_become_absent_not_zero(tmp_path):
    path = tmp_path / "gappy.csv"
    path.write_text("timestamp,close\n0,100\n60,\n120,102\n", encoding="utf-8")
    assert load_frame(path).frame.signals["close"] == (100.0, None, 102.0)


def test_rows_are_sorted_because_indicators_are_order_sensitive(tmp_path):
    path = tmp_path / "shuffled.csv"
    path.write_text("timestamp,close\n120,3\n0,1\n60,2\n", encoding="utf-8")
    loaded = load_frame(path)
    assert loaded.frame.timestamps == (0, 60, 120)
    assert loaded.frame.signals["close"] == (1.0, 2.0, 3.0)


def test_duplicate_timestamps_are_rejected(tmp_path):
    path = tmp_path / "dupes.csv"
    path.write_text("timestamp,close\n0,1\n0,2\n", encoding="utf-8")
    with pytest.raises(FeedError, match="cannot occur twice"):
        load_frame(path)


def test_both_json_shapes_load_to_the_same_frame(tmp_path):
    columnar = tmp_path / "columnar.json"
    columnar.write_text(
        json.dumps({"timestamps": [0, 60], "signals": {"close": [1.0, 2.0]}}),
        encoding="utf-8",
    )
    rows = tmp_path / "rows.json"
    rows.write_text(
        json.dumps([{"timestamp": 0, "close": 1.0}, {"timestamp": 60, "close": 2.0}]),
        encoding="utf-8",
    )
    assert load_frame(columnar).frame == load_frame(rows).frame


def test_unsupported_format_is_reported(tmp_path):
    path = tmp_path / "bars.parquet"
    path.write_text("", encoding="utf-8")
    with pytest.raises(FeedError, match="Unsupported data format"):
        load_frame(path)


# -- nano check ---------------------------------------------------------------


def test_check_is_silent_on_success(strategy, capsys):
    assert _run(["check", str(strategy)], capsys) == (EXIT_OK, "", "")


def test_check_verbose_reports_tier_warmup_and_ir_version(strategy, capsys):
    code, out, _ = _run(["check", str(strategy), "-v"], capsys)
    assert code == EXIT_OK
    assert "tier nano" in out
    assert "warmup 1 bar(s)" in out
    assert "ir 1.0.0" in out


def test_check_reports_position_in_the_form_editors_parse(tmp_path, capsys):
    path = tmp_path / "broken.nano"
    path.write_text(
        "strategy S {\n"
        "    input close: series<float>\n"
        "    every 1m {\n"
        "        if close[-1] > close {\n"
        "            buy(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    code, _, err = _run(["check", str(path)], capsys)
    assert code == EXIT_DIAGNOSTICS
    # Column 18 is the `-` of `-1`: the offset expression, not the `[` or the name.
    assert f"{path}:4:18: error:" in err
    assert "reads into the future" in err


def test_check_missing_file_is_an_io_error(tmp_path, capsys):
    code, _, err = _run(["check", str(tmp_path / "absent.nano")], capsys)
    assert code == EXIT_IO
    assert "cannot read" in err


def test_non_utf8_source_is_a_diagnostic_not_a_traceback(tmp_path, capsys):
    # UnicodeDecodeError is a ValueError, not an OSError, so `except OSError` alone
    # let a file of binary junk escape as a traceback while a *missing* file got a
    # clean message. Both are "I cannot read this".
    path = tmp_path / "binary.nano"
    path.write_bytes(b"\xff\xfe\x00not text")
    code, _, err = _run(["check", str(path)], capsys)
    assert code == EXIT_IO
    assert "not valid UTF-8" in err
    assert "Traceback" not in err


def test_a_utf8_bom_does_not_break_an_otherwise_valid_strategy(tmp_path, capsys):
    # Several Windows editors write a BOM by default. Reading as plain `utf-8`
    # kept the U+FEFF in the source, and the lexer reported `Unexpected character`
    # at 1:1 — pointing at a correct program and naming a character the author
    # cannot see. A first contribution should not begin there.
    path = tmp_path / "bom.nano"
    path.write_text(LEGACY, encoding="utf-8-sig")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")

    code, _, err = _run(["check", str(path)], capsys)
    assert code == EXIT_OK, err
    assert err == ""


def test_non_utf8_market_data_is_a_diagnostic_not_a_traceback(
    strategy, tmp_path, capsys
):
    path = tmp_path / "binary.csv"
    path.write_bytes(b"\xff\xfe\x00not text")
    code, _, err = _run(["replay", str(strategy), "--data", str(path)], capsys)
    assert code == EXIT_IO
    assert "not valid UTF-8" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("report", ["text", "json", "receipt"])
def test_replay_rejects_csv_infinity_consistently_in_every_report_mode(
    strategy, tmp_path, capsys, report
):
    path = tmp_path / "infinite.csv"
    path.write_text("timestamp,close\n0,Infinity\n", encoding="utf-8")
    code, out, err = _run(
        ["replay", str(strategy), "--data", str(path), "--report", report], capsys
    )
    assert code == EXIT_IO
    assert out == ""
    assert "Cell 'Infinity' is non-finite; expected a finite number" in err
    assert "Traceback" not in err


# -- nano compile -------------------------------------------------------------


def test_compile_writes_ir_to_a_file(strategy, tmp_path, capsys):
    out_path = tmp_path / "ir.json"
    code, _, err = _run(["compile", str(strategy), "-o", str(out_path)], capsys)
    assert code == EXIT_OK
    document = json.loads(out_path.read_text(encoding="utf-8"))
    assert document["nanoIrVersion"] == "1.0.0"
    assert document["name"] == "Momentum"
    # Progress goes to stderr so `-o -`-style piping of the artifact stays clean.
    assert "nanoIrVersion 1.0.0" in err


def test_compile_emits_baseline_ir_for_a_baseline_strategy(legacy, capsys):
    code, out, _ = _run(["compile", str(legacy)], capsys)
    assert code == EXIT_OK
    assert json.loads(out)["nanoIrVersion"] == "0.1.0"


def test_compile_can_be_forced_to_the_newer_version(legacy, capsys):
    code, out, _ = _run(["compile", str(legacy), "--ir-version", "1.0.0"], capsys)
    assert code == EXIT_OK
    assert json.loads(out)["nanoIrVersion"] == "1.0.0"


def test_forcing_baseline_on_a_v1_strategy_explains_the_refusal(strategy, capsys):
    # Usage, not a diagnostic: the strategy is fine and the requested version
    # cannot hold it, which is a wrong argument rather than a bad program.
    code, _, err = _run(["compile", str(strategy), "--ir-version", "0.1.0"], capsys)
    assert code == EXIT_USAGE
    assert "cannot represent" in err


def test_compile_emit_types_lists_resolved_types(strategy, capsys):
    code, out, _ = _run(["compile", str(strategy), "--emit", "types"], capsys)
    assert code == EXIT_OK
    assert "input close: series<float>" in out
    assert "let fast: series<float>" in out
    assert "warmup:  1 bar(s)" in out


def test_compile_emit_plan_renders_the_graph(strategy, capsys):
    code, out, _ = _run(["compile", str(strategy), "--emit", "plan"], capsys)
    assert code == EXIT_OK
    assert "strategy Momentum" in out
    assert "SMA(2)" in out


# -- nano replay --------------------------------------------------------------


def test_replay_reports_intents_for_one_date(strategy, bars, capsys):
    code, out, _ = _run(
        ["replay", str(strategy), "--data", str(bars), "--date", "2026-01-15", "--verify"],
        capsys,
    )
    assert code == EXIT_OK
    assert "bars      3  (1 row(s) filtered)" in out
    assert "deterministic (verified over two runs)" in out
    assert "BUY BTCUSD @0.85" in out


def test_replay_json_report_carries_hashes_and_the_audit_log(strategy, bars, capsys):
    code, out, _ = _run(
        ["replay", str(strategy), "--data", str(bars), "--report", "json"], capsys
    )
    assert code == EXIT_OK
    report = json.loads(out)
    assert report["moduleHash"].startswith("sha256:")
    assert report["sourceHash"].startswith("sha256:")
    assert report["warmupDeclared"] == 1
    assert report["log"]  # the run is auditable, not merely summarised


def test_replay_names_the_signal_the_data_lacks(strategy, tmp_path, capsys):
    path = tmp_path / "wrong.csv"
    path.write_text("timestamp,volume\n0,10\n60,20\n", encoding="utf-8")
    # Both inputs read fine; one does not fit the other. That is a diagnostic
    # about the pair, not a failure to read either.
    code, _, err = _run(["replay", str(strategy), "--data", str(path)], capsys)
    assert code == EXIT_DIAGNOSTICS
    assert "does not supply close" in err
    assert "it has: volume" in err


def test_replay_reports_an_empty_date_selection(strategy, bars, capsys):
    # The file was read successfully; it simply holds nothing for that date.
    code, _, err = _run(
        ["replay", str(strategy), "--data", str(bars), "--date", "2020-01-01"], capsys
    )
    assert code == EXIT_DIAGNOSTICS
    assert "no rows to replay for 2020-01-01" in err


# -- nano visualize -----------------------------------------------------------


@pytest.mark.parametrize("fmt", ["ascii", "mermaid", "dot", "json"])
def test_visualize_renders_every_format(strategy, fmt, capsys):
    code, out, _ = _run(["visualize", str(strategy), "-f", fmt], capsys)
    assert code == EXIT_OK
    assert out.strip()


def test_visualize_shows_a_shared_node_once(tmp_path, capsys):
    # `close` feeds two indicators but is one data source; drawing it twice would
    # imply two.
    path = tmp_path / "shared.nano"
    path.write_text(
        "strategy S {\n"
        "    input close: series<float>\n"
        "    let a = SMA(close, 2)\n"
        "    let b = SMA(close, 3)\n"
        "    every 1m {\n"
        "        if a > b {\n"
        "            buy(BTC)\n"
        "        }\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    code, out, _ = _run(["visualize", str(path)], capsys)
    assert code == EXIT_OK
    assert "(shared, see" in out


def test_graph_json_is_consumable_by_a_host_renderer():
    document = json.loads(render(compile_module(VALID), "json"))
    assert document["moduleHash"].startswith("sha256:")
    assert {"id", "op", "label", "isEntry"} <= set(document["nodes"][0])
    assert {"from", "to", "port"} <= set(document["edges"][0])
    assert document["entries"]


# -- nano library -------------------------------------------------------------


def test_library_list_is_stable_and_complete(capsys):
    code, out, err = _run(["library", "list"], capsys)
    lines = out.splitlines()

    assert code == EXIT_OK
    assert err == ""
    assert lines[0] == "ID\tIR\tHOST SIGNALS"
    assert len(lines) == 54
    assert lines[1].startswith("event_volatility/cpi_impulse_pullback_long\t0.1.0\t")
    assert lines[-1].startswith("watchdog/trusted_route_guard\t0.1.0\t")


def test_library_show_accepts_a_stable_slug(capsys):
    code, out, err = _run(
        ["library", "show", "ema_pullback_continuation"], capsys
    )
    document = json.loads(out)

    assert code == EXIT_OK
    assert err == ""
    assert document["id"] == "trend/ema_pullback_continuation"
    assert document["irMaturity"] == "v1"
    assert document["requiredHostSignals"] == ["close"]


def test_library_search_uses_authored_and_derived_metadata(capsys):
    code, out, err = _run(["library", "search", "trend continuation"], capsys)

    assert code == EXIT_OK
    assert err == ""
    assert "trend/ema_pullback_continuation" in out
    assert "trend/golden_cross" not in out


def test_library_search_rejects_an_empty_query(capsys):
    code, out, err = _run(["library", "search", "  "], capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert "non-empty query" in err


def test_library_filters_compose_and_watchdog_count_is_pinned(capsys):
    code, out, _ = _run(["library", "filter", "--category", "watchdog"], capsys)
    assert code == EXIT_OK
    assert len(out.splitlines()) == 9

    code, out, _ = _run(
        ["library", "filter", "--category", "trend", "--input", "close"],
        capsys,
    )
    assert code == EXIT_OK
    assert "trend/ema_pullback_continuation" in out
    assert "trend/golden_cross" not in out


def test_library_filter_without_a_dimension_is_a_usage_error(capsys):
    code, out, err = _run(["library", "filter"], capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert "needs --category, --regime, or --input" in err


def test_library_filter_rejects_an_empty_dimension(capsys):
    code, out, err = _run(["library", "filter", "--regime", " "], capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert "values must not be empty" in err


def test_library_unknown_show_is_a_usage_error(capsys):
    code, out, err = _run(["library", "show", "not_a_strategy"], capsys)

    assert code == EXIT_USAGE
    assert out == ""
    assert "unknown library strategy" in err


def test_library_check_regenerates_byte_identically(capsys):
    code, out, err = _run(["library", "check"], capsys)

    assert code == EXIT_OK
    assert err == ""
    assert "53 strategies" in out
    assert "41 baseline + 12 v1" in out


# -- nano indicators / version / help ----------------------------------------


def test_indicators_lists_signatures(capsys):
    code, out, _ = _run(["indicators"], capsys)
    assert code == EXIT_OK
    assert "EMA(series<float>, int) -> series<float>" in out


def test_indicators_describes_one_and_flags_constant_periods(capsys):
    code, out, _ = _run(["indicators", "RSI"], capsys)
    assert code == EXIT_OK
    assert "Wilder" in out
    assert "compile-time constants" in out


def test_unknown_indicator_is_a_usage_error(capsys):
    code, _, err = _run(["indicators", "NOPE"], capsys)
    assert code == EXIT_USAGE
    assert "unknown indicator" in err


def test_empty_indicator_name_does_not_silently_list_everything(capsys):
    # `nano indicators ""` asked about an indicator. Truthiness would have handed
    # back the whole list as though nothing had been asked.
    code, out, err = _run(["indicators", ""], capsys)
    assert code == EXIT_USAGE
    assert "unknown indicator" in err
    assert out == ""


def test_version_reports_both_ir_versions(capsys):
    code, out, _ = _run(["version"], capsys)
    assert code == EXIT_OK
    assert f"nano {nano.__version__}" in out
    assert "0.1.0, 1.0.0" in out


def test_bare_invocation_prints_help_and_succeeds(capsys):
    code, out, _ = _run([], capsys)
    assert code == EXIT_OK
    assert "compile" in out and "replay" in out and "visualize" in out


def test_parser_exposes_every_documented_command():
    parser = build_parser()
    choices = next(
        action.choices
        for action in parser._subparsers._group_actions  # noqa: SLF001 - argparse
        if getattr(action, "choices", None)
    )
    assert {
        "check",
        "compile",
        "replay",
        "visualize",
        "indicators",
        "library",
        "version",
    } <= set(choices)


def test_console_writes_where_it_is_told():
    out, err = io.StringIO(), io.StringIO()
    console = Console(out=out, err=err)
    console.say("to stdout")
    console.warn("to stderr")
    assert out.getvalue() == "to stdout\n"
    assert err.getvalue() == "to stderr\n"
