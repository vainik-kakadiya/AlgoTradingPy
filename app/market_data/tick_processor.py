"""Normalisation of raw feed messages into a single internal tick schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.timeutil import IST, ensure_utc
from app.trading.types import Quote


@dataclass
class NormalizedTick:
    instrument_key: str
    instrument_id: int | None
    exchange_ts: datetime | None  # exchange timestamp (UTC) when provided by the feed
    receive_ts: datetime  # local receive time (UTC; replay clock in replay mode)
    ltp: float | None
    bid: float | None = None
    ask: float | None = None
    bid_qty: float | None = None
    ask_qty: float | None = None
    volume: float | None = None  # cumulative day volume
    last_quantity: float | None = None
    oi: float | None = None
    source: str = "KITE"
    flags: list[str] = field(default_factory=list)

    @property
    def ts(self) -> datetime:
        """Event time used for candles: exchange time when available, else receive time."""
        return self.exchange_ts or self.receive_ts

    def to_quote(self) -> Quote:
        return Quote(self.instrument_key, self.ts, ltp=self.ltp, bid=self.bid, ask=self.ask, volume=self.volume, source=self.source)


def _ist_naive_to_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=IST)  # Kite sends naive IST timestamps
    return ensure_utc(value)


def normalize_kite_tick(raw: dict[str, Any], key: str, instrument_id: int | None, receive_ts: datetime) -> NormalizedTick:
    flags: list[str] = []
    exchange_ts = _ist_naive_to_utc(raw.get("exchange_timestamp")) or _ist_naive_to_utc(raw.get("last_trade_time"))
    if exchange_ts is None:
        flags.append("NO_EXCHANGE_TS")
    bid = ask = bid_qty = ask_qty = None
    depth = raw.get("depth") or {}
    buys, sells = depth.get("buy") or [], depth.get("sell") or []
    if buys and buys[0].get("price"):
        bid, bid_qty = float(buys[0]["price"]), float(buys[0].get("quantity") or 0)
    if sells and sells[0].get("price"):
        ask, ask_qty = float(sells[0]["price"]), float(sells[0].get("quantity") or 0)
    ltp = raw.get("last_price")
    if ltp is not None and ltp <= 0:
        flags.append("NON_POSITIVE_LTP")
        ltp = None
    if exchange_ts and (receive_ts - exchange_ts).total_seconds() > 5:
        flags.append("DELAYED")
    return NormalizedTick(
        instrument_key=key,
        instrument_id=instrument_id,
        exchange_ts=exchange_ts,
        receive_ts=receive_ts,
        ltp=float(ltp) if ltp is not None else None,
        bid=bid,
        ask=ask,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        volume=raw.get("volume_traded"),
        last_quantity=raw.get("last_traded_quantity"),
        oi=raw.get("oi"),
        source="KITE",
        flags=flags,
    )


def normalize_kite_quote(payload: dict[str, Any], key: str, receive_ts: datetime) -> Quote | None:
    """Normalise a REST ``quote()`` entry into a Quote."""
    if not payload:
        return None
    depth = payload.get("depth") or {}
    buys, sells = depth.get("buy") or [], depth.get("sell") or []
    bid = float(buys[0]["price"]) if buys and buys[0].get("price") else None
    ask = float(sells[0]["price"]) if sells and sells[0].get("price") else None
    ts = _ist_naive_to_utc(payload.get("timestamp")) or receive_ts
    return Quote(key, ts, ltp=payload.get("last_price"), bid=bid, ask=ask, volume=payload.get("volume"), source="KITE_REST")
