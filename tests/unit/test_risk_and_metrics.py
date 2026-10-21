from datetime import datetime, timezone

import pytest

from app.analytics.performance import daily_pnl, max_drawdown, trade_metrics
from app.risk.limits import RiskCode
from app.risk.risk_manager import RiskManager
from app.strategies.config_schema import RiskConfig, SessionConfig
from app.trading.types import Direction
from tests.helpers import at


def rm(**risk) -> RiskManager:
    return RiskManager(RiskConfig(**risk), SessionConfig())


def check(r: RiskManager, now=None, positions=0, lots=1, fresh=True, had_entry=False):
    return r.check_pre_entry(now or at(10, 0), positions, lots, fresh, "stale" if not fresh else None, had_entry)


def test_entry_checks():
    r = rm()
    assert check(r).allowed
    assert check(r, positions=1).code is RiskCode.MAX_OPEN_POSITIONS
    assert check(r, lots=9).code is RiskCode.MAX_LOTS
    assert check(r, fresh=False).code is RiskCode.STALE_DATA
    assert check(r, now=at(15, 5)).code is RiskCode.OUTSIDE_ENTRY_WINDOW
    assert check(r, now=at(9, 10)).code is RiskCode.OUTSIDE_ENTRY_WINDOW
    assert check(rm(allow_reentry_after_exit=False), had_entry=True).code is RiskCode.REENTRY_NOT_ALLOWED
    r.disable_entries("stopped")
    assert check(r).code is RiskCode.ENTRIES_DISABLED


def test_order_checks():
    r = rm(max_loss_per_trade=3000)
    assert r.check_order(Direction.LONG, 200, 150, 280, 65, 0.05).allowed is False  # 50 * 65 = 3250 > 3000
    assert r.check_order(Direction.LONG, 40, -10, 120, 65, 0.05).code is RiskCode.INVALID_STOP
    assert r.check_order(Direction.LONG, 0, None, None, 65, 0.05).code is RiskCode.INVALID_PRICE
    assert rm().check_order(Direction.LONG, 200, 150, 280, 65, 0.05).allowed


def test_daily_loss_limit_and_reset():
    r = rm(max_daily_loss=2000)
    r.on_trade_closed(-1500, at(10, 0))
    assert r.check_daily_loss(at(10, 1), unrealized=0) is None
    assert r.check_daily_loss(at(10, 2), unrealized=-600) is not None
    assert check(r).code is RiskCode.DAILY_LOSS_LIMIT
    from datetime import date
    assert check(r, now=at(10, 0, day=date(2025, 1, 7))).allowed  # new day resets the halt


def test_state_round_trip():
    r = rm()
    r.on_trade_closed(-500, at(10, 0))
    r.disable_entries("x")
    r2 = rm()
    r2.load_state(r.to_state())
    assert r2.day_realized == -500 and not r2.entries_enabled


def T(net, gross=None, ec=10.0, xc=10.0, slip=5.0, exit_ts=None):
    return {"net_pnl": net, "gross_pnl": gross if gross is not None else net + ec + xc + slip, "entry_charges": ec,
            "exit_charges": xc, "slippage_cost": slip, "exit_ts": exit_ts or at(10, 0)}


def test_trade_metrics_definitions():
    m = trade_metrics([T(5000), T(-3000), T(0), T(1000)], 100000)
    assert m["total_trades"] == 4 and m["wins"] == 2 and m["losses"] == 1 and m["breakeven"] == 1
    assert m["win_rate"] == 50.0
    assert m["profit_factor"] == pytest.approx(6000 / 3000)
    assert m["net_pnl"] == 3000 and m["return_pct"] == pytest.approx(3.0)
    assert m["total_charges"] == 80 and m["total_slippage"] == 20
    assert m["avg_win"] == 3000 and m["avg_loss"] == -3000 and m["largest_win"] == 5000 and m["largest_loss"] == -3000


def test_profit_factor_states():
    assert trade_metrics([], 1)["profit_factor_state"] == "NO_TRADES"
    m = trade_metrics([T(10)], 1)
    assert m["profit_factor"] is None and m["profit_factor_state"] == "NO_LOSSES"


def test_max_drawdown_known_series():
    pts = [(at(10, i), v) for i, v in enumerate([110000, 99000, 130000, 115000])]
    dd, dd_pct = max_drawdown(pts, 100000)
    assert dd == 15000
    assert dd_pct == pytest.approx(15000 / 130000 * 100)
    assert max_drawdown([(at(10, 0), 90000)], 100000) == (10000, 10.0)  # peak starts at initial capital


def test_daily_pnl_uses_ist_date():
    late = datetime(2025, 1, 6, 18, 45, tzinfo=timezone.utc)  # 00:15 IST on 7 Jan
    days = daily_pnl([T(100, exit_ts=at(10, 0)), T(50, exit_ts=late)])
    assert [d["date"] for d in days] == ["2025-01-06", "2025-01-07"]
