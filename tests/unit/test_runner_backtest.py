"""Shared runner behaviour, exercised through the backtest engine with hand-built bars."""

from datetime import date, timedelta

import pytest

from app.backtesting.engine import BacktestParams, run_backtest
from app.backtesting.replay import BarQuoteSource, ReplayData
from app.instruments.contract_resolver import ListCatalog
from app.strategies.config_schema import DEFAULT_NIFTY_MOMENTUM_CONFIG
from app.strategies.registry import build_strategy
from app.trading.broker import SimulatedBroker
from app.trading.costs import CostConfig, SlippageConfig
from app.trading.fill_model import ExecutionModelConfig
from app.trading.runner import StrategyRunner
from app.trading.types import Candle
from tests.helpers import at, bar, option

PARAMS = BacktestParams(200000, CostConfig(enabled=False), ExecutionModelConfig(slippage=SlippageConfig(points=0.5)))


def make_data(option_opens: dict, highs: dict | None = None, lows: dict | None = None, extra_minutes: int = 20) -> ReplayData:
    highs, lows = highs or {}, lows or {}
    under = {
        at(9, 15): bar(at(9, 15), 24000, 24050, 23995, 24040),
        at(9, 16): bar(at(9, 16), 24040, 24120, 24030, 24110),
        at(9, 17): bar(at(9, 17), 24110, 24115, 24090, 24100),  # 3m: O 24000 H 24120 L 23995 C 24100
    }
    for i in range(3, 3 + extra_minutes):
        t = at(9, 15) + timedelta(minutes=i)
        under[t] = bar(t, 24100, 24105, 24095, 24100)  # quiet: no further signals
    ce = option(24150, "CE")
    insts = [ce, option(24100, "CE"), option(24200, "CE"), option(24050, "PE")]
    bars = {}
    for minute, o in option_opens.items():
        t = at(9, minute)
        bars[t] = bar(t, o, highs.get(minute, o + 1), lows.get(minute, o - 1), o)
    return ReplayData("NSE:NIFTY 50", 60, under, {ce.key: bars}, insts)


def test_signal_enters_next_candle_and_exits_at_target():
    opens = {m: 120.0 for m in range(15, 38)}
    opens[17] = 50.0  # price before the decision time must never be used
    data = make_data(opens, highs={25: 205.0})
    result = run_backtest(build_strategy(DEFAULT_NIFTY_MOMENTUM_CONFIG), data, PARAMS)
    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.instrument.tradingsymbol == "NIFTY2510724150CE"
    assert t.details["resolution"]["desired_strike"] == 24150 and t.details["resolution"]["selected_strike"] == 24150
    assert t.entry_ts == at(9, 18) and t.entry_ref_price == 120 and t.entry_price == 120.5
    assert t.exit_reason == "TAKE_PROFIT" and t.exit_price == pytest.approx(200.5) and t.exit_ts == at(9, 25)
    assert t.gross_pnl == pytest.approx((200.5 - 120) * 65)
    assert t.net_pnl == pytest.approx((200.5 - 120.5) * 65)
    assert result.report["reconciliation_error"] == pytest.approx(0, abs=1e-6)


def test_stop_loss_uses_option_premium():
    opens = {m: 120.0 for m in range(15, 38)}
    data = make_data(opens, lows={22: 70.0})
    t = run_backtest(build_strategy(DEFAULT_NIFTY_MOMENTUM_CONFIG), data, PARAMS).trades[0]
    assert t.exit_reason == "STOP_LOSS" and t.exit_ref_price == pytest.approx(70.5) and t.exit_price == pytest.approx(70.0)


def test_missing_option_price_skips_entry():
    opens = {m: 120.0 for m in range(19, 38)}  # no bar at 09:18
    result = run_backtest(build_strategy(DEFAULT_NIFTY_MOMENTUM_CONFIG), make_data(opens), PARAMS)
    assert not result.trades
    assert result.signals[0].outcome == "SKIPPED" and "No quote" in result.signals[0].reason


