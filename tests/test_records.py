"""Issue #85 / RFC 0085: immutable records — compile, identity, IR and goldens."""
import copy
import json
import shutil

import pytest

from nano.compiler import (
    NanoCompileError, PackageError, PackageResourceError, check_source, compile_module,
)
from nano.ir import load
from nano.ir.graph import StrategyGraph
from nano.ir.module import NanoModule
from nano.ir.records import RecordIRError
from nano.ir.schema import IRValidationError
from nano.runtime.interpreter import MarketFrame
from nano.runtime.vm import RecordInputError, run_module

from records_support import (
    AGENT, EVIDENCE, FIXTURES, GOLDEN, build, golden_frame, golden_ids, golden_package,
    independent_record_id, simple_package, write_package,
)

OTHER = "module Other {\n  record Other { count: int verified: bool score: float }\n}\n"
INVALID = json.loads((FIXTURES / "records-invalid.json").read_text(encoding="utf-8"))["cases"]


# -- golden: two pure helpers share one imported record ---------------------


def test_golden_record_ids_follow_the_rfc_formula_and_are_pinned(tmp_path):
    compiled = build(golden_package(tmp_path))
    ids = golden_ids()
    assert ids == {"Source": GOLDEN["pinned"]["Source"], "Evidence": GOLDEN["pinned"]["Evidence"]}
    document = compiled.to_dict()["module"]
    assert [r["id"] for r in document["records"]] == [ids["Source"], ids["Evidence"]]
    assert document["nanoIrVersion"] == "1.1.0"
    assert {"name": "evidence", "type": ids["Evidence"]} in document["inputs"]


def test_imported_and_inline_have_identical_executables_and_results(tmp_path):
    imported = build(golden_package(tmp_path / "imported"))
    inline = build(golden_package(tmp_path / "inline", "inline"))
    assert imported.module.content_hash() == inline.module.content_hash() == GOLDEN["pinned"]["moduleHash"]
    assert imported.to_dict()["packageHash"] == GOLDEN["pinned"]["packageHash"]["imported"]
    assert inline.to_dict()["packageHash"] == GOLDEN["pinned"]["packageHash"]["inline"]
    for spec in GOLDEN["frames"]:
        frame = golden_frame(spec)
        left, right = run_module(imported.module, frame), run_module(inline.module, frame)
        assert left.to_dict() == right.to_dict(), spec["id"]
        assert [i.action for i in left.intents] == spec["intents"], spec["id"]
        assert left.warmup_bars_skipped == spec.get("warmupBarsSkipped", 0)


def test_pure_helpers_stay_effect_free_and_grants_still_apply(tmp_path):
    compiled = build(golden_package(tmp_path / "a"))
    assert compiled.module.effects == ("intent.emit", "log.append")
    assert {n.op for n in compiled.module.nodes} <= {
        "schedule", "record.input", "record.get", "input.ref", "const", "compare.ge",
        "logic.and", "intent.emit", "block", "rule",
    }
    manifest = {**GOLDEN["manifests"]["imported"], "effects": ["log.append"]}
    with pytest.raises(PackageError, match="undeclared effects"):
        build(write_package(tmp_path / "b", GOLDEN["sources"], manifest))


def test_provenance_is_stable_across_repeats_and_relocation(tmp_path):
    first = golden_package(tmp_path / "first")
    original = build(first).to_dict()
    assert build(first).to_dict() == original
    moved = tmp_path / "elsewhere" / "nested"
    shutil.copytree(first, moved)
    assert build(moved).to_dict() == original


def test_source_map_traces_field_reads_through_both_helpers(tmp_path):
    compiled = build(golden_package(tmp_path))
    assert set(compiled.source_map) == {n.id for n in compiled.module.nodes}
    traces = {tuple(s["path"] for s in compiled.source_map[n.id])
              for n in compiled.module.nodes if n.op == "record.get"}
    assert traces == {("agent.nano", "quality.nano"), ("agent.nano", "budget.nano")}
    record_input = next(n for n in compiled.module.nodes if n.op == "record.input")
    assert [s["path"] for s in compiled.source_map[record_input.id]] == ["agent.nano", "quality.nano", "agent.nano"]


