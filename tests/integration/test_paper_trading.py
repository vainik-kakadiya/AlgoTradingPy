"""Paper trading driven by a replayed dataset, plus restart recovery and duplicate protection."""

from __future__ import annotations

import copy
from datetime import date

import pytest

from app.core.safety import KITE_FORBIDDEN_METHODS
from app.strategies.config_schema import DEFAULT_NIFTY_MOMENTUM_CONFIG
from tests.helpers import synthetic_csv
from tests.integration.test_api_workflow import wait_for


@pytest.fixture()
def trap_kite_orders(monkeypatch):
    """Make every real trading method on KiteConnect blow up and record the attempt."""
    from kiteconnect import KiteConnect

    calls: list[str] = []
    for name in KITE_FORBIDDEN_METHODS:
        if hasattr(KiteConnect, name):
            def trap(self, *a, _name=name, **k):
                calls.append(_name)
                raise AssertionError(f"real trading method {_name} called")
            monkeypatch.setattr(KiteConnect, name, trap)
    return calls


def test_replay_paper_session_end_to_end(client, trap_kite_orders):
    # Wednesday + Thursday: away from the synthetic Tuesday expiry, so premiums are not near zero.
    candles, instruments = synthetic_csv(days=2, seed=11, strikes_each_side=8, start=date(2025, 1, 8))
    ds = client.post("/api/market-data/import", data={"name": "replay-ds", "is_synthetic": "true"},
                     files={"candles_file": ("c.csv", candles), "instruments_file": ("i.csv", instruments)}).json()
    cfg = copy.deepcopy(DEFAULT_NIFTY_MOMENTUM_CONFIG)
    cfg.update(strategy_id="paper_replay_test", entry={"range_gt": 60, "absolute_body_gt": 40, "evaluate_on_closed_candle_only": True})
    cfg["exits"].update(stop_loss_points=15, take_profit_points=25)
    cfg["risk"].update(max_daily_loss=50000)
    strategy = client.post("/api/strategies", json={"name": "Paper replay", "config": cfg}).json()

    wrong = client.post("/api/paper-sessions", json={"strategy_version_id": strategy["latest_version"]["id"], "name": "x",
                                                     "data_source": "REPLAY"})
    assert wrong.status_code == 400  # replay sessions need a dataset

    r = client.post("/api/paper-sessions", json={
        "strategy_version_id": strategy["latest_version"]["id"], "name": "replay session",
        "data_source": "REPLAY", "dataset_id": ds["id"], "initial_capital": 300000,
    })
    assert r.status_code == 201, r.text
    session = r.json()
    sid = session["id"]
    assert session["status"] == "RUNNING"

    r = client.post("/api/market-data/feed/start", json={"source": "REPLAY", "dataset_id": ds["id"], "speed": 10000})
    assert r.status_code == 200, r.text
    wait_for(lambda: client.get("/api/market-data/status").json()["status"] == "ENDED", timeout=180)

    detail = client.get(f"/api/paper-sessions/{sid}").json()
    signals = client.get(f"/api/paper-sessions/{sid}/signals").json()
    orders = client.get(f"/api/paper-sessions/{sid}/orders").json()
    trades = client.get(f"/api/paper-sessions/{sid}/trades").json()
    assert signals, "replay should produce signals"
    assert all(s["outcome"] in ("ENTERED", "SKIPPED", "BLOCKED", "PENDING") for s in signals)
    assert trades, "replay should produce at least one simulated trade"
    for t in trades:
        assert t["exit_reason"] in ("STOP_LOSS", "TAKE_PROFIT", "SESSION_END", "SESSION_STOPPED")
        assert "LTP_FALLBACK" in t["flags"]  # replay has no bid/ask; the assumption is recorded
    entry_ids = [o["client_order_id"] for o in orders if o["purpose"] == "ENTRY"]
    assert len(entry_ids) == len(set(entry_ids))
    assert detail["metrics"]["total_trades"] == len(trades)
    assert detail["session"]["realized_pnl"] == pytest.approx(sum(t["net_pnl"] for t in trades), abs=0.05)
    equity = client.get(f"/api/paper-sessions/{sid}/equity").json()
    assert equity

    stopped = client.post(f"/api/paper-sessions/{sid}/stop", json={"reason": "test done"}).json()
    assert stopped["status"] == "STOPPED"
    assert client.get(f"/api/paper-sessions/{sid}/positions", params={"status": "OPEN"}).json() == []
    assert trap_kite_orders == []

    summary = client.get("/api/analytics/summary", params={"scope": "paper", "session_id": sid}).json()
    assert summary["metrics"]["total_trades"] == len(trades)


