"""Instrument master snapshots from Kite and database-backed contract catalogs."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.timeutil import ist_date, utcnow
from app.database.models import Instrument
from app.instruments.underlyings import SUPPORTED_UNDERLYINGS, get_underlying
from app.trading.types import InstrumentRef

logger = logging.getLogger(__name__)

INDEX_SYMBOL_TO_UNDERLYING = {spec.index_tradingsymbol: spec.symbol for spec in SUPPORTED_UNDERLYINGS.values()}


def to_ref(row: Instrument) -> InstrumentRef:
    return InstrumentRef(
        key=row.key, exchange=row.exchange, tradingsymbol=row.tradingsymbol, instrument_type=row.instrument_type,
        lot_size=row.lot_size, tick_size=row.tick_size, underlying=row.underlying, strike=row.strike, expiry=row.expiry,
        instrument_id=row.id, instrument_token=row.instrument_token, segment=row.segment,
    )


def map_kite_row(raw: dict[str, Any], underlyings: set[str]) -> dict[str, Any] | None:
    """Map one Kite instrument-master row to our schema, or None if out of scope."""
    segment = raw.get("segment") or ""
    exchange = raw.get("exchange") or ""
    tradingsymbol = raw.get("tradingsymbol") or ""
    name = (raw.get("name") or "").upper()
    if segment == "INDICES":
        itype, underlying = "INDEX", INDEX_SYMBOL_TO_UNDERLYING.get(tradingsymbol)
    elif exchange in ("NFO", "BFO") and raw.get("instrument_type") in ("FUT", "CE", "PE"):
        if name not in underlyings:
            return None
        itype, underlying = raw["instrument_type"], name
    else:
        return None
    expiry = raw.get("expiry")
    if not isinstance(expiry, date):
        expiry = date.fromisoformat(expiry) if isinstance(expiry, str) and expiry else None
    strike = float(raw.get("strike") or 0) or None
    return {
        "exchange": exchange,
        "segment": segment,
        "instrument_token": int(raw["instrument_token"]) if raw.get("instrument_token") is not None else None,
        "exchange_token": int(raw["exchange_token"]) if raw.get("exchange_token") else None,
        "tradingsymbol": tradingsymbol,
        "name": raw.get("name"),
        "underlying": underlying,
        "instrument_type": itype,
        "strike": strike if itype in ("CE", "PE") else None,
        "expiry": expiry,
        "lot_size": int(raw.get("lot_size") or 1),
        "tick_size": float(raw.get("tick_size") or 0.05),
    }


def store_kite_snapshot(db: Session, raw_rows: list[dict[str, Any]], snapshot: date | None = None) -> dict[str, Any]:
    settings = get_settings()
    snapshot = snapshot or ist_date(utcnow())
    source_ref = f"KITE:{snapshot.isoformat()}"
    underlyings = set(settings.underlying_list)
    rows = []
    for raw in raw_rows:
        mapped = map_kite_row(raw, underlyings)
        if mapped is not None:
            rows.append({**mapped, "source": "KITE_MASTER", "source_ref": source_ref, "snapshot_date": snapshot,
                         "is_active": True, "created_at": utcnow()})
    for i in range(0, len(rows), 2000):
        stmt = sqlite_insert(Instrument).values(rows[i:i + 2000])
        stmt = stmt.on_conflict_do_update(
            index_elements=["source_ref", "exchange", "tradingsymbol"],
            set_={c: stmt.excluded[c] for c in ("instrument_token", "exchange_token", "lot_size", "tick_size", "strike", "expiry", "name", "segment")},
        )
        db.execute(stmt)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["instrument_type"]] = counts.get(r["instrument_type"], 0) + 1
    return {"source_ref": source_ref, "snapshot_date": snapshot.isoformat(), "stored": len(rows), "by_type": counts,
            "received": len(raw_rows)}


def latest_kite_source_ref(db: Session, as_of: date | None = None) -> str | None:
    q = select(func.max(Instrument.snapshot_date)).where(Instrument.source == "KITE_MASTER")
    if as_of:
        q = q.where(Instrument.snapshot_date <= as_of)
    snap = db.scalar(q)
    return f"KITE:{snap.isoformat()}" if snap else None


class DbCatalog:
    """Contracts from the newest Kite snapshot taken on or before the decision date."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory
        self._cache: dict[tuple[str, str, tuple[str, ...], date], list[InstrumentRef]] = {}

    def contracts(self, underlying: str, instrument_types: tuple[str, ...], as_of: date) -> list[InstrumentRef]:
        with self._session_factory() as db:
            ref = latest_kite_source_ref(db, as_of)
            if ref is None:
                return []
            key = (ref, underlying, instrument_types, as_of)
            if key not in self._cache:
                rows = db.scalars(select(Instrument).where(
                    Instrument.source_ref == ref, Instrument.underlying == underlying,
                    Instrument.instrument_type.in_(instrument_types), Instrument.expiry >= as_of, Instrument.is_active.is_(True),
                )).all()
                self._cache[key] = [to_ref(r) for r in rows]
            return self._cache[key]


def index_instrument(db: Session, underlying: str, source_ref: str | None = None, dataset_id: int | None = None) -> InstrumentRef | None:
    spec = get_underlying(underlying)
    q = select(Instrument).where(Instrument.exchange == spec.index_exchange, Instrument.tradingsymbol == spec.index_tradingsymbol)
    if dataset_id is not None:
        q = q.where(Instrument.dataset_id == dataset_id)
    else:
        ref = source_ref or latest_kite_source_ref(db)
        if ref is None:
            return None
        q = q.where(Instrument.source_ref == ref)
    row = db.scalars(q).first()
    return to_ref(row) if row else None
