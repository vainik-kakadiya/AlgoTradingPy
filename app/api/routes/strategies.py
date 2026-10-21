from __future__ import annotations

import copy
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.api.schemas import (
    DuplicateRequest,
    StrategyCreate,
    StrategyOut,
    StrategyUpdate,
    ValidateRequest,
    VersionCreate,
    VersionOut,
)
from app.database.models import Strategy, StrategyVersion
from app.database.repositories.strategies import ConfigInvalid, add_version, latest_version, version_count
from app.database.session import get_db
from app.services.event_bus import record_event
from app.strategies.config_schema import DEFAULT_NIFTY_MOMENTUM_CONFIG, ConfigValidationResult, validate_config

router = APIRouter(prefix="/api/strategies", tags=["strategies"])

RULE_BASED_EXAMPLE: dict[str, Any] = {
    "strategy_type": "rule_based",
    "strategy_id": "nifty_ema_cross",
    "name": "NIFTY EMA Cross (example)",
    "mode": "paper",
    "underlying": "NIFTY",
    "timeframe_seconds": 300,
    "rules": {
        "long_entry": {"op": "crosses_above", "left": {"type": "indicator", "name": "ema", "period": 9},
                       "right": {"type": "indicator", "name": "ema", "period": 21}},
        "short_entry": {"op": "crosses_below", "left": {"type": "indicator", "name": "ema", "period": 9},
                        "right": {"type": "indicator", "name": "ema", "period": 21}},
    },
    "trade_instrument": "OPTION",
    "option_selection": {"target_strike_offset_points": 0, "strike_rounding": "NEAREST", "quantity_lots": 1},
    "exits": {"reference": "OPTION_PREMIUM", "stop_loss_points": 30, "take_profit_points": 60, "square_off_at_session_end": True},
    "risk": {"max_open_positions": 1, "max_daily_loss": 5000},
}


def _invalid(exc: ConfigInvalid) -> ApiError:
    return ApiError(422, "INVALID_STRATEGY_CONFIG", "Strategy configuration is invalid", exc.result.model_dump())


def _out(db: Session, s: Strategy) -> StrategyOut:
    latest = latest_version(db, s.id)
    out = StrategyOut.model_validate(s)
    out.latest_version = VersionOut.model_validate(latest) if latest else None
    out.version_count = version_count(db, s.id)
    return out


def _get(db: Session, strategy_id: int) -> Strategy:
    s = db.get(Strategy, strategy_id)
    if s is None:
        raise LookupError(f"Strategy {strategy_id} not found")
    return s


@router.get("/templates")
def templates() -> dict[str, Any]:
    return {"nifty_momentum": DEFAULT_NIFTY_MOMENTUM_CONFIG, "rule_based": RULE_BASED_EXAMPLE}


@router.post("/validate", response_model=ConfigValidationResult)
def validate_new(body: ValidateRequest) -> ConfigValidationResult:
    return validate_config(body.config)


@router.get("", response_model=list[StrategyOut])
def list_strategies(db: Session = Depends(get_db)) -> list[StrategyOut]:
    return [_out(db, s) for s in db.scalars(select(Strategy).order_by(Strategy.created_at.desc()))]


@router.post("", response_model=StrategyOut, status_code=201)
def create_strategy(body: StrategyCreate, db: Session = Depends(get_db)) -> StrategyOut:
    key = body.strategy_key or str(body.config.get("strategy_id") or "")
    if not key:
        raise ApiError(400, "MISSING_KEY", "strategy_key (or config.strategy_id) is required")
    if db.scalar(select(Strategy).where(Strategy.strategy_key == key)):
        raise ApiError(409, "DUPLICATE_KEY", f"A strategy with key '{key}' already exists")
    strategy = Strategy(name=body.name, strategy_key=key, description=body.description)
    db.add(strategy)
    db.flush()
    try:
        add_version(db, strategy, body.config, "Initial version")
    except ConfigInvalid as exc:
        db.rollback()
        raise _invalid(exc) from exc
    db.commit()
    record_event("INFO", "STRATEGY_CREATED", "strategies", f"Strategy '{body.name}' ({key}) created", strategy_id=strategy.id)
    return _out(db, strategy)


