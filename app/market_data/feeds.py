"""Market-data feeds.

* ``KiteTickerFeed`` - Zerodha streaming (KiteTicker / Twisted, runs in its own thread).
  Callbacks are handed to the asyncio loop with ``call_soon_threadsafe``; calls into the
  ticker are marshalled onto the reactor thread with ``reactor.callFromThread``.
* ``ReplayFeed`` - replays a stored historical dataset as ticks (O, H/L, L/H, C per bar)
  on a simulated clock, for testing paper sessions outside market hours. Replay ticks
  are labelled ``REPLAY`` and carry no bid/ask.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.timeutil import ist_datetime, utcnow
from app.database.models import Candle as CandleRow, Instrument
from app.database.session import SessionLocal
from app.instruments.instrument_master import to_ref
from app.market_data.kite_client import kite_sessions
from app.market_data.tick_processor import NormalizedTick, normalize_kite_quote, normalize_kite_tick
from app.trading.types import Quote

if TYPE_CHECKING:
    from app.market_data.hub import MarketDataHub

logger = logging.getLogger(__name__)


class KiteTickerFeed:
    source = "KITE"

    def __init__(self, hub: MarketDataHub, loop: asyncio.AbstractEventLoop) -> None:
        self.hub = hub
        self.loop = loop
        self.ticker: Any = None
        self.tokens: set[int] = set()
        self.connected = False

    def now(self) -> datetime:
        return utcnow()

    def start(self) -> None:
        from kiteconnect import KiteTicker
        from twisted.internet import reactor

        api_key, access_token = kite_sessions.ticker_credentials()
        ticker = KiteTicker(api_key, access_token, reconnect=True, reconnect_max_tries=300, reconnect_max_delay=30)
        ticker.on_connect = self._on_connect
        ticker.on_ticks = self._on_ticks
        ticker.on_close = self._on_close
        ticker.on_error = self._on_error
        ticker.on_reconnect = self._on_reconnect
        ticker.on_noreconnect = self._on_noreconnect
        self.ticker = ticker
        if reactor.running:
            reactor.callFromThread(ticker.connect, threaded=True)
        else:
            ticker.connect(threaded=True)

    def stop(self) -> None:
        from twisted.internet import reactor

        if self.ticker is not None:
            ticker, self.ticker = self.ticker, None
            ticker.on_close = None
            ticker.on_reconnect = None
            ticker.on_noreconnect = None
            reactor.callFromThread(ticker.close)
        self.connected = False

    def subscribe(self, tokens: set[int]) -> None:
        new = {t for t in tokens if t is not None} - self.tokens
        if not new:
            return
        self.tokens |= new
        if self.connected and self.ticker is not None:
            from twisted.internet import reactor

            reactor.callFromThread(self._subscribe_on_reactor, sorted(new))

    def snapshot_quote(self, key: str) -> Quote | None:
        """REST quote (market data only) for an instrument without a streamed quote yet."""
        try:
            payload = kite_sessions.client().quote([key]).get(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("REST quote for %s failed: %s", key, exc)
            return None
        return normalize_kite_quote(payload, key, utcnow())

    def _subscribe_on_reactor(self, tokens: list[int]) -> None:
        if self.ticker is None:
            return
        self.ticker.subscribe(tokens)
        self.ticker.set_mode(self.ticker.MODE_FULL, tokens)

    # -- callbacks (reactor thread) --------------------------------------------------------
    def _on_connect(self, ws: Any, response: Any) -> None:
        self.connected = True
        if self.tokens:
            self._subscribe_on_reactor(sorted(self.tokens))
        self.loop.call_soon_threadsafe(self.hub.feed_status, "CONNECTED", "Kite ticker connected", None)

    def _on_ticks(self, ws: Any, ticks: list[dict[str, Any]]) -> None:
        receive_ts = utcnow()
        normalized = []
        for raw in ticks:
            key = self.hub.token_to_key.get(raw.get("instrument_token"))
            if key is None:
                continue
            inst = self.hub.instruments.get(key)
            normalized.append(normalize_kite_tick(raw, key, inst.instrument_id if inst else None, receive_ts))
        if normalized:
            self.loop.call_soon_threadsafe(self.hub.feed_ticks, normalized)

    def _on_close(self, ws: Any, code: Any, reason: Any) -> None:
        self.connected = False
        self.loop.call_soon_threadsafe(self.hub.feed_status, "DISCONNECTED", f"Kite ticker closed: {code} {reason}", None)

    def _on_error(self, ws: Any, code: Any, reason: Any) -> None:
        self.loop.call_soon_threadsafe(self.hub.feed_status, "ERROR", f"Kite ticker error: {code} {reason}", None)

    def _on_reconnect(self, ws: Any, attempts: int) -> None:
        self.connected = False
        self.loop.call_soon_threadsafe(self.hub.feed_status, "RECONNECTING", f"Reconnecting (attempt {attempts})", {"attempts": attempts})

    def _on_noreconnect(self, ws: Any) -> None:
        self.connected = False
        self.loop.call_soon_threadsafe(self.hub.feed_status, "DISCONNECTED", "Kite ticker gave up reconnecting", None)


def replay_schedule(interval_seconds: int) -> list[tuple[float, int]]:
    """(offset seconds into the bar, index into the O/extreme/extreme/C path) for replay ticks.

    Ticks are emitted every ~5 seconds so replayed data never looks stale; the path visits
    open, the first extreme, the second extreme and close, in that order.
    """
    n = max(1, min(12, interval_seconds // 5))
    if n == 1:
        return [(0.0, 3)]
    idx = [min(3, (4 * k) // n) for k in range(n)]
    idx[-1] = 3
    return [(k * interval_seconds / n, idx[k]) for k in range(n)]


class ReplayFeed:
    source = "REPLAY"

    def __init__(self, hub: MarketDataHub, dataset_id: int, start: date | None, end: date | None, speed: float) -> None:
        self.hub = hub
        self.dataset_id = dataset_id
        self.start_date = start
        self.end_date = end
        self.speed = max(speed, 0.1)
        self.clock: datetime = utcnow()
        self.task: asyncio.Task[None] | None = None
        self.progress = 0.0
        self.interval = 60
        self._bars_now: dict[str, tuple[float, float, float, float]] = {}
        self._path_index = 0

    def now(self) -> datetime:
        return self.clock

    def load(self) -> dict[datetime, list[tuple[str, float, float, float, float]]]:
        with SessionLocal() as db:
            instruments = db.scalars(select(Instrument).where(Instrument.dataset_id == self.dataset_id)).all()
            refs = {i.id: to_ref(i) for i in instruments}
            self.hub.register_instruments(list(refs.values()))
            from app.database.models import HistoricalDataset

            ds = db.get(HistoricalDataset, self.dataset_id)
            if ds is None:
                raise ValueError(f"Dataset {self.dataset_id} not found")
            self.interval = ds.interval_seconds
            q = select(CandleRow.instrument_id, CandleRow.start_time, CandleRow.open, CandleRow.high, CandleRow.low,
                       CandleRow.close).where(CandleRow.dataset_id == self.dataset_id)
            if self.start_date:
                q = q.where(CandleRow.start_time >= ist_datetime(self.start_date, time(0, 0)))
            if self.end_date:
                q = q.where(CandleRow.start_time < ist_datetime(self.end_date + timedelta(days=1), time(0, 0)))
            grouped: dict[datetime, list[tuple[str, float, float, float, float]]] = defaultdict(list)
            for inst_id, ts, o, h, l, c in db.execute(q):
                grouped[ts].append((refs[inst_id].key, o, h, l, c))
        if not grouped:
            raise ValueError("Selected dataset range contains no bars")
        return grouped

    def start(self) -> None:
        grouped = self.load()
        self.clock = min(grouped)
        self.task = asyncio.get_running_loop().create_task(self._run(grouped))

    def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()

    def subscribe(self, tokens: set[int]) -> None:
        pass  # replay emits for subscribed instrument keys; no network subscription needed

    def snapshot_quote(self, key: str) -> Quote | None:
        """Price of ``key`` at the current replay instant (the replay analogue of a REST quote)."""
        bar = self._bars_now.get(key)
        if bar is None:
            return None
        o, h, l, c = bar
        path = (o, l, h, c) if c >= o else (o, h, l, c)
        return Quote(key, self.clock, ltp=path[self._path_index], source="REPLAY")

    async def _advance(self, t: datetime) -> None:
        gap = (t - self.clock).total_seconds()
        if gap > 0:
            await asyncio.sleep(min(gap, 120) / self.speed)
        self.clock = t

    async def _run(self, grouped: dict[datetime, list[tuple[str, float, float, float, float]]]) -> None:
        stamps = sorted(grouped)
        schedule = replay_schedule(self.interval)
        bar_span = timedelta(seconds=self.interval)
        self.hub.feed_status("CONNECTED", f"Replaying dataset {self.dataset_id} at {self.speed:g}x", {"bars": len(stamps)})
        try:
            for i, ts in enumerate(stamps):
                bars = grouped[ts]
                for offset, p_index in schedule:
                    await self._advance(ts + timedelta(seconds=offset))
                    self._bars_now = {key: (o, h, l, c) for key, o, h, l, c in bars}
                    self._path_index = p_index
                    ticks = []
                    for key, o, h, l, c in bars:
                        if key not in self.hub.subscribed_keys:
                            continue
                        path = (o, l, h, c) if c >= o else (o, h, l, c)
                        inst = self.hub.instruments.get(key)
                        ticks.append(NormalizedTick(key, inst.instrument_id if inst else None, self.clock, self.clock,
                                                    path[p_index], source="REPLAY"))
                    self.hub.feed_ticks(ticks)
                    self.hub.on_clock()
                nxt = stamps[i + 1] if i + 1 < len(stamps) else None
                if nxt is None or nxt - ts > bar_span:
                    # Data pause (end of day / gap): let the clock pass the bar end so open candles close on time.
                    self._bars_now = {}
                    await self._advance(ts + bar_span + timedelta(seconds=self.hub.settings.candle_close_grace_seconds + 0.5))
                    self.hub.on_clock()
                self.progress = (i + 1) / len(stamps)
            self.hub.feed_status("ENDED", "Replay finished", None)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("Replay failed")
            self.hub.feed_status("ERROR", f"Replay failed: {exc}", None)
