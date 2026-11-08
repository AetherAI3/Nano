"""Shared helpers for the RFC 0085 record tests (not a test module)."""
import hashlib
import json
from pathlib import Path

from nano.compiler import compile_package
from nano.runtime.interpreter import MarketFrame

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "records"
GOLDEN = json.loads((FIXTURES / "records-golden.json").read_text(encoding="utf-8"))

EVIDENCE = (
    "module Evidence {\n"
    "  record Evidence { count: int verified: bool score: float }\n"
    "  pure fn sufficient(e: Evidence): bool { e.count >= 2 and e.verified }\n"
    "}\n"
)
AGENT = (
    "import Evidence\n"
    "strategy Agent {\n"
    "  input evidence: Evidence\n"
    "  every 1m { if sufficient(evidence) { execute() } else { pause() } }\n"
    "}\n"
)


def independent_record_id(module, name, fields):
    """RFC 0085 identity, recomputed without calling the implementation."""
    payload = {"type": "NanoRecord", "identityVersion": 1, "module": module, "name": name,
               "fields": [{"name": f, "type": t} for f, t in fields]}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "record:sha256:" + hashlib.sha256(text.encode()).hexdigest()


def write_package(root, sources, manifest):
    root.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        (root / name).write_bytes(source.encode("utf-8"))
    (root / "nano.json").write_text(json.dumps(manifest), encoding="utf-8")
    paths = [manifest["entry"], *manifest["modules"].values()]
    deps = {p: "sha256:" + hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths}
    (root / "nano.lock").write_text(json.dumps({"nanoLockVersion": "0.1.0", "dependencies": deps}),
                                    encoding="utf-8")
    return root


def build(root):
    return compile_package(root / "nano.json", lock_path=root / "nano.lock")


def simple_package(root, *, evidence=EVIDENCE, agent=AGENT, extra=None):
    sources = {"evidence.nano": evidence, "agent.nano": agent, **(extra or {})}
    modules = {"Evidence": "evidence.nano"}
    for name in extra or {}:
        modules[name.split(".")[0].capitalize()] = name
    manifest = {"nanoPackageVersion": "0.1.0", "entry": "agent.nano", "modules": modules,
                "effects": ["intent.emit", "log.append"]}
    return write_package(root, sources, manifest)


def golden_package(root, kind="imported", sources=None):
    return write_package(root, {**GOLDEN["sources"], **(sources or {})}, GOLDEN["manifests"][kind])


def golden_ids():
    source = independent_record_id("Evidence", "Source", GOLDEN["recordFields"]["Source"])
    fields = [(f, source if t == "Source" else t) for f, t in GOLDEN["recordFields"]["Evidence"]]
    return {"Source": source, "Evidence": independent_record_id("Evidence", "Evidence", fields)}


def envelope(record_id, fields):
    return {"recordType": record_id, "fields": fields}


def golden_evidence(row, ids=None):
    if row is None:
        return None
    ids = ids or golden_ids()
    source = envelope(ids["Source"], {"origin": row["origin"], "verified": row["verified"]})
    return envelope(ids["Evidence"], {"count": row["count"], "score": row["score"], "source": source})


def golden_frame(spec, evidence=None):
    rows = evidence if evidence is not None else [golden_evidence(r) for r in spec["evidence"]]
    return MarketFrame(timestamps=tuple(spec["timestamps"]),
                       signals={"evidence": rows, "model_tokens": tuple(spec["model_tokens"])})
