"""Deterministic, bounded normalization for human command text."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from .tokens import IntentInputError, MAX_INPUT_CHARS

_SPACE = re.compile(r"\s+")
_LEADING_FILLER = re.compile(
    r"^(?:(?:please|kindly)\s+|(?:(?:can|could|would)\s+you\s+))+"
)
_FORECAST_OF = re.compile(
    r"^(describe|explain|assess|summari[sz]e)\s+"
    r"(?:the\s+)?(predictions?|forecasts?)\s+(?:of|for)\s+"
    r"([^\s]+)\s+"
    r"(earnings|volatility|options|technicals|quote|chart)\b(.*)$"
)
_PUNCT_TO_SPACE = str.maketrans(
    {
        ",": " ",
        ";": " ",
        "?": " ",
        "!": " ",
        "(": " ",
        ")": " ",
        "[": " ",
        "]": " ",
        "{": " ",
        "}": " ",
        '"': " ",
    }
)
_CONTRACTION = re.compile(r"\b(?:don['’]?t|dont)\b")
_FUTURE_BANG = re.compile(r"\b([a-z]{1,4}\d)!", re.IGNORECASE)
_UNICODE_PUNCT = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": " ",
        "\u2013": " ",
        "\u2014": " ",
        "\u2212": "-",
        "\u00a0": " ",
    }
)


@dataclass(frozen=True)
class NormalizedText:
    source: str
    value: str
    source_hash: str


def _source_hash(source: str) -> str:
    return "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


def normalize(source: str) -> NormalizedText:
    """Normalize text without guessing symbols or changing command semantics.

    The one structural rewrite canonicalizes the common "predictions of X"
    possessive frame into ``X ... predictions``.  It is explicit and receipts
    still retain the original source hash; no vocabulary or symbol is corrected.
    """

    if type(source) is not str:
        raise IntentInputError("intent source must be text")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise IntentInputError("intent source contains invalid Unicode") from exc
    if len(source) > MAX_INPUT_CHARS:
        raise IntentInputError(
            f"intent source exceeds the {MAX_INPUT_CHARS}-character limit"
        )

    text = unicodedata.normalize("NFKC", source).translate(_UNICODE_PUNCT)
    text = _CONTRACTION.sub("do not", text)
    text = _FUTURE_BANG.sub(r"\1__nano_future_bang__", text)
    text = text.casefold().translate(_PUNCT_TO_SPACE)
    text = text.replace("__nano_future_bang__", "!")
    text = _SPACE.sub(" ", text).strip()
    text = _LEADING_FILLER.sub("", text).strip()

    forecast = _FORECAST_OF.match(text)
    if forecast:
        verb, noun, subject, topic, tail = forecast.groups()
        text = _SPACE.sub(
            " ", f"{verb} {subject} {topic} {noun}{tail}"
        ).strip()

    # Unicode normalization and case-folding can expand one source code point
    # into several output characters. Re-check the emitted representation so
    # it stays within the bound promised by the frozen JSON Schema.
    if len(text) > MAX_INPUT_CHARS:
        raise IntentInputError(
            f"normalized intent exceeds the {MAX_INPUT_CHARS}-character limit"
        )

    return NormalizedText(source=source, value=text, source_hash=_source_hash(source))


def normalize_text(source: str) -> str:
    """Convenience form returning only the normalized string."""

    return normalize(source).value


__all__ = ["NormalizedText", "normalize", "normalize_text"]
