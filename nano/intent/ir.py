"""Immutable Nano Intent IR 0.1.0 objects and canonical serialization."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, Tuple

from .frozen import FrozenMap, freeze_json, thaw_json
from .policy import (
    ALLOWED_EFFECTS,
    BUDGET_CLASSES,
    CONFIRMATION_MODES,
    EFFECT_ORDER,
    RESPONSE_MODES,
)

NANO_INTENT_VERSION = "0.1.0"
SUPPORTED_INTENT_VERSIONS = (NANO_INTENT_VERSION,)
INTENT_COMPILER_NAME = "nano-intent-compiler"
INTENT_COMPILER_VERSION = "0.1.0"

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORMS = frozenset(
    {
        "noun_phrase",
        "local_imperative",
        "factual_question",
        "temporal_question",
        "analytical_request",
        "compound_request",
        "external_action_request",
        "prohibited_or_unsupported",
        "ambiguous",
    }
)


class IntentValidationError(ValueError):
    """An Intent IR document violates the frozen 0.1.0 contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise IntentValidationError(message)


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IntentValidationError(f"{path} must be an object")
    return value


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if type(value) not in (list, tuple):
        raise IntentValidationError(f"{path} must be an array")
    return value


def _keys(value: Mapping[str, Any], required: set[str], path: str) -> None:
    actual = set(value)
    missing = required - actual
    extra = actual - required
    if missing:
        raise IntentValidationError(f"{path} is missing {', '.join(sorted(missing))}")
    if extra:
        raise IntentValidationError(f"{path} has unknown fields {', '.join(sorted(extra))}")


@dataclass(frozen=True)
class IntentEntity:
    kind: str
    raw: str
    canonical: Any
    span: Tuple[int, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical", freeze_json(self.canonical))
        _require(type(self.kind) is str and bool(self.kind), "entity kind must be non-empty text")
        _require(type(self.raw) is str, "entity raw must be text")
        _require(
            len(self.span) == 2
            and all(type(value) is int for value in self.span)
            and 0 <= self.span[0] <= self.span[1],
            "entity span must be two ordered non-negative integers",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "raw": self.raw,
            "canonical": thaw_json(self.canonical),
            "span": list(self.span),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], path: str) -> "IntentEntity":
        _keys(value, {"kind", "raw", "canonical", "span"}, path)
        span = _sequence(value["span"], f"{path}/span")
        return cls(str(value["kind"]), value["raw"], value["canonical"], tuple(span))  # type: ignore[arg-type]


@dataclass(frozen=True)
class ResponsePlan:
    mode: str
    budget_class: str

    def __post_init__(self) -> None:
        _require(type(self.mode) is str and self.mode in RESPONSE_MODES, f"unknown response mode {self.mode!r}")
        _require(
            type(self.budget_class) is str and self.budget_class in BUDGET_CLASSES,
            f"unknown budget class {self.budget_class!r}",
        )

    def to_dict(self) -> dict[str, str]:
        return {"mode": self.mode, "budgetClass": self.budget_class}


@dataclass(frozen=True)
class IntentStep:
    kind: str
    capability: str
    args: FrozenMap

    def __init__(self, kind: str, capability: str, args: Mapping[str, Any]) -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "args", FrozenMap(args))
        _require(type(kind) is str and kind in ALLOWED_EFFECTS, f"unknown step kind {kind!r}")
        _require(type(capability) is str and bool(capability), "step capability must be non-empty text")
        lowered = capability.casefold()
        _require(
            not any(provider in lowered for provider in ("dsv4", "openai", "anthropic", "claude", "gemini")),
            "provider or model names are not permitted in Intent IR capabilities",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "capability": self.capability,
            "args": thaw_json(self.args),
        }


@dataclass(frozen=True)
class ConfirmationPlan:
    mode: str
    reason: str

    def __post_init__(self) -> None:
        _require(
            type(self.mode) is str and self.mode in CONFIRMATION_MODES,
            f"unknown confirmation mode {self.mode!r}",
        )
        _require(type(self.reason) is str and bool(self.reason), "confirmation reason must be non-empty text")

    def to_dict(self) -> dict[str, str]:
        return {"mode": self.mode, "reason": self.reason}


