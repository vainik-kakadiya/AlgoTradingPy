from __future__ import annotations

from typing import Any

from app.strategies.base import BaseStrategy, RuleBasedStrategy
from app.strategies.config_schema import NiftyMomentumConfig, RuleBasedConfig, parse_config
from app.strategies.nifty_momentum import NiftyMomentumStrategy

STRATEGY_TYPES: dict[str, type[BaseStrategy]] = {
    "nifty_momentum": NiftyMomentumStrategy,
    "rule_based": RuleBasedStrategy,
}


def build_strategy(config: NiftyMomentumConfig | RuleBasedConfig | dict[str, Any]) -> BaseStrategy:
    if isinstance(config, dict):
        config = parse_config(config)
    cls = STRATEGY_TYPES[config.strategy_type]
    return cls(config)  # type: ignore[arg-type]
