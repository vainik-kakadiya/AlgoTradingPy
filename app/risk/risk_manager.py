"""Backend risk checks applied to every proposed entry (backtest and paper alike)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.core.timeutil import ist_date, ist_time
from app.risk.limits import RiskCode
from app.strategies.config_schema import RiskConfig, SessionConfig
from app.trading.types import Direction


@dataclass
class RiskDecision:
    allowed: bool
    code: RiskCode | None = None
    reason: str | None = None

    @classmethod
    def ok(cls) -> RiskDecision:
        return cls(True)

    @classmethod
    def block(cls, code: RiskCode, reason: str) -> RiskDecision:
        return cls(False, code, reason)


class RiskManager:
    def __init__(self, risk: RiskConfig, session: SessionConfig) -> None:
        self.cfg = risk
        self.session = session
        self.entries_enabled = True
        self.entries_disabled_reason: str | None = None
        self.day: date | None = None
        self.day_realized = 0.0
        self.entries_today = 0
        self.halted_reason: str | None = None  # daily-loss halt, resets next day

    # ------------------------------------------------------------------ state
    def roll_day(self, now: datetime) -> bool:
        today = ist_date(now)
        if self.day != today:
            self.day = today
            self.day_realized = 0.0
            self.entries_today = 0
            self.halted_reason = None
            return True
        return False

    def to_state(self) -> dict[str, Any]:
        return {
            "entries_enabled": self.entries_enabled,
            "entries_disabled_reason": self.entries_disabled_reason,
            "day": self.day.isoformat() if self.day else None,
            "day_realized": self.day_realized,
            "entries_today": self.entries_today,
            "halted_reason": self.halted_reason,
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self.entries_enabled = state.get("entries_enabled", True)
        self.entries_disabled_reason = state.get("entries_disabled_reason")
        self.day = date.fromisoformat(state["day"]) if state.get("day") else None
        self.day_realized = state.get("day_realized", 0.0)
        self.entries_today = state.get("entries_today", 0)
        self.halted_reason = state.get("halted_reason")

    def disable_entries(self, reason: str) -> None:
        self.entries_enabled = False
        self.entries_disabled_reason = reason

    @property
    def blocked_reason(self) -> str | None:
        if not self.entries_enabled:
            return self.entries_disabled_reason or "New entries disabled"
        return self.halted_reason

    # ------------------------------------------------------------------ checks
    def check_pre_entry(
        self,
        now: datetime,
        open_positions: int,
        lots: int,
        data_fresh: bool,
        stale_reason: str | None,
        had_entry_today: bool,
    ) -> RiskDecision:
        self.roll_day(now)
        if not self.entries_enabled:
            return RiskDecision.block(RiskCode.ENTRIES_DISABLED, self.entries_disabled_reason or "New entries disabled for this session")
        if self.halted_reason:
            return RiskDecision.block(RiskCode.DAILY_LOSS_LIMIT, self.halted_reason)
        t = ist_time(now)
        if not (self.session.market_open <= t < self.session.no_new_entries_after):
            return RiskDecision.block(
                RiskCode.OUTSIDE_ENTRY_WINDOW,
                f"{t:%H:%M:%S} IST is outside the entry window {self.session.market_open:%H:%M}-{self.session.no_new_entries_after:%H:%M}",
            )
        if not data_fresh:
            return RiskDecision.block(RiskCode.STALE_DATA, stale_reason or "Market data is stale")
        if open_positions >= self.cfg.max_open_positions:
            return RiskDecision.block(RiskCode.MAX_OPEN_POSITIONS, f"{open_positions} open/pending position(s); max {self.cfg.max_open_positions}")
        if lots > self.cfg.max_lots_per_trade:
            return RiskDecision.block(RiskCode.MAX_LOTS, f"{lots} lots exceeds max {self.cfg.max_lots_per_trade}")
        if not self.cfg.allow_reentry_after_exit and had_entry_today:
            return RiskDecision.block(RiskCode.REENTRY_NOT_ALLOWED, "Re-entry after exit is disabled and a trade was already taken today")
        if self.cfg.max_entries_per_day and self.entries_today >= self.cfg.max_entries_per_day:
            return RiskDecision.block(RiskCode.MAX_ENTRIES_PER_DAY, f"Max {self.cfg.max_entries_per_day} entries per day reached")
        return RiskDecision.ok()

    def check_order(
        self,
        direction: Direction,
        entry_price: float,
        stop_price: float | None,
        target_price: float | None,
        quantity: int,
        tick_size: float,
    ) -> RiskDecision:
        if entry_price is None or entry_price <= 0:
            return RiskDecision.block(RiskCode.INVALID_PRICE, f"Invalid entry price {entry_price}")
        if stop_price is not None:
            if stop_price < tick_size:
                return RiskDecision.block(
                    RiskCode.INVALID_STOP,
                    f"Stop-loss {stop_price:.2f} is not a valid positive price for entry {entry_price:.2f}; entry blocked (rule not altered)",
                )
            if (direction is Direction.LONG and stop_price >= entry_price) or (direction is Direction.SHORT and stop_price <= entry_price):
                return RiskDecision.block(RiskCode.INVALID_STOP, "Stop-loss is on the wrong side of the entry price")
        if target_price is not None:
            if target_price < tick_size:
                return RiskDecision.block(RiskCode.INVALID_TARGET, f"Target {target_price:.2f} is not a valid positive price")
            if (direction is Direction.LONG and target_price <= entry_price) or (direction is Direction.SHORT and target_price >= entry_price):
                return RiskDecision.block(RiskCode.INVALID_TARGET, "Take-profit is on the wrong side of the entry price")
        if self.cfg.max_loss_per_trade and stop_price is not None:
            planned_loss = abs(entry_price - stop_price) * quantity
            if planned_loss > self.cfg.max_loss_per_trade:
                return RiskDecision.block(
                    RiskCode.MAX_LOSS_PER_TRADE,
                    f"Planned stop loss ₹{planned_loss:,.2f} exceeds per-trade budget ₹{self.cfg.max_loss_per_trade:,.2f}",
                )
        return RiskDecision.ok()

    # ------------------------------------------------------------------ P&L tracking
    def on_entry(self, now: datetime) -> None:
        self.roll_day(now)
        self.entries_today += 1

    def on_trade_closed(self, net_pnl: float, exit_ts: datetime) -> None:
        self.roll_day(exit_ts)
        if ist_date(exit_ts) == self.day:
            self.day_realized += net_pnl

    def check_daily_loss(self, now: datetime, unrealized: float) -> str | None:
        """Return a reason string when the daily-loss limit is newly breached."""
        self.roll_day(now)
        if not self.cfg.max_daily_loss or self.halted_reason:
            return None
        day_pnl = self.day_realized + (unrealized if self.cfg.daily_loss_includes_unrealized else 0.0)
        if day_pnl <= -self.cfg.max_daily_loss:
            self.halted_reason = (
                f"Daily loss limit reached: day P&L ₹{day_pnl:,.2f} <= -₹{self.cfg.max_daily_loss:,.2f}. New entries blocked for today."
            )
            return self.halted_reason
        return None