@dataclass(frozen=True)
class IntentReceipt:
    frame: str
    reason_codes: Tuple[str, ...]
    compiler: FrozenMap
    catalogs: Tuple[FrozenMap, ...]
    source_hash: str
    normalized: str
    tokens: Tuple[FrozenMap, ...]
    entities: Tuple[FrozenMap, ...]
    relations: Tuple[FrozenMap, ...]
    confidence_components: FrozenMap
    operation: str
    response: FrozenMap
    effects: Tuple[str, ...]
    confirmation: FrozenMap
    warnings: Tuple[str, ...]
    unresolved_entities: Tuple[FrozenMap, ...]
    alternate_interpretations: Tuple[FrozenMap, ...]

    def __init__(
        self,
        *,
        frame: str,
        reason_codes: Sequence[str],
        compiler: Mapping[str, Any],
        catalogs: Sequence[Mapping[str, Any]],
        source_hash: str,
        normalized: str,
        tokens: Sequence[Mapping[str, Any]],
        entities: Sequence[Mapping[str, Any]],
        relations: Sequence[Mapping[str, Any]],
        confidence_components: Mapping[str, Any],
        operation: str,
        response: Mapping[str, Any],
        effects: Sequence[str],
        confirmation: Mapping[str, Any],
        warnings: Sequence[str],
        unresolved_entities: Sequence[Mapping[str, Any]],
        alternate_interpretations: Sequence[Mapping[str, Any]],
    ) -> None:
        object.__setattr__(self, "frame", frame)
        object.__setattr__(self, "reason_codes", tuple(reason_codes))
        object.__setattr__(self, "compiler", FrozenMap(compiler))
        object.__setattr__(self, "catalogs", tuple(FrozenMap(item) for item in catalogs))
        object.__setattr__(self, "source_hash", source_hash)
        object.__setattr__(self, "normalized", normalized)
        object.__setattr__(self, "tokens", tuple(FrozenMap(item) for item in tokens))
        object.__setattr__(self, "entities", tuple(FrozenMap(item) for item in entities))
        object.__setattr__(self, "relations", tuple(FrozenMap(item) for item in relations))
        object.__setattr__(self, "confidence_components", FrozenMap(confidence_components))
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "response", FrozenMap(response))
        object.__setattr__(self, "effects", tuple(effects))
        object.__setattr__(self, "confirmation", FrozenMap(confirmation))
        object.__setattr__(self, "warnings", tuple(warnings))
        object.__setattr__(
            self,
            "unresolved_entities",
            tuple(FrozenMap(item) for item in unresolved_entities),
        )
        object.__setattr__(
            self,
            "alternate_interpretations",
            tuple(FrozenMap(item) for item in alternate_interpretations),
        )
        _require(type(frame) is str and bool(frame), "receipt frame must be non-empty text")
        _require(
            bool(reason_codes)
            and all(type(code) is str and code for code in reason_codes),
            "receipt reasonCodes must contain non-empty strings",
        )
        _require(
            len(set(reason_codes)) == len(reason_codes),
            "receipt reasonCodes must not contain duplicates",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame": self.frame,
            "reasonCodes": list(self.reason_codes),
            "compiler": thaw_json(self.compiler),
            "catalogs": thaw_json(self.catalogs),
            "sourceHash": self.source_hash,
            "normalized": self.normalized,
            "tokens": thaw_json(self.tokens),
            "entities": thaw_json(self.entities),
            "relations": thaw_json(self.relations),
            "confidenceComponents": thaw_json(self.confidence_components),
            "operation": self.operation,
            "response": thaw_json(self.response),
            "effects": list(self.effects),
            "confirmation": thaw_json(self.confirmation),
            "warnings": list(self.warnings),
            "unresolvedEntities": thaw_json(self.unresolved_entities),
            "alternateInterpretations": thaw_json(self.alternate_interpretations),
        }


