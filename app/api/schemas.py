"""Request bodies and response models for the REST API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.trading.costs import CostConfig
from app.trading.fill_model import ExecutionModelConfig


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------------------- strategies
class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    strategy_key: str | None = Field(None, description="Stable identifier; defaults to config.strategy_id")
    description: str | None = None
    config: dict[str, Any]


class StrategyUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    is_enabled: bool | None = None


class VersionCreate(BaseModel):
    config: dict[str, Any]
    notes: str | None = None


class ValidateRequest(BaseModel):
    config: dict[str, Any]


class DuplicateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    strategy_key: str = Field(min_length=3, max_length=100)


class VersionOut(ORM):
    id: int
    strategy_id: int
    version_number: int
    config_json: dict[str, Any]
    config_hash: str
    notes: str | None
    created_at: datetime


class StrategyOut(ORM):
    id: int
    name: str
    strategy_key: str
    description: str | None
    is_enabled: bool
    created_at: datetime
    updated_at: datetime
    latest_version: VersionOut | None = None
    version_count: int = 0


# ----------------------------------------------------------------------------- instruments / data
class InstrumentOut(ORM):
    id: int
    source: str
    source_ref: str
    snapshot_date: date
    dataset_id: int | None
    exchange: str
    segment: str | None
    instrument_token: int | None
    tradingsymbol: str
    underlying: str | None
    instrument_type: str
    strike: float | None
    expiry: date | None
    lot_size: int
    tick_size: float
    is_active: bool


class DatasetOut(ORM):
    id: int
    name: str
    source: str
    source_label: str | None
    is_synthetic: bool
    status: str
    interval_seconds: int
    timezone: str
    instrument_coverage: dict[str, Any] | None
    start_time: datetime | None
    end_time: datetime | None
    row_count: int
    quality_summary: dict[str, Any] | None
    checksum: str | None
    notes: str | None
    imported_at: datetime


class FeedStartRequest(BaseModel):
    source: Literal["KITE", "REPLAY"]
    dataset_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    speed: float = Field(60.0, gt=0, le=10000, description="Replay speed: market seconds per wall-clock second")


class KiteFetchRequest(BaseModel):
    name: str
    underlying: str = "NIFTY"
    from_date: date
    to_date: date
    interval: Literal["minute", "3minute", "5minute", "15minute"] = "minute"
    option_expiry: date | None = None
    strikes_each_side: int = Field(10, ge=0, le=60)


class CaptureDatasetRequest(BaseModel):
    name: str
    from_date: date
    to_date: date
    interval_seconds: int = Field(1, ge=1)


# ----------------------------------------------------------------------------- backtests
class BacktestCreate(BaseModel):
    strategy_version_id: int
    dataset_id: int
    name: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    initial_capital: float = Field(200000, gt=0)
    costs: CostConfig = Field(default_factory=CostConfig)
    execution: ExecutionModelConfig = Field(default_factory=ExecutionModelConfig)


class BacktestOut(ORM):
    id: int
    name: str | None
    strategy_version_id: int
    dataset_id: int
    start_date: date | None
    end_date: date | None
    initial_capital: float
    config_hash: str
    status: str
    progress: float
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    total_trades: int | None
    gross_pnl: float | None
    total_charges: float | None
    total_slippage: float | None
    net_pnl: float | None
    return_pct: float | None
    win_rate: float | None
    profit_factor: float | None
    max_drawdown: float | None
    max_drawdown_pct: float | None
    error: str | None
    strategy_name: str | None = None
    strategy_id: int | None = None
    version_number: int | None = None
    dataset_name: str | None = None


class BacktestDetail(BacktestOut):
    strategy_config: dict[str, Any]
    execution_config: dict[str, Any]
    cost_config: dict[str, Any]
    report: dict[str, Any] | None


# ----------------------------------------------------------------------------- paper
class PaperSessionCreate(BaseModel):
    strategy_version_id: int
    name: str = Field(min_length=1, max_length=200)
    initial_capital: float = Field(200000, gt=0)
    data_source: Literal["KITE", "REPLAY"] = "KITE"
    dataset_id: int | None = None
    costs: CostConfig = Field(default_factory=CostConfig)
    execution: ExecutionModelConfig = Field(default_factory=ExecutionModelConfig)


class PaperStopRequest(BaseModel):
    reason: str | None = None


class PaperSessionOut(ORM):
    id: int
    strategy_version_id: int
    name: str
    status: str
    data_source: str
    initial_capital: float
    config_hash: str
    entries_enabled: bool
    entries_blocked_reason: str | None
    started_at: datetime
    stopped_at: datetime | None
    stop_reason: str | None
    cash: float
    realized_pnl: float
    unrealized_pnl: float
    net_pnl: float
    last_market_ts: datetime | None
    updated_at: datetime
    strategy_name: str | None = None
    strategy_id: int | None = None
    version_number: int | None = None


class OrderOut(ORM):
    id: int
    paper_session_id: int
    client_order_id: str
    instrument_key: str
    tradingsymbol: str
    side: str
    order_type: str
    purpose: str
    quantity: int
    limit_price: float | None
    trigger_price: float | None
    status: str
    filled_quantity: int
    average_fill_price: float | None
    signal_id: str | None
    exit_reason: str | None
    rejection_reason: str | None
    execution_details: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    filled_at: datetime | None


class PositionOut(ORM):
    id: int
    paper_session_id: int
    position_uid: str
    instrument_key: str
    tradingsymbol: str
    direction: str
    net_quantity: int
    average_entry_price: float
    realized_pnl: float
    unrealized_pnl: float
    last_price: float | None
    stop_loss: float | None
    take_profit: float | None
    status: str
    signal_id: str | None
    opened_at: datetime
    closed_at: datetime | None


class TradeOut(ORM):
    id: int
    trade_uid: str
    instrument_key: str
    tradingsymbol: str
    instrument_type: str
    underlying: str | None
    strike: float | None
    expiry: date | None
    lot_size: int
    lots: float
    quantity: int
    direction: str
    signal_id: str | None
    signal_ts: datetime | None
    entry_ts: datetime
    entry_price: float
    entry_ref_price: float
    exit_ts: datetime
    exit_price: float
    exit_ref_price: float
    entry_charges: float
    exit_charges: float
    slippage_cost: float
    gross_pnl: float
    net_pnl: float
    exit_reason: str
    flags: list[str] | None
    details: dict[str, Any] | None
    backtest_run_id: int | None = None
    paper_session_id: int | None = None
    scope: str | None = None
    strategy_name: str | None = None
    version_number: int | None = None


class SignalOut(ORM):
    id: int
    backtest_run_id: int | None
    paper_session_id: int | None
    signal_id: str
    ts: datetime
    strategy_key: str
    direction: str
    action: str
    reference_price: float
    candle: dict[str, Any]
    explanation: dict[str, Any] | None
    outcome: str
    outcome_reason: str | None
    details: dict[str, Any] | None


class EquityOut(ORM):
    ts: datetime
    cash: float
    realized_pnl: float
    unrealized_pnl: float
    equity: float
    peak_equity: float
    drawdown: float
    drawdown_pct: float


class EventOut(ORM):
    id: int
    ts: datetime
    severity: str
    event_type: str
    component: str
    strategy_id: int | None
    paper_session_id: int | None
    backtest_run_id: int | None
    message: str
    details: dict[str, Any] | None
