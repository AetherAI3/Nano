"""Deterministic market-frame loading.

This is the one place in Nano that reads a file, and it sits deliberately at the
edge. Everything downstream — the compiler, the VM, the bridge — is a pure
function of values already in memory, and it stays that way because loading
happens here and nowhere else.

Two shapes are accepted, because both already exist in the wild:

    CSV     timestamp,close,volume
            1767225600,101.5,1200

    JSON    {"timestamps": [0, 300], "signals": {"close": [101.5, 102.0]}}
            [{"timestamp": 0, "close": 101.5}, {"timestamp": 300, "close": 102.0}]

Rules that matter for reproducibility:

**Timestamps become integers immediately.** An epoch-second integer is taken as
is; an ISO-8601 string is parsed as **UTC** unless it carries an explicit offset.
Reading a naive timestamp as local time would make the same CSV replay
differently on two machines — precisely the class of bug determinism exists to
eliminate.

**Rows are sorted by timestamp and duplicates are rejected.** Indicators are
order-sensitive, so a file whose rows arrived out of order would produce
confident nonsense. The sort is stable, and a repeated timestamp is an error
rather than a silent last-one-wins.

**Empty cells are absent, not zero.** A blank CSV field becomes `None`, which
propagates through the kernels as "no value" (see ``nano/indicators/compute.py``).
Filling a gap with zero would invent a price.

Nano still never *fetches* anything. There is no socket here and no credential — a
live feed or broker connector implements the host's side of this contract and
hands frames in. That boundary is what makes "a Nano program cannot act on the
world" a structural claim rather than a policy.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..runtime.interpreter import MarketFrame

# Column names accepted as the timeline, in priority order.
TIMESTAMP_COLUMNS = ("timestamp", "time", "ts", "date", "datetime")

# Cell spellings that mean "no observation". Anything else non-numeric is an
# error: a typo should not quietly become a gap.
_ABSENT_CELLS = frozenset({"", "NA", "NAN", "NULL", "-"})


class FeedError(ValueError):
    """The data file could not be read as a market frame."""


@dataclass(frozen=True)
class LoadedFrame:
    """A frame plus what was dropped getting there, so a replay can report it."""

    frame: MarketFrame
    rows_read: int
    rows_kept: int
    signal_names: Tuple[str, ...]

    @property
    def rows_filtered(self) -> int:
        return self.rows_read - self.rows_kept


def parse_timestamp(raw: Any) -> int:
    """Coerce a timestamp cell to epoch seconds.

    Accepts an integer (used as is), a numeric string, or ISO-8601. A naive
    ISO-8601 value is read as UTC: guessing the reader's local zone would make the
    same file replay differently in two places.
    """
    if isinstance(raw, bool):
        raise FeedError(f"Timestamp {raw!r} is not a time")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if not math.isfinite(raw):
            raise FeedError(f"Timestamp {raw!r} is non-finite")
        return int(raw)
    if not isinstance(raw, str) or not raw.strip():
        raise FeedError(f"Timestamp {raw!r} is empty or not a scalar")

    text = raw.strip()
    try:
        return int(text)
    except ValueError:
        pass

    normalised = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise FeedError(
            f"Timestamp {raw!r} is neither epoch seconds nor ISO-8601"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def parse_date(text: str) -> date:
    """Parse a `--date` argument as a UTC calendar date."""
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise FeedError(f"Date {text!r} is not ISO-8601 (expected YYYY-MM-DD)") from exc


def _parse_cell(raw: Any) -> Optional[float]:
    """A numeric cell, or None when the cell records no observation."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        value = float(raw)
        if not math.isfinite(value):
            raise FeedError(f"Cell {raw!r} is non-finite; expected a finite number")
        return value
    text = str(raw).strip()
    if text.upper() in _ABSENT_CELLS:
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise FeedError(f"Cell {raw!r} is not numeric") from exc
    if not math.isfinite(value):
        raise FeedError(f"Cell {raw!r} is non-finite; expected a finite number")
    return value


def _timestamp_column(fieldnames: Sequence[str]) -> str:
    lowered = {name.lower(): name for name in fieldnames}
    for candidate in TIMESTAMP_COLUMNS:
        if candidate in lowered:
            return lowered[candidate]
    raise FeedError(
        "No timestamp column found (expected one of "
        f"{', '.join(TIMESTAMP_COLUMNS)}), got: {', '.join(fieldnames)}"
    )


def _matches_date(timestamp: int, wanted: Optional[date]) -> bool:
    if wanted is None:
        return True
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).date() == wanted


