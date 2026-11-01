"""Untrusted Intent documents keep their declared JSON types at admission."""

from copy import deepcopy

import pytest

from nano.intent import IntentIR, canonical_bytes, compile_intent
from nano.intent.ir import IntentValidationError
from nano.intent.schema import validate_document


LOADERS = (IntentIR.from_dict, validate_document, canonical_bytes)


@pytest.mark.parametrize("load", LOADERS)
@pytest.mark.parametrize("confidence", [True, False, "0.9", "1", None, [], {}, float("nan"), float("inf"), -float("inf"), -0.1, 1.1, 10**1000])
def test_untrusted_confidence_is_rejected_without_coercion(load, confidence):
    document = compile_intent("spy earnings").to_dict()
    document["confidence"] = confidence
    with pytest.raises(IntentValidationError, match="confidence"):
        load(document)


@pytest.mark.parametrize("load", LOADERS)
@pytest.mark.parametrize("kind", [42, True, None, [], {}])
def test_untrusted_entity_kind_is_not_stringified(load, kind):
    document = compile_intent("spy earnings").to_dict()
    document["entities"][0]["kind"] = kind
    with pytest.raises(IntentValidationError, match="entity kind"):
        load(document)


@pytest.mark.parametrize("confidence", [0, 0.0, 0.5, 1, 1.0])
def test_json_number_confidence_remains_supported(confidence):
    document = compile_intent("spy earnings").to_dict()
    document["confidence"] = confidence
    accepted = validate_document(document)
    assert accepted.confidence == confidence
    assert type(accepted.confidence) is float
    assert canonical_bytes(document) == accepted.canonical_bytes()


@pytest.mark.parametrize("phrase", ["spy earnings", "when is spy earnings", "explain spy volatility", "unknown short fragment"])
def test_compiler_output_roundtrips_without_byte_or_input_changes(phrase):
    compiled = compile_intent(phrase)
    document = compiled.to_dict()
    original = deepcopy(document)
    assert validate_document(document).canonical_bytes() == compiled.canonical_bytes()
    assert document == original
