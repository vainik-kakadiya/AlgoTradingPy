"""NIFTY 3-Minute Momentum (strategy id ``nifty_3m_momentum_v1``).

On each completed candle of the underlying:

    range = H - L, body = |C - O|
    qualifies when range > range_gt AND body > absolute_body_gt   (strict inequalities)
    C > O -> BUY_CALL, C < O -> BUY_PUT, C == O -> no trade

The thresholds are compiled into the shared rule engine so backtests and paper trading
evaluate exactly the same rules.
"""

from __future__ import annotations

from typing import Any

from app.strategies import rule_engine as re_
from app.strategies.base import BaseStrategy
from app.strategies.config_schema import NiftyMomentumConfig
from app.trading.types import Candle, Direction


def _field(name: str) -> re_.FieldOperand:
    return re_.FieldOperand(name=name)


def _const(v: float) -> re_.ConstOperand:
    return re_.ConstOperand(value=v)


class NiftyMomentumStrategy(BaseStrategy):
    strategy_type = "nifty_momentum"

    def __init__(self, config: NiftyMomentumConfig) -> None:
        super().__init__(config)
        entry = config.entry
        qualifies = [
            re_.Comparison(op="gt", left=_field("range"), right=_const(entry.range_gt)),
            re_.Comparison(op="gt", left=_field("abs_body"), right=_const(entry.absolute_body_gt)),
        ]
        self.long_rule = re_.AndNode(
            op="and", conditions=[*qualifies, re_.Comparison(op="gt", left=_field("close"), right=_field("open"))]
        )
        self.short_rule = re_.AndNode(
            op="and", conditions=[*qualifies, re_.Comparison(op="lt", left=_field("close"), right=_field("open"))]
        )

    def explain(self, candle: Candle, direction: Direction, trace: list[dict[str, Any]]) -> dict[str, Any]:
        cfg = self.config
        assert isinstance(cfg, NiftyMomentumConfig)
        base = super().explain(candle, direction, trace)
        base["text"] = (
            f"{'Bullish' if direction is Direction.LONG else 'Bearish'} candle: range {candle.range:.2f} > {cfg.entry.range_gt:g} "
            f"and |body| {candle.abs_body:.2f} > {cfg.entry.absolute_body_gt:g} -> "
            f"{'BUY CALL' if direction is Direction.LONG else 'BUY PUT'}"
        )
        return base

    def _entry_summary(self) -> list[str]:
        cfg = self.config
        assert isinstance(cfg, NiftyMomentumConfig)
        return [
            f"Signal candle qualifies when range (H − L) > {cfg.entry.range_gt:g} and |close − open| > {cfg.entry.absolute_body_gt:g} (strict).",
            "Bullish (close > open) → BUY CALL; bearish (close < open) → BUY PUT; doji (close = open) → no trade.",
            "Entry on the next candle after the signal candle closes; only completed candles are evaluated.",
        ]
