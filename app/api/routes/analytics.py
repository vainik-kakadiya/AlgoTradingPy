"""Analytics, trade history, events, system status and the consolidated dashboard."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.performance import daily_pnl, trade_metrics
from app.analytics.reports import equity_report, market_phase, query_trades, summary_report
from app.api.schemas import EventOut, OrderOut, PaperSessionOut, PositionOut, SignalOut, TradeOut
from app.api.serializers import csv_response
from app.core.config import get_settings
from app.core.timeutil import ist_date, utcnow
from app.database.models import (
    BacktestRun,
    PaperOrder,
    PaperPosition,
    PaperSession,
    SignalRecord,
    StrategyVersion,
    SystemEvent,
)
from app.database.session import get_db
from app.market_data.kite_client import kite_sessions
from app.services.runtime import hub, paper_service
from app.trading.costs import CostConfig
from app.trading.fill_model import ExecutionModelConfig

router = APIRouter(tags=["analytics"])
Scope = Literal["paper", "backtest"]


def _filters(
    backtest_id: int | None = None,
    session_id: int | None = None,
    strategy_id: int | None = None,
    version_id: int | None = None,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    return {"backtest_id": backtest_id, "session_id": session_id, "strategy_id": strategy_id, "version_id": version_id,
            "start": start, "end": end}


@router.get("/api/analytics/summary")
def analytics_summary(scope: Scope = "paper", f: dict = Depends(_filters), db: Session = Depends(get_db)) -> dict[str, Any]:
    return summary_report(db, scope, **f)


@router.get("/api/analytics/equity")
def analytics_equity(scope: Scope = "paper", f: dict = Depends(_filters), db: Session = Depends(get_db)) -> dict[str, Any]:
    bt, ss = f.pop("backtest_id"), f.pop("session_id")
    return equity_report(db, scope, bt, ss, **f)


@router.get("/api/analytics/drawdown")
def analytics_drawdown(scope: Scope = "paper", f: dict = Depends(_filters), db: Session = Depends(get_db)) -> dict[str, Any]:
    bt, ss = f.pop("backtest_id"), f.pop("session_id")
    report = equity_report(db, scope, bt, ss, **f)
    return {"basis": report["basis"], "points": [{"ts": p["ts"], "drawdown": p["drawdown"], "drawdown_pct": p["drawdown_pct"]} for p in report["points"]]}


@router.get("/api/analytics/daily-pnl")
def analytics_daily(scope: Scope = "paper", f: dict = Depends(_filters), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"basis": "IST calendar date of trade exit", "days": daily_pnl(query_trades(db, scope, **f))}


@router.get("/api/trades", response_model=list[TradeOut])
def list_trades(
    scope: Scope = "paper",
    f: dict = Depends(_filters),
    instrument_type: str | None = None,
    direction: str | None = None,
    exit_reason: str | None = None,
    symbol: str | None = None,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return query_trades(db, scope, instrument_type=instrument_type, direction=direction, exit_reason=exit_reason, symbol=symbol, **f)


@router.get("/api/trades.csv")
def export_trades(
    scope: Scope = "paper",
    f: dict = Depends(_filters),
    instrument_type: str | None = None,
    direction: str | None = None,
    exit_reason: str | None = None,
    symbol: str | None = None,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    rows = query_trades(db, scope, instrument_type=instrument_type, direction=direction, exit_reason=exit_reason, symbol=symbol, **f)
    return csv_response(rows, f"{scope}_trades.csv")


@router.get("/api/events", response_model=list[EventOut])
def list_events(
    severity: str | None = None,
    event_type: str | None = None,
    component: str | None = None,
    session_id: int | None = None,
    backtest_id: int | None = None,
    before_id: int | None = None,
    limit: int = Query(200, le=2000),
    db: Session = Depends(get_db),
) -> list[SystemEvent]:
    stmt = select(SystemEvent)
    if severity:
        levels = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
        floor = levels.get(severity.upper(), 0)
        stmt = stmt.where(SystemEvent.severity.in_([k for k, v in levels.items() if v >= floor]))
    if event_type:
        stmt = stmt.where(SystemEvent.event_type == event_type.upper())
    if component:
        stmt = stmt.where(SystemEvent.component == component)
    if session_id is not None:
        stmt = stmt.where(SystemEvent.paper_session_id == session_id)
    if backtest_id is not None:
        stmt = stmt.where(SystemEvent.backtest_run_id == backtest_id)
    if before_id is not None:
        stmt = stmt.where(SystemEvent.id < before_id)
    return list(db.scalars(stmt.order_by(SystemEvent.id.desc()).limit(limit)))


@router.get("/api/health")
def health(db: Session = Depends(get_db)) -> dict[str, Any]:
    db.execute(select(1))
    return {"status": "ok", "time": utcnow(), "execution_mode": "PAPER_ONLY"}


@router.get("/api/settings")
def settings() -> dict[str, Any]:
    return {
        **get_settings().public_dict(),
        "kite": kite_sessions.status(),
        "kite_redirect_url": f"http://{get_settings().api_host}:{get_settings().api_port}/api/kite/callback",
        "defaults": {"costs": CostConfig().model_dump(), "execution": ExecutionModelConfig().model_dump()},
    }


def _system_status(db: Session) -> dict[str, Any]:
    now = utcnow()
    feed = hub.status_dict()
    feed.pop("subscriptions", None)
    running = db.scalar(select(func.count()).select_from(BacktestRun).where(BacktestRun.status.in_(["QUEUED", "RUNNING"]))) or 0
    return {
        "execution_mode": "PAPER_ONLY",
        "real_orders_possible": False,
        "server_time": now,
        "market_phase": market_phase(now),
        "market_phase_note": "Exchange holidays are not modelled; check the NSE calendar.",
        "feed": feed,
        "kite": kite_sessions.status(),
        "active_sessions": len(paper_service.runners),
        "backtests_in_progress": running,
    }


@router.get("/api/system/status")
def system_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return _system_status(db)


@router.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Consolidated dashboard payload, computed from persisted backend records."""
    sessions = list(db.scalars(select(PaperSession).where(PaperSession.status.in_(["RUNNING", "ENTRIES_STOPPED"]))
                               .order_by(PaperSession.started_at.desc())))
    active = []
    for s in sessions:
        out = PaperSessionOut.model_validate(s)
        version = db.get(StrategyVersion, s.strategy_version_id)
        if version is not None:
            out.strategy_id, out.strategy_name, out.version_number = version.strategy_id, version.strategy.name, version.version_number
        active.append({"session": out, "live": paper_service.live_summary(s.id),
                       "metrics": paper_service.session_metrics(s.id, s.initial_capital)})
    all_trades = query_trades(db, "paper")
    today = ist_date(utcnow())
    today_trades = [t for t in all_trades if ist_date(t["exit_ts"]) == today]
    session_ids = [s.id for s in sessions]
    open_positions = list(db.scalars(select(PaperPosition).where(PaperPosition.status == "OPEN")
                                     .where(PaperPosition.paper_session_id.in_(session_ids))))
    capital = sum(s.initial_capital for s in sessions) or None
    return {
        "system": _system_status(db),
        "active_sessions": active,
        "paper_all_time": trade_metrics(all_trades, None),
        "paper_today": {**trade_metrics(today_trades, capital), "capital_basis": "sum of active session capital" if capital else None},
        "open_positions": [PositionOut.model_validate(p) for p in open_positions],
        "recent_signals": [SignalOut.model_validate(s) for s in db.scalars(
            select(SignalRecord).where(SignalRecord.paper_session_id.is_not(None)).order_by(SignalRecord.ts.desc()).limit(10))],
        "recent_orders": [OrderOut.model_validate(o) for o in db.scalars(select(PaperOrder).order_by(PaperOrder.updated_at.desc()).limit(10))],
        "recent_alerts": [EventOut.model_validate(e) for e in db.scalars(
            select(SystemEvent).where(SystemEvent.severity.in_(["WARNING", "ERROR", "CRITICAL"])).order_by(SystemEvent.id.desc()).limit(15))],
    }
