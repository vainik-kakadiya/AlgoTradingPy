"""Database-backed backtest jobs: load a dataset, run the engine, persist results."""

from __future__ import annotations

import logging
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.analytics.performance import drawdown_series
from app.backtesting.engine import BacktestCancelled, BacktestParams, run_backtest
from app.backtesting.replay import ReplayData
from app.core.timeutil import ist_datetime, utcnow
from app.database.models import (
    BacktestRun,
    BacktestTrade,
    Candle as CandleRow,
    EquitySnapshot,
    HistoricalDataset,
    Instrument,
    SignalRecord,
)
from app.database.session import session_scope
from app.instruments.underlyings import get_underlying
from app.services.event_bus import bus, record_event, to_jsonable
from app.strategies.registry import build_strategy
from app.trading.costs import CostConfig
from app.trading.fill_model import ExecutionModelConfig
from app.trading.types import Candle, InstrumentRef

logger = logging.getLogger(__name__)


def instrument_ref(row: Instrument) -> InstrumentRef:
    return InstrumentRef(
        key=row.key, exchange=row.exchange, tradingsymbol=row.tradingsymbol, instrument_type=row.instrument_type,
        lot_size=row.lot_size, tick_size=row.tick_size, underlying=row.underlying, strike=row.strike, expiry=row.expiry,
        instrument_id=row.id, instrument_token=row.instrument_token, segment=row.segment,
    )


def load_replay_data(db: Session, dataset_id: int, underlying: str, start: date | None, end: date | None) -> tuple[ReplayData, dict[str, date]]:
    dataset = db.get(HistoricalDataset, dataset_id)
    if dataset is None:
        raise ValueError(f"Dataset {dataset_id} not found")
    spec = get_underlying(underlying)
    instruments = db.scalars(select(Instrument).where(Instrument.dataset_id == dataset_id)).all()
    index_rows = [i for i in instruments if i.instrument_type == "INDEX" and (i.underlying == underlying or i.tradingsymbol == spec.index_tradingsymbol)]
    if not index_rows:
        raise ValueError(f"Dataset has no {underlying} index instrument (expected tradingsymbol '{spec.index_tradingsymbol}')")
    index_row = index_rows[0]
    by_id = {i.id: i for i in instruments}

    q = select(
        CandleRow.instrument_id, CandleRow.start_time, CandleRow.open, CandleRow.high, CandleRow.low,
        CandleRow.close, CandleRow.volume, CandleRow.is_complete,
    ).where(CandleRow.dataset_id == dataset_id)
    if start:
        q = q.where(CandleRow.start_time >= ist_datetime(start, datetime.min.time()))
    if end:
        q = q.where(CandleRow.start_time < ist_datetime(end + timedelta(days=1), datetime.min.time()))
    underlying_bars: dict[datetime, Candle] = {}
    contract_bars: dict[str, dict[datetime, Candle]] = {}
    interval = dataset.interval_seconds
    for inst_id, ts, o, h, l, c, v, complete in db.execute(q):
        bar = Candle(ts, interval, o, h, l, c, v, bool(complete))
        if inst_id == index_row.id:
            underlying_bars[ts] = bar
        else:
            key = by_id[inst_id].key
            contract_bars.setdefault(key, {})[ts] = bar
    refs = [instrument_ref(i) for i in instruments if i.id != index_row.id]
    listed_from = {i.key: i.listed_from for i in instruments if i.listed_from}
    data = ReplayData(index_row.key, interval, underlying_bars, contract_bars, refs)
    return data, listed_from