def test_defer_policy_waits_for_quote():
    cfg = {**DEFAULT_NIFTY_MOMENTUM_CONFIG, "execution": {**DEFAULT_NIFTY_MOMENTUM_CONFIG["execution"],
                                                           "missing_quote_policy": "DEFER", "max_entry_delay_seconds": 120}}
    opens = {m: 120.0 for m in range(19, 38)}
    t = run_backtest(build_strategy(cfg), make_data(opens), PARAMS).trades[0]
    assert t.entry_ts == at(9, 19)


def test_invalid_stop_blocks_entry_without_changing_rule():
    opens = {m: 40.0 for m in range(15, 38)}  # premium 40.5 - 50 < 0
    result = run_backtest(build_strategy(DEFAULT_NIFTY_MOMENTUM_CONFIG), make_data(opens), PARAMS)
    assert not result.trades
    s = result.signals[0]
    assert s.outcome == "BLOCKED" and s.details["risk_code"] == "INVALID_STOP"


def test_session_end_square_off():
    cfg = {**DEFAULT_NIFTY_MOMENTUM_CONFIG, "session": {"square_off_time": "09:30:00", "no_new_entries_after": "09:29:00"}}
    opens = {m: 120.0 for m in range(15, 38)}
    t = run_backtest(build_strategy(cfg), make_data(opens), PARAMS).trades[0]
    assert t.exit_reason == "SESSION_END" and t.exit_ts == at(9, 30)


def test_duplicate_signal_creates_one_order():
    strategy = build_strategy(DEFAULT_NIFTY_MOMENTUM_CONFIG)
    ce = option(24150, "CE")
    bars = {at(9, 18): bar(at(9, 18), 120, 121, 119, 120)}
    broker = SimulatedBroker(200000, CostConfig(enabled=False))

    class Sink:
        events: list = []

        def event(self, severity, event_type, message, details=None, ts=None):
            self.events.append(event_type)

        def record_signal(self, *a):
            pass

    sink = Sink()
    runner = StrategyRunner(strategy, broker, ListCatalog([ce]), BarQuoteSource({ce.key: bars}), sink, "T")
    candle = Candle(at(9, 15), 180, 24000, 24120, 23995, 24100)
    runner.on_candle(candle, at(9, 18))
    runner.on_candle(candle, at(9, 18))  # same candle again (e.g. re-delivered)
    state = runner.export_state()
    runner2 = StrategyRunner(strategy, broker, ListCatalog([ce]), BarQuoteSource({ce.key: bars}), sink, "T")
    runner2.import_state(state)  # restart: processed signals survive
    runner2.on_candle(candle, at(9, 18))
    entries = [o for o in broker.orders.values() if o.purpose.value == "ENTRY"]
    assert len(entries) == 1
    assert "DUPLICATE_SIGNAL" in sink.events


def test_daily_loss_limit_blocks_further_entries():
    strategy = build_strategy({**DEFAULT_NIFTY_MOMENTUM_CONFIG, "risk": {"max_daily_loss": 1000}})
    data = make_data({m: 120.0 for m in range(15, 50)}, lows={20: 60.0}, extra_minutes=30)
    # second qualifying candle later in the day
    for i, t in enumerate([at(9, 39), at(9, 40), at(9, 41)]):
        data.underlying_bars[t] = bar(t, 24100 + i * 40, 24100 + (i + 1) * 40 + 5, 24100 + i * 40 - 1, 24100 + (i + 1) * 40)
    result = run_backtest(strategy, data, PARAMS)
    assert len(result.trades) == 1 and result.trades[0].exit_reason == "STOP_LOSS"
    blocked = [s for s in result.signals if s.outcome == "BLOCKED"]
    assert blocked and blocked[0].details["risk_code"] == "DAILY_LOSS_LIMIT"


def test_rejects_insufficient_granularity():
    data = make_data({18: 120.0})
    data.base_interval_seconds = 120
    with pytest.raises(ValueError):
        run_backtest(build_strategy(DEFAULT_NIFTY_MOMENTUM_CONFIG), data, PARAMS)


def test_date_filter_excludes_other_days():
    params = BacktestParams(200000, start_date=date(2025, 1, 7))
    result = run_backtest(build_strategy(DEFAULT_NIFTY_MOMENTUM_CONFIG), make_data({18: 120.0}), params)
    assert not result.signals and not result.trades