@pytest.mark.parametrize("edit,same_id", [
    (lambda s: s.replace("count: int score: float", "score: float count: int"), False),
    (lambda s: s.replace("score: float source", "score: float extra: int source").replace(
        "score: 1.0, source", "score: 1.0, extra: 0, source"), False),
    (lambda s: s.replace("origin", "origin_name"), False),
    (lambda s: s + "// comment only\n", True),
    (lambda s: s.replace("  record Source", "\n\n  record Source"), True),
])
def test_schema_change_changes_identity_and_dependent_hashes(tmp_path, edit, same_id):
    base = build(golden_package(tmp_path / "base")).to_dict()
    sources = {"evidence.nano": edit(GOLDEN["sources"]["evidence.nano"])}
    changed = build(golden_package(tmp_path / "changed", sources=sources)).to_dict()
    assert changed["packageHash"] != base["packageHash"]
    same = changed["module"]["records"] == base["module"]["records"]
    assert same is same_id
    assert (changed["module"]["moduleHash"] == base["module"]["moduleHash"]) is same_id


def test_identity_commits_to_module_name_and_field_order():
    fields = [("count", "int"), ("verified", "bool")]
    base = independent_record_id("Evidence", "Evidence", fields)
    assert base != independent_record_id("Other", "Evidence", fields)
    assert base != independent_record_id("Evidence", "Proof", fields)
    assert base != independent_record_id("Evidence", "Evidence", list(reversed(fields)))


# -- construction, projection and substitution semantics -------------------


def _inline_hash(body, inputs="input c: int input v: bool"):
    return compile_module(f"strategy Agent {{ {inputs} every 1m {{ if {body} {{ execute() }} }} }}").content_hash()


@pytest.mark.parametrize("helper,call,inline", [
    # construction then a helper: projection reduces to plain scalar opcodes
    ("", "sufficient(Evidence(count: c, verified: v, score: 1.0))", "c >= 2 and v"),
    # unused argument: its expression emits no node
    ("pure fn first(a: Evidence, b: Evidence): bool { a.verified }",
     "first(Evidence(count: c, verified: v, score: 1.0), Evidence(count: c * 1000, verified: false, score: 2.5))",
     "v"),
    # repeated argument: substituted at each occurrence, as if written twice
    ("pure fn band(e: Evidence): bool { e.count >= 2 and e.count <= 10 }",
     "band(Evidence(count: c + 1, verified: v, score: 1.0))", "c + 1 >= 2 and c + 1 <= 10"),
    # helper returning a constructed record
    ("pure fn mk(n: int, ok: bool): Evidence { Evidence(count: n, verified: ok, score: 0.5) }",
     "sufficient(mk(c, v))", "c >= 2 and v"),
])
def test_construction_lowers_exactly_like_the_inline_expression(tmp_path, helper, call, inline):
    evidence = EVIDENCE.replace("\n}\n", f"\n  {helper}\n}}\n")
    agent = f"import Evidence\nstrategy Agent {{ input c: int input v: bool every 1m {{ if {call} {{ execute() }} }} }}\n"
    compiled = build(simple_package(tmp_path, evidence=evidence, agent=agent))
    assert compiled.module.to_dict()["nanoIrVersion"] == "1.0.0"
    assert compiled.module.content_hash() == _inline_hash(inline)


def test_record_let_is_a_compile_time_binding(tmp_path):
    agent = ("import Evidence\nstrategy Agent {\n input c: int input v: bool\n"
             " let e = Evidence(count: c, verified: v, score: 1.0)\n let n = e.count\n"
             " every 1m { if sufficient(e) and n >= 1 { execute() } }\n}\n")
    compiled = build(simple_package(tmp_path, agent=agent))
    inline = compile_module("strategy Agent { input c: int input v: bool let n = c "
                            "every 1m { if (c >= 2 and v) and n >= 1 { execute() } } }")
    assert compiled.module.content_hash() == inline.content_hash()


def test_record_input_aliases_and_identity_helpers_read_the_same_value(tmp_path):
    evidence = EVIDENCE.replace("\n}\n", "\n  pure fn same(e: Evidence): Evidence { e }\n}\n")
    agent = AGENT.replace("sufficient(evidence)", "sufficient(same(alias))").replace(
        "  every", "  let alias = evidence\n  every")
    compiled = build(simple_package(tmp_path / "a", evidence=evidence, agent=agent))
    plain = build(simple_package(tmp_path / "b"))
    assert compiled.module.content_hash() == plain.module.content_hash()


# -- invalid sources --------------------------------------------------------


@pytest.mark.parametrize("case", INVALID, ids=[c["id"] for c in INVALID])
def test_invalid_sources_have_stable_code_path_and_span(tmp_path, case):
    files = {"evidence.nano": EVIDENCE, "agent.nano": AGENT, case["file"]: case["source"]}
    root = simple_package(tmp_path, evidence=files["evidence.nano"], agent=files["agent.nano"],
                          extra={"other.nano": OTHER})
    with pytest.raises(PackageError) as err:
        build(root)
    text_line = case["source"].split("\n")[case["line"] - 1]
    expected = (case["code"], case["file"], case["line"], text_line.index(case["at"]) + 1)
    actual = (err.value.code, err.value.path, err.value.line, err.value.column)
    assert actual == expected, err.value.message


