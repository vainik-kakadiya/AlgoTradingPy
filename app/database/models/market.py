from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutil import utcnow
from app.database.base import Base, UTCDateTime


class Instrument(Base):
    """A contract as described by one metadata source at one point in time.

    ``source_ref`` identifies the snapshot (e.g. ``KITE:2026-10-21`` or ``DATASET:4``).
    Instrument tokens are only meaningful within their snapshot and are never treated as
    permanent identifiers.
    """

    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("source_ref", "exchange", "tradingsymbol", name="uq_instruments_snapshot_symbol"),
        Index("ix_instruments_underlying_type_expiry", "underlying", "instrument_type", "expiry"),
        Index("ix_instruments_exchange_symbol", "exchange", "tradingsymbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(30))  # KITE_MASTER | DATASET
    source_ref: Mapped[str] = mapped_column(String(60), index=True)
    snapshot_date: Mapped[date] = mapped_column(Date)
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey("historical_datasets.id", ondelete="CASCADE"), nullable=True)
    exchange: Mapped[str] = mapped_column(String(10))
    segment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    instrument_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exchange_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tradingsymbol: Mapped[str] = mapped_column(String(60))
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    underlying: Mapped[str | None] = mapped_column(String(40), nullable=True)
    instrument_type: Mapped[str] = mapped_column(String(10))  # INDEX | EQ | FUT | CE | PE
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    lot_size: Mapped[int] = mapped_column(Integer, default=1)
    tick_size: Mapped[float] = mapped_column(Float, default=0.05)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    listed_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"


class MarketTick(Base):
    __tablename__ = "market_ticks"
    __table_args__ = (Index("ix_market_ticks_instrument_ts", "instrument_id", "exchange_ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    exchange_ts: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    receive_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    ltp: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    oi: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(30))
    quality_flags: Mapped[list | None] = mapped_column(JSON, nullable=True)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("instrument_id", "interval_seconds", "start_time", "source", name="uq_candles_identity"),
        Index("ix_candles_lookup", "instrument_id", "interval_seconds", "start_time"),
        Index("ix_candles_dataset", "dataset_id", "start_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey("historical_datasets.id", ondelete="CASCADE"), nullable=True)
    interval_seconds: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[datetime] = mapped_column(UTCDateTime)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    oi: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(40))  # LIVE | DATASET:<id> | REPLAY
    is_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    quality_flags: Mapped[list | None] = mapped_column(JSON, nullable=True)


class HistoricalDataset(Base):
    __tablename__ = "historical_datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(30))  # CSV_IMPORT | KITE_HISTORICAL | LIVE_CAPTURE
    source_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="READY")  # IMPORTING | READY | FAILED
    interval_seconds: Mapped[int] = mapped_column(Integer)
    timezone: Mapped[str] = mapped_column(String(40), default="Asia/Kolkata")
    instrument_coverage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    start_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    quality_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
