from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutil import utcnow
from app.database.base import Base, UTCDateTime


class SystemEvent(Base):
    __tablename__ = "system_events"
    __table_args__ = (Index("ix_system_events_ts", "ts"), Index("ix_system_events_session", "paper_session_id", "ts"))

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    severity: Mapped[str] = mapped_column(String(10))  # DEBUG | INFO | WARNING | ERROR | CRITICAL
    event_type: Mapped[str] = mapped_column(String(50))
    component: Mapped[str] = mapped_column(String(50))
    strategy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paper_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("paper_sessions.id", ondelete="SET NULL"), nullable=True
    )
    backtest_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="SET NULL"), nullable=True
    )
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
