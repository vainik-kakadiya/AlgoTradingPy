"""In-process publish/subscribe used to push updates to dashboard WebSocket clients,
plus a helper that persists SystemEvent rows. Safe to call from worker threads."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from typing import Any

from app.core.timeutil import isoformat, utcnow

logger = logging.getLogger("app.events")


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return isoformat(o)
    if isinstance(o, date):
        return o.isoformat()
    if hasattr(o, "value"):
        return o.value
    return str(o)


def to_jsonable(data: Any) -> Any:
    return json.loads(json.dumps(data, default=_json_default))


class EventBus:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[str]] = set()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    def publish(self, msg_type: str, data: Any) -> None:
        if self._loop is None or not self._subscribers:
            return
        message = json.dumps({"type": msg_type, "ts": isoformat(utcnow()), "data": data}, default=_json_default)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._dispatch(message)
        else:
            self._loop.call_soon_threadsafe(self._dispatch, message)

    def _dispatch(self, message: str) -> None:
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()  # drop the oldest message for slow clients
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(message)


bus = EventBus()


def record_event(
    severity: str,
    event_type: str,
    component: str,
    message: str,
    details: dict[str, Any] | None = None,
    paper_session_id: int | None = None,
    backtest_run_id: int | None = None,
    strategy_id: int | None = None,
    ts: datetime | None = None,
    db: Any = None,
) -> None:
    """Persist a SystemEvent and publish it. Never raises (event logging must not break trading)."""
    from app.database.models import SystemEvent
    from app.database.session import session_scope

    log_level = getattr(logging, severity if severity != "CRITICAL" else "CRITICAL", logging.INFO)
    logger.log(log_level, "[%s/%s] %s", component, event_type, message)
    try:
        row = SystemEvent(
            ts=ts or utcnow(), severity=severity, event_type=event_type, component=component, message=message,
            details=to_jsonable(details) if details else None, paper_session_id=paper_session_id,
            backtest_run_id=backtest_run_id, strategy_id=strategy_id,
        )
        if db is not None:
            db.add(row)
            db.flush()
        else:
            with session_scope() as s:
                s.add(row)
                s.flush()
        bus.publish("event", {
            "id": row.id, "ts": row.ts, "severity": severity, "event_type": event_type, "component": component,
            "message": message, "paper_session_id": paper_session_id, "backtest_run_id": backtest_run_id,
        })
    except Exception:  # noqa: BLE001
        logger.exception("Failed to record event %s", event_type)
