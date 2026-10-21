"""Feed freshness and gap tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time

from app.core.timeutil import ist_time
from app.market_data.candle_builder import SESSION_ANCHOR

MARKET_CLOSE = time(15, 30)


@dataclass
class InstrumentHealth:
    last_event_ts: datetime | None = None
    last_receive_ts: datetime | None = None
    ticks: int = 0
    gaps: int = 0
    in_gap: bool = False
    max_latency_ms: float = 0.0


@dataclass
class FeedHealth:
    stale_seconds: float
    per_key: dict[str, InstrumentHealth] = field(default_factory=dict)
    disconnects: int = 0
    reconnects: int = 0
    disconnected_since: datetime | None = None
    late_ticks: int = 0
    invalid_ticks: int = 0

    def on_tick(self, key: str, event_ts: datetime, receive_ts: datetime) -> float | None:
        """Record a tick; returns the gap length (s) if this tick ends a data gap."""
        h = self.per_key.setdefault(key, InstrumentHealth())
        gap_ended: float | None = None
        if h.in_gap and h.last_event_ts is not None:
            gap_ended = (event_ts - h.last_event_ts).total_seconds()
        h.in_gap = False
        h.last_event_ts = event_ts
        h.last_receive_ts = receive_ts
        h.ticks += 1
        h.max_latency_ms = max(h.max_latency_ms, max((receive_ts - event_ts).total_seconds() * 1000, 0))
        return gap_ended

    def age(self, key: str, now: datetime) -> float | None:
        h = self.per_key.get(key)
        if h is None or h.last_event_ts is None:
            return None
        return (now - h.last_event_ts).total_seconds()

    def is_fresh(self, key: str, now: datetime, max_age: float | None = None) -> bool:
        age = self.age(key, now)
        return age is not None and age <= (max_age or self.stale_seconds)

    def detect_new_gaps(self, keys: list[str], now: datetime) -> list[str]:
        """Keys that just became stale during market hours (each gap reported once)."""
        if not (SESSION_ANCHOR <= ist_time(now) < MARKET_CLOSE):
            return []
        newly = []
        for key in keys:
            h = self.per_key.get(key)
            if h is None or h.last_event_ts is None or h.in_gap:
                continue
            if (now - h.last_event_ts).total_seconds() > self.stale_seconds:
                h.in_gap = True
                h.gaps += 1
                newly.append(key)
        return newly
