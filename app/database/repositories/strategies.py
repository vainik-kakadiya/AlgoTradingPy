from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import Strategy, StrategyVersion
from app.strategies.config_schema import ConfigValidationResult, validate_config


class ConfigInvalid(ValueError):
    def __init__(self, result: ConfigValidationResult) -> None:
        super().__init__("Strategy configuration is invalid")
        self.result = result


def validated(config: dict[str, Any], strategy_key: str) -> ConfigValidationResult:
    """Validate with the config's strategy_id pinned to the strategy's stable key."""
    result = validate_config({**config, "strategy_id": strategy_key})
    if not result.valid:
        raise ConfigInvalid(result)
    return result


def latest_version(db: Session, strategy_id: int) -> StrategyVersion | None:
    return db.scalars(
        select(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id).order_by(StrategyVersion.version_number.desc())
    ).first()


def add_version(db: Session, strategy: Strategy, config: dict[str, Any], notes: str | None) -> tuple[StrategyVersion, bool]:
    """Create a new immutable version. Returns (version, created); identical configs are not duplicated."""
    result = validated(config, strategy.strategy_key)
    latest = latest_version(db, strategy.id)
    if latest is not None and latest.config_hash == result.config_hash:
        return latest, False
    number = (db.scalar(select(func.max(StrategyVersion.version_number)).where(StrategyVersion.strategy_id == strategy.id)) or 0) + 1
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=number, config_json=result.normalized, config_hash=result.config_hash, notes=notes
    )
    db.add(version)
    db.flush()
    return version, True


def version_count(db: Session, strategy_id: int) -> int:
    return db.scalar(select(func.count()).select_from(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id)) or 0
