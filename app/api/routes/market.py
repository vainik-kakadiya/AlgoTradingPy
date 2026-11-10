"""Instruments, market data, datasets and the Kite login flow."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.api.schemas import CaptureDatasetRequest, DatasetOut, FeedStartRequest, InstrumentOut, KiteFetchRequest
from app.core.auth import COOKIE_NAME, issue_state, verify_state, verify_token
from app.core.config import get_settings
from app.database.models import BacktestRun, Candle as CandleRow, HistoricalDataset, Instrument, PaperSession
from app.database.session import get_db, session_scope
from app.instruments.instrument_master import latest_kite_source_ref, store_kite_snapshot
from app.market_data.historical_provider import (
    DatasetImportError,
    dataset_from_capture,
    fetch_kite_dataset,
    import_csv_dataset,
)
from app.market_data.hub import hub
from app.market_data.kite_client import kite_sessions
from app.services.event_bus import record_event

logger = logging.getLogger(__name__)
router = APIRouter(tags=["market data"])


# ----------------------------------------------------------------------------- instruments
@router.get("/api/instruments")
def list_instruments(
    q: str | None = None,
    underlying: str | None = None,
    instrument_type: str | None = None,
    expiry: date | None = None,
    source: str | None = Query(None, description="KITE (latest master snapshot) or DATASET"),
    dataset_id: int | None = None,
    limit: int = Query(200, le=2000),
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(Instrument)
    if dataset_id is not None:
        stmt = stmt.where(Instrument.dataset_id == dataset_id)
    elif source == "DATASET":
        stmt = stmt.where(Instrument.source == "DATASET")
    else:
        stmt = stmt.where(Instrument.source_ref == (latest_kite_source_ref(db) or "__none__"))
    if q:
        stmt = stmt.where(Instrument.tradingsymbol.ilike(f"%{q}%"))
    if underlying:
        stmt = stmt.where(Instrument.underlying == underlying.upper())
    if instrument_type:
        stmt = stmt.where(Instrument.instrument_type == instrument_type.upper())
    if expiry:
        stmt = stmt.where(Instrument.expiry == expiry)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Instrument.underlying, Instrument.expiry, Instrument.strike, Instrument.tradingsymbol)
                      .limit(limit).offset(offset)).all()
    return {"total": total, "items": [InstrumentOut.model_validate(r) for r in rows],
            "snapshot": latest_kite_source_ref(db)}


@router.get("/api/instruments/expiries")
def list_expiries(underlying: str = "NIFTY", dataset_id: int | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    stmt = select(Instrument.expiry, Instrument.instrument_type, func.count()).where(
        Instrument.underlying == underlying.upper(), Instrument.expiry.is_not(None))
    if dataset_id is not None:
        stmt = stmt.where(Instrument.dataset_id == dataset_id)
    else:
        stmt = stmt.where(Instrument.source_ref == (latest_kite_source_ref(db) or "__none__"))
    rows = db.execute(stmt.group_by(Instrument.expiry, Instrument.instrument_type).order_by(Instrument.expiry)).all()
    out: dict[str, dict[str, int]] = {}
    for exp, itype, n in rows:
        out.setdefault(exp.isoformat(), {})[itype] = n
    return {"underlying": underlying.upper(), "expiries": [{"expiry": k, "counts": v} for k, v in out.items()]}


@router.get("/api/instruments/{instrument_id}", response_model=InstrumentOut)
def get_instrument(instrument_id: int, db: Session = Depends(get_db)) -> Instrument:
    row = db.get(Instrument, instrument_id)
    if row is None:
        raise LookupError(f"Instrument {instrument_id} not found")
    return row


@router.post("/api/instruments/refresh")
async def refresh_instruments() -> dict[str, Any]:
    kite = kite_sessions.client()

    def work() -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for exchange in ("NSE", "NFO", "BSE", "BFO"):
            rows.extend(kite.instruments(exchange))
        with session_scope() as db:
            return store_kite_snapshot(db, rows)

    result = await asyncio.to_thread(work)
    record_event("INFO", "INSTRUMENTS_REFRESHED", "instruments", f"Instrument master snapshot {result['source_ref']}: {result['stored']} contracts", result)
    return result


# ----------------------------------------------------------------------------- market data
@router.get("/api/market-data/status")
def market_status() -> dict[str, Any]:
    return hub.status_dict()


@router.get("/api/market-data/latest")
def latest_quotes() -> dict[str, Any]:
    return {"clock": hub.now(), "source": hub.source, "quotes": hub.status_dict()["subscriptions"]}


@router.get("/api/market-data/candles")
def get_candles(
    instrument_id: int | None = None,
    instrument_key: str | None = Query(None, description="EXCHANGE:TRADINGSYMBOL"),
    interval_seconds: int = 180,
    source: str | None = Query(None, description="LIVE or DATASET:<id>"),
    dataset_id: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(500, le=5000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(CandleRow)
    if instrument_id is not None:
        stmt = stmt.where(CandleRow.instrument_id == instrument_id)
    elif instrument_key:
        exchange, _, symbol = instrument_key.partition(":")
        ids = select(Instrument.id).where(Instrument.exchange == exchange, Instrument.tradingsymbol == symbol)
        stmt = stmt.where(CandleRow.instrument_id.in_(ids))
    else:
        raise ApiError(400, "MISSING_INSTRUMENT", "instrument_id or instrument_key is required")
    stmt = stmt.where(CandleRow.interval_seconds == interval_seconds)
    if dataset_id is not None:
        stmt = stmt.where(CandleRow.dataset_id == dataset_id)
    if source:
        stmt = stmt.where(CandleRow.source == source)
    if start:
        stmt = stmt.where(CandleRow.start_time >= start)
    if end:
        stmt = stmt.where(CandleRow.start_time < end)
    rows = list(reversed(db.scalars(stmt.order_by(CandleRow.start_time.desc()).limit(limit)).all()))
    return {"items": [{
        "start": r.start_time, "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume,
        "source": r.source, "is_complete": r.is_complete, "quality_flags": r.quality_flags,
    } for r in rows]}


@router.post("/api/market-data/feed/start")
async def start_feed(body: FeedStartRequest) -> dict[str, Any]:
    if body.source == "KITE":
        kite_sessions.ticker_credentials()  # raises if not logged in
    elif body.dataset_id is None:
        raise ApiError(400, "MISSING_DATASET", "dataset_id is required for REPLAY")
    await hub.start(body.source, body.dataset_id, body.start_date, body.end_date, body.speed)
    return hub.status_dict()


@router.post("/api/market-data/feed/stop")
async def stop_feed() -> dict[str, Any]:
    await hub.stop("Stopped by user")
    return hub.status_dict()


@router.post("/api/market-data/import", response_model=DatasetOut, status_code=201)
async def import_dataset(
    name: str = Form(...),
    candles_file: UploadFile = File(...),
    instruments_file: UploadFile | None = File(None),
    interval_seconds: int | None = Form(None),
    source_label: str | None = Form(None),
    timezone: str = Form("Asia/Kolkata"),
    is_synthetic: bool = Form(False),
    notes: str | None = Form(None),
) -> HistoricalDataset:
    candles = await candles_file.read()
    instruments = await instruments_file.read() if instruments_file is not None else None

    def work() -> int:
        with session_scope() as db:
            ds = import_csv_dataset(db, name, candles, instruments, interval_seconds, source_label, timezone, is_synthetic, notes)
            return ds.id

    try:
        dataset_id = await asyncio.to_thread(work)
    except DatasetImportError as exc:
        raise ApiError(422, "DATASET_INVALID", str(exc)) from exc
    with session_scope() as db:
        ds = db.get(HistoricalDataset, dataset_id)
        assert ds is not None
        record_event("INFO", "DATASET_IMPORTED", "market_data", f"Dataset '{name}' imported: {ds.row_count} bars",
                     {"dataset_id": ds.id, "quality": ds.quality_summary})
        return ds


def _background_dataset_job(label: str, fn: Any) -> None:
    def run() -> None:
        try:
            with session_scope() as db:
                ds = fn(db)
                name, rows = ds.name, ds.row_count
            record_event("INFO", "DATASET_IMPORTED", "market_data", f"{label} dataset '{name}' ready: {rows} bars")
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s dataset job failed", label)
            record_event("ERROR", "DATASET_IMPORT_FAILED", "market_data", f"{label} dataset failed: {exc}")

    threading.Thread(target=run, daemon=True, name=f"dataset-{label}").start()


@router.post("/api/datasets/kite-fetch", status_code=202)
def kite_fetch(body: KiteFetchRequest) -> dict[str, Any]:
    kite = kite_sessions.client()
    if body.to_date < body.from_date:
        raise ApiError(400, "BAD_RANGE", "to_date must not be before from_date")
    _background_dataset_job("Kite historical", lambda db: fetch_kite_dataset(
        db, kite, body.name, body.underlying.upper(), body.from_date, body.to_date, body.interval, body.option_expiry, body.strikes_each_side))
    return {"accepted": True, "message": "Download started; the dataset appears when complete (see System Logs)."}


@router.post("/api/datasets/from-capture", response_model=DatasetOut, status_code=201)
def capture_dataset(body: CaptureDatasetRequest, db: Session = Depends(get_db)) -> HistoricalDataset:
    try:
        ds = dataset_from_capture(db, body.name, body.from_date, body.to_date, body.interval_seconds)
    except DatasetImportError as exc:
        raise ApiError(422, "DATASET_INVALID", str(exc)) from exc
    db.commit()
    return ds


@router.get("/api/datasets", response_model=list[DatasetOut])
def list_datasets(db: Session = Depends(get_db)) -> list[HistoricalDataset]:
    return list(db.scalars(select(HistoricalDataset).order_by(HistoricalDataset.imported_at.desc())))


@router.get("/api/datasets/{dataset_id}", response_model=DatasetOut)
def get_dataset(dataset_id: int, db: Session = Depends(get_db)) -> HistoricalDataset:
    ds = db.get(HistoricalDataset, dataset_id)
    if ds is None:
        raise LookupError(f"Dataset {dataset_id} not found")
    return ds


@router.delete("/api/datasets/{dataset_id}", status_code=204, response_model=None)
def delete_dataset(dataset_id: int, db: Session = Depends(get_db)) -> None:
    ds = db.get(HistoricalDataset, dataset_id)
    if ds is None:
        raise LookupError(f"Dataset {dataset_id} not found")
    used = db.scalar(select(func.count()).select_from(BacktestRun).where(BacktestRun.dataset_id == dataset_id))
    sessions = [s for s in db.scalars(select(PaperSession)) if (s.session_config or {}).get("dataset_id") == dataset_id]
    if used or sessions:
        raise ApiError(409, "DATASET_IN_USE", "Dataset is referenced by backtests or paper sessions; results must stay reproducible")
    db.execute(delete(CandleRow).where(CandleRow.dataset_id == dataset_id))
    db.execute(delete(Instrument).where(Instrument.dataset_id == dataset_id))
    db.delete(ds)
    db.commit()


# ----------------------------------------------------------------------------- kite auth
@router.get("/api/kite/status")
def kite_status() -> dict[str, Any]:
    return kite_sessions.status()


KITE_STATE_PURPOSE = "kite-login"


@router.get("/api/kite/login-url")
def kite_login_url() -> dict[str, str]:
    # Zerodha passes redirect_params back to the callback as query parameters, so the callback
    # can tell that this login was started from the app by the signed-in user.
    state = urlencode({"state": issue_state(KITE_STATE_PURPOSE)})
    return {"login_url": f"{kite_sessions.login_url()}&{urlencode({'redirect_params': state})}"}


def _callback_allowed(request: Request, state: str | None) -> bool:
    """The callback is public (see PUBLIC_PATHS); accept it only for a login the owner started."""
    if not get_settings().auth_enabled:
        return True
    return verify_state(state, KITE_STATE_PURPOSE) or verify_token(request.cookies.get(COOKIE_NAME)) is not None


@router.get("/api/kite/callback")
def kite_callback(
    request: Request,
    request_token: str | None = None,
    status: str | None = None,
    state: str | None = None,
) -> RedirectResponse:
    """Redirect URL registered in the Kite developer console. Exchanges the request token server-side."""
    frontend = get_settings().frontend_url.rstrip("/")
    if not _callback_allowed(request, state):
        record_event("WARNING", "KITE_LOGIN_REJECTED", "kite", "Kite callback rejected: not started from a signed-in session")
        return RedirectResponse(f"{frontend}/settings?kite=expired")
    if status != "success" or not request_token:
        return RedirectResponse(f"{frontend}/settings?kite=failed")
    try:
        kite_sessions.complete_login(request_token)
        record_event("INFO", "KITE_LOGIN", "kite", "Kite session established")
        return RedirectResponse(f"{frontend}/settings?kite=connected")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Kite login failed: %s", exc)
        record_event("ERROR", "KITE_LOGIN_FAILED", "kite", "Kite login failed")
        return RedirectResponse(f"{frontend}/settings?kite=failed")


@router.post("/api/kite/logout")
def kite_logout() -> dict[str, Any]:
    kite_sessions.logout()
    record_event("INFO", "KITE_LOGOUT", "kite", "Kite session cleared")
    return kite_sessions.status()
