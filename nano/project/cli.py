"""File I/O adapter for project discovery; the library stays pure."""
from __future__ import annotations

import json

from . import ProjectError, ProjectIndex, canonical_json, classify_record, compile_query


def command_project(args, console) -> int:
    try:
        if args.project_action == "parse":
            plan = compile_query(args.query)
            console.say(canonical_json(plan.to_dict()))
            return 0 if plan.valid else 1
        try:
            # Bound the CLI input independently of the in-memory embedding API.
            with args.file.open("r", encoding="utf-8-sig") as handle:
                text = handle.read(32 * 1024 * 1024 + 1)
            if len(text) > 32 * 1024 * 1024:
                raise ProjectError("record file exceeds 32 MiB of decoded text")
            payload = json.loads(text)
        except (OSError, UnicodeError) as exc:
            console.warn(f"error: cannot read records: {exc}")
            return 3
        except json.JSONDecodeError as exc:
            raise ProjectError(f"invalid records JSON: {exc}") from exc
        if args.project_action == "classify":
            if not isinstance(payload, dict):
                raise ProjectError("classify expects one record object")
            result = classify_record(payload)
        else:
            if not isinstance(payload, list):
                raise ProjectError("search expects an array of records")
            result = ProjectIndex(payload).search(
                args.query, project_id=args.project_id, limit=args.limit, offset=args.offset)
        console.say(canonical_json(result))
        return 0
    except ProjectError as exc:
        console.warn(f"error: {exc}")
        return 1
