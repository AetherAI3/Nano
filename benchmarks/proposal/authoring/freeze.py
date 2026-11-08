"""One-time freeze: materialize recorded proposal bytes and write the hashed manifest.

Command-document proposals are recorded inputs (stand-ins for a model's output):
their base document is compiled once from the proposal source, then edited by
the hand-written recipe. Task expectations are copied verbatim from the case
tables and are never computed here. Run only before any candidate run:

    python -m benchmarks.proposal.authoring.freeze --write
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from .. import CASE_SCHEMA, DATASET_SCHEMA, MANIFEST_SCHEMA
from ..schema import DATA, ROOT, canonical_text, check_dataset, file_sha256, pretty_text, sha256_text
from . import dev_cases, eval_cases
from .spec import CATALOG

LOCKED = {"dev": False, "eval": True}


def _walk(document: Any, path: str) -> tuple[Any, str | int]:
    parts = path.split(".")
    node = document
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    last = parts[-1]
    return node, int(last) if isinstance(node, list) else last


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"recipe text must occur exactly once: {old!r}")
    return text.replace(old, new)


def materialize(recipe: dict[str, Any]) -> str:
    from nano.intent.quantum import compile_quantum  # authoring-only import

    document = json.loads(compile_quantum(recipe["source"], catalog=CATALOG).canonical_json())
    for path, value in recipe["sets"]:
        node, key = _walk(document, path)
        node[key] = value
    for path in recipe["deletes"]:
        node, key = _walk(document, path)
        del node[key]
    text = canonical_text(document)
    for old, new in recipe["raw"]:
        text = _replace_once(text, old, new)
    for old, new in recipe["replace_all"]:
        if old not in text:
            raise ValueError(f"replace_all text absent: {old!r}")
        text = text.replace(old, new)
    return text


def _freeze_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None or "recipe" not in payload:
        return payload
    return {**payload, "text": materialize(payload["recipe"])}


def build_split(split: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    frozen = [{**case, "proposal": _freeze_payload(case["proposal"]),
               "recorded_repair": _freeze_payload(case["recorded_repair"])} for case in cases]
    document = {"schema": DATASET_SCHEMA, "case_schema": CASE_SCHEMA, "split": split, "locked": LOCKED[split],
                "catalog": "catalog.json", "cases": frozen}
    check_dataset(document, split)
    return document


def manifest(documents: dict[str, dict[str, Any]]) -> dict[str, Any]:
    splits = {}
    for split, document in documents.items():
        cases = document["cases"]
        counts: dict[str, int] = {}
        for case in cases:
            key = f"{case['class']}/{case['subset']}"
            counts[key] = counts.get(key, 0) + 1
        splits[split] = {"file": f"data/{split}.json", "sha256": file_sha256(DATA / f"{split}.json"),
                         "locked": LOCKED[split], "case_count": len(cases), "counts": dict(sorted(counts.items())),
                         "case_ids": [c["id"] for c in cases],
                         "case_sha256": {c["id"]: sha256_text(canonical_text(c)) for c in cases}}
    authoring = ROOT / "authoring"
    return {"schema": MANIFEST_SCHEMA, "dataset_schema": DATASET_SCHEMA, "case_schema": CASE_SCHEMA,
            "frozen_before_candidate_runs": True,
            "catalog": {"file": "data/catalog.json", "sha256": file_sha256(DATA / "catalog.json")},
            "policy": {"file": "policy.json", "sha256": file_sha256(ROOT / "policy.json")},
            "authoring": {name: file_sha256(authoring / name) for name in ("spec.py", "dev_cases.py",
                                                                           "eval_cases.py", "freeze.py")},
            "splits": splits}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write data/*.json and manifest.json")
    args = parser.parse_args()
    documents = {"dev": build_split("dev", dev_cases.CASES), "eval": build_split("eval", eval_cases.CASES)}
    if not args.write:
        return 0
    DATA.mkdir(exist_ok=True)
    (DATA / "catalog.json").write_text(pretty_text(CATALOG), encoding="utf-8", newline="\n")
    for split, document in documents.items():
        (DATA / f"{split}.json").write_text(pretty_text(document), encoding="utf-8", newline="\n")
    (ROOT / "manifest.json").write_text(pretty_text(manifest(documents)), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