@dataclass(frozen=True)
class IntentIR:
    nano_intent_version: str
    source_hash: str
    normalized: str
    form: str
    confidence: float
    entities: Tuple[IntentEntity, ...]
    operation: str
    response: ResponsePlan
    steps: Tuple[IntentStep, ...]
    effects: Tuple[str, ...]
    confirmation: ConfirmationPlan
    receipt: IntentReceipt

    def __post_init__(self) -> None:
        _require(
            type(self.nano_intent_version) is str
            and self.nano_intent_version in SUPPORTED_INTENT_VERSIONS,
            f"unsupported nanoIntentVersion {self.nano_intent_version!r}",
        )
        _require(
            type(self.source_hash) is str and bool(_HASH.fullmatch(self.source_hash)),
            "sourceHash must be sha256:<64 lowercase hex>",
        )
        _require(
            type(self.normalized) is str and len(self.normalized) <= 4096,
            "normalized must be text within the 4096-character limit",
        )
        _require(type(self.form) is str and self.form in _FORMS, f"unknown intent form {self.form!r}")
        _require(
            type(self.confidence) is float
            and math.isfinite(self.confidence)
            and 0.0 <= self.confidence <= 1.0,
            "confidence must be a finite float from 0 to 1",
        )
        _require(type(self.operation) is str and bool(self.operation), "operation must be non-empty text")
        _require(len(self.entities) <= 256, "entities exceed the 256-item limit")
        _require(len(self.steps) <= 16, "steps exceed the 16-item limit")
        _require(
            all(type(effect) is str for effect in self.effects),
            "effects must contain strings",
        )
        _require(len(set(self.effects)) == len(self.effects), "effects must not contain duplicates")
        _require(all(effect in ALLOWED_EFFECTS for effect in self.effects), "effects contain an unknown value")
        ordered = tuple(effect for effect in EFFECT_ORDER if effect in self.effects)
        _require(self.effects == ordered, "effects are not in canonical effect order")
        step_effects = tuple(effect for effect in EFFECT_ORDER if any(step.kind == effect for step in self.steps))
        _require(self.effects == step_effects, "effects must exactly describe step kinds")
        _require(sum(step.kind == "llm.call" for step in self.steps) <= 1, "Intent IR 0.1 permits at most one llm.call")
        if self.form in ("noun_phrase", "local_imperative"):
            _require(not any(step.kind == "llm.call" for step in self.steps), "local intents must not propose llm.call")
        if self.form == "ambiguous":
            _require(not self.steps and not self.effects, "ambiguous input must not emit executable steps")
            _require(self.response.mode == "interpretations", "ambiguous input must return interpretations")
        if "external.write" in self.effects:
            _require(self.confirmation.mode == "explicit_confirmation", "external.write requires explicit_confirmation")
        if any(step.kind == "llm.call" for step in self.steps):
            _require(any(code.startswith("LLM_") or "LLM" in code for code in self.receipt.reason_codes), "llm.call requires an explanatory reason code")
        for index, entity in enumerate(self.entities):
            _require(entity.span[1] <= len(self.normalized), f"entities/{index}/span exceeds normalized input")
            _require(self.normalized[entity.span[0] : entity.span[1]] == entity.raw, f"entities/{index}/raw does not match normalized span")
        _require(self.receipt.source_hash == self.source_hash, "receipt sourceHash differs from the IR")
        _require(self.receipt.normalized == self.normalized, "receipt normalized text differs from the IR")
        _require(self.receipt.operation == self.operation, "receipt operation differs from the IR")
        _require(
            thaw_json(self.receipt.response) == self.response.to_dict(),
            "receipt response differs from the IR",
        )
        _require(self.receipt.effects == self.effects, "receipt effects differ from the IR")
        _require(
            thaw_json(self.receipt.confirmation) == self.confirmation.to_dict(),
            "receipt confirmation differs from the IR",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "nanoIntentVersion": self.nano_intent_version,
            "sourceHash": self.source_hash,
            "normalized": self.normalized,
            "form": self.form,
            "confidence": self.confidence,
            "entities": [entity.to_dict() for entity in self.entities],
            "operation": self.operation,
            "response": self.response.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "effects": list(self.effects),
            "confirmation": self.confirmation.to_dict(),
            "receipt": self.receipt.to_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self)

    def canonical_json(self) -> str:
        return canonical_json(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "IntentIR":
        root = _mapping(value, "/")
        _keys(
            root,
            {
                "nanoIntentVersion",
                "sourceHash",
                "normalized",
                "form",
                "confidence",
                "entities",
                "operation",
                "response",
                "steps",
                "effects",
                "confirmation",
                "receipt",
            },
            "/",
        )
        response_doc = _mapping(root["response"], "/response")
        _keys(response_doc, {"mode", "budgetClass"}, "/response")
        confirmation_doc = _mapping(root["confirmation"], "/confirmation")
        _keys(confirmation_doc, {"mode", "reason"}, "/confirmation")
        entities = tuple(
            IntentEntity.from_dict(_mapping(item, f"/entities/{index}"), f"/entities/{index}")
            for index, item in enumerate(_sequence(root["entities"], "/entities"))
        )
        steps: list[IntentStep] = []
        for index, item in enumerate(_sequence(root["steps"], "/steps")):
            step_doc = _mapping(item, f"/steps/{index}")
            _keys(step_doc, {"kind", "capability", "args"}, f"/steps/{index}")
            steps.append(
                IntentStep(
                    step_doc["kind"],
                    step_doc["capability"],
                    _mapping(step_doc["args"], f"/steps/{index}/args"),
                )
            )
        receipt_doc = _mapping(root["receipt"], "/receipt")
        receipt_fields = {
            "frame",
            "reasonCodes",
            "compiler",
            "catalogs",
            "sourceHash",
            "normalized",
            "tokens",
            "entities",
            "relations",
            "confidenceComponents",
            "operation",
            "response",
            "effects",
            "confirmation",
            "warnings",
            "unresolvedEntities",
            "alternateInterpretations",
        }
        _keys(receipt_doc, receipt_fields, "/receipt")
        receipt = IntentReceipt(
            frame=receipt_doc["frame"],
            reason_codes=_sequence(receipt_doc["reasonCodes"], "/receipt/reasonCodes"),
            compiler=_mapping(receipt_doc["compiler"], "/receipt/compiler"),
            catalogs=[_mapping(item, "/receipt/catalogs/*") for item in _sequence(receipt_doc["catalogs"], "/receipt/catalogs")],
            source_hash=receipt_doc["sourceHash"],
            normalized=receipt_doc["normalized"],
            tokens=[_mapping(item, "/receipt/tokens/*") for item in _sequence(receipt_doc["tokens"], "/receipt/tokens")],
            entities=[_mapping(item, "/receipt/entities/*") for item in _sequence(receipt_doc["entities"], "/receipt/entities")],
            relations=[_mapping(item, "/receipt/relations/*") for item in _sequence(receipt_doc["relations"], "/receipt/relations")],
            confidence_components=_mapping(receipt_doc["confidenceComponents"], "/receipt/confidenceComponents"),
            operation=receipt_doc["operation"],
            response=_mapping(receipt_doc["response"], "/receipt/response"),
            effects=_sequence(receipt_doc["effects"], "/receipt/effects"),
            confirmation=_mapping(receipt_doc["confirmation"], "/receipt/confirmation"),
            warnings=_sequence(receipt_doc["warnings"], "/receipt/warnings"),
            unresolved_entities=[_mapping(item, "/receipt/unresolvedEntities/*") for item in _sequence(receipt_doc["unresolvedEntities"], "/receipt/unresolvedEntities")],
            alternate_interpretations=[_mapping(item, "/receipt/alternateInterpretations/*") for item in _sequence(receipt_doc["alternateInterpretations"], "/receipt/alternateInterpretations")],
        )
        return cls(
            nano_intent_version=root["nanoIntentVersion"],
            source_hash=root["sourceHash"],
            normalized=root["normalized"],
            form=root["form"],
            confidence=float(root["confidence"]),
            entities=entities,
            operation=root["operation"],
            response=ResponsePlan(response_doc["mode"], response_doc["budgetClass"]),
            steps=tuple(steps),
            effects=tuple(_sequence(root["effects"], "/effects")),
            confirmation=ConfirmationPlan(confirmation_doc["mode"], confirmation_doc["reason"]),
            receipt=receipt,
        )


def canonical_bytes(value: IntentIR | Mapping[str, Any]) -> bytes:
    document = (
        value.to_dict()
        if isinstance(value, IntentIR)
        else IntentIR.from_dict(dict(value)).to_dict()
    )
    try:
        return json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IntentValidationError(f"Intent IR is not canonically encodable: {exc}") from exc


def canonical_json(value: IntentIR | Mapping[str, Any]) -> str:
    return canonical_bytes(value).decode("ascii")


__all__ = [
    "ConfirmationPlan",
    "INTENT_COMPILER_NAME",
    "INTENT_COMPILER_VERSION",
    "IntentEntity",
    "IntentIR",
    "IntentReceipt",
    "IntentStep",
    "IntentValidationError",
    "NANO_INTENT_VERSION",
    "ResponsePlan",
    "SUPPORTED_INTENT_VERSIONS",
    "canonical_bytes",
    "canonical_json",
]
