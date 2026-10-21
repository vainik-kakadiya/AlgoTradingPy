"""Chronological replay of historical bars.

The timeline is the sorted union of bar start times across the underlying and all
contracts. At each timestamp ``t``:

1. bars of *traded* contracts starting at ``t`` are processed (pending entries, time exits,
   stop/target evaluation within the bar);
2. the underlying bar ``[t, t+i)`` is closed at ``t+i``; finished strategy candles are
   evaluated and new entries are filled at the open of the contract's bar starting at
   ``t+i`` (the "next candle").

Nothing that happens after the decision time is visible at that time.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, time

from app.core.timeutil import ist_time
from app.trading.fill_model import bar_open_quote
from app.trading.types import Candle, InstrumentRef, Quote


@dataclass
class ReplayData:
    underlying_key: str
    base_interval_seconds: int
    underlying_bars: dict[datetime, Candle]
    contract_bars: dict[str, dict[datetime, Candle]]
    instruments: list[InstrumentRef]

    def timeline(self, session_open: time, session_close: time) -> list[datetime]:
        stamps: set[datetime] = set(self.underlying_bars)
        for bars in self.contract_bars.values():
            stamps.update(bars)
        return sorted(t for t in stamps if session_open <= ist_time(t) < session_close)


class BarQuoteSource:
    """Quote at decision time ``now`` = the open of the contract's bar starting at ``now``."""

    def __init__(self, contract_bars: dict[str, dict[datetime, Candle]]) -> None:
        self.contract_bars = contract_bars

    def get_quote(self, instrument: InstrumentRef, now: datetime) -> Quote | None:
        bar = self.contract_bars.get(instrument.key, {}).get(now)
        return bar_open_quote(instrument.key, bar) if bar is not None else None

    def data_freshness(self, now: datetime) -> tuple[bool, str | None]:
        # Historical freshness is enforced through candle completeness flags.
        return True, None


def iterate(timeline: list[datetime]) -> Iterator[tuple[int, datetime]]:
    yield from enumerate(timeline)
