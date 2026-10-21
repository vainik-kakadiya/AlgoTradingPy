"""Domain types shared by the strategy engine, backtester and paper broker."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1

    @property
    def entry_side(self) -> Side:
        return Side.BUY if self is Direction.LONG else Side.SELL


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"  # stop-market (SL-M): becomes a market order once the trigger is hit


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED)


class OrderPurpose(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class ExitReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    SESSION_END = "SESSION_END"
    TIME_EXIT = "TIME_EXIT"
    EXIT_RULE = "EXIT_RULE"
    SESSION_STOPPED = "SESSION_STOPPED"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    RISK_EXIT = "RISK_EXIT"
    END_OF_DATA = "END_OF_DATA"


class SignalAction(str, Enum):
    BUY_CALL = "BUY_CALL"
    BUY_PUT = "BUY_PUT"
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Candle:
    start: datetime
    interval_seconds: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    complete: bool = True
    flags: tuple[str, ...] = ()

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.interval_seconds)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return self.close - self.open

    @property
    def abs_body(self) -> float:
        return abs(self.close - self.open)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "interval_seconds": self.interval_seconds,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "complete": self.complete,
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class Quote:
    """A point-in-time price observation for one instrument."""

    instrument_key: str
    ts: datetime
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None
    source: str = "UNKNOWN"


@dataclass(frozen=True)
class InstrumentRef:
    key: str  # "<exchange>:<tradingsymbol>"
    exchange: str
    tradingsymbol: str
    instrument_type: str  # INDEX | EQ | FUT | CE | PE
    lot_size: int = 1
    tick_size: float = 0.05
    underlying: str | None = None
    strike: float | None = None
    expiry: date | None = None
    instrument_id: int | None = None
    instrument_token: int | None = None
    segment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["expiry"] = self.expiry.isoformat() if self.expiry else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> InstrumentRef:
        d = dict(d)
        if d.get("expiry") and isinstance(d["expiry"], str):
            d["expiry"] = date.fromisoformat(d["expiry"])
        return cls(**d)


@dataclass
class Signal:
    signal_id: str
    strategy_key: str
    ts: datetime  # decision time (signal candle close)
    underlying: str
    direction: Direction
    action: SignalAction
    reference_price: float
    candle: Candle
    explanation: dict[str, Any] = field(default_factory=dict)
