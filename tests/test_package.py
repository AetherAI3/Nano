"""Issue #66: pinned composition, agent evidence validation and hostile bounds."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from nano.compiler import PackageError, PackageResourceError, compile_module, compile_package
from nano.runtime.interpreter import MarketFrame
from nano.runtime.vm import run_module

EVIDENCE = """module Evidence {
  pure fn sufficient(count: int, verified: bool): bool { count >= 2 and verified }
}
"""
TASK = """import Evidence
module Task {
  pure fn ready(count: int, verified: bool, tokens: int): bool {
    sufficient(count, verified) and tokens >= 100
  }
}
"""
ENTRY = """import Task
strategy AgentTask {
  input evidence_count: int
  input evidence_verified: bool
  input model_tokens: int
  every 1m {
    if ready(evidence_count, evidence_verified, model_tokens) { execute() }
    else { pause() }
  }
}
"""
INLINE = ENTRY.replace("import Task\n", "").replace(
    "ready(evidence_count, evidence_verified, model_tokens)",
    "(evidence_count >= 2 and evidence_verified) and model_tokens >= 100",
)


def package(root, *, evidence=EVIDENCE, task=TASK, entry=ENTRY, effects=None):
    root.mkdir(parents=True, exist_ok=True)
    sources = {"evidence.nano": evidence, "task.nano": task, "agent.nano": entry}
    for name, source in sources.items():
        (root / name).write_bytes(source.encode())
    manifest = {
        "nanoPackageVersion": "0.1.0", "entry": "agent.nano",
        "modules": {"Evidence": "evidence.nano", "Task": "task.nano"},
        "effects": effects if effects is not None else ["intent.emit", "log.append"],
    }
    (root / "nano.json").write_text(json.dumps(manifest), encoding="utf-8")
    repin(root)
    return root


def repin(root):
    deps = {p.name: "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.glob("*.nano")}
    (root / "nano.lock").write_text(json.dumps({"nanoLockVersion": "0.1.0", "dependencies": deps}), encoding="utf-8")


def build(root):
    return compile_package(root / "nano.json", lock_path=root / "nano.lock")


def test_two_modules_match_inline_graph_and_positive_negative_results(tmp_path):
    compiled = build(package(tmp_path))
    inline = compile_module(INLINE)
    assert compiled.module.content_hash() == inline.content_hash()
    for count, verified, tokens, expected in [
        (2, True, 100, "EXECUTE"), (1, True, 100, "PAUSE"),
        (2, False, 100, "PAUSE"), (2, True, 99, "PAUSE"),
    ]:
        frame = MarketFrame(timestamps=(0,), signals={
            "evidence_count": (count,), "evidence_verified": (verified,), "model_tokens": (tokens,),
        })
        imported_result = run_module(compiled.module, frame)
        assert imported_result.to_dict() == run_module(inline, frame).to_dict()
        assert imported_result.intents[0].action == expected
    assert compiled.module.effects == inline.effects
    assert compiled.module.to_dict()["nanoIrVersion"] == "1.0.0"


def test_identity_relocation_order_and_all_dependencies(tmp_path):
    first = package(tmp_path / "a")
    second = tmp_path / "b"
    shutil.copytree(first, second)
    original = build(first).to_dict()
    assert build(first).to_dict() == original == build(second).to_dict()
    manifest = json.loads((second / "nano.json").read_text())
    manifest["modules"] = dict(reversed(list(manifest["modules"].items())))
    (second / "nano.json").write_text(json.dumps(manifest, indent=4))
    assert build(second).to_dict()["packageHash"] == original["packageHash"]
    (second / "evidence.nano").write_bytes((EVIDENCE + "// provenance\n").encode())
    repin(second)
    changed = build(second).to_dict()
    assert changed["packageHash"] != original["packageHash"]
    assert changed["module"]["moduleHash"] == original["module"]["moduleHash"]
    (second / "evidence.nano").write_bytes(EVIDENCE.replace(">= 2", ">= 3").encode())
    repin(second)
    assert build(second).module.content_hash() != build(first).module.content_hash()


def test_source_maps_include_definition_and_each_call_site(tmp_path):
    compiled = build(package(tmp_path))
    assert set(compiled.source_map) == {n.id for n in compiled.module.nodes}
    compare = next(n for n in compiled.module.nodes if n.op == "compare.ge")
    trace = compiled.source_map[compare.id]
    assert [s["path"] for s in trace] == ["agent.nano", "task.nano", "evidence.nano"]
    assert [s["line"] for s in trace] == [7, 4, 2]
    for sites in compiled.source_map.values():
        assert all(s["line"] >= 1 and s["column"] >= 1 for s in sites)


@pytest.mark.parametrize("kwargs,match,path,line", [
    ({"entry": ENTRY.replace("model_tokens)", "true)")}, "expects int, got bool", "agent.nano", 7),
    ({"entry": ENTRY.replace(", model_tokens)", ")")}, "expects 3 arguments", "agent.nano", 7),
    ({"evidence": EVIDENCE.replace("count >= 2 and verified", "count + 2")}, "returns int, expected bool", "evidence.nano", 2),
    ({"evidence": EVIDENCE.replace("count >= 2 and verified", "ambient > 2")}, "cannot capture", "evidence.nano", 2),
    ({"evidence": EVIDENCE.replace("count >= 2 and verified", "infer(count)")}, "cannot call", "evidence.nano", 2),
    ({"evidence": EVIDENCE.replace("count >= 2 and verified", "RNG(count)")}, "cannot call", "evidence.nano", 2),
    ({"evidence": EVIDENCE.replace("count >= 2 and verified", "sufficient(count, verified)")}, "recursive", "evidence.nano", 2),
    ({"evidence": EVIDENCE.replace("}\n}", "}\n pure fn sufficient(): bool { true }\n}")}, "duplicate symbol", "evidence.nano", 3),
    ({"entry": ENTRY.replace("input model_tokens: int", "input ready: int")}, "duplicate symbol", "agent.nano", 5),
    ({"task": TASK.replace("import Evidence", "import Evidence\nimport Evidence")}, "duplicate import", "task.nano", 2),
    ({"task": TASK.replace("import Evidence", "import Missing")}, "not explicitly pinned", "task.nano", 1),
    ({"evidence": "import Task\n" + EVIDENCE}, "import cycle", "evidence.nano", 1),
    ({"effects": ["log.append"]}, "undeclared effects", "agent.nano", 1),
    ({"entry": ENTRY.replace("model_tokens)", "SMA(model_tokens, 2))")}, "helper arguments cannot call", "agent.nano", 7),
    ({"evidence": EVIDENCE.replace("count: int", "count: series<int>")}, "scalar types", "evidence.nano", 2),
    ({"evidence": EVIDENCE.replace("verified: bool", "count: bool")}, "duplicate parameter", "evidence.nano", 2),
])
def test_positioned_diagnostics(tmp_path, kwargs, match, path, line):
    with pytest.raises(PackageError, match=match) as err:
        build(package(tmp_path, **kwargs))
    assert err.value.path == path
    assert err.value.line == line
    assert err.value.column >= 1


def test_stale_transitive_pin_points_at_import(tmp_path):
    root = package(tmp_path)
    (root / "evidence.nano").write_bytes((EVIDENCE + "// changed").encode())
    with pytest.raises(PackageError, match="stale dependency hash for evidence.nano") as err:
        build(root)
    assert (err.value.path, err.value.line, err.value.column) == ("task.nano", 1, 8)


def test_missing_and_extra_lock_pins_are_rejected(tmp_path):
    root = package(tmp_path)
    lock = json.loads((root / "nano.lock").read_text())
    del lock["dependencies"]["evidence.nano"]
    (root / "nano.lock").write_text(json.dumps(lock))
    with pytest.raises(PackageError, match="pin exactly"):
        build(root)


@pytest.mark.parametrize("path", ["../evidence.nano", "/tmp/evidence.nano", "C:/evidence.nano", "https://example/evidence.nano", "./evidence.nano", "a\\evidence.nano"])
def test_paths_cannot_discover_or_escape(tmp_path, path):
    root = package(tmp_path)
    manifest = json.loads((root / "nano.json").read_text())
    manifest["modules"]["Evidence"] = path
    (root / "nano.json").write_text(json.dumps(manifest))
    with pytest.raises(PackageError, match="canonical relative"):
        build(root)


def test_unused_invalid_module_is_checked(tmp_path):
    root = package(tmp_path, entry=INLINE, evidence=EVIDENCE.replace("count >= 2 and verified", "clock"))
    with pytest.raises(PackageError, match="cannot capture"):
        build(root)


def test_mutual_function_recursion(tmp_path):
    evidence = """module Evidence {
 pure fn a(x: int): bool { b(x) }
 pure fn b(x: int): bool { a(x) }
}
"""
    with pytest.raises(PackageError, match="recursive"):
        build(package(tmp_path, evidence=evidence, task="module Task {}", entry=INLINE))


def test_no_implicit_reexport(tmp_path):
    with pytest.raises(PackageError, match="Unknown function 'sufficient'"):
        build(package(tmp_path, entry=ENTRY.replace("ready(evidence_count, evidence_verified, model_tokens)", "sufficient(evidence_count, evidence_verified)")))


def test_diamond_imports_share_pins_and_reject_export_collisions(tmp_path):
    root = package(tmp_path, entry="import Evidence\n" + ENTRY)
    assert build(root).module.content_hash() == compile_module(INLINE).content_hash()
    task = TASK.replace("  pure fn ready", "  pure fn sufficient(x: int): int { x }\n  pure fn ready")
    with pytest.raises(PackageError, match="duplicate symbol"):
        build(package(root, task=task))


def test_unused_helper_signatures_are_checked(tmp_path):
    evidence = EVIDENCE.replace("}\n}", "}\n pure fn unused(): bool { sufficient(true, false) }\n}")
    with pytest.raises(PackageError, match="expects int, got bool") as err:
        build(package(tmp_path, evidence=evidence, entry=INLINE))
    assert err.value.path == "evidence.nano"


def test_constant_helpers_exact_types_and_parameter_hygiene(tmp_path):
    evidence = "module Evidence { pure fn constant(): int { 100 } pure fn identity(__result: int): int { __result } }"
    entry = "import Evidence\nstrategy Agent { let result = identity(constant()) every 1m { if result >= 100 { execute() } } }"
    result = build(package(tmp_path, evidence=evidence, task="module Task {}", entry=entry))
    inline = compile_module("strategy Agent { let result = 100 every 1m { if result >= 100 { execute() } } }")
    assert result.module.content_hash() == inline.content_hash()
    evidence = evidence.replace("identity(__result: int)", "identity(__result: float)")
    with pytest.raises(PackageError, match="expects float, got int|returns float, expected int"):
        build(package(tmp_path, evidence=evidence, task="module Task {}", entry=entry))


def test_host_call_argument_is_rejected_even_if_unused(tmp_path):
    evidence = "module Evidence { pure fn ignore(x: float): bool { true } }"
    entry = """import Evidence