def test_single_source_grammar_does_not_accept_records():
    with pytest.raises(NanoCompileError, match="Unknown type"):
        check_source("strategy A { input e: Evidence every 1m { observe() } }")
    with pytest.raises(NanoCompileError):
        check_source("strategy A { let e = Evidence(count: 1) every 1m { observe() } }")


# -- fixed ceilings: boundary and boundary + 1 -------------------------------


def _fields(count):
    return " ".join(f"f{i}: int" for i in range(count))


@pytest.mark.parametrize("count,ok", [(32, True), (33, False)])
def test_field_ceiling(tmp_path, count, ok):
    evidence = f"module Evidence {{\n  record Wide {{ {_fields(count)} }}\n}}\n"
    agent = "import Evidence\nstrategy Agent {\n  input w: Wide\n  every 1m { if w.f0 >= 1 { execute() } }\n}\n"
    root = simple_package(tmp_path, evidence=evidence, agent=agent)
    if ok:
        assert len(build(root).module.records[0].fields) == 32
        return
    with pytest.raises(PackageResourceError, match="record field budget") as err:
        build(root)
    assert (err.value.code, err.value.path, err.value.line) == ("RESOURCE_LIMIT", "evidence.nano", 2)


def _chain(depth):
    lines = ["  record R1 { leaf: int }"]
    lines += [f"  record R{i} {{ inner: R{i - 1} }}" for i in range(2, depth + 1)]
    return "module Evidence {\n" + "\n".join(lines) + "\n}\n"


@pytest.mark.parametrize("depth,ok", [(8, True), (9, False)])
def test_depth_ceiling(tmp_path, depth, ok):
    path = ".".join(["inner"] * (depth - 1) + ["leaf"])
    agent = f"import Evidence\nstrategy Agent {{\n  input r: R{depth}\n  every 1m {{ if r.{path} >= 1 {{ execute() }} }}\n}}\n"
    root = simple_package(tmp_path, evidence=_chain(depth), agent=agent)
    if ok:
        assert len(build(root).module.records) == 8
        return
    with pytest.raises(PackageResourceError, match="record depth budget") as err:
        build(root)
    assert (err.value.path, err.value.line) == ("evidence.nano", depth + 1)


def test_declaration_ceiling_counts_unused_records(tmp_path):
    many = "\n".join(f"  record R{i} {{ x: int }}" for i in range(129))
    root = simple_package(tmp_path, evidence=EVIDENCE.replace("\n}\n", "\n" + many + "\n}\n"))
    with pytest.raises(PackageResourceError, match="record declaration budget"):
        build(root)


def test_repeated_record_arguments_hit_the_expansion_budget_with_a_stack(tmp_path):
    helpers = ["pure fn r0(e: Evidence): bool { e.verified and e.verified }"]
    helpers += [f"pure fn r{i}(e: Evidence): bool {{ r{i - 1}(e) and r{i - 1}(e) }}" for i in range(1, 14)]
    evidence = EVIDENCE.replace("\n}\n", "\n  " + "\n  ".join(helpers) + "\n}\n")
    root = simple_package(tmp_path, evidence=evidence, agent=AGENT.replace("sufficient(evidence)", "r13(evidence)"))
    with pytest.raises(PackageResourceError, match="budget exceeded") as err:
        build(root)
    assert err.value.stack and err.value.stack[0]["path"] == "agent.nano"
    assert {site["path"] for site in err.value.stack[1:]} == {"evidence.nano"}


# -- IR 1.1.0 loader --------------------------------------------------------


@pytest.fixture
def golden_document(tmp_path):
    return build(golden_package(tmp_path)).module.to_dict()


def test_ir_round_trip_and_dispatch(golden_document):
    module = NanoModule.from_dict(golden_document)
    assert module.to_dict() == golden_document
    assert load(golden_document).content_hash() == golden_document["moduleHash"]
    assert module.validate() == module


def _first(document, op):
    return next(n for n in document["nodes"] if n["op"] == op)