@router.get("/{strategy_id}", response_model=StrategyOut)
def get_strategy(strategy_id: int, db: Session = Depends(get_db)) -> StrategyOut:
    return _out(db, _get(db, strategy_id))


@router.put("/{strategy_id}", response_model=StrategyOut)
def update_strategy(strategy_id: int, body: StrategyUpdate, db: Session = Depends(get_db)) -> StrategyOut:
    """Updates metadata only. Configuration changes always create a new version via POST /versions."""
    s = _get(db, strategy_id)
    for field in ("name", "description", "is_enabled"):
        value = getattr(body, field)
        if value is not None:
            setattr(s, field, value)
    db.commit()
    if body.is_enabled is not None:
        record_event("INFO", "STRATEGY_ENABLED" if body.is_enabled else "STRATEGY_DISABLED", "strategies",
                     f"Strategy {s.strategy_key} {'enabled' if body.is_enabled else 'disabled'}", strategy_id=s.id)
    return _out(db, s)


@router.get("/{strategy_id}/versions", response_model=list[VersionOut])
def list_versions(strategy_id: int, db: Session = Depends(get_db)) -> list[StrategyVersion]:
    _get(db, strategy_id)
    return list(db.scalars(select(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id)
                           .order_by(StrategyVersion.version_number.desc())))


@router.post("/{strategy_id}/versions", response_model=VersionOut, status_code=201)
def create_version(strategy_id: int, body: VersionCreate, db: Session = Depends(get_db)) -> StrategyVersion:
    s = _get(db, strategy_id)
    try:
        version, created = add_version(db, s, body.config, body.notes)
    except ConfigInvalid as exc:
        raise _invalid(exc) from exc
    if not created:
        raise ApiError(409, "UNCHANGED_CONFIG", f"Configuration is identical to version {version.version_number}",
                       {"version_id": version.id})
    db.commit()
    record_event("INFO", "STRATEGY_VERSION_CREATED", "strategies", f"{s.strategy_key} v{version.version_number} saved",
                 {"config_hash": version.config_hash}, strategy_id=s.id)
    return version


@router.post("/{strategy_id}/validate", response_model=ConfigValidationResult)
def validate_for_strategy(strategy_id: int, body: ValidateRequest, db: Session = Depends(get_db)) -> ConfigValidationResult:
    s = _get(db, strategy_id)
    return validate_config({**body.config, "strategy_id": s.strategy_key})


@router.post("/{strategy_id}/duplicate", response_model=StrategyOut, status_code=201)
def duplicate_strategy(strategy_id: int, body: DuplicateRequest, db: Session = Depends(get_db)) -> StrategyOut:
    source = _get(db, strategy_id)
    latest = latest_version(db, source.id)
    if latest is None:
        raise ApiError(400, "NO_VERSION", "Strategy has no versions to duplicate")
    if db.scalar(select(Strategy).where(Strategy.strategy_key == body.strategy_key)):
        raise ApiError(409, "DUPLICATE_KEY", f"A strategy with key '{body.strategy_key}' already exists")
    copy_ = Strategy(name=body.name, strategy_key=body.strategy_key, description=f"Copy of {source.name} v{latest.version_number}")
    db.add(copy_)
    db.flush()
    config = copy.deepcopy(latest.config_json)
    config["name"] = body.name
    try:
        add_version(db, copy_, config, f"Duplicated from {source.strategy_key} v{latest.version_number}")
    except ConfigInvalid as exc:
        db.rollback()
        raise _invalid(exc) from exc
    db.commit()
    return _out(db, copy_)
