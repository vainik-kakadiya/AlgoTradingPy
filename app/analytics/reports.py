"""Trade queries and analytics reports assembled from persisted records."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.performance import (
    daily_pnl,
    drawdown_series,
    group_metrics,
    max_drawdown,
    monthly_pnl,
    pnl_distribution,
    trade_metrics,
)
from app.core.timeutil import ist_datetime
from app.database.models import (
    BacktestRun,
    BacktestTrade,
    EquitySnapshot,
    PaperSession,
    PaperTrade,
    Strategy,
    StrategyVersion,
)

TRADE_FIELDS = (
    "id", "trade_uid", "instrument_key", "tradingsymbol", "instrument_type", "underlying", "strike", "expiry", "lot_size",
    "lots", "quantity", "direction", "signal_id", "signal_ts", "entry_ts", "entry_price", "entry_ref_price", "exit_ts",
    "exit_price", "exit_ref_price", "entry_charges", "exit_charges", "slippage_cost", "gross_pnl", "net_pnl",
    "exit_reason", "flags", "details",
)


def query_trades(
    db: Session,
    scope: str,
    backtest_id: int | None = None,
    session_id: int | None = None,
    strategy_id: int | None = None,
    version_id: int | None = None,
    start: date | None = None,
    end: date | None = None,
    instrument_type: str | None = None,
    direction: str | None = None,
    exit_reason: str | None = None,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    if scope == "backtest":
        model, parent, parent_fk = BacktestTrade, BacktestRun, BacktestTrade.backtest_run_id
    elif scope == "paper":
        model, parent, parent_fk = PaperTrade, PaperSession, PaperTrade.paper_session_id
    else:
        raise ValueError("scope must be 'paper' or 'backtest'")
    stmt = (
        select(model, parent.id, StrategyVersion.version_number, StrategyVersion.id, Strategy.id, Strategy.name)
        .join(parent, parent_fk == parent.id)
        .join(StrategyVersion, parent.strategy_version_id == StrategyVersion.id)
        .join(Strategy, StrategyVersion.strategy_id == Strategy.id)
    )
    if backtest_id is not None and scope == "backtest":
        stmt = stmt.where(parent.id == backtest_id)
    if session_id is not None and scope == "paper":
        stmt = stmt.where(parent.id == session_id)
    if strategy_id is not None:
        stmt = stmt.where(Strategy.id == strategy_id)
    if version_id is not None:
        stmt = stmt.where(StrategyVersion.id == version_id)
    if start:
        stmt = stmt.where(model.exit_ts >= ist_datetime(start, time(0, 0)))
    if end:
        stmt = stmt.where(model.exit_ts < ist_datetime(end + timedelta(days=1), time(0, 0)))
    if instrument_type:
        stmt = stmt.where(model.instrument_type == instrument_type.upper())
    if direction:
        stmt = stmt.where(model.direction == direction.upper())
    if exit_reason:
        stmt = stmt.where(model.exit_reason == exit_reason.upper())
    if symbol:
        stmt = stmt.where(model.tradingsymbol.ilike(f"%{symbol}%"))
    out = []
    for trade, parent_id, version_number, version_pk, strat_id, strat_name in db.execute(stmt.order_by(model.entry_ts)):
        row = {f: getattr(trade, f) for f in TRADE_FIELDS}
        row.update(
            scope=scope, strategy_id=strat_id, strategy_name=strat_name, version_number=version_number, strategy_version_id=version_pk,
            backtest_run_id=parent_id if scope == "backtest" else None, paper_session_id=parent_id if scope == "paper" else None,
        )
        out.append(row)
    return out


def _option_side(t: dict[str, Any]) -> str:
    if t["instrument_type"] == "CE":
        return "CALL"
    if t["instrument_type"] == "PE":
        return "PUT"
    return f"{t['instrument_type']} {t['direction']}"


def equity_points(db: Session, backtest_id: int | None, session_id: int | None) -> list[EquitySnapshot]:
    stmt = select(EquitySnapshot)
    if backtest_id is not None:
        stmt = stmt.where(EquitySnapshot.backtest_run_id == backtest_id)
    elif session_id is not None:
        stmt = stmt.where(EquitySnapshot.paper_session_id == session_id)
    else:
        return []
    return list(db.scalars(stmt.order_by(EquitySnapshot.ts)))


def initial_capital_for(db: Session, backtest_id: int | None, session_id: int | None) -> float | None:
    if backtest_id is not None:
        run = db.get(BacktestRun, backtest_id)
        return run.initial_capital if run else None
    if session_id is not None:
        s = db.get(PaperSession, session_id)
        return s.initial_capital if s else None
    return None


def summary_report(db: Session, scope: str, **filters: Any) -> dict[str, Any]:
    backtest_id, session_id = filters.get("backtest_id"), filters.get("session_id")
    trades = query_trades(db, scope, **filters)
    capital = initial_capital_for(db, backtest_id, session_id)
    metrics = trade_metrics(trades, capital)
    points = equity_points(db, backtest_id, session_id)
    if points and capital:
        dd, dd_pct = max_drawdown([(p.ts, p.equity) for p in points], capital)
        dd_basis = "mark-to-market equity snapshots"
    else:
        cumulative, curve = 0.0, []
        for t in sorted(trades, key=lambda x: x["exit_ts"]):
            cumulative += t["net_pnl"]
            curve.append((t["exit_ts"], cumulative))
        base = capital or 0.0
        dd, dd_pct = max_drawdown([(ts, base + v) for ts, v in curve], base) if curve else (0.0, 0.0)
        if not capital:
            dd_pct = None
        dd_basis = "closed-trade cumulative net P&L"
    metrics.update(max_drawdown=dd, max_drawdown_pct=dd_pct, drawdown_basis=dd_basis)
    return {
        "scope": scope,
        "filters": {k: v for k, v in filters.items() if v is not None},
        "initial_capital": capital,
        "return_basis": "net P&L / configured initial capital" if capital else "unavailable for multi-run aggregates",
        "metrics": metrics,
        "by_option_side": group_metrics(trades, _option_side),
        "by_exit_reason": group_metrics(trades, lambda t: t["exit_reason"]),
        "by_strategy_version": group_metrics(trades, lambda t: f"{t['strategy_name']} v{t['version_number']}"),
        "by_expiry": group_metrics(trades, lambda t: t["expiry"].isoformat() if t["expiry"] else "n/a"),
        "by_instrument": group_metrics(trades, lambda t: t["tradingsymbol"])[:50],
        "by_run": group_metrics(trades, lambda t: (f"Backtest #{t['backtest_run_id']}" if scope == "backtest" else f"Session #{t['paper_session_id']}")),
        "daily": daily_pnl(trades),
        "monthly": monthly_pnl(trades),
        "distribution": pnl_distribution(trades),
        "costs": {
            "entry_charges": sum(t["entry_charges"] for t in trades),
            "exit_charges": sum(t["exit_charges"] for t in trades),
            "slippage": sum(t["slippage_cost"] for t in trades),
            "gross_pnl": metrics["gross_pnl"],
            "net_pnl": metrics["net_pnl"],
        },
    }


def equity_report(db: Session, scope: str, backtest_id: int | None, session_id: int | None, **filters: Any) -> dict[str, Any]:
    capital = initial_capital_for(db, backtest_id, session_id)
    points = equity_points(db, backtest_id, session_id)
    if points:
        return {
            "basis": "mark-to-market equity snapshots",
            "initial_capital": capital,
            "points": [{"ts": p.ts, "equity": p.equity, "realized_pnl": p.realized_pnl, "unrealized_pnl": p.unrealized_pnl,
                        "drawdown": p.drawdown, "drawdown_pct": p.drawdown_pct} for p in points],
        }
    trades = query_trades(db, scope, backtest_id=backtest_id, session_id=session_id, **filters)
    cumulative, curve = 0.0, []
    for t in sorted(trades, key=lambda x: x["exit_ts"]):
        cumulative += t["net_pnl"]
        curve.append((t["exit_ts"], cumulative))
    series = drawdown_series(curve, 0.0)
    return {
        "basis": "closed-trade cumulative net P&L (no capital base)",
        "initial_capital": None,
        "points": [{"ts": s["ts"], "equity": s["equity"], "realized_pnl": s["equity"], "unrealized_pnl": 0.0,
                    "drawdown": s["drawdown"], "drawdown_pct": None} for s in series],
    }


def market_phase(now: datetime) -> str:
    from app.core.timeutil import to_ist

    local = to_ist(now)
    if local.weekday() >= 5:
        return "CLOSED_WEEKEND"
    t = local.time()
    if time(9, 0) <= t < time(9, 15):
        return "PRE_OPEN"
    if time(9, 15) <= t < time(15, 30):
        return "OPEN"
    return "CLOSED"
