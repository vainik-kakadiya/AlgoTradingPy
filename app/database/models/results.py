"""Backtest runs, paper sessions and their orders, positions, trades, signals and equity."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
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
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.core.timeutil import utcnow
from app.database.base import Base, UTCDateTime


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"), index=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("historical_datasets.id"), index=True)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    initial_capital: Mapped[float] = mapped_column(Float)
    strategy_config: Mapped[dict] = mapped_column(JSON)  # exact config snapshot used
    config_hash: Mapped[str] = mapped_column(String(64))
    execution_config: Mapped[dict] = mapped_column(JSON)
    cost_config: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")  # QUEUED|RUNNING|COMPLETED|FAILED|CANCELLED
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    total_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gross_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_charges: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_slippage: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # metrics, warnings, data quality, assumptions
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class PaperSession(Base):
    __tablename__ = "paper_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_version_id: Mapped[int] = mapped_column(ForeignKey("strategy_versions.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="RUNNING")  # RUNNING|ENTRIES_STOPPED|STOPPED|FAILED
    data_source: Mapped[str] = mapped_column(String(20))  # KITE | REPLAY
    initial_capital: Mapped[float] = mapped_column(Float)
    strategy_config: Mapped[dict] = mapped_column(JSON)
    config_hash: Mapped[str] = mapped_column(String(64))
    session_config: Mapped[dict] = mapped_column(JSON)  # costs, slippage, execution model
    entries_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    entries_blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    stopped_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cash: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    last_market_ts: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    runtime_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # risk day state, processed signals
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class PaperOrder(Base):
    __tablename__ = "paper_orders"
    __table_args__ = (
        # Idempotency: the same client order id can never create two simulated orders in a session.
        UniqueConstraint("paper_session_id", "client_order_id", name="uq_paper_orders_client_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    paper_session_id: Mapped[int] = mapped_column(ForeignKey("paper_sessions.id", ondelete="CASCADE"), index=True)
    client_order_id: Mapped[str] = mapped_column(String(200))
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"), nullable=True)
    instrument_key: Mapped[str] = mapped_column(String(80))
    tradingsymbol: Mapped[str] = mapped_column(String(60))
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(10))  # MARKET | LIMIT | STOP
    purpose: Mapped[str] = mapped_column(String(10))  # ENTRY | EXIT
    quantity: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    oco_group: Mapped[str | None] = mapped_column(String(200), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    order_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # full order state for restart recovery
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    filled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (Index("ix_paper_positions_session_status", "paper_session_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    paper_session_id: Mapped[int] = mapped_column(ForeignKey("paper_sessions.id", ondelete="CASCADE"))
    position_uid: Mapped[str] = mapped_column(String(200), unique=True)
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"), nullable=True)
    instrument_key: Mapped[str] = mapped_column(String(80))
    tradingsymbol: Mapped[str] = mapped_column(String(60))
    direction: Mapped[str] = mapped_column(String(5))  # LONG | SHORT
    net_quantity: Mapped[int] = mapped_column(Integer)
    average_entry_price: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(10))  # OPEN | CLOSED
    signal_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    state: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # full position state for restart recovery


class _TradeColumns:
    """Columns shared by backtest and paper round-trip trade records."""

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_uid: Mapped[str] = mapped_column(String(200))
    instrument_key: Mapped[str] = mapped_column(String(80))
    tradingsymbol: Mapped[str] = mapped_column(String(60))
    instrument_type: Mapped[str] = mapped_column(String(10))
    underlying: Mapped[str | None] = mapped_column(String(40), nullable=True)
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    lot_size: Mapped[int] = mapped_column(Integer)
    lots: Mapped[float] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(5))
    signal_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    signal_ts: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    entry_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_ref_price: Mapped[float] = mapped_column(Float)
    exit_ts: Mapped[datetime] = mapped_column(UTCDateTime)
    exit_price: Mapped[float] = mapped_column(Float)
    exit_ref_price: Mapped[float] = mapped_column(Float)
    entry_charges: Mapped[float] = mapped_column(Float)
    exit_charges: Mapped[float] = mapped_column(Float)
    slippage_cost: Mapped[float] = mapped_column(Float)
    gross_pnl: Mapped[float] = mapped_column(Float)
    net_pnl: Mapped[float] = mapped_column(Float)
    exit_reason: Mapped[str] = mapped_column(String(30))
    flags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # strikes, spot, explanations, assumptions


class BacktestTrade(_TradeColumns, Base):
    __tablename__ = "backtest_trades"

    backtest_run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True)
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"), nullable=True)


class PaperTrade(_TradeColumns, Base):
    __tablename__ = "paper_trades"

    @declared_attr.directive
    def __table_args__(cls):  # noqa: N805
        return (UniqueConstraint("paper_session_id", "trade_uid", name="uq_paper_trades_uid"),)

    paper_session_id: Mapped[int] = mapped_column(ForeignKey("paper_sessions.id", ondelete="CASCADE"), index=True)
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"), nullable=True)


class SignalRecord(Base):
    """Every strategy signal together with what happened to it (entered, skipped, blocked)."""

    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signals_session", "paper_session_id", "ts"),
        Index("ix_signals_backtest", "backtest_run_id", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    backtest_run_id: Mapped[int | None] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=True)
    paper_session_id: Mapped[int | None] = mapped_column(ForeignKey("paper_sessions.id", ondelete="CASCADE"), nullable=True)
    signal_id: Mapped[str] = mapped_column(String(200))
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    strategy_key: Mapped[str] = mapped_column(String(100))
    direction: Mapped[str] = mapped_column(String(5))
    action: Mapped[str] = mapped_column(String(10))
    reference_price: Mapped[float] = mapped_column(Float)
    candle: Mapped[dict] = mapped_column(JSON)
    explanation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    outcome: Mapped[str] = mapped_column(String(20))  # ENTERED | SKIPPED | BLOCKED | PENDING
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (
        Index("ix_equity_backtest", "backtest_run_id", "ts"),
        Index("ix_equity_session", "paper_session_id", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    backtest_run_id: Mapped[int | None] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=True)
    paper_session_id: Mapped[int | None] = mapped_column(ForeignKey("paper_sessions.id", ondelete="CASCADE"), nullable=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    cash: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    peak_equity: Mapped[float] = mapped_column(Float)
    drawdown: Mapped[float] = mapped_column(Float)
    drawdown_pct: Mapped[float] = mapped_column(Float)
