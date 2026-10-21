"""MarketDataHub: one place where feed data is normalised, quality-checked, turned into
candles, persisted and dispatched to paper sessions and the dashboard.

All hub methods run on the asyncio event loop (feeds marshal their callbacks onto it),
so session runners are invoked sequentially and never concurrently.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import date, datetime
from typing import Any, Protocol

from sqlalchemy import insert

from app.core.config import get_settings
from app.core.timeutil import utcnow
from app.database.models import Candle as CandleRow, MarketTick
from app.database.session import session_scope
from app.market_data.candle_builder import TickCandleBuilder
from app.market_data.data_quality import FeedHealth
from app.market_data.feeds import KiteTickerFeed, ReplayFeed
from app.market_data.tick_processor import NormalizedTick
from app.services.event_bus import bus, record_event
from app.trading.types import Candle, InstrumentRef, Quote

logger = logging.getLogger(__name__)


class HubListener(Protocol):
    def on_quote(self, quote: Quote, now: datetime) -> None: ...
    def on_candle(self, instrument_key: str, candle: Candle, now: datetime) -> None: ...
    def on_time(self, now: datetime) -> None: ...


class MarketDataHub:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.status = "DISCONNECTED"
        self.status_message: str | None = None
        self.source: str | None = None
        self.feed: KiteTickerFeed | ReplayFeed | None = None
        self.instruments: dict[str, InstrumentRef] = {}
        self.token_to_key: dict[int, str] = {}
        self.latest: dict[str, Quote] = {}
        self.subscribers: dict[str, set[int]] = {}  # instrument key -> owner ids
        self.intervals: dict[str, set[int]] = {}
        self.builders: dict[tuple[str, int], TickCandleBuilder] = {}
        self.listeners: list[HubListener] = []
        self.health = FeedHealth(self.settings.feed_stale_seconds)
        self.connected_at: datetime | None = None
        self.last_tick_receive: datetime | None = None
        self.ticks_received = 0
        self.max_batch_latency_ms = 0.0
        self._tick_buffer: list[dict[str, Any]] = []
        self._candle_buffer: list[dict[str, Any]] = []
        self._timer: asyncio.Task[None] | None = None
        self._last_publish = 0.0
        self._last_flush = 0.0

    # ------------------------------------------------------------------ lifecycle
    @property
    def subscribed_keys(self) -> set[str]:
        return set(self.subscribers)

    def now(self) -> datetime:
        return self.feed.now() if self.feed is not None else utcnow()

    async def start(self, source: str, dataset_id: int | None = None, start: date | None = None,
                    end: date | None = None, speed: float = 60.0) -> None:
        await self.stop("Restarting feed")
        loop = asyncio.get_running_loop()
        self.source = source
        self.health = FeedHealth(self.settings.feed_stale_seconds)
        self.latest.clear()
        for builder in self.builders.values():
            builder.mark_gap("FEED_RESTART")
        if source == "KITE":
            self.feed = KiteTickerFeed(self, loop)
            self.status = "CONNECTING"
            self.feed.start()
            self.feed.subscribe({i.instrument_token for k, i in self.instruments.items() if k in self.subscribers and i.instrument_token})
        elif source == "REPLAY":
            if dataset_id is None:
                raise ValueError("Replay requires a dataset_id")
            self.feed = ReplayFeed(self, dataset_id, start, end, speed)
            self.status = "CONNECTING"
            self.feed.start()
        else:
            raise ValueError(f"Unknown feed source {source}")
        if self._timer is None or self._timer.done():
            self._timer = loop.create_task(self._timer_loop())
        record_event("INFO", "FEED_STARTING", "market_data", f"Starting {source} feed", {"dataset_id": dataset_id, "speed": speed})

    async def stop(self, reason: str = "Stopped by user") -> None:
        if self.feed is not None:
            feed, self.feed = self.feed, None
            feed.stop()
            for builder in self.builders.values():
                builder.mark_gap("FEED_STOPPED")
            self.status = "DISCONNECTED"
            self.status_message = reason
            record_event("INFO", "FEED_STOPPED", "market_data", reason)
            self._flush_buffers()
            self._publish_status(force=True)

    async def shutdown(self) -> None:
        await self.stop("Backend shutting down")
        if self._timer is not None:
            self._timer.cancel()

    # ------------------------------------------------------------------ subscriptions
    def register_instruments(self, refs: list[InstrumentRef]) -> None:
        for ref in refs:
            self.instruments[ref.key] = ref
            if ref.instrument_token is not None:
                self.token_to_key[ref.instrument_token] = ref.key

    def subscribe(self, owner: int, inst: InstrumentRef, intervals: set[int] | None = None) -> None:
        self.register_instruments([inst])
        self.subscribers.setdefault(inst.key, set()).add(owner)
        wanted = set(intervals or set())
        if self.settings.capture_second_bars and self.source == "KITE":
            wanted.add(1)
        for interval in wanted:
            if (inst.key, interval) not in self.builders:
                builder = TickCandleBuilder(interval, grace_seconds=self.settings.candle_close_grace_seconds)
                self.builders[(inst.key, interval)] = builder
            self.intervals.setdefault(inst.key, set()).add(interval)
        if self.feed is not None and inst.instrument_token is not None:
            self.feed.subscribe({inst.instrument_token})

    def unsubscribe_owner(self, owner: int) -> None:
        for key in list(self.subscribers):
            self.subscribers[key].discard(owner)
            if not self.subscribers[key]:
                del self.subscribers[key]

    def add_listener(self, listener: HubListener) -> None:
        if listener not in self.listeners:
            self.listeners.append(listener)

    def remove_listener(self, listener: HubListener) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    # ------------------------------------------------------------------ feed callbacks (event loop)
    def feed_status(self, status: str, message: str, details: dict[str, Any] | None) -> None:
        previous = self.status
        self.status = status
        self.status_message = message
        if status == "CONNECTED":
            self.connected_at = utcnow()
            if previous == "RECONNECTING":
                self.health.reconnects += 1
        if status in ("DISCONNECTED", "RECONNECTING", "ERROR") and previous == "CONNECTED":
            self.health.disconnects += 1
            self.health.disconnected_since = utcnow()
            for builder in self.builders.values():
                builder.mark_gap("FEED_DISCONNECT")
        severity = "INFO" if status in ("CONNECTED", "ENDED") else "WARNING"
        record_event(severity, f"FEED_{status}", "market_data", message, details)
        self._publish_status(force=True)

    def feed_ticks(self, ticks: list[NormalizedTick]) -> None:
        started = _time.perf_counter()
        now = self.now()
        candles: list[tuple[str, Candle]] = []
        valid: list[NormalizedTick] = []
        for tick in ticks:
            if tick.ltp is None:
                self.health.invalid_ticks += 1
                continue
            gap = self.health.on_tick(tick.instrument_key, tick.ts, tick.receive_ts)
            if gap is not None:
                record_event("INFO", "DATA_RESUMED", "market_data", f"{tick.instrument_key} data resumed after {gap:.1f}s", {"gap_seconds": gap})
            self.latest[tick.instrument_key] = tick.to_quote()
            valid.append(tick)
        self.ticks_received += len(valid)
        self.last_tick_receive = utcnow()
        # Latest quotes are updated for the whole batch before candles close, so a signal's
        # entry sees every price that arrived in the same batch.
        for tick in valid:
            for interval in self.intervals.get(tick.instrument_key, ()):
                builder = self.builders[(tick.instrument_key, interval)]
                for candle in builder.on_tick(tick.ts, tick.ltp, tick.volume):  # type: ignore[arg-type]
                    candles.append((tick.instrument_key, candle))
            if self.source == "KITE" and self.settings.capture_ticks and tick.instrument_id is not None:
                self._tick_buffer.append({
                    "instrument_id": tick.instrument_id, "exchange_ts": tick.exchange_ts, "receive_ts": tick.receive_ts,
                    "ltp": tick.ltp, "volume": tick.volume, "last_quantity": tick.last_quantity, "oi": tick.oi,
                    "bid": tick.bid, "ask": tick.ask, "bid_qty": tick.bid_qty, "ask_qty": tick.ask_qty,
                    "source": tick.source, "quality_flags": tick.flags or None,
                })
        for key, candle in candles:
            self._dispatch_candle(key, candle, now)
        for tick in valid:
            quote = self.latest[tick.instrument_key]
            for listener in list(self.listeners):
                try:
                    listener.on_quote(quote, now)
                except Exception:  # noqa: BLE001
                    logger.exception("Listener failed on quote")
        self.max_batch_latency_ms = max(self.max_batch_latency_ms, (_time.perf_counter() - started) * 1000)

    def on_clock(self) -> None:
        """Advance time-driven work: candle closes, staleness checks, session timers."""
        now = self.now()
        for (key, _interval), builder in list(self.builders.items()):
            for candle in builder.flush_until(now):
                self._dispatch_candle(key, candle, now)
        if self.status == "CONNECTED":
            for key in self.health.detect_new_gaps(sorted(self.subscribers), now):
                for (k, _), builder in self.builders.items():
                    if k == key:
                        builder.mark_gap("DATA_GAP")
                record_event("WARNING", "DATA_GAP", "market_data", f"No data for {key} for more than {self.health.stale_seconds:g}s")
        for listener in list(self.listeners):
            try:
                listener.on_time(now)
            except Exception:  # noqa: BLE001
                logger.exception("Listener failed on time")
        mono = _time.monotonic()
        if mono - self._last_flush >= 1.0:
            self._last_flush = mono
            self._flush_buffers()
        self._publish_status()

    def _dispatch_candle(self, key: str, candle: Candle, now: datetime) -> None:
        inst = self.instruments.get(key)
        if self.source == "KITE" and inst is not None and inst.instrument_id is not None:
            self._candle_buffer.append({
                "instrument_id": inst.instrument_id, "interval_seconds": candle.interval_seconds, "start_time": candle.start,
                "open": candle.open, "high": candle.high, "low": candle.low, "close": candle.close, "volume": candle.volume,
                "source": "LIVE", "is_complete": candle.complete, "quality_flags": list(candle.flags) or None,
            })
        if candle.interval_seconds > 1:
            bus.publish("candle", {"instrument_key": key, **candle.to_dict()})
        for listener in list(self.listeners):
            try:
                listener.on_candle(key, candle, now)
            except Exception:  # noqa: BLE001
                logger.exception("Listener failed on candle")

    async def _timer_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            try:
                if self.feed is not None or self.listeners:
                    self.on_clock()
            except Exception:  # noqa: BLE001
                logger.exception("Hub timer error")

    # ------------------------------------------------------------------ persistence & status
    def _flush_buffers(self) -> None:
        ticks, self._tick_buffer = self._tick_buffer, []
        candles, self._candle_buffer = self._candle_buffer, []
        if not ticks and not candles:
            return
        try:
            with session_scope() as db:
                if ticks:
                    db.execute(insert(MarketTick), ticks)
                if candles:
                    db.execute(insert(CandleRow).prefix_with("OR IGNORE"), candles)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist market data (%d ticks, %d candles)", len(ticks), len(candles))

    def snapshot_quote(self, key: str) -> Quote | None:
        """Point-in-time quote from the active feed for an instrument not yet streaming."""
        return self.feed.snapshot_quote(key) if self.feed is not None else None

    def data_freshness(self, key: str, now: datetime, max_age: float) -> tuple[bool, str | None]:
        if self.feed is None or self.status not in ("CONNECTED",):
            return False, f"Market data feed is {self.status.lower()}"
        age = self.health.age(key, now)
        if age is None:
            return False, f"No data received yet for {key}"
        if age > max_age:
            return False, f"Last {key} update {age:.1f}s ago exceeds {max_age:g}s"
        return True, None

    def status_dict(self) -> dict[str, Any]:
        now = self.now()
        per_key = {}
        for key in sorted(self.subscribers):
            q = self.latest.get(key)
            h = self.health.per_key.get(key)
            age = self.health.age(key, now)
            per_key[key] = {
                "ltp": q.ltp if q else None, "bid": q.bid if q else None, "ask": q.ask if q else None,
                "ts": q.ts if q else None, "age_seconds": round(age, 2) if age is not None else None,
                "stale": age is None or age > self.health.stale_seconds, "ticks": h.ticks if h else 0,
                "gaps": h.gaps if h else 0,
            }
        last_ts = max((q.ts for q in self.latest.values()), default=None)
        return {
            "status": self.status,
            "message": self.status_message,
            "source": self.source,
            "clock": now,
            "connected_at": self.connected_at,
            "last_market_ts": last_ts,
            "last_receive_ts": self.last_tick_receive,
            "stale": last_ts is None or (now - last_ts).total_seconds() > self.health.stale_seconds,
            "stale_seconds": self.health.stale_seconds,
            "ticks_received": self.ticks_received,
            "disconnects": self.health.disconnects,
            "reconnects": self.health.reconnects,
            "invalid_ticks": self.health.invalid_ticks,
            "max_batch_latency_ms": round(self.max_batch_latency_ms, 2),
            "replay_progress": getattr(self.feed, "progress", None),
            "subscriptions": per_key,
        }

    def _publish_status(self, force: bool = False) -> None:
        mono = _time.monotonic()
        if not force and mono - self._last_publish < 1.0:
            return
        self._last_publish = mono
        bus.publish("feed", self.status_dict())


hub = MarketDataHub()
