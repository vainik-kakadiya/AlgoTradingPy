"""Execution assumptions used by the simulated broker.

Two observation types are supported:

* Quotes (live ticks / replay ticks): market orders fill at the executable side of the
  quote (ask for buys, bid for sells) plus adverse slippage. When bid/ask are missing the
  last traded price may be used and the fill is flagged ``LTP_FALLBACK``.
* Bars (historical OHLC): market orders fill at the bar open plus adverse slippage.
  Stops that gap through fill at the open, not at the stop price. Limit orders require
  the price to trade *through* the limit by default (a touch is not assumed to fill).
  When a stop and a target are both reachable inside the same bar and the bar does not
  reveal which came first, the stop is assumed to fill first and the fill is flagged
  ``AMBIGUOUS_INTRABAR``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.trading.costs import SlippageConfig
from app.trading.types import Candle, Quote, Side

BAR_OPEN_SOURCE = "BAR_OPEN"
# Used only for forced closes (session stop / end of data) when no fresh quote exists.
LAST_KNOWN_SOURCE = "LAST_KNOWN_PRICE"
_PRICE_ONLY_SOURCES = {BAR_OPEN_SOURCE: "BAR_OPEN_FILL", LAST_KNOWN_SOURCE: "FORCED_AT_LAST_KNOWN_PRICE"}


def bar_open_quote(instrument_key: str, bar: Candle) -> Quote:
    """The quote observable at a historical bar's start: its opening price."""
    return Quote(instrument_key=instrument_key, ts=bar.start, ltp=bar.open, volume=bar.volume, source=BAR_OPEN_SOURCE)


class ExecutionModelConfig(BaseModel):
    price_source: Literal["BID_ASK", "LTP"] = "BID_ASK"
    allow_ltp_fallback: bool = True
    max_quote_age_seconds: float = Field(5.0, gt=0)
    limit_fill_rule: Literal["TRADE_THROUGH", "TOUCH"] = "TRADE_THROUGH"
    intrabar_conflict: Literal["STOP_FIRST"] = "STOP_FIRST"
    slippage: SlippageConfig = Field(default_factory=SlippageConfig)

    def describe(self) -> list[str]:
        return [
            f"Market orders fill at {'ask/bid' if self.price_source == 'BID_ASK' else 'last traded price'}"
            + (" (LTP fallback when bid/ask unavailable)" if self.allow_ltp_fallback and self.price_source == "BID_ASK" else ""),
            "On bar data, market orders fill at the open of the next available bar.",
            f"Adverse slippage per market/stop fill: {self.slippage.points} points + {self.slippage.pct}% of price.",
            "Stops that gap through fill at the gap price (bar open / current quote), not the stop price.",
            f"Limit orders require the price to {'trade through' if self.limit_fill_rule == 'TRADE_THROUGH' else 'touch'} the limit; fills at the limit price.",
            "When stop and target are both inside one bar, the stop is assumed first (flagged AMBIGUOUS_INTRABAR).",
            f"Quotes older than {self.max_quote_age_seconds}s are treated as stale and cannot be filled against.",
        ]


@dataclass
class FillDecision:
    reference_price: float
    fill_price: float
    flags: list[str] = field(default_factory=list)


def round_to_tick(price: float, tick: float, mode: Literal["nearest", "up", "down"] = "nearest") -> float:
    if tick <= 0:
        return price
    units = price / tick
    if mode == "up":
        n = math.ceil(units - 1e-9)
    elif mode == "down":
        n = math.floor(units + 1e-9)
    else:
        n = round(units)
    return round(n * tick, 10)


def _apply_slippage(side: Side, reference: float, cfg: ExecutionModelConfig, tick: float) -> tuple[float, list[str]]:
    slip = cfg.slippage.amount(reference)
    flags: list[str] = []
    if side is Side.BUY:
        fill = round_to_tick(reference + slip, tick, "up")
    else:
        fill = round_to_tick(reference - slip, tick, "down")
        if fill < tick:
            fill = tick
            flags.append("FILL_FLOORED_AT_TICK")
    return fill, flags


def executable_price(side: Side, quote: Quote, cfg: ExecutionModelConfig) -> tuple[float | None, list[str]]:
    """Price at which ``side`` could execute against ``quote``."""
    if quote.source in _PRICE_ONLY_SOURCES:
        # Historical bar open, or a forced close at the last known price: a single price, no book.
        return (quote.ltp, [_PRICE_ONLY_SOURCES[quote.source]]) if quote.ltp and quote.ltp > 0 else (None, [])
    if cfg.price_source == "BID_ASK":
        price = quote.ask if side is Side.BUY else quote.bid
        if price is not None and price > 0:
            return price, []
        if cfg.allow_ltp_fallback and quote.ltp is not None and quote.ltp > 0:
            return quote.ltp, ["LTP_FALLBACK"]
        return None, []
    if quote.ltp is not None and quote.ltp > 0:
        return quote.ltp, []
    return None, []


