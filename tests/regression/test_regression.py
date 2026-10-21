"""Regression suite: fixed datasets with stored expected results.

Any change to the strategy engine, execution simulator or accounting that alters these
numbers must be deliberate. Regenerate the fixture with:

    REGRESSION_UPDATE=1 python -m pytest tests/regression
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from app.backtesting.engine import BacktestParams, run_backtest
from app.strategies.config_schema import DEFAULT_NIFTY_MOMENTUM_CONFIG
from app.strategies.registry import build_strategy
from tests.helpers import synthetic_replay

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "regression_expected.json"

CASES = {
    "momentum_default_seed42_10d": (copy.deepcopy(DEFAULT_NIFTY_MOMENTUM_CONFIG), 42, 10),
    "momentum_loose_seed7_8d": (
        {**copy.deepcopy(DEFAULT_NIFTY_MOMENTUM_CONFIG),
         "entry": {"range_gt": 60, "absolute_body_gt": 40, "evaluate_on_closed_candle_only": True},
         "exits": {"reference": "OPTION_PREMIUM", "stop_loss_points": 20, "take_profit_points": 30, "square_off_at_session_end": True},
         "risk": {"max_open_positions": 1, "max_daily_loss": 10000}},
        7, 8,
    ),
}


def summarize(config: dict, seed: int, days: int) -> dict:
    result = run_backtest(build_strategy(config), synthetic_replay(days=days, seed=seed), BacktestParams(initial_capital=200000))
    m = result.metrics
    return {
        "total_trades": m["total_trades"],
        "wins": m["wins"],
        "losses": m["losses"],
        "net_pnl": round(m["net_pnl"], 2),
        "gross_pnl": round(m["gross_pnl"], 2),
        "total_charges": round(m["total_charges"], 2),
        "total_slippage": round(m["total_slippage"], 2),
        "max_drawdown": round(m["max_drawdown"], 2),
        "signals": result.report["signals"],
        "exit_reasons": result.report["exit_reasons"],
        "trades": [[t.instrument.tradingsymbol, t.entry_ts.isoformat(), t.entry_price, t.exit_ts.isoformat(), t.exit_price, t.exit_reason]
                   for t in result.trades],
    }


def test_regression_results_match_fixture():
    actual = {name: summarize(*case) for name, case in CASES.items()}
    if os.environ.get("REGRESSION_UPDATE") == "1" or not FIXTURE.exists():
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(actual, indent=2, sort_keys=True))
        if os.environ.get("REGRESSION_UPDATE") != "1":
            pytest.fail("Regression fixture was missing and has been created; review and commit it")
    expected = json.loads(FIXTURE.read_text())
    assert actual == expected
