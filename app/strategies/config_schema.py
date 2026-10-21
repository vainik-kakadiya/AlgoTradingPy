"""Strategy configuration schema.

Two strategy types exist:

* ``nifty_momentum`` - the PRD's NIFTY 3-Minute Momentum strategy with its own readable
  parameters (range / body thresholds).
* ``rule_based`` - generic long/short entry rules written in the JSON rule language.

Both compile to the same entry/exit rule representation and run on the same engine.

Paper-only: ``mode`` must be ``paper``, ``execution.execution_mode`` must be ``PAPER``
and ``execution.allow_real_orders`` must be ``false``. Any other value is rejected; there
is no configuration that enables real orders.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import time
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.instruments.underlyings import SUPPORTED_UNDERLYINGS
from app.strategies.rule_engine import Condition, RuleValidationError, check_complexity


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionConfig(_Model):
    timezone: Literal["Asia/Kolkata"] = "Asia/Kolkata"
    market_open: time = time(9, 15)
    market_close: time = time(15, 30)
    no_new_entries_after: time = time(15, 0)
    square_off_time: time = time(15, 15)

    @model_validator(mode="after")
    def _ordered(self) -> SessionConfig:
        if not (self.market_open < self.no_new_entries_after <= self.market_close):
            raise ValueError("no_new_entries_after must be after market_open and not after market_close")
        if not (self.market_open < self.square_off_time <= self.market_close):
            raise ValueError("square_off_time must be within market hours")
        return self


class OptionSelection(_Model):
    target_strike_offset_points: float = Field(50, ge=0, le=5000)
    call_strike_side: Literal["ABOVE_SPOT", "BELOW_SPOT"] = "ABOVE_SPOT"
    put_strike_side: Literal["ABOVE_SPOT", "BELOW_SPOT"] = "BELOW_SPOT"
    strike_rounding: Literal["AWAY_FROM_SPOT", "NEAREST", "TOWARD_SPOT"] = "AWAY_FROM_SPOT"
    max_strike_deviation_points: float = Field(
        100, gt=0, description="Skip the entry if no listed strike lies within this distance of the desired strike"
    )
    expiry_policy: Literal["NEAREST_ELIGIBLE", "NEXT_AFTER_NEAREST", "NEAREST_MONTHLY"] = "NEAREST_ELIGIBLE"
    min_days_to_expiry: int = Field(0, ge=0, le=60)
    quantity_lots: int = Field(1, ge=1, le=100)


class FutureSelection(_Model):
    expiry_policy: Literal["NEAREST_ELIGIBLE", "NEXT_AFTER_NEAREST"] = "NEAREST_ELIGIBLE"
    min_days_to_expiry: int = Field(0, ge=0, le=60)
    quantity_lots: int = Field(1, ge=1, le=100)


class ExecutionConfig(_Model):
    entry_timing: Literal["NEXT_CANDLE"] = "NEXT_CANDLE"
    execution_mode: Literal["PAPER"] = "PAPER"
    allow_real_orders: Literal[False] = False
    entry_order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_offset_points: float = Field(0.0, ge=0, description="Buy limit = executable price + offset")
    limit_timeout_seconds: int = Field(30, ge=1, le=3600)
    missing_quote_policy: Literal["SKIP", "DEFER"] = "SKIP"
    max_entry_delay_seconds: int = Field(60, ge=0, le=3600)
    execution_delay_ms: int = Field(0, ge=0, le=60000)

    @field_validator("allow_real_orders", mode="before")
    @classmethod
    def _no_real_orders(cls, v: Any) -> Any:
        if v not in (False, "false", 0, None):
            raise ValueError("Real-order execution is not available in this application (paper-only)")
        return False


class ExitConfig(_Model):
    reference: Literal["OPTION_PREMIUM", "INSTRUMENT_PRICE"] = "OPTION_PREMIUM"
    stop_loss_points: float | None = Field(50, gt=0)
    take_profit_points: float | None = Field(80, gt=0)
    square_off_at_session_end: bool = True
    max_holding_minutes: int | None = Field(None, ge=1, le=24 * 60)
    exit_rule: Condition | None = None


class RiskConfig(_Model):
    max_open_positions: int = Field(1, ge=1, le=20)
    max_daily_loss: float | None = Field(2000, gt=0)
    daily_loss_includes_unrealized: bool = True
    on_daily_loss_breach: Literal["KEEP_PROTECTIVE_EXITS", "SQUARE_OFF"] = "KEEP_PROTECTIVE_EXITS"
    max_loss_per_trade: float | None = Field(None, gt=0)
    max_lots_per_trade: int = Field(5, ge=1, le=100)
    allow_reentry_after_exit: bool = True
    stale_data_seconds: float = Field(10, gt=0, le=3600)
    max_entries_per_day: int | None = Field(None, ge=1)


class MomentumEntry(_Model):
    range_gt: float = Field(100, gt=0)
    absolute_body_gt: float = Field(80, ge=0)
    evaluate_on_closed_candle_only: Literal[True] = True


class MomentumDirection(_Model):
    bullish: Literal["BUY_CALL"] = "BUY_CALL"
    bearish: Literal["BUY_PUT"] = "BUY_PUT"
    doji: Literal["NO_TRADE"] = "NO_TRADE"


class _StrategyBase(_Model):
    strategy_id: str = Field(min_length=3, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    mode: Literal["paper"] = "paper"
    underlying: str = "NIFTY"
    timeframe_seconds: int = Field(180, ge=1, le=3600)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    exits: ExitConfig = Field(default_factory=ExitConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)

    @field_validator("mode", mode="before")
    @classmethod
    def _paper_only(cls, v: Any) -> Any:
        if str(v).lower() != "paper":
            raise ValueError("Only 'paper' mode exists; real-money trading is not supported")
        return "paper"

    @field_validator("strategy_id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]*", v):
            raise ValueError("strategy_id must be lowercase letters, digits, '_' or '-'")
        return v

    @field_validator("underlying")
    @classmethod
    def _underlying(cls, v: str) -> str:
        v = v.upper()
        if v not in SUPPORTED_UNDERLYINGS:
            raise ValueError(f"Unsupported underlying '{v}'. Supported: {sorted(SUPPORTED_UNDERLYINGS)}")
        return v

    @field_validator("timeframe_seconds")
    @classmethod
    def _timeframe(cls, v: int) -> int:
        if v < 60 and 60 % v != 0:
            raise ValueError("Sub-minute timeframes must divide 60 seconds evenly")
        if v >= 60 and v % 60 != 0:
            raise ValueError("Timeframes of a minute or more must be whole minutes")
        return v


class NiftyMomentumConfig(_StrategyBase):
    strategy_type: Literal["nifty_momentum"] = "nifty_momentum"
    entry: MomentumEntry = Field(default_factory=MomentumEntry)
    direction: MomentumDirection = Field(default_factory=MomentumDirection)
    option_selection: OptionSelection = Field(default_factory=OptionSelection)

    @model_validator(mode="after")
    def _momentum_checks(self) -> NiftyMomentumConfig:
        if self.exits.reference != "OPTION_PREMIUM":
            raise ValueError("nifty_momentum exits are defined in option premium points (reference OPTION_PREMIUM)")
        if self.exits.exit_rule is not None:
            raise ValueError("nifty_momentum does not support exit_rule; use a rule_based strategy")
        if self.option_selection.quantity_lots > self.risk.max_lots_per_trade:
            raise ValueError("option_selection.quantity_lots exceeds risk.max_lots_per_trade")
        return self


class RuleSet(_Model):
    long_entry: Condition | None = None
    short_entry: Condition | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> RuleSet:
        if self.long_entry is None and self.short_entry is None:
            raise ValueError("At least one of long_entry / short_entry is required")
        for rule in (self.long_entry, self.short_entry):
            if rule is not None:
                check_complexity(rule)
        return self


class RuleBasedConfig(_StrategyBase):
    strategy_type: Literal["rule_based"] = "rule_based"
    rules: RuleSet
    trade_instrument: Literal["OPTION", "FUTURE"] = "OPTION"
    option_selection: OptionSelection = Field(default_factory=OptionSelection)
    future_selection: FutureSelection = Field(default_factory=FutureSelection)

    @model_validator(mode="after")
    def _rule_checks(self) -> RuleBasedConfig:
        if self.trade_instrument == "OPTION" and self.exits.reference != "OPTION_PREMIUM":
            raise ValueError("Option strategies must use exits.reference OPTION_PREMIUM")
        if self.trade_instrument == "FUTURE" and self.exits.reference != "INSTRUMENT_PRICE":
            raise ValueError("Futures strategies must use exits.reference INSTRUMENT_PRICE")
        lots = self.option_selection.quantity_lots if self.trade_instrument == "OPTION" else self.future_selection.quantity_lots
        if lots > self.risk.max_lots_per_trade:
            raise ValueError("quantity_lots exceeds risk.max_lots_per_trade")
        if self.exits.exit_rule is not None:
            check_complexity(self.exits.exit_rule)
        return self


StrategyConfig = Annotated[Union[NiftyMomentumConfig, RuleBasedConfig], Field(discriminator="strategy_type")]


class _Wrapper(BaseModel):
    config: StrategyConfig


class ConfigValidationResult(BaseModel):
    valid: bool
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: list[str] = Field(default_factory=list)
    normalized: dict[str, Any] | None = None
    config_hash: str | None = None


def parse_config(raw: dict[str, Any]) -> NiftyMomentumConfig | RuleBasedConfig:
    """Parse and validate a raw strategy config. Raises ValidationError / RuleValidationError."""
    if not isinstance(raw, dict):
        raise TypeError("Strategy configuration must be a JSON object")
    data = dict(raw)
    data.setdefault("strategy_type", "rule_based" if "rules" in data else "nifty_momentum")
    return _Wrapper(config=data).config


def normalized_dict(config: BaseModel) -> dict[str, Any]:
    return config.model_dump(mode="json", exclude_none=False)


def config_hash(normalized: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_config(raw: Any) -> ConfigValidationResult:
    from app.strategies.registry import build_strategy  # local import: registry depends on this module

    try:
        cfg = parse_config(raw)
    except ValidationError as exc:
        errors = [
            {"loc": ".".join(str(p) for p in e["loc"][1:] if p not in ("nifty_momentum", "rule_based")), "msg": e["msg"]}
            for e in exc.errors()
        ]
        return ConfigValidationResult(valid=False, errors=errors)
    except (RuleValidationError, TypeError, ValueError) as exc:
        return ConfigValidationResult(valid=False, errors=[{"loc": "", "msg": str(exc)}])

    strategy = build_strategy(cfg)
    normalized = normalized_dict(cfg)
    return ConfigValidationResult(
        valid=True,
        warnings=strategy.warnings(),
        summary=strategy.summary(),
        normalized=normalized,
        config_hash=config_hash(normalized),
    )


DEFAULT_NIFTY_MOMENTUM_CONFIG: dict[str, Any] = {
    "strategy_id": "nifty_3m_momentum_v1",
    "name": "NIFTY 3-Minute Momentum",
    "mode": "paper",
    "underlying": "NIFTY",
    "timeframe_seconds": 180,
    "entry": {"range_gt": 100, "absolute_body_gt": 80, "evaluate_on_closed_candle_only": True},
    "direction": {"bullish": "BUY_CALL", "bearish": "BUY_PUT", "doji": "NO_TRADE"},
    "option_selection": {
        "target_strike_offset_points": 50,
        "call_strike_side": "ABOVE_SPOT",
        "put_strike_side": "BELOW_SPOT",
        "expiry_policy": "NEAREST_ELIGIBLE",
        "quantity_lots": 1,
    },
    "execution": {"entry_timing": "NEXT_CANDLE", "execution_mode": "PAPER", "allow_real_orders": False},
    "exits": {
        "reference": "OPTION_PREMIUM",
        "stop_loss_points": 50,
        "take_profit_points": 80,
        "square_off_at_session_end": True,
    },
    "risk": {"max_open_positions": 1, "max_daily_loss": 2000, "allow_reentry_after_exit": True},
}
