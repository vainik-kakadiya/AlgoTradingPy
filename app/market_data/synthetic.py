"""SYNTHETIC data generator for tests and demos.

Generated data is NOT market data. Datasets built from it are flagged ``is_synthetic`` and
every report built on them carries a prominent warning. It exists so the full pipeline can
be exercised before real historical data has been imported.

Index: 1-minute random walk with occasional injected 3-minute momentum bursts.
Options: Black-Scholes prices at a fixed implied volatility for weekly expiries.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any

from app.core.timeutil import ist_datetime

MONTH_CODES = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9", 10: "O", 11: "N", 12: "D"}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot: float, strike: float, years: float, vol: float, rate: float, is_call: bool) -> float:
    years = max(years, 1 / (365 * 24))
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / (vol * math.sqrt(years))
    d2 = d1 - vol * math.sqrt(years)
    if is_call:
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * years) * _norm_cdf(d2)
    return strike * math.exp(-rate * years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _tick(p: float, tick: float = 0.05) -> float:
    return max(tick, round(round(p / tick) * tick, 2))


def weekly_symbol(underlying: str, expiry: date, strike: float, opt: str) -> str:
    return f"{underlying}{expiry:%y}{MONTH_CODES[expiry.month]}{expiry:%d}{int(strike)}{opt}"


@dataclass
class SyntheticConfig:
    start_date: date = date(2025, 1, 6)
    days: int = 5
    start_spot: float = 24000.0
    minute_vol_points: float = 12.0
    burst_probability: float = 0.04  # per 3-minute bucket
    burst_points: tuple[float, float] = (130.0, 220.0)
    strike_step: int = 50
    strikes_each_side: int = 10
    implied_vol: float = 0.13
    rate: float = 0.065
    lot_size: int = 65
    expiry_weekday: int = 1  # Tuesday
    seed: int = 42


def _trading_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _next_expiry(d: date, weekday: int) -> date:
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def generate(cfg: SyntheticConfig | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (instruments, bars). Bars have timestamp (aware UTC), tradingsymbol, exchange, OHLC, volume."""
    cfg = cfg or SyntheticConfig()
    rng = random.Random(cfg.seed)
    instruments: dict[str, dict[str, Any]] = {
        "NIFTY 50": {
            "tradingsymbol": "NIFTY 50", "exchange": "NSE", "segment": "INDICES", "instrument_type": "INDEX",
            "underlying": "NIFTY", "strike": None, "expiry": None, "lot_size": 1, "tick_size": 0.05, "listed_from": None,
        }
    }
    bars: list[dict[str, Any]] = []
    spot = cfg.start_spot
    for day in _trading_days(cfg.start_date, cfg.days):
        expiry = _next_expiry(day, cfg.expiry_weekday)
        atm = round(spot / cfg.strike_step) * cfg.strike_step
        strikes = [atm + k * cfg.strike_step for k in range(-cfg.strikes_each_side, cfg.strikes_each_side + 1)]
        contracts = []
        for k in strikes:
            for opt in ("CE", "PE"):
                sym = weekly_symbol("NIFTY", expiry, k, opt)
                if sym not in instruments:
                    instruments[sym] = {
                        "tradingsymbol": sym, "exchange": "NFO", "segment": "NFO-OPT", "instrument_type": opt,
                        "underlying": "NIFTY", "strike": float(k), "expiry": expiry, "lot_size": cfg.lot_size,
                        "tick_size": 0.05, "listed_from": None,
                    }
                contracts.append((sym, float(k), opt == "CE"))
        expiry_close = ist_datetime(expiry, time(15, 30))
        burst_left, burst_step = 0, 0.0
        for minute in range(375):
            ts = ist_datetime(day, time(9, 15)) + timedelta(minutes=minute)
            if minute % 3 == 0 and burst_left == 0 and rng.random() < cfg.burst_probability:
                total = rng.uniform(*cfg.burst_points) * rng.choice((-1, 1))
                burst_left, burst_step = 3, total / 3
            move = rng.gauss(0, cfg.minute_vol_points) + (burst_step if burst_left else 0.0)
            if burst_left:
                burst_left -= 1
            o = spot
            c = spot + move
            wick = abs(rng.gauss(0, cfg.minute_vol_points * 0.4))
            h = max(o, c) + wick
            l = min(o, c) - abs(rng.gauss(0, cfg.minute_vol_points * 0.4))
            spot = c
            bars.append({"timestamp": ts, "tradingsymbol": "NIFTY 50", "exchange": "NSE",
                         "open": round(o, 2), "high": round(h, 2), "low": round(l, 2), "close": round(c, 2), "volume": None})
            years_open = (expiry_close - ts).total_seconds() / (365 * 86400)
            years_close = years_open - 60 / (365 * 86400)
            for sym, k, is_call in contracts:
                po = bs_price(o, k, years_open, cfg.implied_vol, cfg.rate, is_call)
                pc = bs_price(c, k, years_close, cfg.implied_vol, cfg.rate, is_call)
                p_hi = bs_price(h if is_call else l, k, years_open, cfg.implied_vol, cfg.rate, is_call)
                p_lo = bs_price(l if is_call else h, k, years_close, cfg.implied_vol, cfg.rate, is_call)
                oo, cc = _tick(po), _tick(pc)
                bars.append({
                    "timestamp": ts, "tradingsymbol": sym, "exchange": "NFO",
                    "open": oo, "high": max(oo, cc, _tick(p_hi)), "low": min(oo, cc, _tick(p_lo)), "close": cc,
                    "volume": float(rng.randint(500, 50000)),
                })
    return list(instruments.values()), bars


def to_csv_rows(instruments: list[dict[str, Any]], bars: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    inst_rows = [{**i, "expiry": i["expiry"].isoformat() if i["expiry"] else ""} for i in instruments]
    bar_rows = [{**b, "timestamp": b["timestamp"].isoformat(), "volume": "" if b["volume"] is None else b["volume"]} for b in bars]
    return inst_rows, bar_rows
