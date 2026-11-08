"""Input manifest: observe the environment, pin it, and admit a run against it.

A manifest pins the source commit and tree, the test interpreter and its
dependency versions, the declared optional-SDK pins, Node and wheel-builder
versions, caller-supplied checkouts, and every suite's files and fixtures with
their SHA-256. A run is admitted only when what it observes equals the pins.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from rc_catalog import Catalog, resolve_suites
from rc_common import MANIFEST_SCHEMA, HarnessError
from rc_runner import probe_interpreter, probe_node
from rc_source import SourceIdentity, commit_tree, dirty_paths, head_identity, is_full_hex
from rc_wheel import Builder, select_builder

INTERPRETER_KEYS = ("implementation", "version", "system", "machine", "platform")
PIN_PATTERN = re.compile(r"^quantum\s*=\s*\[(?P<items>[^\]]*)\]", re.MULTILINE)
COMPARED_KEYS = ("interpreter", "dependencies", "sdk_pins", "node", "wheel_builder", "checkouts",
                 "selected_suites")


@dataclass(frozen=True)
class Checkout:
    role: str
    path: Path
    env_name: str


@dataclass
class Observation:
    identity: SourceIdentity
    interpreter: dict
    base_executable: str
    nodes: List[Tuple[str, Optional[str]]]
    builder: Optional[Builder]
    checkouts: List[dict] = field(default_factory=list)


def sdk_pins(source: Path) -> Dict[str, str]:
    """Exact pins of the optional ``quantum`` extra declared in pyproject.toml."""
    pyproject = source / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    match = PIN_PATTERN.search(pyproject.read_text(encoding="utf-8"))
    pins: Dict[str, str] = {}
    for item in re.findall(r'"([^"]+)"', match.group("items")) if match else []:
        name, sep, version = item.partition("==")
        if sep:
            pins[name.strip()] = version.strip()
    return pins


def _checkout_fact(checkout: Checkout) -> dict:
    try:
        ident = head_identity(checkout.path)
        dirty = dirty_paths(checkout.path)
    except HarnessError as exc:
        return {"role": checkout.role, "commit": None, "tree": None, "dirty": None, "error": str(exc)}
    return {"role": checkout.role, "commit": ident.commit, "tree": ident.tree, "dirty": bool(dirty),
            "error": None}


def observe(repo: Path, src: Path, python: str, env: Mapping[str, str], nodes: Sequence[str],
            wheel_python: Optional[str], checkouts: Sequence[Checkout], needs_wheel: bool) -> Observation:
    try:
        probe = probe_interpreter(python, src, env)
    except OSError as exc:
        raise HarnessError(f"the test interpreter could not start: {exc}") from exc
    node_facts = [(node, probe_node(node, src, env)) for node in nodes]
    builder = None
    if needs_wheel:
        candidates = [(wheel_python, "explicit")] if wheel_python else \
            [(python, "test-interpreter"), (probe["base_executable"], "base-interpreter")]
        builder = select_builder(candidates, src, env)
    return Observation(head_identity(repo), probe, probe["base_executable"], node_facts, builder,
                       [_checkout_fact(c) for c in checkouts])


def build_manifest(obs: Observation, src: Path, catalog: Catalog, selected: Sequence[str],
                   merge_tree: Optional[str]) -> dict:
    return {
        "schema": MANIFEST_SCHEMA,
        "source": {"commit": obs.identity.commit, "tree": obs.identity.tree, "merge_tree": merge_tree},
        "interpreter": {key: obs.interpreter[key] for key in INTERPRETER_KEYS},
        "dependencies": dict(sorted(obs.interpreter["deps"].items())),
        "sdk_pins": sdk_pins(src),
        "node": [{"slot": slot, "version": version} for slot, (_, version) in enumerate(obs.nodes)],
        "wheel_builder": obs.builder.manifest() if obs.builder else None,
        "checkouts": [{k: fact[k] for k in ("role", "commit", "tree", "dirty")} for fact in obs.checkouts],
        "selected_suites": sorted(selected),
        "suites": resolve_suites(catalog, src),
    }


def _identity_reasons(pinned: dict, obs: Observation, repo: Path) -> Tuple[str, List[str]]:
    commit, tree = pinned.get("commit"), pinned.get("tree")
    if not is_full_hex(commit) or not is_full_hex(tree):
        return "mismatch", ["manifest source commit and tree must be full 40-hex ids"]
    reasons = []
    if commit == obs.identity.commit:
        identity = "exact_commit"
    else:
        identity = "equivalent_tree"
        local = commit_tree(repo, commit)
        if local is None:
            reasons.append(f"pinned commit {commit} is unknown in the tested repository")
        elif local != tree:
            reasons.append("pinned commit's tree differs from the pinned tree")
    if tree != obs.identity.tree:
        reasons.append(f"tested tree {obs.identity.tree} differs from pinned tree {tree}")
    return ("mismatch" if reasons else identity), reasons


def _diff_suites(pinned: list, observed: list) -> List[str]:
    pinned_by_id = {entry.get("id"): entry for entry in pinned}
    observed_by_id = {entry["id"]: entry for entry in observed}
    reasons = [f"manifest mismatch: suite {sid} is not declared by this source"
               for sid in sorted(set(pinned_by_id) - set(observed_by_id), key=str)]
    reasons += [f"manifest mismatch: suite {sid} is missing from the manifest"
                for sid in sorted(set(observed_by_id) - set(pinned_by_id))]
    for sid in sorted(set(pinned_by_id) & set(observed_by_id)):
        if pinned_by_id[sid] != observed_by_id[sid]:
            reasons.append(f"manifest mismatch: suite {sid} files, fixtures or requirements differ")
    return reasons


def _diff_key(key: str, pinned: object, observed: object) -> List[str]:
    if pinned == observed:
        return []
    if key == "dependencies" and isinstance(pinned, dict) and isinstance(observed, dict):
        names = sorted(set(pinned) | set(observed))
        return [f"dependency drift: {name} pinned {pinned.get(name)} observed {observed.get(name)}"
                for name in names if pinned.get(name) != observed.get(name)]
    return [f"manifest mismatch: {key}"]


def admit(supplied: Optional[dict], observed: dict, obs: Observation, repo: Path,
          dirty_before: Sequence[str]) -> Tuple[dict, dict]:
    """Return (admission, source facts). An empty reason list admits the run."""
    reasons: List[str] = []
    pinned = supplied if supplied is not None else observed
    if pinned.get("schema") != MANIFEST_SCHEMA:
        reasons.append(f"unsupported manifest schema {pinned.get('schema')!r}")
    identity, id_reasons = _identity_reasons(pinned.get("source") or {}, obs, repo)
    reasons += id_reasons
    if supplied is not None:
        for key in COMPARED_KEYS:
            reasons += _diff_key(key, supplied.get(key), observed[key])
        reasons += _diff_suites(supplied.get("suites") or [], observed["suites"])
        declared = {entry["id"] for entry in observed["suites"]}
        if not set(supplied.get("selected_suites") or []) <= declared:
            reasons.append("manifest mismatch: selected_suites selects undeclared suites")
    merge_tree = (pinned.get("source") or {}).get("merge_tree")
    merge_match = None if merge_tree is None else merge_tree == obs.identity.tree
    if merge_match is False:
        reasons.append("proposed merge tree does not match the tested tree")
    if dirty_before:
        reasons.append(f"source checkout is dirty before execution ({len(dirty_before)} paths)")
    source = {
        "tested_commit": obs.identity.commit, "tested_tree": obs.identity.tree,
        "pinned_commit": (pinned.get("source") or {}).get("commit"),
        "pinned_tree": (pinned.get("source") or {}).get("tree"),
        "identity": identity, "merge_tree": merge_tree, "merge_tree_match": merge_match,
        "dirty_before": list(dirty_before),
    }
    admission = {"status": "REFUSED" if reasons else "ADMITTED", "reasons": reasons,
                 "manifest_supplied": supplied is not None}
    return admission, source