tier nano+
strategy Agent {
 signature Review { input value: float output confidence: float }
 input value: float
 every 1m { if ignore(infer(Review, value).confidence) { execute() } }
}
"""
    with pytest.raises(PackageError, match="helper arguments cannot call"):
        build(package(tmp_path, evidence=evidence, task="module Task {}", entry=entry))


def test_memoized_function_graph_cannot_hide_excessive_depth(tmp_path):
    functions = ["pure fn f0(x: int): int { x }"]
    functions += [f"pure fn f{i}(x: int): int {{ f{i-1}(x) }}" for i in range(1, 33)]
    evidence = "module Evidence {\n" + "\n".join(functions) + "\n}"
    with pytest.raises(PackageError, match="call depth budget exceeded"):
        build(package(tmp_path, evidence=evidence, task="module Task {}", entry=INLINE))


def test_function_count_is_bounded_even_when_unused(tmp_path):
    evidence = "module Evidence {\n" + "\n".join(
        f"pure fn f{i}(): bool {{ true }}" for i in range(129)
    ) + "\n}"
    with pytest.raises(PackageError, match="function budget exceeded"):
        build(package(tmp_path, evidence=evidence, task="module Task {}", entry=INLINE))


def test_metadata_depth_is_positioned(tmp_path):
    root = package(tmp_path)
    (root / "nano.lock").write_text("[" * 2000 + "]" * 2000)
    with pytest.raises(PackageError, match="metadata depth budget"):
        build(root)


def test_source_symlink_cannot_escape_root(tmp_path):
    root = package(tmp_path / "package")
    outside = tmp_path / "outside.nano"
    outside.write_bytes(EVIDENCE.encode())
    source = root / "evidence.nano"
    source.unlink()
    try:
        source.symlink_to(outside)
    except OSError:
        pytest.skip("host lacks symlink creation privilege")
    with pytest.raises(PackageError, match="escapes package root"):
        build(root)


def test_expansion_rejects_exponential_parameter_duplication(tmp_path):
    functions = ["pure fn f0(x: int): int { x + x }"]
    functions += [f"pure fn f{i}(x: int): int {{ f{i-1}(f{i-1}(x)) }}" for i in range(1, 6)]
    evidence = "module Evidence {\n" + "\n".join(functions) + "\n}"
    entry = "import Evidence\nstrategy Agent { input count: int let result = f5(count) every 1m { observe() } }"
    with pytest.raises(PackageError, match="budget exceeded"):
        build(package(tmp_path, evidence=evidence, task="module Task {}", entry=entry))


def test_fixed_byte_and_syntax_depth_limits(tmp_path):
    root = package(tmp_path, evidence=EVIDENCE + "//" + "x" * 262144)
    with pytest.raises(PackageError, match="byte budget"):
        build(root)
    nested = "(" * 40 + "true" + ")" * 40
    package(root, evidence=EVIDENCE.replace("count >= 2 and verified", nested))
    with pytest.raises(PackageError, match="depth budget"):
        build(root)


def test_checked_in_example(tmp_path):
    root = Path(__file__).resolve().parents[1] / "examples" / "agent-package"
    result = build(root)
    assert result.module.content_hash() == compile_module((root / "inline.nano").read_text()).content_hash()


def test_duplicate_json_keys_rejected(tmp_path):
    root = package(tmp_path)
    (root / "nano.json").write_text('{"entry":"a.nano","entry":"b.nano"}')
    with pytest.raises(PackageError, match="duplicate metadata key"):
        build(root)


def test_identity_helper_retains_parameter_and_argument_locations(tmp_path):
    evidence = "module Evidence { pure fn identity(x: int): int { x } }"
    entry = "import Evidence\nstrategy Agent { input count: int let result = identity(count) every 1m { observe() } }"
    compiled = build(package(tmp_path, evidence=evidence, task="module Task {}", entry=entry))
    node = next(n for n in compiled.module.nodes if n.op == "input.ref")
    assert [s["path"] for s in compiled.source_map[node.id]] == ["agent.nano", "evidence.nano", "agent.nano"]


def test_unknown_effect_grants_rejected(tmp_path):
    with pytest.raises(PackageError, match="unknown effect grant"):
        build(package(tmp_path, effects=["log.append", "filesystem.write"]))


@pytest.mark.parametrize("expression", ["not " * 40 + "true", " + ".join(["1"] * 40) + " > 0"])
def test_entry_expression_ceiling_is_32_and_structured(tmp_path, expression):
    entry = f"strategy Agent {{ every 1m {{ if {expression} {{ observe() }} }} }}"
    with pytest.raises(PackageResourceError) as err:
        build(package(tmp_path, entry=entry))
    assert err.value.code == "RESOURCE_LIMIT"
    assert (err.value.path, err.value.line) == ("agent.nano", 1)
    assert err.value.column > 1


def test_expanded_expression_depth_is_bounded_before_checking(tmp_path):
    evidence = "module Evidence { pure fn double(x: int): int { x + x } }"
    expression = "double(" * 31 + "1" + ")" * 31
    entry = f"import Evidence\nstrategy Agent {{ let result = {expression} every 1m {{ observe() }} }}"
    with pytest.raises(PackageResourceError) as err:
        build(package(tmp_path, evidence=evidence, task="module Task {}", entry=entry))
    assert err.value.code == "RESOURCE_LIMIT"


def test_ir_emission_budget_stops_many_small_schedules(tmp_path):
    entry = "strategy Agent {" + " every 1m { observe() }" * 1800 + "}"
    with pytest.raises(PackageResourceError, match="IR node budget exceeded"):
        build(package(tmp_path, entry=entry))


def test_file_count_and_token_budgets(tmp_path):
    root = package(tmp_path)
    manifest = json.loads((root / "nano.json").read_text())
    manifest["modules"] = {f"M{i}": f"m{i}.nano" for i in range(32)}
    (root / "nano.json").write_text(json.dumps(manifest))
    with pytest.raises(PackageResourceError, match="file budget exceeded"):
        build(root)
    package(root, entry="strategy Agent {" + " every 1m { observe() }" * 2400 + "}")
    with pytest.raises(PackageResourceError, match="token budget exceeded"):
        build(root)
