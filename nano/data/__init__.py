"""Market-data adapters — the edge where files become frames.

Nano reads data; it never fetches it. A historical CSV or JSON file becomes a
``MarketFrame`` here, and a live feed or broker connector is expected to implement
the host's side of the same contract: build frames, hand them in. There is no
socket and no credential in this package, which is what keeps "a Nano program
cannot act on the world" structural rather than aspirational.

See ``frames.py`` for the accepted file shapes and the UTC timestamp rule.
"""

from .frames import (
    TIMESTAMP_COLUMNS,
    FeedError,
    LoadedFrame,
    load_csv,
    load_frame,
    load_json,
    parse_date,
    parse_timestamp,
)

__all__ = [
    "FeedError",
    "LoadedFrame",
    "TIMESTAMP_COLUMNS",
    "load_csv",
    "load_frame",
    "load_json",
    "parse_date",
    "parse_timestamp",
]
