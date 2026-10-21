"""Time helpers. All persisted timestamps are UTC; market-session logic uses IST."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Return an aware UTC datetime. Naive values are assumed to already be UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_ist(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(IST)


def ist_date(dt: datetime) -> date:
    return to_ist(dt).date()


def ist_time(dt: datetime) -> time:
    return to_ist(dt).time()


def ist_datetime(d: date, t: time) -> datetime:
    """Aware UTC datetime for a wall-clock IST date+time."""
    return datetime.combine(d, t, tzinfo=IST).astimezone(UTC)


def floor_to_interval(ts: datetime, interval_seconds: int, anchor: time = time(9, 15)) -> datetime:
    """Start of the interval bucket containing ``ts``.

    Buckets are aligned to the IST session anchor (09:15 by default) so that three-minute
    candles start at 09:15, 09:18, ... regardless of the epoch.
    """
    ts = ensure_utc(ts)
    anchor_dt = ist_datetime(ist_date(ts), anchor)
    offset = (ts - anchor_dt).total_seconds()
    buckets = offset // interval_seconds
    return anchor_dt + timedelta(seconds=buckets * interval_seconds)


def isoformat(dt: datetime | None) -> str | None:
    return ensure_utc(dt).isoformat().replace("+00:00", "Z") if dt else None
