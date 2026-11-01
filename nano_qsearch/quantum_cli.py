"""Local-file JSON commands for explicit quantum compilation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import canonical_json
from .prepare import (PreparationError, check_request, parse_prepared_artifact,
                      prepare_request)


def _read(path: Path) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON field")
            value[key] = item
        return value

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON number")))


def command_quantum(args: Any, console: Any) -> int:
    """Always emit JSON, including read/validation/dependency failures."""
    try:
        document = _read(args.file)
        if args.artifact:
            if args.profile is not None or args.target_snapshot is not None:
                raise PreparationError("invalid_plan", "artifact checks use the embedded profile and target; external overrides are not accepted")
            artifact = parse_prepared_artifact(document, expected_digest=args.expected_digest)
            if args.quantum_action != "check":
                raise PreparationError("unsupported_capability", "an artifact can be checked; preparation consumes a request")
            output = {"schema": "nano.qsearch.check.v1", "ok": True, "artifact_digest": artifact.artifact_digest,
                      "validation_scope": "structure_and_digests", "isa_validation": "not_loaded",
                      "resources": artifact.to_payload()["resources"], "diagnostics": []}
        else:
            if args.quantum_action == "check" and args.expected_digest is not None:
                raise PreparationError("invalid_plan", "expected artifact digests require --artifact or prepare")
            if args.profile is None:
                raise PreparationError("invalid_plan", "an explicit local compile profile is required")
            profile = _read(args.profile)
            snapshot = _read(args.target_snapshot) if args.target_snapshot else None
            if args.quantum_action == "check":
                output = check_request(document, profile, snapshot)
            else:
                artifact = prepare_request(document, profile, snapshot)
                if args.expected_digest is not None:
                    artifact = parse_prepared_artifact(artifact, expected_digest=args.expected_digest)
                output = artifact.to_payload()
        code = 0 if output.get("ok", True) else 1
    except OSError:
        output = {"schema": "nano.qsearch.check.v1", "ok": False,
                  "diagnostics": [{"code": "input_unavailable", "message": "a local input file could not be read"}]}
        code = 3
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        output = {"schema": "nano.qsearch.check.v1", "ok": False,
                  "diagnostics": [{"code": getattr(exc, "code", "invalid_plan"),
                                   "message": str(exc) if isinstance(exc, PreparationError) else "input is not a valid JSON plan"}]}
        code = 1
    text = canonical_json(output)
    if args.output:
        try:
            args.output.write_text(text + "\n", encoding="utf-8")
        except OSError:
            console.say(canonical_json({"schema": "nano.qsearch.check.v1", "ok": False,
                "diagnostics": [{"code": "output_unavailable", "message": "the local output file could not be written"}]}))
            return 3
    else:
        console.say(text)
    return code


__all__ = ["command_quantum"]
