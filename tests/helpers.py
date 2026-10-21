"""Builders for test data."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta
from typing import Any

from app.backtesting.replay import ReplayData
from app.core.timeutil import ist_datetime
from app.market_data.synthetic import SyntheticConfig, generate, to_csv_rows
from app.trading.types import Candle, InstrumentRef

DAY = date(2025, 1, 6)  # a Monday


def at(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return ist_datetime(day, time(hh, mm, ss))


def bar(start: datetime, o: float, h: float, low: float, c: float, interval: int = 60, volume: float | None = None) -> Candle:
    return Candle(start, interval, o, h, low, c, volume)


def option(strike: float, opt: str, expiry: date = date(2025, 1, 7), lot: int = 65) -> InstrumentRef:
    sym = f"NIFTY25107{int(strike)}{opt}"
    return InstrumentRef(key=f"NFO:{sym}", exchange="NFO", tradingsymbol=sym, instrument_type=opt, lot_size=lot,
                         tick_size=0.05, underlying="NIFTY", strike=float(strike), expiry=expiry)


def synthetic_replay(days: int = 5, seed: int = 42, strikes_each_side: int = 10) -> ReplayData:
    insts, bars = generate(SyntheticConfig(days=days, seed=seed, strikes_each_side=strikes_each_side))
    refs = [
        InstrumentRef(key=f"{i['exchange']}:{i['tradingsymbol']}", exchange=i["exchange"], tradingsymbol=i["tradingsymbol"],
                      instrument_type=i["instrument_type"], lot_size=i["lot_size"], tick_size=i["tick_size"],
                      underlying=i["underlying"], strike=i["strike"], expiry=i["expiry"])
        for i in insts if i["instrument_type"] != "INDEX"
    ]
    ub: dict[datetime, Candle] = {}
    cb: dict[str, dict[datetime, Candle]] = {}
    for b in bars:
        c = Candle(b["timestamp"], 60, b["open"], b["high"], b["low"], b["close"], b["volume"])
        if b["tradingsymbol"] == "NIFTY 50":
            ub[b["timestamp"]] = c
        else:
            cb.setdefault(f"NFO:{b['tradingsymbol']}", {})[b["timestamp"]] = c
    return ReplayData("NSE:NIFTY 50", 60, ub, cb, refs)


def synthetic_csv(days: int = 3, seed: int = 7, strikes_each_side: int = 6, start: date = DAY) -> tuple[bytes, bytes]:
    insts, bars = generate(SyntheticConfig(start_date=start, days=days, seed=seed, strikes_each_side=strikes_each_side))
    inst_rows, bar_rows = to_csv_rows(insts, bars)

    def dump(rows: list[dict[str, Any]], fields: list[str]) -> bytes:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})
        return buf.getvalue().encode()

    return (
        dump(bar_rows, ["timestamp", "tradingsymbol", "exchange", "open", "high", "low", "close", "volume"]),
        dump(inst_rows, ["tradingsymbol", "exchange", "segment", "instrument_type", "underlying", "strike", "expiry", "lot_size", "tick_size"]),
    )


def minutes(start: datetime, n: int) -> list[datetime]:
    return [start + timedelta(minutes=i) for i in range(n)]
