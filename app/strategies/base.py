"""Strategy evaluation shared by backtesting and paper trading.

A strategy only sees *completed* candles of its underlying and returns at most one signal
per candle. It never sees order books, option prices or future data; contract selection,
risk checks and execution happen in the runner and broker.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.timeutil import isoformat
from app.strategies import rule_engine
from app.strategies.config_schema import NiftyMomentumConfig, RuleBasedConfig
from app.trading.types import Candle, Direction, Signal, SignalAction


class BaseStrategy:
    strategy_type: str = "base"

    def __init__(self, config: NiftyMomentumConfig | RuleBasedConfig) -> None:
        self.config = config
        self.key = config.strategy_id
        self.underlying = config.underlying
        self.timeframe_seconds = config.timeframe_seconds
        self.long_rule: Any = None
        self.short_rule: Any = None
        self.exit_rule: Any = config.exits.exit_rule

    # -- instrument selection --------------------------------------------------------------
    @property
    def trade_instrument(self) -> str:
        return "OPTION"

    @property
    def quantity_lots(self) -> int:
        return self.config.option_selection.quantity_lots  # type: ignore[union-attr]

    def action_for(self, direction: Direction) -> SignalAction:
        if self.trade_instrument == "OPTION":
            return SignalAction.BUY_CALL if direction is Direction.LONG else SignalAction.BUY_PUT
        return SignalAction.BUY if direction is Direction.LONG else SignalAction.SELL

    @property
    def lookback(self) -> int:
        rules = [r for r in (self.long_rule, self.short_rule, self.exit_rule) if r is not None]
        return max([rule_engine.required_lookback(r) for r in rules] + [1])

    # -- evaluation ------------------------------------------------------------------------
    def evaluate(self, candles: Sequence[Candle]) -> Signal | None:
        """Evaluate the latest completed candle. Returns a signal or None."""
        if not candles:
            return None
        latest = candles[-1]
        if not latest.complete:
            return None
        long_trace: list[dict[str, Any]] = []
        short_trace: list[dict[str, Any]] = []
        long_hit = rule_engine.evaluate(self.long_rule, candles, long_trace) if self.long_rule is not None else False
        short_hit = rule_engine.evaluate(self.short_rule, candles, short_trace) if self.short_rule is not None else False
        if long_hit is True and short_hit is True:
            return None  # contradictory rules: never trade
        if long_hit is not True and short_hit is not True:
            return None
        direction = Direction.LONG if long_hit is True else Direction.SHORT
        explanation = self.explain(latest, direction, long_trace if direction is Direction.LONG else short_trace)
        return Signal(
            signal_id=f"{self.key}:{self.underlying}:{isoformat(latest.start)}",
            strategy_key=self.key,
            ts=latest.end,
            underlying=self.underlying,
            direction=direction,
            action=self.action_for(direction),
            reference_price=latest.close,
            candle=latest,
            explanation=explanation,
        )

    def should_exit(self, candles: Sequence[Candle]) -> tuple[bool, list[dict[str, Any]]]:
        if self.exit_rule is None or not candles or not candles[-1].complete:
            return False, []
        trace: list[dict[str, Any]] = []
        return rule_engine.evaluate(self.exit_rule, candles, trace) is True, trace

    def explain(self, candle: Candle, direction: Direction, trace: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "direction": direction.value,
            "candle": {"open": candle.open, "high": candle.high, "low": candle.low, "close": candle.close},
            "range": round(candle.range, 4),
            "body": round(candle.body, 4),
            "conditions": trace,
        }

    # -- descriptions ----------------------------------------------------------------------
    def summary(self) -> list[str]:
        cfg = self.config
        lines = [f"Underlying {cfg.underlying}, evaluated on completed {cfg.timeframe_seconds // 60 or cfg.timeframe_seconds}"
                 f"{'-minute' if cfg.timeframe_seconds >= 60 else '-second'} candles."]
        lines += self._entry_summary()
        lines += self._instrument_summary()
        ex = cfg.exits
        unit = "option premium points" if ex.reference == "OPTION_PREMIUM" else "price points"
        if ex.stop_loss_points:
            lines.append(f"Stop-loss: entry price − {ex.stop_loss_points:g} {unit} (long) / + for short.")
        if ex.take_profit_points:
            lines.append(f"Take-profit: entry price + {ex.take_profit_points:g} {unit} (long) / − for short.")
        if ex.max_holding_minutes:
            lines.append(f"Time exit after {ex.max_holding_minutes} minutes in the trade.")
        if ex.exit_rule is not None:
            lines.append(f"Rule exit when {rule_engine.describe(ex.exit_rule)} on the underlying.")
        s = cfg.session
        lines.append(
            f"Entries only between {s.market_open:%H:%M} and {s.no_new_entries_after:%H:%M} IST"
            + (f"; open positions squared off at {s.square_off_time:%H:%M} IST." if ex.square_off_at_session_end else "; no session-end square-off.")
        )
        r = cfg.risk
        risk_bits = [f"max {r.max_open_positions} open position(s)"]
        if r.max_daily_loss:
            risk_bits.append(f"daily loss limit ₹{r.max_daily_loss:,.0f} ({'incl.' if r.daily_loss_includes_unrealized else 'excl.'} unrealized)")
        if r.max_loss_per_trade:
            risk_bits.append(f"per-trade loss budget ₹{r.max_loss_per_trade:,.0f}")
        risk_bits.append("re-entry after exit " + ("allowed" if r.allow_reentry_after_exit else "not allowed"))
        lines.append("Risk: " + ", ".join(risk_bits) + ".")
        lines.append("Execution: paper only - orders are simulated and never sent to a broker.")
        return lines

    def _entry_summary(self) -> list[str]:
        lines = []
        if self.long_rule is not None:
            lines.append(f"Long entry when {rule_engine.describe(self.long_rule)}.")
        if self.short_rule is not None:
            lines.append(f"Short entry when {rule_engine.describe(self.short_rule)}.")
        return lines

    def _instrument_summary(self) -> list[str]:
        if self.trade_instrument != "OPTION":
            return []
        o = self.config.option_selection  # type: ignore[union-attr]
        call = "above" if o.call_strike_side == "ABOVE_SPOT" else "below"
        put = "above" if o.put_strike_side == "ABOVE_SPOT" else "below"
        return [
            f"Long signal buys a CALL with strike ≈ {o.target_strike_offset_points:g} points {call} spot; "
            f"short signal buys a PUT with strike ≈ {o.target_strike_offset_points:g} points {put} spot "
            f"(rounding {o.strike_rounding.replace('_', ' ').lower()}, expiry {o.expiry_policy.replace('_', ' ').lower()}).",
            f"Quantity: {o.quantity_lots} lot(s); lot size taken from contract metadata.",
        ]

    def warnings(self) -> list[str]:
        cfg = self.config
        out: list[str] = []
        if cfg.timeframe_seconds < 60:
            out.append("Sub-minute timeframes need second-level or tick data; minute candles cannot reproduce them in backtests.")
        r, ex = cfg.risk, cfg.exits
        if r.max_daily_loss and ex.stop_loss_points and self.trade_instrument == "OPTION":
            example_loss = ex.stop_loss_points * 65 * self.quantity_lots
            if example_loss >= r.max_daily_loss:
                out.append(
                    f"A single stop-loss ({ex.stop_loss_points:g} points × lot size) may exceed the daily loss limit "
                    f"(e.g. ₹{example_loss:,.0f} with a 65-unit lot vs ₹{r.max_daily_loss:,.0f}); one losing trade would halt entries for the day."
                )
        if not ex.stop_loss_points:
            out.append("No stop-loss configured.")
        if not ex.square_off_at_session_end:
            out.append("Positions may be held overnight; overnight gaps are not protected by stops.")
        return out


class RuleBasedStrategy(BaseStrategy):
    strategy_type = "rule_based"

    def __init__(self, config: RuleBasedConfig) -> None:
        super().__init__(config)
        self.long_rule = config.rules.long_entry
        self.short_rule = config.rules.short_entry

    @property
    def trade_instrument(self) -> str:
        return self.config.trade_instrument  # type: ignore[union-attr]

    @property
    def quantity_lots(self) -> int:
        cfg = self.config
        assert isinstance(cfg, RuleBasedConfig)
        return cfg.option_selection.quantity_lots if cfg.trade_instrument == "OPTION" else cfg.future_selection.quantity_lots

    def _instrument_summary(self) -> list[str]:
        if self.trade_instrument == "FUTURE":
            f = self.config.future_selection  # type: ignore[union-attr]
            return [f"Trades {self.underlying} futures ({f.expiry_policy.replace('_', ' ').lower()}), {f.quantity_lots} lot(s); short signals sell futures."]
        return super()._instrument_summary()