def _rows_to_frame(
    rows: List[Tuple[int, Mapping[str, Optional[float]]]],
    signal_names: Sequence[str],
    *,
    rows_read: int,
) -> LoadedFrame:
    """Assemble sorted rows into a frame, rejecting duplicate timestamps."""
    # Stable sort by timestamp: indicators are order-sensitive, and a file whose
    # rows arrived out of order would otherwise produce confident nonsense.
    rows.sort(key=lambda item: item[0])

    seen: Set[int] = set()
    for timestamp, _ in rows:
        if timestamp in seen:
            raise FeedError(
                f"Duplicate timestamp {timestamp} — a bar cannot occur twice, and "
                "silently keeping one of them would change the result"
            )
        seen.add(timestamp)

    signals: Dict[str, Tuple[Optional[float], ...]] = {
        name: tuple(values.get(name) for _, values in rows) for name in signal_names
    }
    return LoadedFrame(
        frame=MarketFrame(
            timestamps=tuple(timestamp for timestamp, _ in rows), signals=signals
        ),
        rows_read=rows_read,
        rows_kept=len(rows),
        signal_names=tuple(signal_names),
    )


def load_csv(path: Path, *, on_date: Optional[date] = None) -> LoadedFrame:
    """Read a CSV whose header names the timeline and one column per signal."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise FeedError(f"{path} has no header row")
        time_column = _timestamp_column(reader.fieldnames)
        signal_names = [name for name in reader.fieldnames if name != time_column]
        if not signal_names:
            raise FeedError(f"{path} declares a timeline but no signal columns")

        rows: List[Tuple[int, Mapping[str, Optional[float]]]] = []
        rows_read = 0
        for record in reader:
            rows_read += 1
            timestamp = parse_timestamp(record[time_column])
            if not _matches_date(timestamp, on_date):
                continue
            rows.append(
                (
                    timestamp,
                    {name: _parse_cell(record.get(name)) for name in signal_names},
                )
            )
    return _rows_to_frame(rows, signal_names, rows_read=rows_read)


def _load_columnar(
    document: Mapping[str, Any], *, on_date: Optional[date]
) -> LoadedFrame:
    raw_timestamps = document.get("timestamps")
    raw_signals = document.get("signals")
    if not isinstance(raw_timestamps, list) or not isinstance(raw_signals, Mapping):
        raise FeedError(
            "Columnar JSON requires 'timestamps' (a list) and 'signals' (an object)"
        )

    timestamps = [parse_timestamp(t) for t in raw_timestamps]
    signal_names = list(raw_signals)
    for name, values in raw_signals.items():
        if not isinstance(values, list) or len(values) != len(timestamps):
            length = len(values) if isinstance(values, list) else "?"
            raise FeedError(
                f"Signal {name!r} has {length} points for {len(timestamps)} timestamps"
            )

    rows = [
        (
            timestamp,
            {name: _parse_cell(raw_signals[name][position]) for name in signal_names},
        )
        for position, timestamp in enumerate(timestamps)
        if _matches_date(timestamp, on_date)
    ]
    return _rows_to_frame(rows, signal_names, rows_read=len(timestamps))


def _load_records(records: Sequence[Any], *, on_date: Optional[date]) -> LoadedFrame:
    if not records:
        raise FeedError("Row-list JSON is empty")
    if not all(isinstance(record, Mapping) for record in records):
        raise FeedError("Row-list JSON must contain objects")

    time_column = _timestamp_column(list(records[0]))
    signal_names = [name for name in records[0] if name != time_column]
    if not signal_names:
        raise FeedError("Row-list JSON declares a timeline but no signal fields")

    rows: List[Tuple[int, Mapping[str, Optional[float]]]] = []
    for record in records:
        timestamp = parse_timestamp(record[time_column])
        if not _matches_date(timestamp, on_date):
            continue
        rows.append(
            (timestamp, {name: _parse_cell(record.get(name)) for name in signal_names})
        )
    return _rows_to_frame(rows, signal_names, rows_read=len(records))


def load_json(path: Path, *, on_date: Optional[date] = None) -> LoadedFrame:
    """Read either JSON shape: columnar `{timestamps, signals}` or a row list."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FeedError(f"{path} is not valid JSON: {exc}") from exc

    if isinstance(document, Mapping):
        return _load_columnar(document, on_date=on_date)
    if isinstance(document, list):
        return _load_records(document, on_date=on_date)
    raise FeedError(f"{path} must contain an object or a list of rows")


def load_frame(path: Path, *, on_date: Optional[date] = None) -> LoadedFrame:
    """Load a market frame, choosing the reader by file extension.

    Every failure leaves here as a ``FeedError``. A caller handling bad data
    should not also have to handle `OSError` for an unreadable file and
    `UnicodeDecodeError` for a binary one — those are the same problem from the
    operator's point of view, and one exception type means one diagnostic path.
    """
    if not path.exists():
        raise FeedError(f"{path} does not exist")
    suffix = path.suffix.lower()
    if suffix not in (".csv", ".json"):
        raise FeedError(f"Unsupported data format {suffix!r} (expected .csv or .json)")

    try:
        if suffix == ".csv":
            return load_csv(path, on_date=on_date)
        return load_json(path, on_date=on_date)
    except UnicodeDecodeError as exc:
        raise FeedError(
            f"{path} is not valid UTF-8 (market data must be UTF-8 encoded text)"
        ) from exc
    except OSError as exc:
        raise FeedError(f"cannot read {path}: {exc}") from exc
