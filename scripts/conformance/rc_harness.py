"""Orchestrate one release-conformance run and write its evidence.

Order: refuse an output directory inside the repository, record the source
identity and cleanliness, export the exact commit, observe the environment,
admit against the (supplied or observed) manifest, confirm imports resolve to
the export, run the selected suites, re-check cleanliness, then evaluate.
"""
from __future__ import annotations

import datetime
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

import rc_checks
from rc_catalog import CapabilitySpec, Catalog, default_catalog
from rc_common import (BLOCKED, EVIDENCE_SCHEMA, FAIL, NOT_EXECUTED, PASS, RUN_LOG_AUTHORITY, HarnessError,
                       canonical_bytes, canonical_sha256, sha256_file)
from rc_evaluate import (Execution, audit_row, compute_gate, evaluate_suite, not_run_row, precheck,
                         rollup_capabilities)
from rc_manifest import Checkout, Observation, admit, build_manifest, observe
from rc_runner import Context, child_env, env_names, run_node, run_proc, run_pytest
from rc_source import checkout_commit, dirty_paths, export_commit, head_identity, toplevel
from rc_wheel import build_wheel, inspect_wheel

DEFAULT_TIMEOUT_S = 3600
ORIGIN_TIMEOUT_S = 300
ORIGIN_CHECK = ("import importlib, sys; "
                "sys.stdout.write(chr(10).join(importlib.import_module(m).__file__ for m in sys.argv[1:]))")


@dataclass(frozen=True)
class Options:
    repo: Path
    out: Path
    python: str
    manifest: Optional[dict] = None
    select: Optional[Sequence[str]] = None
    nodes: Sequence[str] = ()
    wheel_python: Optional[str] = None
    checkouts: Sequence[Checkout] = ()
    merge_tree: Optional[str] = None
    npm_cache: Optional[Path] = None
    timeout_s: int = DEFAULT_TIMEOUT_S
    catalog: Catalog = field(default_factory=default_catalog)
    inherited_env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))


