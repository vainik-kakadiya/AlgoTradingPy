"""Historical datasets: CSV import, Kite historical download and live-capture snapshots.

Every dataset records its provenance (source, label, checksum, synthetic flag) and a
data-quality summary. Invalid rows are excluded and reported - never repaired - and
missing intervals are counted, not interpolated. An import fails outright when more than
``max_rejected_pct`` of rows are invalid.

CSV formats
-----------
candles.csv     timestamp, tradingsymbol, open, high, low, close [, exchange, volume, oi]
                (timestamp = bar start; naive timestamps are interpreted in the dataset timezone)
instruments.csv tradingsymbol, exchange, instrument_type [, segment, underlying, strike, expiry,
                lot_size, tick_size, instrument_token, listed_from]
                instrument_type: INDEX | EQ | FUT | CE | PE
"""

from __future__ import annotations

import hashlib
import io
import logging
import time as _time
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.core.timeutil import IST, ist_date, ist_datetime, utcnow
from app.database.models import Candle as CandleRow, HistoricalDataset, Instrument
from app.instruments.instrument_master import INDEX_SYMBOL_TO_UNDERLYING, latest_kite_source_ref
from app.instruments.underlyings import SUPPORTED_UNDERLYINGS, get_underlying

logger = logging.getLogger(__name__)

SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)
VALID_TYPES = {"INDEX", "EQ", "FUT", "CE", "PE"}


class DatasetImportError(ValueError):
    pass


def _checksum(*blobs: bytes | None) -> str:
    h = hashlib.sha256()
    for b in blobs:
        if b:
            h.update(b)
    return h.hexdigest()


def _parse_instruments(instruments_csv: bytes | None, symbols: set[str]) -> pd.DataFrame:
    if instruments_csv:
        inst = pd.read_csv(io.BytesIO(instruments_csv), dtype=str).fillna("")
        missing = {"tradingsymbol", "exchange", "instrument_type"} - set(inst.columns)
        if missing:
            raise DatasetImportError(f"instruments.csv is missing columns: {sorted(missing)}")
    else:
        inst = pd.DataFrame(columns=["tradingsymbol", "exchange", "instrument_type"])
    known = set(inst["tradingsymbol"])
    extra = []
    for sym in sorted(symbols - known):
        if sym in INDEX_SYMBOL_TO_UNDERLYING:
            spec = SUPPORTED_UNDERLYINGS[INDEX_SYMBOL_TO_UNDERLYING[sym]]
            extra.append({"tradingsymbol": sym, "exchange": spec.index_exchange, "instrument_type": "INDEX", "segment": "INDICES"})
    if extra:
        inst = pd.concat([inst, pd.DataFrame(extra)], ignore_index=True).fillna("")
    unknown = symbols - set(inst["tradingsymbol"])
    if unknown:
        raise DatasetImportError(
            f"{len(unknown)} symbol(s) in candles.csv have no instrument metadata (e.g. {sorted(unknown)[:5]}); "
            "provide instruments.csv with exchange, instrument_type, strike, expiry and lot_size"
        )
    bad_types = set(inst["instrument_type"].str.upper()) - VALID_TYPES
    if bad_types:
        raise DatasetImportError(f"Unsupported instrument_type values: {sorted(bad_types)}")
    return inst


def _instrument_rows(inst: pd.DataFrame, dataset_id: int, snapshot: date) -> list[dict[str, Any]]:
    rows = []
    for r in inst.to_dict("records"):
        itype = r["instrument_type"].upper()
        underlying = (r.get("underlying") or "").upper() or INDEX_SYMBOL_TO_UNDERLYING.get(r["tradingsymbol"])
        if itype in ("CE", "PE", "FUT"):
            if not underlying:
                raise DatasetImportError(f"{r['tradingsymbol']}: derivatives require an underlying")
            if not r.get("expiry"):
                raise DatasetImportError(f"{r['tradingsymbol']}: derivatives require an expiry")
            if not r.get("lot_size"):
                raise DatasetImportError(f"{r['tradingsymbol']}: derivatives require a lot_size")
        if itype in ("CE", "PE") and not r.get("strike"):
            raise DatasetImportError(f"{r['tradingsymbol']}: options require a strike")
        rows.append({
            "source": "DATASET", "source_ref": f"DATASET:{dataset_id}", "snapshot_date": snapshot, "dataset_id": dataset_id,
            "exchange": r["exchange"].upper(), "segment": r.get("segment") or None,
            "instrument_token": int(float(r["instrument_token"])) if r.get("instrument_token") else None,
            "tradingsymbol": r["tradingsymbol"], "name": underlying, "underlying": underlying, "instrument_type": itype,
            "strike": float(r["strike"]) if r.get("strike") else None,
            "expiry": date.fromisoformat(r["expiry"][:10]) if r.get("expiry") else None,
            "lot_size": int(float(r["lot_size"])) if r.get("lot_size") else 1,
            "tick_size": float(r["tick_size"]) if r.get("tick_size") else 0.05,
            "listed_from": date.fromisoformat(r["listed_from"][:10]) if r.get("listed_from") else None,
            "is_active": True, "created_at": utcnow(),
        })
    return rows