def test_paper_broker_state_survives_restart(database):
    from app.database.models import PaperSession, Strategy, StrategyVersion
    from app.database.session import session_scope
    from app.paper_trading.broker import PaperBroker
    from app.trading.broker import Order
    from app.trading.costs import CostConfig
    from app.trading.fill_model import ExecutionModelConfig
    from app.trading.types import OrderPurpose, OrderStatus, OrderType, Quote, Side
    from tests.helpers import at, option

    with session_scope() as db:
        s = Strategy(name="restart", strategy_key="restart_test")
        db.add(s)
        db.flush()
        v = StrategyVersion(strategy_id=s.id, version_number=1, config_json={}, config_hash="x")
        db.add(v)
        db.flush()
        row = PaperSession(strategy_version_id=v.id, name="r", data_source="REPLAY", initial_capital=100000, strategy_config={},
                           config_hash="x", session_config={}, cash=100000)
        db.add(row)
        db.flush()
        sid = row.id

    inst = option(24050, "CE")
    broker = PaperBroker(sid, 100000, CostConfig(), ExecutionModelConfig())
    q = Quote(inst.key, at(9, 18), ltp=200, bid=199.5, ask=200, source="TEST")
    broker.submit_order(Order("E1", inst, Side.BUY, OrderType.MARKET, 65, OrderPurpose.ENTRY, at(9, 18)), at(9, 18), q)
    broker.submit_order(Order("SL1", inst, Side.SELL, OrderType.STOP, 65, OrderPurpose.EXIT, at(9, 18), trigger_price=150,
                              oco_group="G", exit_reason="STOP_LOSS"), at(9, 18))

    restored = PaperBroker(sid, 100000, CostConfig(), ExecutionModelConfig())
    restored.load_from_db()
    assert restored.cash == pytest.approx(broker.cash, abs=1e-3)
    assert [p.quantity for p in restored.open_positions()] == [65]
    assert [o.client_order_id for o in restored.active_orders()] == ["SL1"]
    again = restored.submit_order(Order("E1", inst, Side.BUY, OrderType.MARKET, 65, OrderPurpose.ENTRY, at(9, 19)), at(9, 19), q)
    assert again.status is OrderStatus.FILLED and len(restored.open_positions()) == 1  # no duplicate order
    restored.on_quote(Quote(inst.key, at(9, 20), ltp=140, bid=140, ask=141, source="TEST"), at(9, 20))
    assert restored.trades and restored.trades[0].exit_reason == "STOP_LOSS"
    assert abs(restored.reconciliation_error()) < 1e-3


def test_migrations_create_schema(tmp_path):
    from sqlalchemy import create_engine, inspect

    from app.database.migrate import upgrade_database

    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    upgrade_database(url)
    tables = set(inspect(create_engine(url)).get_table_names())
    expected = {"strategies", "strategy_versions", "instruments", "market_ticks", "candles", "historical_datasets",
                "backtest_runs", "backtest_trades", "paper_sessions", "paper_orders", "paper_positions", "paper_trades",
                "equity_snapshots", "system_events", "signals"}
    assert expected <= tables


def test_hub_session_waits_when_feed_missing(client):
    status = client.get("/api/market-data/status").json()
    assert status["status"] in ("DISCONNECTED", "ENDED", "CONNECTED")
    r = client.post("/api/market-data/feed/start", json={"source": "KITE"})
    assert r.status_code in (400, 401)  # no Kite credentials in tests


def test_sessions_restored_after_restart(client):
    """Simulate a backend restart: drop in-memory runners, then restore from the database."""
    from app.paper_trading.engine import PaperTradingService
    from app.services.runtime import hub, paper_service

    candles, instruments = synthetic_csv(days=1, seed=3, strikes_each_side=4, start=date(2025, 1, 8))
    ds = client.post("/api/market-data/import", data={"name": "restart-ds", "is_synthetic": "true"},
                     files={"candles_file": ("c.csv", candles), "instruments_file": ("i.csv", instruments)}).json()
    cfg = {**copy.deepcopy(DEFAULT_NIFTY_MOMENTUM_CONFIG), "strategy_id": "restart_api_test"}
    strategy = client.post("/api/strategies", json={"name": "Restart", "config": cfg}).json()
    sid = client.post("/api/paper-sessions", json={"strategy_version_id": strategy["latest_version"]["id"], "name": "restart me",
                                                   "data_source": "REPLAY", "dataset_id": ds["id"]}).json()["id"]
    client.post(f"/api/paper-sessions/{sid}/stop-entries")

    old = paper_service.runners.pop(sid)
    old.detach()  # the old process is gone
    fresh = PaperTradingService(hub)
    assert fresh.restore_all() >= 1
    runner = fresh.get(sid)
    assert runner is not None and runner.status == "ENTRIES_STOPPED"
    assert runner.runner.risk.entries_enabled is False  # stop-entries survives the restart
    runner.detach()
    paper_service.runners[sid] = old  # hand back so other tests see a consistent service
    events = client.get("/api/events", params={"session_id": sid}).json()
    assert any(e["event_type"] == "SESSION_RESTORED" for e in events)