@dataclass
class State:
    opts: Options
    ctx: Context
    obs: Observation
    manifest: dict
    wheel_src: Optional[Path]
    work: Path
    wheel: Optional[Path] = None
    rows: Dict[str, dict] = field(default_factory=dict)
    durations: Dict[str, float] = field(default_factory=dict)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _prepare_out(opts: Options) -> Path:
    out = opts.out.resolve()
    repo_top = toplevel(opts.repo)
    if out == repo_top or repo_top in out.parents:
        raise HarnessError("the output directory must be outside the tested repository")
    if out.exists() and any(out.iterdir()):
        raise HarnessError(f"the output directory must be new or empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    return out


def _needs_wheel(catalog: Catalog) -> bool:
    return any(spec.kind.startswith("wheel") for spec in catalog.suites)


def _redactions(opts: Options, out: Path, src: Path, obs: Observation) -> Dict[str, str]:
    mapping = {str(out): "{out}", str(src): "{src}", opts.python: "{python}"}
    if obs.builder is not None:
        mapping[obs.builder.python] = "{wheel_python}"
    for checkout in opts.checkouts:
        mapping[str(checkout.path)] = "{checkout:%s}" % checkout.role
    if opts.npm_cache is not None:
        mapping[str(opts.npm_cache)] = "{npm_cache}"
    return mapping


def selected_suites(opts: Options) -> List[str]:
    every = [spec.id for spec in opts.catalog.suites]
    if opts.manifest is not None and opts.select is None:
        return sorted(opts.manifest.get("selected_suites") or [])
    chosen = list(opts.select) if opts.select is not None else every
    unknown = sorted(set(chosen) - set(every))
    if unknown:
        raise HarnessError(f"unknown suite ids: {', '.join(unknown)}")
    return chosen


def _origin_reasons(state: State) -> List[str]:
    env = {**state.ctx.env, "PYTHONPATH": str(state.ctx.src)}
    argv = [state.ctx.python, "-c", ORIGIN_CHECK, *state.opts.catalog.origin_modules]
    proc = run_proc(argv, state.ctx.src, env, ORIGIN_TIMEOUT_S)
    if not proc.launched or proc.exit_code != 0:
        return ["import origin check could not run"]
    origins = proc.stdout.decode("utf-8", errors="replace").splitlines()
    root = os.path.normcase(str(state.ctx.src)) + os.sep
    if not origins or not all(os.path.normcase(path).startswith(root) for path in origins):
        return ["PYTHONPATH/cwd leakage: a tested package resolved outside the exported source"]
    return []


def _qiskit_need(state: State, entry: dict) -> Optional[dict]:
    deps, pins = state.manifest["dependencies"], state.manifest["sdk_pins"]
    if not pins:
        return not_run_row(entry, BLOCKED, ["no optional SDK pins declared by the source"])
    wrong = [f"{name} {deps.get(name)} (pinned {version})" for name, version in sorted(pins.items())
             if deps.get(name) != version]
    if wrong:
        return not_run_row(entry, BLOCKED, ["pinned optional SDK unavailable: " + ", ".join(wrong)])
    return None


def _checkout_need(state: State, entry: dict) -> Optional[dict]:
    if not state.opts.checkouts:
        return not_run_row(entry, NOT_EXECUTED, [
            "no caller-supplied checkout paths (--atlas-checkout / --predator-checkout)"])
    bad = [f"{fact['role']}: {fact['error'] or 'dirty checkout'}" for fact in state.obs.checkouts
           if fact["error"] or fact["dirty"]]
    return not_run_row(entry, FAIL, bad) if bad else None


def _one_need(state: State, entry: dict, need: str) -> Optional[dict]:
    if need == "qiskit_pin":
        return _qiskit_need(state, entry)
    if need == "checkouts":
        return _checkout_need(state, entry)
    if need == "wheel_builder" and state.obs.builder is None:
        return not_run_row(entry, NOT_EXECUTED, ["no interpreter with setuptools to build the wheel"])
    if need == "wheel" and state.wheel is None:
        built = state.rows.get("wheel.build", {}).get("status")
        return not_run_row(entry, FAIL if built == FAIL else NOT_EXECUTED, ["core wheel unavailable"])
    if need == "npm_cache" and state.opts.npm_cache is None:
        return not_run_row(entry, NOT_EXECUTED, [
            "no --npm-cache supplied for the offline lockfile install of the TypeScript compiler"])
    if need == "ts_build" and state.rows.get("cap.ts_client.build", {}).get("status") != PASS:
        built = state.rows.get("cap.ts_client.build", {}).get("status")
        return not_run_row(entry, FAIL if built == FAIL else NOT_EXECUTED, ["TypeScript client build unavailable"])
    if need.startswith("node:"):
        slot = int(need.split(":")[1])
        if slot >= len(state.obs.nodes) or state.obs.nodes[slot][1] is None:
            return not_run_row(entry, NOT_EXECUTED, [f"no Node runtime supplied for slot {slot}"])
    return None


def _gate_need(state: State, entry: dict) -> Optional[dict]:
    """A row for a suite whose prerequisite is absent, else None."""
    for need in entry["needs"]:
        row = _one_need(state, entry, need)
        if row is not None:
            return row
    return None


def _child(state: State, sid: str, root: Path, modules: Sequence[str], help_argvs: Sequence,
           native: bool) -> Callable[[], Execution]:
    return lambda: rc_checks.run_child_check(state.ctx, sid, root, modules, help_argvs, native, state.work)


def _dispatch(state: State, entry: dict) -> Execution:
    ctx, kind, sid = state.ctx, entry["kind"], entry["id"]
    if kind == "pytest":
        files = [item["path"] for item in entry["files"]]
        cross = entry["mode"] == "cross_repo"
        extra = {c.env_name: str(c.path) for c in state.opts.checkouts} if cross else {}
        return run_pytest(ctx, sid, files, extra)
    if kind == "node":
        slot = int(next(n for n in entry["needs"] if n.startswith("node:")).split(":")[1])
        node, version = state.obs.nodes[slot]
        return run_node(ctx, sid, node, version)
    root = state.wheel if kind.startswith("wheel_") else ctx.src
    core, cli = rc_checks.CORE_MODULES, ("nano", "nano.cli.main")
    table: Dict[str, Callable[[], Execution]] = {
        "contribution": lambda: rc_checks.run_contribution(ctx, sid),
        "sha256sums": lambda: rc_checks.run_sha256sums(ctx, entry["files"][0]["path"]),
        "regen_check": lambda: rc_checks.run_regen_check(ctx, sid, entry["files"][0]["path"]),
        "ts_build": lambda: rc_checks.run_ts_build(ctx, sid, state.obs.nodes[0][0], state.opts.npm_cache),
        "cli_help": _child(state, sid, root, cli, rc_checks.HELP_ARGVS, False),
        "import_blocked": _child(state, sid, root, core, (), False),
        "wheel_build": lambda: _build(state, sid),
        "wheel_contents": lambda: inspect_wheel(state.wheel, state.wheel_src),
        "wheel_import": _child(state, sid, root, core, (), False),
        "wheel_cli_help": _child(state, sid, root, cli, rc_checks.HELP_ARGVS, False),
        "wheel_native_fixture": _child(state, sid, root, ("nano", "nano_qsearch.native_artifact"), (), True),
        "wheel_package_cli": lambda: rc_checks.run_wheel_package_cli(ctx, sid, state.wheel, state.work),
    }
    if kind not in table:
        raise HarnessError(f"unknown suite kind {kind!r}")
    return table[kind]()


def _build(state: State, sid: str) -> Execution:
    execution, wheel = build_wheel(state.ctx, sid, state.obs.builder, state.wheel_src,
                                   state.ctx.out / "artifacts", state.obs.identity.commit_time)
    state.wheel = wheel
    return execution


def _run_suite(state: State, entry: dict, selected: Sequence[str]) -> dict:
    if entry["id"] not in selected:
        return not_run_row(entry, NOT_EXECUTED, ["not selected in the manifest"])
    gated = _gate_need(state, entry)
    if gated is not None:
        return gated
    problems = precheck(entry)
    if problems:
        return not_run_row(entry, FAIL, problems)
    started = time.monotonic()
    row = evaluate_suite(entry, _dispatch(state, entry))
    state.durations[entry["id"]] = round(time.monotonic() - started, 3)
    return row


def _artifacts(rows: Sequence[dict]) -> List[dict]:
    by_id = {row["id"]: row for row in rows}
    build = by_id.get("wheel.build")
    if build is None or "wheel" not in build["detail"]:
        return []
    item = {"id": "core-wheel", "path": "artifacts/" + build["detail"]["wheel"],
            "sha256": build["detail"]["sha256"], "builder": build["detail"]["builder"],
            "members_sha256": None, "member_count": None}
    contents = by_id.get("wheel.contents")
    if contents is not None and contents["detail"]:
        item.update(members_sha256=contents["detail"]["members_sha256"],
                    member_count=contents["detail"]["member_count"])
    return [item]


def _telemetry(state: State, started: str, paths: Dict[str, object]) -> dict:
    out = state.ctx.out
    files = {}
    for sub in ("junit", "logs", "artifacts"):
        folder = out / sub
        for path in sorted(folder.glob("*")) if folder.is_dir() else []:
            files[path.relative_to(out).as_posix()] = sha256_file(path)
    return {"started_at": started, "finished_at": _now(), "durations_s": dict(state.durations),
            "paths": paths, "files": files}


def _execute_all(state: State, admission: dict) -> List[dict]:
    selected = state.manifest["selected_suites"]
    rows = []
    for entry in state.manifest["suites"]:
        if admission["status"] != "ADMITTED":
            row = not_run_row(entry, NOT_EXECUTED, ["admission refused"])
        else:
            row = _run_suite(state, entry, selected)
        state.rows[entry["id"]] = row
        rows.append(row)
    return rows


def _setup(opts: Options, out: Path) -> State:
    work = out / "work"
    src = work / "src"
    commit = head_identity(opts.repo).commit
    checkout_commit(opts.repo, commit, src)
    wheel_src = None
    if _needs_wheel(opts.catalog):
        wheel_src = work / "wheel-src"
        export_commit(opts.repo, commit, wheel_src)
    env = child_env(out, opts.inherited_env)
    obs = observe(opts.repo, src, opts.python, env, opts.nodes, opts.wheel_python, opts.checkouts,
                  _needs_wheel(opts.catalog))
    manifest = build_manifest(obs, src, opts.catalog, selected_suites(opts), opts.merge_tree)
    ctx = Context(src, out, opts.python, env, _redactions(opts, out, src, obs), opts.timeout_s)
    return State(opts, ctx, obs, manifest, wheel_src, work)


def prepare_manifest(opts: Options) -> dict:
    """Pin the current source and environment without running any suite."""
    out = _prepare_out(opts)
    if dirty_paths(opts.repo):
        raise HarnessError("refusing to pin a dirty checkout; commit or clean it first")
    return _setup(opts, out).manifest


def _admission(opts: Options, state: State, dirty_before: List[str]):
    admission, source = admit(opts.manifest, state.manifest, state.obs, opts.repo, dirty_before)
    if admission["status"] == "ADMITTED":
        admission["reasons"] += _origin_reasons(state)
        admission["status"] = "REFUSED" if admission["reasons"] else "ADMITTED"
    return admission, source


def run(opts: Options) -> dict:
    """Execute one harness run; write and return the evidence document."""
    out = _prepare_out(opts)
    started = _now()
    dirty_before = dirty_paths(opts.repo)
    state = _setup(opts, out)
    admission, source = _admission(opts, state, dirty_before)
    manifest = opts.manifest if opts.manifest is not None else state.manifest
    rows = _execute_all(state, admission)
    source["dirty_after"] = dirty_paths(opts.repo)
    source_reasons = list(admission["reasons"])
    if source["dirty_after"]:
        source_reasons.append(f"source checkout changed during the run ({len(source['dirty_after'])} paths)")
    capabilities = rollup_capabilities(opts.catalog.capabilities, rows)
    canonical = {
        "schema": EVIDENCE_SCHEMA,
        "manifest": manifest, "manifest_sha256": canonical_sha256(manifest),
        "observed_manifest_sha256": canonical_sha256(state.manifest),
        "admission": admission, "source": source,
        "environment": {**env_names(opts.inherited_env), "runner": {"kind": "local", "ci_run": None}},
        "artifacts": _artifacts(rows), "suites": rows, "capabilities": capabilities,
        "gate": compute_gate(rows, capabilities, source_reasons),
        "related": {"run_log_schema": RUN_LOG_AUTHORITY},
    }
    paths = {"out": str(out), "src": str(state.ctx.src), "python": opts.python,
             "wheel_python": state.obs.builder.python if state.obs.builder else None,
             "nodes": [node for node, _ in state.obs.nodes],
             "checkouts": {c.role: str(c.path) for c in opts.checkouts}}
    doc = {"schema": EVIDENCE_SCHEMA, "canonical": canonical, "canonical_sha256": canonical_sha256(canonical),
           "telemetry": _telemetry(state, started, paths)}
    (out / "manifest.json").write_bytes(canonical_bytes(manifest))
    (out / "evidence.canonical.json").write_bytes(canonical_bytes(canonical))
    (out / "evidence.json").write_bytes(canonical_bytes(doc))
    return doc


def _capability_specs(caps: Sequence[dict]) -> List[CapabilitySpec]:
    return [CapabilitySpec(c["id"], c["issue"], c["required"], c["summary"]) for c in caps]


def verify_evidence(doc: dict) -> List[str]:
    """Re-derive the canonical hash, PASS-row invariants, capabilities and gate."""
    problems = []
    canonical = doc.get("canonical") or {}
    if doc.get("schema") != EVIDENCE_SCHEMA or canonical.get("schema") != EVIDENCE_SCHEMA:
        problems.append("unsupported evidence schema")
    if canonical_sha256(canonical) != doc.get("canonical_sha256"):
        problems.append("canonical_sha256 does not match the canonical evidence")
    if canonical_sha256(canonical.get("manifest")) != canonical.get("manifest_sha256"):
        problems.append("manifest_sha256 does not match the manifest")
    rows = canonical.get("suites") or []
    for row in rows:
        problems += audit_row(row)
    caps = rollup_capabilities(_capability_specs(canonical.get("capabilities") or []), rows)
    if caps != canonical.get("capabilities"):
        problems.append("capability statuses do not follow from the suite rows")
    gate = compute_gate(rows, caps, (canonical.get("gate") or {}).get("source_reasons") or [])
    if gate != canonical.get("gate"):
        problems.append("gate does not follow from the suite rows")
    if canonical.get("admission", {}).get("status") != "ADMITTED" and gate.get("release") == PASS:
        problems.append("a refused admission cannot pass")
    return problems
