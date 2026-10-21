"""Paper-trading sessions. There is deliberately no endpoint that places, modifies or cancels
real orders: every order here is simulated by the PaperBroker."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import (
    EquityOut,
    EventOut,
    OrderOut,
    PaperSessionCreate,
    PaperSessionOut,
    PaperStopRequest,
    PositionOut,
    SignalOut,
    TradeOut,
)
from app.database.models import (
    EquitySnapshot,
    PaperOrder,
    PaperPosition,
    PaperSession,
    PaperTrade,
    SignalRecord,
    StrategyVersion,
    SystemEvent,
)
from app.database.session import get_db
from app.services.runtime import paper_service

router = APIRouter(prefix="/api/paper-sessions", tags=["paper trading"])


def _get(db: Session, session_id: int) -> PaperSession:
    row = db.get(PaperSession, session_id)
    if row is None:
        raise LookupError(f"Paper session {session_id} not found")
    return row


def _out(db: Session, row: PaperSession) -> PaperSessionOut:
    out = PaperSessionOut.model_validate(row)
    version = db.get(StrategyVersion, row.strategy_version_id)
    if version is not None:
        out.strategy_id, out.strategy_name, out.version_number = version.strategy_id, version.strategy.name, version.version_number
    return out


@router.post("", response_model=PaperSessionOut, status_code=201)
def create_session(body: PaperSessionCreate, db: Session = Depends(get_db)) -> PaperSessionOut:
    session_id = paper_service.create(body.strategy_version_id, body.name, body.initial_capital, body.data_source,
                                      body.dataset_id, body.costs, body.execution)
    db.expire_all()
    return _out(db, _get(db, session_id))


@router.get("", response_model=list[PaperSessionOut])
def list_sessions(status: str | None = None, db: Session = Depends(get_db)) -> list[PaperSessionOut]:
    stmt = select(PaperSession).order_by(PaperSession.started_at.desc())
    if status:
        stmt = stmt.where(PaperSession.status == status.upper())
    return [_out(db, r) for r in db.scalars(stmt.limit(200))]


@router.get("/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = _get(db, session_id)
    return {
        "session": _out(db, row),
        "live": paper_service.live_summary(session_id),
        "metrics": paper_service.session_metrics(session_id, row.initial_capital),
        "strategy_config": row.strategy_config,
        "session_config": row.session_config,
        "stop_semantics": {
            "stop_entries": "Blocks new entries. Open simulated positions keep their stop-loss, target and session-end square-off.",
            "stop": "Cancels pending simulated orders, closes all open simulated positions at the current quote "
                    "(last known price if no fresh quote, flagged), and ends the session.",
        },
    }


@router.post("/{session_id}/stop-entries", response_model=PaperSessionOut)
def stop_entries(session_id: int, db: Session = Depends(get_db)) -> PaperSessionOut:
    paper_service.stop_entries(session_id)
    db.expire_all()
    return _out(db, _get(db, session_id))


@router.post("/{session_id}/stop", response_model=PaperSessionOut)
def stop_session(session_id: int, body: PaperStopRequest | None = None, db: Session = Depends(get_db)) -> PaperSessionOut:
    row = _get(db, session_id)
    if paper_service.get(session_id) is not None:
        paper_service.stop(session_id, (body.reason if body else None) or "Stopped by user")
    elif row.status in ("RUNNING", "ENTRIES_STOPPED"):
        raise LookupError(f"Paper session {session_id} is not loaded in the engine")
    db.expire_all()
    return _out(db, _get(db, session_id))


@router.get("/{session_id}/orders", response_model=list[OrderOut])
def session_orders(session_id: int, status: str | None = None, db: Session = Depends(get_db)) -> list[PaperOrder]:
    _get(db, session_id)
    stmt = select(PaperOrder).where(PaperOrder.paper_session_id == session_id)
    if status:
        stmt = stmt.where(PaperOrder.status == status.upper())
    return list(db.scalars(stmt.order_by(PaperOrder.created_at.desc()).limit(1000)))


@router.get("/{session_id}/positions", response_model=list[PositionOut])
def session_positions(session_id: int, status: str | None = Query(None, description="OPEN or CLOSED"), db: Session = Depends(get_db)) -> list[PaperPosition]:
    _get(db, session_id)
    stmt = select(PaperPosition).where(PaperPosition.paper_session_id == session_id)
    if status:
        stmt = stmt.where(PaperPosition.status == status.upper())
    return list(db.scalars(stmt.order_by(PaperPosition.opened_at.desc())))


@router.get("/{session_id}/trades", response_model=list[TradeOut])
def session_trades(session_id: int, db: Session = Depends(get_db)) -> list[PaperTrade]:
    _get(db, session_id)
    return list(db.scalars(select(PaperTrade).where(PaperTrade.paper_session_id == session_id).order_by(PaperTrade.entry_ts.desc())))


@router.get("/{session_id}/signals", response_model=list[SignalOut])
def session_signals(session_id: int, limit: int = Query(200, le=2000), db: Session = Depends(get_db)) -> list[SignalRecord]:
    _get(db, session_id)
    return list(db.scalars(select(SignalRecord).where(SignalRecord.paper_session_id == session_id)
                           .order_by(SignalRecord.ts.desc()).limit(limit)))


@router.get("/{session_id}/equity", response_model=list[EquityOut])
def session_equity(session_id: int, db: Session = Depends(get_db)) -> list[EquitySnapshot]:
    _get(db, session_id)
    return list(db.scalars(select(EquitySnapshot).where(EquitySnapshot.paper_session_id == session_id).order_by(EquitySnapshot.ts)))


@router.get("/{session_id}/events", response_model=list[EventOut])
def session_events(session_id: int, limit: int = Query(200, le=2000), db: Session = Depends(get_db)) -> list[SystemEvent]:
    _get(db, session_id)
    return list(db.scalars(select(SystemEvent).where(SystemEvent.paper_session_id == session_id)
                           .order_by(SystemEvent.id.desc()).limit(limit)))
