"""Indicator calculations over a window of completed candles.

All functions return ``None`` when there is not enough history, so that rule evaluation
can distinguish "condition false" from "cannot be evaluated yet".
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.timeutil import ist_date
from app.trading.types import Candle

SOURCES = ("open", "high", "low", "close", "hl2", "hlc3")


def source_values(candles: Sequence[Candle], source: str) -> list[float]:
    if source == "hl2":
        return [(c.high + c.low) / 2 for c in candles]
    if source == "hlc3":
        return [(c.high + c.low + c.close) / 3 for c in candles]
    return [getattr(c, source) for c in candles]


def sma(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def ema(values: Sequence[float], period: int) -> float | None:
    """EMA seeded with the SMA of the first ``period`` values; needs ``period`` values minimum."""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    value = sum(values[:period]) / period
    for v in values[period:]:
        value = v * k + value * (1 - k)
    return value


def rsi(values: Sequence[float], period: int) -> float | None:
    """Wilder's RSI; needs ``period + 1`` values."""
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def session_vwap(candles: Sequence[Candle]) -> float | None:
    """VWAP of the current IST trading day. Unavailable when volume is missing (e.g. indices)."""
    if not candles:
        return None
    day = ist_date(candles[-1].start)
    pv, vol = 0.0, 0.0
    for c in reversed(candles):
        if ist_date(c.start) != day:
            break
        if c.volume is None:
            return None
        typical = (c.high + c.low + c.close) / 3
        pv += typical * c.volume
        vol += c.volume
    if vol <= 0:
        return None
    return pv / vol


def compute(name: str, candles: Sequence[Candle], period: int | None, source: str) -> float | None:
    if name == "vwap":
        return session_vwap(candles)
    assert period is not None
    values = source_values(candles, source)
    if name == "sma":
        return sma(values, period)
    if name == "ema":
        return ema(values, period)
    if name == "rsi":
        return rsi(values, period)
    raise ValueError(f"Unknown indicator {name}")
