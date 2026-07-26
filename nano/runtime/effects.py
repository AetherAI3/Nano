"""Intent values and the ordered, in-memory execution log.

Nano never places trades. The reference runtime returns Intents as proposals
for a downstream host gate to accept or reject, together with per-run events.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Intent:
    action: str
    timestamp: int
    asset: Optional[str] = None
    confidence: Optional[float] = None

    def to_dict(self) -> dict:
        out: dict = {"intent": self.action, "timestamp": self.timestamp}
        if self.asset is not None:
            out["asset"] = self.asset
        if self.confidence is not None:
            out["confidence"] = self.confidence
        return out


@dataclass(frozen=True)
class LogEntry:
    event: str
    timestamp: int
    detail: str

    def to_dict(self) -> dict:
        return {"event": self.event, "timestamp": self.timestamp, "detail": self.detail}
