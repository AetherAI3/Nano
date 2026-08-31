"""Intent IR schema location, validation entry point, and versioning rules.

Versioning policy for the independent ``nanoIntentVersion`` contract:

* patch: clarifications and new optional receipt diagnostics only;
* minor: additive optional capabilities/fields that old hosts can ignore;
* major: removals, renamed fields, changed meanings, or new required behavior.

Strategy ``nanoIrVersion`` is deliberately unrelated and never inspected here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .ir import (
    NANO_INTENT_VERSION,
    SUPPORTED_INTENT_VERSIONS,
    IntentIR,
    IntentValidationError,
)

SCHEMA_PATH = Path(__file__).with_name("schemas") / "nano-intent-0.1.0.schema.json"


def validate_document(document: Mapping[str, Any]) -> IntentIR:
    """Validate and return an immutable representation of a JSON document."""

    try:
        return IntentIR.from_dict(document)
    except IntentValidationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise IntentValidationError(f"invalid Intent IR value: {exc}") from exc


def load_schema() -> dict[str, Any]:
    try:
        return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntentValidationError(f"cannot load canonical Intent schema: {exc}") from exc


__all__ = [
    "NANO_INTENT_VERSION",
    "SCHEMA_PATH",
    "SUPPORTED_INTENT_VERSIONS",
    "IntentValidationError",
    "load_schema",
    "validate_document",
]
