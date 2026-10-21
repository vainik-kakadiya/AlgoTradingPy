"""End-to-end API workflow: strategy -> dataset import -> backtest -> analytics."""

from __future__ import annotations

import copy
import time

import pytest

from app.strategies.config_schema import DEFAULT_NIFTY_MOMENTUM_CONFIG
from tests.helpers import synthetic_csv


def wait_for(fn, timeout=90.0, interval=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met before timeout")


@pytest.fixture(scope="module")
def strategy(client):
    r = client.post("/api/strategies", json={"name": "Momentum test", "config": DEFAULT_NIFTY_MOMENTUM_CONFIG})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture(scope="module")
def dataset(client):
    candles, instruments = synthetic_csv(days=3, seed=7)
    r = client.post(
        "/api/market-data/import",
        data={"name": "synthetic-3d", "source_label": "SYNTHETIC test data", "is_synthetic": "true"},
        files={"candles_file": ("candles.csv", candles, "text/csv"), "instruments_file": ("instruments.csv", instruments, "text/csv")},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_health_and_settings_hide_secrets(client):
    assert client.get("/api/health").json()["execution_mode"] == "PAPER_ONLY"
    s = client.get("/api/settings").json()
    assert "kite_api_secret" not in s and "kite_access_token" not in s and s["execution_mode"] == "PAPER_ONLY"
    status = client.get("/api/system/status").json()
    assert status["real_orders_possible"] is False


def test_strategy_crud_and_versioning(client, strategy):
    sid = strategy["id"]
    assert strategy["strategy_key"] == "nifty_3m_momentum_v1"
    assert strategy["latest_version"]["version_number"] == 1
    cfg = copy.deepcopy(DEFAULT_NIFTY_MOMENTUM_CONFIG)
    same = client.post(f"/api/strategies/{sid}/versions", json={"config": cfg})
    assert same.status_code == 409 and same.json()["error"]["code"] == "UNCHANGED_CONFIG"
    cfg["exits"]["stop_loss_points"] = 40
    v2 = client.post(f"/api/strategies/{sid}/versions", json={"config": cfg, "notes": "tighter stop"})
    assert v2.status_code == 201 and v2.json()["version_number"] == 2
    versions = client.get(f"/api/strategies/{sid}/versions").json()
    assert [v["version_number"] for v in versions] == [2, 1]
    assert versions[1]["config_json"]["exits"]["stop_loss_points"] == 50  # v1 never modified

    dup = client.post(f"/api/strategies/{sid}/duplicate", json={"name": "Copy", "strategy_key": "momentum_copy"})
    assert dup.status_code == 201 and dup.json()["latest_version"]["config_json"]["strategy_id"] == "momentum_copy"
    upd = client.put(f"/api/strategies/{dup.json()['id']}", json={"is_enabled": False})
    assert upd.json()["is_enabled"] is False


def test_real_orders_rejected_by_api(client, strategy):
    bad = copy.deepcopy(DEFAULT_NIFTY_MOMENTUM_CONFIG)
    bad["execution"]["allow_real_orders"] = True
    r = client.post(f"/api/strategies/{strategy['id']}/versions", json={"config": bad})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_STRATEGY_CONFIG"
    v = client.post("/api/strategies/validate", json={"config": {**DEFAULT_NIFTY_MOMENTUM_CONFIG, "mode": "live"}}).json()
    assert v["valid"] is False


def test_dataset_import_records_provenance_and_quality(client, dataset):
    assert dataset["is_synthetic"] is True and dataset["interval_seconds"] == 60
    assert dataset["checksum"] and dataset["status"] == "READY"
    q = dataset["quality_summary"]
    assert q["rejected_rows"] == 0 and q["missing_intervals"] == 0
    insts = client.get("/api/instruments", params={"dataset_id": dataset["id"], "instrument_type": "CE"}).json()
    assert insts["total"] > 0 and all(i["lot_size"] == 65 for i in insts["items"])


def test_dataset_import_rejects_bad_data(client):
    csv = b"timestamp,tradingsymbol,open,high,low,close\n2025-01-06 09:15:00,NIFTY 50,100,90,95,98\n2025-01-06 09:16:00,NIFTY 50,abc,1,1,1\n"
    r = client.post("/api/market-data/import", data={"name": "bad"}, files={"candles_file": ("c.csv", csv, "text/csv")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "DATASET_INVALID"
    unknown = b"timestamp,tradingsymbol,open,high,low,close\n2025-01-06 09:15:00,FOO,100,101,99,100\n2025-01-06 09:16:00,FOO,100,101,99,100\n"
    r2 = client.post("/api/market-data/import", data={"name": "unknown"}, files={"candles_file": ("c.csv", unknown, "text/csv")})
    assert r2.status_code == 422 and "instrument metadata" in r2.json()["error"]["message"]


def test_backtest_runs_and_is_reproducible(client, strategy, dataset):
    version_id = strategy["latest_version"]["id"]
    body = {"strategy_version_id": version_id, "dataset_id": dataset["id"], "initial_capital": 200000}
    ids = []
    for _ in range(2):
        r = client.post("/api/backtests", json=body)
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    runs = [wait_for(lambda i=i: (b := client.get(f"/api/backtests/{i}").json())["status"] in ("COMPLETED", "FAILED") and b) for i in ids]
    for run in runs:
        assert run["status"] == "COMPLETED", run.get("error")
        assert run["report"]["reconciliation_error"] == pytest.approx(0, abs=1e-4)
        assert any("SYNTHETIC" in w for w in run["report"]["warnings"])
        assert run["report"]["dataset"]["checksum"] == dataset["checksum"]
    a, b = runs
    for key in ("total_trades", "net_pnl", "gross_pnl", "win_rate", "max_drawdown"):
        assert a[key] == b[key], key

    trades = client.get(f"/api/backtests/{a['id']}/trades").json()
    assert len(trades) == a["total_trades"]
    for t in trades:
        assert t["lot_size"] == 65 and t["quantity"] % 65 == 0
        assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["entry_charges"] - t["exit_charges"] - t["slippage_cost"], abs=1e-3)
        assert "desired_strike" in t["details"]["resolution"] and "selected_strike" in t["details"]["resolution"]
    signals = client.get(f"/api/backtests/{a['id']}/signals").json()
    assert signals and all(s["outcome"] in ("ENTERED", "SKIPPED", "BLOCKED") for s in signals)
    equity = client.get(f"/api/backtests/{a['id']}/equity").json()
    assert equity and equity[0]["equity"] == 200000

    summary = client.get("/api/analytics/summary", params={"scope": "backtest", "backtest_id": a["id"]}).json()
    assert summary["metrics"]["total_trades"] == a["total_trades"]
    assert summary["metrics"]["net_pnl"] == pytest.approx(a["net_pnl"])
    csv = client.get(f"/api/backtests/{a['id']}/trades.csv")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")

    assert client.delete(f"/api/datasets/{dataset['id']}").status_code == 409  # results stay reproducible


def test_backtest_rejects_insufficient_granularity(client, dataset):
    cfg = {**DEFAULT_NIFTY_MOMENTUM_CONFIG, "strategy_id": "sub_minute", "timeframe_seconds": 30}
    s = client.post("/api/strategies", json={"name": "30s", "config": cfg}).json()
    r = client.post("/api/backtests", json={"strategy_version_id": s["latest_version"]["id"], "dataset_id": dataset["id"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INSUFFICIENT_GRANULARITY"


def test_events_and_dashboard(client):
    events = client.get("/api/events", params={"limit": 50}).json()
    assert any(e["event_type"] == "BACKTEST_COMPLETED" for e in events)
    d = client.get("/api/dashboard").json()
    assert d["system"]["execution_mode"] == "PAPER_ONLY"
    assert "paper_all_time" in d and "recent_alerts" in d


def test_error_format_for_missing_resources(client):
    r = client.get("/api/backtests/99999")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    r = client.post("/api/backtests", json={"strategy_version_id": "x"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