class BacktestJobManager:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backtest")
        self._cancel: dict[int, threading.Event] = {}

    def submit(self, run_id: int) -> None:
        self._cancel[run_id] = threading.Event()
        self._executor.submit(self._run_safe, run_id)

    def cancel(self, run_id: int) -> bool:
        flag = self._cancel.get(run_id)
        if flag is None:
            return False
        flag.set()
        return True

    def run_sync(self, run_id: int) -> None:
        """Run in the calling thread (used by tests and CLI)."""
        self._cancel[run_id] = threading.Event()
        self._run_safe(run_id)

    def shutdown(self) -> None:
        for flag in self._cancel.values():
            flag.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def resume_interrupted(self) -> None:
        with session_scope() as db:
            runs = db.scalars(select(BacktestRun).where(BacktestRun.status.in_(["QUEUED", "RUNNING"]))).all()
            ids = [r.id for r in runs]
            for r in runs:
                r.status = "QUEUED"
                r.progress = 0.0
        for run_id in ids:
            self.submit(run_id)

    # ------------------------------------------------------------------
    def _set_status(self, run_id: int, **fields: Any) -> None:
        with session_scope() as db:
            run = db.get(BacktestRun, run_id)
            if run is None:
                return
            for k, v in fields.items():
                setattr(run, k, v)
        bus.publish("backtest", {"id": run_id, **{k: v for k, v in fields.items() if k in ("status", "progress", "error")}})

    def _run_safe(self, run_id: int) -> None:
        try:
            self._run(run_id)
        except BacktestCancelled:
            self._set_status(run_id, status="CANCELLED", completed_at=utcnow())
            record_event("INFO", "BACKTEST_CANCELLED", "backtest", f"Backtest {run_id} cancelled", backtest_run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Backtest %s failed", run_id)
            self._set_status(run_id, status="FAILED", error=str(exc), completed_at=utcnow())
            record_event("ERROR", "BACKTEST_FAILED", "backtest", f"Backtest {run_id} failed: {exc}", backtest_run_id=run_id)
        finally:
            self._cancel.pop(run_id, None)

    def _run(self, run_id: int) -> None:
        cancel = self._cancel.get(run_id) or threading.Event()
        with session_scope() as db:
            run = db.get(BacktestRun, run_id)
            if run is None:
                return
            if cancel.is_set():
                raise BacktestCancelled()
            for table in (BacktestTrade, SignalRecord, EquitySnapshot):
                db.execute(delete(table).where(table.backtest_run_id == run_id))
            run.status, run.started_at, run.progress, run.error = "RUNNING", utcnow(), 0.0, None
            strategy = build_strategy(run.strategy_config)
            params = BacktestParams(
                initial_capital=run.initial_capital,
                costs=CostConfig(**run.cost_config),
                execution=ExecutionModelConfig(**run.execution_config),
                start_date=run.start_date,
                end_date=run.end_date,
            )
            data, listed_from = load_replay_data(db, run.dataset_id, strategy.underlying, run.start_date, run.end_date)
            dataset = db.get(HistoricalDataset, run.dataset_id)
            assert dataset is not None
            dataset_info = {
                "id": dataset.id, "name": dataset.name, "source": dataset.source, "source_label": dataset.source_label,
                "is_synthetic": dataset.is_synthetic, "interval_seconds": dataset.interval_seconds, "checksum": dataset.checksum,
                "quality_summary": dataset.quality_summary,
            }
        bus.publish("backtest", {"id": run_id, "status": "RUNNING", "progress": 0.0})
        if not data.underlying_bars:
            raise ValueError("No underlying bars in the selected date range")

        last_report = [0.0]

        def progress(p: float) -> None:
            now = _time.monotonic()
            if now - last_report[0] > 1.0 or p >= 1.0:
                last_report[0] = now
                self._set_status(run_id, progress=round(p, 4))

        result = run_backtest(strategy, data, params, progress=progress, should_cancel=cancel.is_set, listed_from=listed_from)
        self._persist(run_id, result, params, dataset_info)

    def _persist(self, run_id: int, result: Any, params: BacktestParams, dataset_info: dict[str, Any]) -> None:
        m = result.metrics
        with session_scope() as db:
            if result.trades:
                db.execute(insert(BacktestTrade), [
                    {**t.to_row(), "backtest_run_id": run_id, "details": to_jsonable(t.to_row()["details"])} for t in result.trades
                ])
            if result.signals:
                db.execute(insert(SignalRecord), [
                    {
                        "backtest_run_id": run_id, "signal_id": s.signal.signal_id, "ts": s.signal.ts,
                        "strategy_key": s.signal.strategy_key, "direction": s.signal.direction.value,
                        "action": s.signal.action.value, "reference_price": s.signal.reference_price,
                        "candle": s.signal.candle.to_dict(), "explanation": to_jsonable(s.signal.explanation),
                        "outcome": s.outcome, "outcome_reason": s.reason, "details": to_jsonable(s.details),
                    }
                    for s in result.signals
                ])
            series = drawdown_series([(p.ts, p.equity) for p in result.equity], params.initial_capital)
            if series:
                db.execute(insert(EquitySnapshot), [
                    {
                        "backtest_run_id": run_id, "ts": p.ts, "cash": p.cash, "realized_pnl": p.realized,
                        "unrealized_pnl": p.unrealized, "equity": p.equity, "peak_equity": s["peak"],
                        "drawdown": s["drawdown"], "drawdown_pct": s["drawdown_pct"],
                    }
                    for p, s in zip(result.equity, series, strict=True)
                ])
            run = db.get(BacktestRun, run_id)
            assert run is not None
            report = dict(result.report)
            report["dataset"] = dataset_info
            report["events"] = result.events[:500]
            if dataset_info.get("is_synthetic"):
                report["warnings"] = ["Dataset is SYNTHETIC (generated data, not observed market prices). Results say nothing about real performance."] + report["warnings"]
            run.report = to_jsonable(report)
            run.total_trades = m["total_trades"]
            run.gross_pnl = m["gross_pnl"]
            run.total_charges = m["total_charges"]
            run.total_slippage = m["total_slippage"]
            run.net_pnl = m["net_pnl"]
            run.return_pct = m["return_pct"]
            run.win_rate = m["win_rate"]
            run.profit_factor = m["profit_factor"]
            run.max_drawdown = m["max_drawdown"]
            run.max_drawdown_pct = m["max_drawdown_pct"]
            run.status = "COMPLETED"
            run.progress = 1.0
            run.completed_at = utcnow()
        bus.publish("backtest", {"id": run_id, "status": "COMPLETED", "progress": 1.0})
        record_event("INFO", "BACKTEST_COMPLETED", "backtest",
                     f"Backtest {run_id} completed: {m['total_trades']} trades, net ₹{m['net_pnl']:,.2f}", backtest_run_id=run_id)


backtest_jobs = BacktestJobManager()