def _mutations():
    def relabel(d):
        d["nanoIrVersion"] = "1.0.0"

    def drop_table(d):
        del d["records"]

    def drop_and_relabel(d):
        del d["records"]
        d["nanoIrVersion"] = "1.0.0"

    def tamper_schema(d):
        d["records"][0]["fields"][0]["type"] = "int"

    def stale_input(d):
        d["inputs"][0]["type"] = "record:sha256:" + "0" * 64

    def extra_record(d):
        d["records"].append({"id": independent_record_id("X", "X", [("x", "int")]), "module": "X",
                             "name": "X", "fields": [{"name": "x", "type": "int"}]})

    def reorder(d):
        d["records"].reverse()

    def get_on_const(d):
        get = _first(d, "record.get")
        d["nodes"].insert(0, {"id": "c0", "op": "const", "inputs": [], "attrs": {"value": 1}})
        get["inputs"] = ["c0"]

    def compare_record(d):
        node = _first(d, "compare.ge")
        node["inputs"][0] = _first(d, "record.input")["id"]

    def unknown_field(d):
        _first(d, "record.get")["attrs"]["field"] = "hidden"

    def wrong_record(d):
        _first(d, "record.get")["attrs"]["record"] = d["records"][0]["id"]

    def input_type_mismatch(d):
        _first(d, "record.input")["attrs"]["record"] = d["records"][0]["id"]

    return [
        ("relabel-1.0.0", relabel, "RECORD_IR_VERSION"),
        ("1.1.0-without-table", drop_table, "RECORD_IR_VERSION"),
        ("record-ops-in-1.0.0", drop_and_relabel, "RECORD_IR_VERSION"),
        ("tampered-schema", tamper_schema, "RECORD_IR"),
        ("stale-input-id", stale_input, "RECORD_IR_SCHEMA"),
        ("unreferenced-record", extra_record, "RECORD_IR_SCHEMA"),
        ("non-canonical-order", reorder, "RECORD_IR_SCHEMA"),
        ("field-access-on-unvalidated-value", get_on_const, "RECORD_IR_FLOW"),
        ("record-into-comparison", compare_record, "RECORD_IR_FLOW"),
        ("hidden-field-read", unknown_field, "RECORD_IR_FLOW"),
        ("wrong-record-attr", wrong_record, "RECORD_IR_FLOW"),
        ("input-type-mismatch", input_type_mismatch, "RECORD_IR_FLOW"),
    ]


@pytest.mark.parametrize("name,mutate,code", _mutations(), ids=[m[0] for m in _mutations()])
def test_loader_rejects_invalid_record_documents(golden_document, name, mutate, code):
    document = copy.deepcopy(golden_document)
    mutate(document)
    with pytest.raises(IRValidationError, match=code):
        NanoModule.from_dict(document)


def test_loader_identity_mismatch_is_reported_as_identity(golden_document):
    document = copy.deepcopy(golden_document)
    document["records"][0]["name"] = "Renamed"
    with pytest.raises(RecordIRError) as err:
        NanoModule.from_dict(document)
    assert err.value.code == "RECORD_IR_IDENTITY"


def _table(depth, width=1):
    records, previous = [], None
    for level in range(1, depth + 1):
        fields = [(f"f{i}", "int") for i in range(width)] if previous is None else [("inner", previous)]
        rid = independent_record_id("M", f"R{level}", fields)
        records.append({"id": rid, "module": "M", "name": f"R{level}",
                        "fields": [{"name": n, "type": t} for n, t in fields]})
        previous = rid
    return records


@pytest.mark.parametrize("depth,width,ok", [(8, 32, True), (9, 1, False), (1, 33, False)])
def test_loader_enforces_field_and_depth_ceilings(depth, width, ok):
    records = _table(depth, width)
    document = {
        "type": "Strategy", "nanoIrVersion": "1.1.0", "name": "A", "effects": ["log.append"],
        "records": records, "inputs": [{"name": "r", "type": records[-1]["id"]}],
        "nodes": [{"id": "n1", "op": "record.input", "inputs": [],
                   "attrs": {"name": "r", "record": records[-1]["id"]}}],
        "entries": [],
    }
    if ok:
        assert len(NanoModule.from_dict(document).records) == depth
        return
    with pytest.raises(RecordIRError) as err:
        NanoModule.from_dict(document)
    assert err.value.code == "RECORD_IR_LIMIT"


def test_older_loader_paths_reject_the_new_revision(golden_document):
    with pytest.raises(IRValidationError):
        StrategyGraph.from_dict(golden_document)
    with pytest.raises(IRValidationError, match="unsupported"):
        load({**golden_document, "nanoIrVersion": "1.2.0"})


def test_record_get_never_sees_an_unadmitted_value(golden_document):
    module = NanoModule.from_dict(golden_document)
    frame = MarketFrame(timestamps=(0,), signals={"evidence": [{"count": 2}], "model_tokens": (100,)})
    with pytest.raises(RecordInputError):
        run_module(module, frame)
