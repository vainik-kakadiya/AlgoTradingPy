from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.api.schemas import BacktestCreate, BacktestDetail, BacktestOut, EquityOut, SignalOut, TradeOut
from app.api.serializers import csv_response
from app.backtesting.service import backtest_jobs
from app.database.models import (
    BacktestRun,
    BacktestTrade,
    EquitySnapshot,
    HistoricalDataset,
    SignalRecord,
    StrategyVersion,
)
from app.database.session import get_db
from app.services.event_bus import record_event
from app.strategies.registry import build_strategy

router = APIRouter(prefix="/api/backtests", tags=["backtests"])


def _enrich(db: Session, run: BacktestRun, detail: bool = False) -> BacktestOut:
    version = db.get(StrategyVersion, run.strategy_version_id)
    dataset = db.get(HistoricalDataset, run.dataset_id)
    out = (BacktestDetail if detail else BacktestOut).model_validate(run)
    if version is not None:
        out.strategy_id = version.strategy_id
        out.strategy_name = version.strategy.name
        out.version_number = version.version_number
    out.dataset_name = dataset.name if dataset else None
    return out


def _get(db: Session, run_id: int) -> BacktestRun:
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise LookupError(f"Backtest {run_id} not found")
    return run


@router.post("", response_model=BacktestOut, status_code=201)
def create_backtest(body: BacktestCreate, db: Session = Depends(get_db)) -> BacktestOut:
    version = db.get(StrategyVersion, body.strategy_version_id)
    if version is None:
        raise LookupError(f"Strategy version {body.strategy_version_id} not found")
    dataset = db.get(HistoricalDataset, body.dataset_id)
    if dataset is None:
        raise LookupError(f"Dataset {body.dataset_id} not found")
    if dataset.status != "READY":
        raise ApiError(409, "DATASET_NOT_READY", f"Dataset status is {dataset.status}")
    strategy = build_strategy(version.config_json)
    if strategy.timeframe_seconds % dataset.interval_seconds != 0:
        raise ApiError(422, "INSUFFICIENT_GRANULARITY",
                       f"Strategy timeframe {strategy.timeframe_seconds}s cannot be built from {dataset.interval_seconds}s bars")
    if body.start_date and body.end_date and body.end_date < body.start_date:
        raise ApiError(400, "BAD_RANGE", "end_date must not be before start_date")
    run = BacktestRun(
        name=body.name, strategy_version_id=version.id, dataset_id=dataset.id, start_date=body.start_date,
        end_date=body.end_date, initial_capital=body.initial_capital, strategy_config=version.config_json,
        config_hash=version.config_hash, execution_config=body.execution.model_dump(), cost_config=body.costs.model_dump(),
        status="QUEUED",
    )
    db.add(run)
    db.commit()
    record_event("INFO", "BACKTEST_QUEUED", "backtest", f"Backtest {run.id} queued ({version.strategy.strategy_key} v{version.version_number} on '{dataset.name}')",
                 backtest_run_id=run.id, strategy_id=version.strategy_id)
    backtest_jobs.submit(run.id)
    return _enrich(db, run)


@router.get("", response_model=list[BacktestOut])
def list_backtests(strategy_id: int | None = None, status: str | None = None, db: Session = Depends(get_db)) -> list[BacktestOut]:
    stmt = select(BacktestRun).order_by(BacktestRun.created_at.desc())
    if status:
        stmt = stmt.where(BacktestRun.status == status.upper())
    if strategy_id is not None:
        stmt = stmt.where(BacktestRun.strategy_version_id.in_(select(StrategyVersion.id).where(StrategyVersion.strategy_id == strategy_id)))
    return [_enrich(db, r) for r in db.scalars(stmt.limit(500))]


@router.get("/{run_id}", response_model=BacktestDetail)
def get_backtest(run_id: int, db: Session = Depends(get_db)) -> BacktestOut:
    return _enrich(db, _get(db, run_id), detail=True)


@router.get("/{run_id}/trades", response_model=list[TradeOut])
def backtest_trades(run_id: int, db: Session = Depends(get_db)) -> list[BacktestTrade]:
    _get(db, run_id)
    return list(db.scalars(select(BacktestTrade).where(BacktestTrade.backtest_run_id == run_id).order_by(BacktestTrade.entry_ts)))


@router.get("/{run_id}/trades.csv")
def backtest_trades_csv(run_id: int, db: Session = Depends(get_db)) -> StreamingResponse:
    trades = backtest_trades(run_id, db)
    return csv_response([TradeOut.model_validate(t).model_dump() for t in trades], f"backtest_{run_id}_trades.csv")


@router.get("/{run_id}/equity", response_model=list[EquityOut])
def backtest_equity(run_id: int, max_points: int = Query(5000, le=50000), db: Session = Depends(get_db)) -> list[EquitySnapshot]:
    _get(db, run_id)
    rows = list(db.scalars(select(EquitySnapshot).where(EquitySnapshot.backtest_run_id == run_id).order_by(EquitySnapshot.ts)))
    if len(rows) > max_points:
        step = len(rows) / max_points
        # Downsample for display but always keep the max-drawdown point and the last point.
        worst = max(rows, key=lambda r: r.drawdown)
        keep = {int(i * step) for i in range(max_points)} | {rows.index(worst), len(rows) - 1}
        rows = [r for i, r in enumerate(rows) if i in keep]
    return rows


@router.get("/{run_id}/signals", response_model=list[SignalOut])
def backtest_signals(run_id: int, outcome: str | None = None, db: Session = Depends(get_db)) -> list[SignalRecord]:
    _get(db, run_id)
    stmt = select(SignalRecord).where(SignalRecord.backtest_run_id == run_id)
    if outcome:
        stmt = stmt.where(SignalRecord.outcome == outcome.upper())
    return list(db.scalars(stmt.order_by(SignalRecord.ts).limit(5000)))


@router.post("/{run_id}/cancel", response_model=BacktestOut)
def cancel_backtest(run_id: int, db: Session = Depends(get_db)) -> BacktestOut:
    run = _get(db, run_id)
    if run.status not in ("QUEUED", "RUNNING"):
        raise ApiError(409, "NOT_CANCELLABLE", f"Backtest is {run.status}")
    if not backtest_jobs.cancel(run_id):
        run.status = "CANCELLED"
        db.commit()
    return _enrich(db, run)


@router.delete("/{run_id}", status_code=204, response_model=None)
def delete_backtest(run_id: int, db: Session = Depends(get_db)) -> None:
    run = _get(db, run_id)
    if run.status in ("QUEUED", "RUNNING"):
        raise ApiError(409, "RUNNING", "Cancel the backtest before deleting it")
    for table in (BacktestTrade, SignalRecord, EquitySnapshot):
        db.execute(delete(table).where(table.backtest_run_id == run_id))
    db.delete(run)
    db.commit()