def validate_candles(df: pd.DataFrame, tz: str, interval_seconds: int | None) -> tuple[pd.DataFrame, dict[str, Any], int]:
    """Validate a candle frame. Returns (valid rows, quality summary, interval)."""
    required = {"timestamp", "tradingsymbol", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise DatasetImportError(f"candles.csv is missing columns: {sorted(missing)}")
    total = len(df)
    issues: dict[str, int] = {}
    examples: list[str] = []

    def reject(mask: pd.Series, reason: str) -> None:
        nonlocal df
        n = int(mask.sum())
        if n:
            issues[reason] = issues.get(reason, 0) + n
            if len(examples) < 10:
                examples.extend(f"{reason}: {r.tradingsymbol} {r.timestamp}" for r in df[mask].head(2).itertuples())
            df = df[~mask]

    ts = pd.to_datetime(df["timestamp"], errors="coerce", utc=False)
    if getattr(ts.dt, "tz", None) is None:
        ts = ts.dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
    df = df.assign(timestamp=ts.dt.tz_convert("UTC"))
    reject(df["timestamp"].isna(), "invalid_timestamp")
    df = df.copy()
    for col in ("open", "high", "low", "close", "volume", "oi"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    reject(df[["open", "high", "low", "close"]].isna().any(axis=1), "non_numeric_price")
    reject((df[["open", "high", "low", "close"]] <= 0).any(axis=1), "non_positive_price")
    reject((df["high"] < df[["open", "close", "low"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1)), "inconsistent_ohlc")

    dup_key = ["tradingsymbol", "timestamp"]
    exact = df.duplicated(keep="first")
    issues["exact_duplicates_dropped"] = int(exact.sum())
    df = df[~exact]
    conflicting = df.duplicated(subset=dup_key, keep=False)
    reject(conflicting, "conflicting_duplicates")

    df = df.sort_values(["tradingsymbol", "timestamp"]).reset_index(drop=True)
    if interval_seconds is None:
        diffs = df.groupby("tradingsymbol")["timestamp"].diff().dt.total_seconds().dropna()
        diffs = diffs[diffs > 0]
        if diffs.empty:
            raise DatasetImportError("Cannot infer bar interval; provide interval_seconds")
        interval_seconds = int(diffs.mode().iloc[0])
    local = df["timestamp"].dt.tz_convert(IST)
    seconds_of_day = local.dt.hour * 3600 + local.dt.minute * 60 + local.dt.second
    anchor = SESSION_OPEN.hour * 3600 + SESSION_OPEN.minute * 60
    reject(((seconds_of_day - anchor) % interval_seconds) != 0, "misaligned_timestamp")
    local = df["timestamp"].dt.tz_convert(IST)
    tod = local.dt.time
    outside = (tod < SESSION_OPEN) | (tod >= SESSION_CLOSE)
    issues["outside_session_rows"] = int(outside.sum())

    coverage: dict[str, Any] = {}
    missing_total = 0
    for sym, g in df[~outside].groupby("tradingsymbol"):
        days = g["timestamp"].dt.tz_convert(IST).dt.date
        missing = 0
        for _, day_ts in g.groupby(days)["timestamp"]:
            span = (day_ts.max() - day_ts.min()).total_seconds() / interval_seconds + 1
            missing += int(span - len(day_ts))
        missing_total += missing
        coverage[sym] = {"first": g["timestamp"].min().isoformat(), "last": g["timestamp"].max().isoformat(),
                         "bars": int(len(g)), "missing_intervals": missing}
    rejected = sum(v for k, v in issues.items() if k not in ("exact_duplicates_dropped", "outside_session_rows"))
    summary = {
        "total_rows": total, "valid_rows": int(len(df)), "rejected_rows": rejected, "issues": issues,
        "examples": examples[:10], "missing_intervals": missing_total, "interval_seconds": interval_seconds,
        "missing_policy": "Missing intervals are reported, never interpolated.",
    }
    return df, {"summary": summary, "coverage": coverage}, interval_seconds


def import_csv_dataset(
    db: Session,
    name: str,
    candles_csv: bytes,
    instruments_csv: bytes | None,
    interval_seconds: int | None = None,
    source_label: str | None = None,
    timezone: str = "Asia/Kolkata",
    is_synthetic: bool = False,
    notes: str | None = None,
    max_rejected_pct: float = 5.0,
) -> HistoricalDataset:
    try:
        df = pd.read_csv(io.BytesIO(candles_csv), dtype={"tradingsymbol": str, "exchange": str})
    except Exception as exc:  # noqa: BLE001
        raise DatasetImportError(f"Could not parse candles.csv: {exc}") from exc
    if df.empty:
        raise DatasetImportError("candles.csv has no rows")
    valid, quality, interval = validate_candles(df, timezone, interval_seconds)
    s = quality["summary"]
    if s["valid_rows"] == 0:
        raise DatasetImportError(f"No valid rows. Issues: {s['issues']}")
    if s["rejected_rows"] / max(s["total_rows"], 1) * 100 > max_rejected_pct:
        raise DatasetImportError(f"{s['rejected_rows']} of {s['total_rows']} rows are invalid (> {max_rejected_pct}%). Issues: {s['issues']}")
    inst_df = _parse_instruments(instruments_csv, set(valid["tradingsymbol"]))

    dataset = HistoricalDataset(
        name=name, source="CSV_IMPORT", source_label=source_label, is_synthetic=is_synthetic, status="IMPORTING",
        interval_seconds=interval, timezone=timezone, checksum=_checksum(candles_csv, instruments_csv), notes=notes,
        start_time=valid["timestamp"].min().to_pydatetime(), end_time=valid["timestamp"].max().to_pydatetime(),
        row_count=int(len(valid)), quality_summary=s, instrument_coverage=quality["coverage"],
    )
    db.add(dataset)
    db.flush()
    inst_rows = _instrument_rows(inst_df[inst_df["tradingsymbol"].isin(set(valid["tradingsymbol"]))], dataset.id, ist_date(utcnow()))
    db.execute(insert(Instrument), inst_rows)
    ids = {r.tradingsymbol: r.id for r in db.scalars(select(Instrument).where(Instrument.dataset_id == dataset.id))}
    _insert_candles(db, dataset.id, interval, [
        (ids[r.tradingsymbol], r.timestamp.to_pydatetime(), r.open, r.high, r.low, r.close,
         None if "volume" not in valid.columns or pd.isna(r.volume) else float(r.volume),
         None if "oi" not in valid.columns or pd.isna(r.oi) else float(r.oi))
        for r in valid.itertuples(index=False)
    ])
    dataset.status = "READY"
    return dataset


def _insert_candles(db: Session, dataset_id: int, interval: int, rows: list[tuple[Any, ...]]) -> None:
    source = f"DATASET:{dataset_id}"
    batch = []
    for inst_id, ts, o, h, l, c, v, oi in rows:
        batch.append({"instrument_id": inst_id, "dataset_id": dataset_id, "interval_seconds": interval, "start_time": ts,
                      "open": o, "high": h, "low": l, "close": c, "volume": v, "oi": oi, "source": source, "is_complete": True})
        if len(batch) >= 20000:
            db.execute(insert(CandleRow), batch)
            batch = []
    if batch:
        db.execute(insert(CandleRow), batch)


# ---------------------------------------------------------------------------- Kite historical

KITE_INTERVALS = {"minute": (60, 60), "3minute": (180, 100), "5minute": (300, 100), "15minute": (900, 200)}


def fetch_kite_dataset(
    db: Session,
    kite: Any,
    name: str,
    underlying: str,
    from_date: date,
    to_date: date,
    interval: str = "minute",
    option_expiry: date | None = None,
    strikes_each_side: int = 10,
) -> HistoricalDataset:
    """Download index (+ optionally one expiry's options) history from Kite into a dataset.

    Kite only serves history for currently listed contracts; expired option contracts
    cannot be downloaded, so option history is limited to the selected live expiry.
    """
    if interval not in KITE_INTERVALS:
        raise DatasetImportError(f"Unsupported interval {interval}")
    seconds, max_days = KITE_INTERVALS[interval]
    spec = get_underlying(underlying)
    ref = latest_kite_source_ref(db)
    if ref is None:
        raise DatasetImportError("Refresh the Kite instrument master first")
    master = db.scalars(select(Instrument).where(Instrument.source_ref == ref)).all()
    index_row = next((i for i in master if i.tradingsymbol == spec.index_tradingsymbol and i.exchange == spec.index_exchange), None)
    if index_row is None or index_row.instrument_token is None:
        raise DatasetImportError(f"{spec.index_tradingsymbol} not found in instrument master")

    def history(token: int) -> list[dict[str, Any]]:
        out, start = [], from_date
        while start <= to_date:
            end = min(start + timedelta(days=max_days - 1), to_date)
            out.extend(kite.historical_data(token, start, end, interval, oi=True))
            _time.sleep(0.35)  # Kite historical API rate limit (3 req/s)
            start = end + timedelta(days=1)
        return out

    index_bars = history(index_row.instrument_token)
    if not index_bars:
        raise DatasetImportError("Kite returned no index history for the requested range")
    contracts: list[Instrument] = []
    if option_expiry is not None:
        lo = min(b["low"] for b in index_bars)
        hi = max(b["high"] for b in index_bars)
        options = [i for i in master if i.underlying == underlying and i.instrument_type in ("CE", "PE") and i.expiry == option_expiry]
        strikes = sorted({i.strike for i in options if i.strike is not None})
        lo_idx = max(0, next((k for k, s in enumerate(strikes) if s >= lo), 0) - strikes_each_side)
        hi_idx = min(len(strikes), next((k for k, s in enumerate(strikes) if s > hi), len(strikes)) + strikes_each_side)
        wanted = set(strikes[lo_idx:hi_idx])
        contracts = [i for i in options if i.strike in wanted]

    dataset = HistoricalDataset(
        name=name, source="KITE_HISTORICAL", source_label=f"Kite Connect historical API ({interval})", status="IMPORTING",
        interval_seconds=seconds, timezone="Asia/Kolkata",
        notes=f"Index {spec.index_tradingsymbol}" + (f"; options expiry {option_expiry} ({len(contracts)} contracts)" if contracts else ""),
    )
    db.add(dataset)
    db.flush()
    snapshot = ist_date(utcnow())
    all_inst = [index_row, *contracts]
    db.execute(insert(Instrument), [{
        "source": "DATASET", "source_ref": f"DATASET:{dataset.id}", "snapshot_date": snapshot, "dataset_id": dataset.id,
        "exchange": i.exchange, "segment": i.segment, "instrument_token": i.instrument_token, "exchange_token": i.exchange_token,
        "tradingsymbol": i.tradingsymbol, "name": i.name, "underlying": i.underlying or underlying, "instrument_type": i.instrument_type,
        "strike": i.strike, "expiry": i.expiry, "lot_size": i.lot_size, "tick_size": i.tick_size, "is_active": True, "created_at": utcnow(),
        "metadata_json": {"copied_from": ref},
    } for i in all_inst])
    ids = {r.tradingsymbol: r.id for r in db.scalars(select(Instrument).where(Instrument.dataset_id == dataset.id))}

    def to_rows(sym: str, bars: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        rows = []
        for b in bars:
            ts = b["date"]
            ts = ts.replace(tzinfo=IST) if ts.tzinfo is None else ts
            rows.append((ids[sym], ts, b["open"], b["high"], b["low"], b["close"], b.get("volume"), b.get("oi")))
        return rows

    rows = to_rows(index_row.tradingsymbol, index_bars)
    for c in contracts:
        assert c.instrument_token is not None
        rows.extend(to_rows(c.tradingsymbol, history(c.instrument_token)))
    frame = pd.DataFrame(rows, columns=["iid", "timestamp", "open", "high", "low", "close", "volume", "oi"])
    sym_by_id = {v: k for k, v in ids.items()}
    frame["tradingsymbol"] = frame["iid"].map(sym_by_id)
    valid, quality, _ = validate_candles(frame.drop(columns=["iid"]), "Asia/Kolkata", seconds)
    _insert_candles(db, dataset.id, seconds, [
        (ids[r.tradingsymbol], r.timestamp.to_pydatetime(), r.open, r.high, r.low, r.close,
         None if pd.isna(r.volume) else float(r.volume), None if pd.isna(r.oi) else float(r.oi))
        for r in valid.itertuples(index=False)
    ])
    dataset.row_count = int(len(valid))
    dataset.start_time = valid["timestamp"].min().to_pydatetime()
    dataset.end_time = valid["timestamp"].max().to_pydatetime()
    dataset.quality_summary = quality["summary"]
    dataset.instrument_coverage = quality["coverage"]
    dataset.checksum = _checksum(pd.util.hash_pandas_object(valid, index=False).values.tobytes())
    dataset.status = "READY"
    return dataset


# ---------------------------------------------------------------------------- live capture

def dataset_from_capture(db: Session, name: str, from_date: date, to_date: date, interval_seconds: int) -> HistoricalDataset:
    """Snapshot bars captured from the live feed (source LIVE) into an immutable dataset."""
    start = ist_datetime(from_date, time(0, 0))
    end = ist_datetime(to_date + timedelta(days=1), time(0, 0))
    rows = db.execute(select(CandleRow, Instrument).join(Instrument, CandleRow.instrument_id == Instrument.id).where(
        CandleRow.source == "LIVE", CandleRow.interval_seconds == interval_seconds,
        CandleRow.start_time >= start, CandleRow.start_time < end,
    )).all()
    if not rows:
        raise DatasetImportError("No captured live bars for that range and interval")
    dataset = HistoricalDataset(
        name=name, source="LIVE_CAPTURE", source_label="Captured from Kite live feed by this application",
        interval_seconds=interval_seconds, status="IMPORTING", timezone="Asia/Kolkata",
    )
    db.add(dataset)
    db.flush()
    instruments = {inst.id: inst for _, inst in rows}
    by_symbol: dict[tuple[str, str], Instrument] = {}
    for inst in instruments.values():
        by_symbol.setdefault((inst.exchange, inst.tradingsymbol), inst)
    db.execute(insert(Instrument), [{
        "source": "DATASET", "source_ref": f"DATASET:{dataset.id}", "snapshot_date": i.snapshot_date, "dataset_id": dataset.id,
        "exchange": i.exchange, "segment": i.segment, "instrument_token": i.instrument_token, "tradingsymbol": i.tradingsymbol,
        "name": i.name, "underlying": i.underlying, "instrument_type": i.instrument_type, "strike": i.strike, "expiry": i.expiry,
        "lot_size": i.lot_size, "tick_size": i.tick_size, "is_active": True, "created_at": utcnow(),
    } for i in by_symbol.values()])
    new_ids = {(r.exchange, r.tradingsymbol): r.id for r in db.scalars(select(Instrument).where(Instrument.dataset_id == dataset.id))}
    seen: set[tuple[int, datetime]] = set()
    batch, incomplete = [], 0
    for candle, inst in rows:
        nid = new_ids[(inst.exchange, inst.tradingsymbol)]
        if (nid, candle.start_time) in seen:
            continue
        seen.add((nid, candle.start_time))
        incomplete += 0 if candle.is_complete else 1
        batch.append({"instrument_id": nid, "dataset_id": dataset.id, "interval_seconds": interval_seconds,
                      "start_time": candle.start_time, "open": candle.open, "high": candle.high, "low": candle.low,
                      "close": candle.close, "volume": candle.volume, "source": f"DATASET:{dataset.id}",
                      "is_complete": candle.is_complete, "quality_flags": candle.quality_flags})
    db.execute(insert(CandleRow), batch)
    dataset.row_count = len(batch)
    dataset.start_time = min(b["start_time"] for b in batch)
    dataset.end_time = max(b["start_time"] for b in batch)
    dataset.quality_summary = {"total_rows": len(batch), "incomplete_bars": incomplete,
                               "missing_policy": "Bars captured during feed gaps are marked incomplete; nothing is interpolated."}
    dataset.instrument_coverage = {sym: {} for (_, sym) in new_ids}
    dataset.checksum = _checksum(repr(sorted((b["instrument_id"], b["start_time"].isoformat(), b["close"]) for b in batch)).encode())
    dataset.status = "READY"
    return dataset
