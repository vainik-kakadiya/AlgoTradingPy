"""Candle construction.

* ``BarAggregator`` builds coarser candles from finer historical bars (e.g. 1-minute ->
  3-minute). A candle is complete only when every constituent bar is present; otherwise it
  is emitted with ``complete=False`` and a ``MISSING_BARS`` flag, and the strategy will not
  evaluate it.
* ``TickCandleBuilder`` builds candles from live ticks. A candle is complete only if the
  builder was already observing before the candle started and no feed gap occurred during
  it. Candles close when a tick arrives past the boundary or when the clock passes the
  boundary plus a small grace period, so signals do not wait for the next tick.

Buckets are aligned to the IST session anchor (09:15), giving 09:15, 09:18, ... for 3m.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from app.core.timeutil import floor_to_interval
from app.trading.types import Candle

SESSION_ANCHOR = time(9, 15)


class BarAggregator:
    def __init__(self, interval_seconds: int, base_interval_seconds: int, anchor: time = SESSION_ANCHOR) -> None:
        if interval_seconds < base_interval_seconds or interval_seconds % base_interval_seconds != 0:
            raise ValueError(
                f"Cannot build {interval_seconds}s candles from {base_interval_seconds}s bars "
                "(target must be a whole multiple of the source granularity)"
            )
        self.interval = interval_seconds
        self.base = base_interval_seconds
        self.expected = interval_seconds // base_interval_seconds
        self.anchor = anchor
        self._bucket: datetime | None = None
        self._bars: list[Candle] = []

    def add(self, bar: Candle) -> list[Candle]:
        """Add a bar; returns zero or more finished candles (incomplete ones flagged)."""
        out: list[Candle] = []
        bucket = floor_to_interval(bar.start, self.interval, self.anchor)
        if self._bucket is not None and bucket != self._bucket:
            out.append(self._emit())
        if self._bucket is None or bucket != self._bucket:
            self._bucket = bucket
            self._bars = []
        if self._bars and bar.start <= self._bars[-1].start:
            return out  # duplicate / out-of-order bar
        self._bars.append(bar)
        if bar.end >= self._bucket + timedelta(seconds=self.interval):
            out.append(self._emit())
        return out

    def flush(self) -> list[Candle]:
        return [self._emit()] if self._bars else []

    def _emit(self) -> Candle:
        assert self._bucket is not None
        bars = self._bars
        vols = [b.volume for b in bars]
        flags: list[str] = []
        complete = len(bars) == self.expected and all(b.complete for b in bars)
        if len(bars) != self.expected:
            flags.append("MISSING_BARS")
        if any(not b.complete for b in bars):
            flags.append("INCOMPLETE_SOURCE_BAR")
        candle = Candle(
            start=self._bucket,
            interval_seconds=self.interval,
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=bars[-1].close,
            volume=None if any(v is None for v in vols) else float(sum(vols)),  # type: ignore[arg-type]
            complete=complete,
            flags=tuple(flags),
        )
        self._bucket = None
        self._bars = []
        return candle


@dataclass
class _Bucket:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    first_volume: float | None
    last_volume: float | None
    ticks: int = 1
    flags: set[str] = field(default_factory=set)
    observed_from_start: bool = True


class TickCandleBuilder:
    def __init__(self, interval_seconds: int, anchor: time = SESSION_ANCHOR, grace_seconds: float = 1.5) -> None:
        self.interval = interval_seconds
        self.anchor = anchor
        self.grace = timedelta(seconds=grace_seconds)
        self.observing_since: datetime | None = None
        self.late_ticks = 0
        self._cur: _Bucket | None = None
        self._last_closed_start: datetime | None = None

    def start_observing(self, now: datetime) -> None:
        if self.observing_since is None:
            self.observing_since = now

    def mark_gap(self, reason: str = "FEED_GAP") -> None:
        """The feed was interrupted: the current candle cannot be trusted as complete."""
        if self._cur is not None:
            self._cur.flags.add(reason)
        self.observing_since = None  # the next candle must also start after we resumed observing

    def on_tick(self, ts: datetime, price: float, cumulative_volume: float | None = None) -> list[Candle]:
        if self.observing_since is None:
            self.observing_since = ts
        out: list[Candle] = []
        bucket = floor_to_interval(ts, self.interval, self.anchor)
        if self._cur is not None and bucket < self._cur.start:
            self.late_ticks += 1
            return out
        if self._last_closed_start is not None and bucket <= self._last_closed_start:
            self.late_ticks += 1
            return out
        if self._cur is not None and bucket > self._cur.start:
            out.append(self._close())
        if self._cur is None:
            self._cur = _Bucket(
                start=bucket, open=price, high=price, low=price, close=price,
                first_volume=cumulative_volume, last_volume=cumulative_volume,
                observed_from_start=self.observing_since <= bucket,
            )
        else:
            c = self._cur
            c.high = max(c.high, price)
            c.low = min(c.low, price)
            c.close = price
            c.ticks += 1
            if cumulative_volume is not None:
                c.last_volume = cumulative_volume
                if c.first_volume is None:
                    c.first_volume = cumulative_volume
        return out

    def flush_until(self, now: datetime) -> list[Candle]:
        if self._cur is not None and now >= self._cur.start + timedelta(seconds=self.interval) + self.grace:
            return [self._close()]
        return []

    def _close(self) -> Candle:
        c = self._cur
        assert c is not None
        flags = set(c.flags)
        if not c.observed_from_start:
            flags.add("PARTIAL_OBSERVATION")
        volume = None
        if c.first_volume is not None and c.last_volume is not None:
            volume = max(c.last_volume - c.first_volume, 0.0)
        candle = Candle(
            start=c.start, interval_seconds=self.interval, open=c.open, high=c.high, low=c.low, close=c.close,
            volume=volume, complete=not flags, flags=tuple(sorted(flags)),
        )
        self._last_closed_start = c.start
        self._cur = None
        return candle