def quote_age_ok(quote: Quote, now: datetime, cfg: ExecutionModelConfig) -> bool:
    return (now - quote.ts).total_seconds() <= cfg.max_quote_age_seconds


def market_fill_quote(side: Side, quote: Quote, now: datetime, cfg: ExecutionModelConfig, tick: float) -> FillDecision | str:
    if not quote_age_ok(quote, now, cfg):
        return "STALE_QUOTE"
    ref, flags = executable_price(side, quote, cfg)
    if ref is None:
        return "NO_EXECUTABLE_PRICE"
    fill, more = _apply_slippage(side, ref, cfg, tick)
    return FillDecision(ref, fill, flags + more + ([] if quote.source in _PRICE_ONLY_SOURCES else ["QUOTE_FILL"]))


def stop_fill_quote(side: Side, trigger: float, quote: Quote, now: datetime, cfg: ExecutionModelConfig, tick: float) -> FillDecision | None:
    """Stop-market: triggers when the executable price reaches the trigger, then fills like a market order."""
    if not quote_age_ok(quote, now, cfg):
        return None
    ref, flags = executable_price(side, quote, cfg)
    if ref is None:
        return None
    triggered = ref <= trigger if side is Side.SELL else ref >= trigger
    if not triggered:
        return None
    gap = ref < trigger if side is Side.SELL else ref > trigger
    fill, more = _apply_slippage(side, ref, cfg, tick)
    return FillDecision(ref, fill, flags + more + ["QUOTE_FILL"] + (["GAP_THROUGH_STOP"] if gap else []))


def limit_fill_quote(side: Side, limit: float, quote: Quote, now: datetime, cfg: ExecutionModelConfig) -> FillDecision | None:
    if not quote_age_ok(quote, now, cfg):
        return None
    has_book = cfg.price_source == "BID_ASK" and (quote.ask if side is Side.BUY else quote.bid)
    if has_book:
        price = quote.ask if side is Side.BUY else quote.bid
        assert price is not None
        marketable = price <= limit if side is Side.BUY else price >= limit
        if marketable:
            # The opposite side of the book is at or better than the limit: executable at that price.
            return FillDecision(price, price, ["QUOTE_FILL", "LIMIT_MARKETABLE"])
        return None
    if quote.ltp is None or quote.ltp <= 0:
        return None
    if cfg.limit_fill_rule == "TRADE_THROUGH":
        crossed = quote.ltp < limit if side is Side.BUY else quote.ltp > limit
    else:
        crossed = quote.ltp <= limit if side is Side.BUY else quote.ltp >= limit
    if crossed:
        return FillDecision(limit, limit, ["LTP_FALLBACK", f"LIMIT_{cfg.limit_fill_rule}"])
    return None


def market_fill_bar(side: Side, bar: Candle, cfg: ExecutionModelConfig, tick: float) -> FillDecision:
    fill, flags = _apply_slippage(side, bar.open, cfg, tick)
    return FillDecision(bar.open, fill, flags + ["BAR_OPEN_FILL"])


def stop_fill_bar(side: Side, trigger: float, bar: Candle, cfg: ExecutionModelConfig, tick: float) -> FillDecision | None:
    if side is Side.SELL:
        if bar.open <= trigger:
            ref, flags = bar.open, ["GAP_THROUGH_STOP"]
        elif bar.low <= trigger:
            ref, flags = trigger, []
        else:
            return None
    else:
        if bar.open >= trigger:
            ref, flags = bar.open, ["GAP_THROUGH_STOP"]
        elif bar.high >= trigger:
            ref, flags = trigger, []
        else:
            return None
    fill, more = _apply_slippage(side, ref, cfg, tick)
    return FillDecision(ref, fill, flags + more + ["BAR_FILL"])


def limit_fill_bar(side: Side, limit: float, bar: Candle, cfg: ExecutionModelConfig) -> FillDecision | None:
    through = cfg.limit_fill_rule == "TRADE_THROUGH"
    if side is Side.SELL:
        crossed = bar.high > limit if through else bar.high >= limit
        at_open = bar.open > limit if through else bar.open >= limit
    else:
        crossed = bar.low < limit if through else bar.low <= limit
        at_open = bar.open < limit if through else bar.open <= limit
    if not crossed:
        return None
    flags = ["BAR_FILL", f"LIMIT_{cfg.limit_fill_rule}"]
    if at_open:
        flags.append("GAP_FILLED_AT_LIMIT")  # conservative: price improvement from the gap is ignored
    return FillDecision(limit, limit, flags)


def stop_triggered_at_open(side: Side, trigger: float, bar: Candle) -> bool:
    return bar.open <= trigger if side is Side.SELL else bar.open >= trigger
