"""Real-money (LIVE) execution against a fake Zerodha.

``FakeZerodha`` stands in for Kite Connect: it records every request, keeps an order book and a
net-position table, and lets each test decide what the exchange does (fill, reject, time out,
trigger without filling, or show the position already closed in the Kite app).
"""

from __future__ import annotations

import copy
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.instruments.contract_resolver import ListCatalog
from app.live_trading.broker import LiveBroker
from app.strategies.config_schema import DEFAULT_NIFTY_MOMENTUM_CONFIG
from app.strategies.registry import build_strategy
from app.trading.costs import CostConfig
from app.trading.fill_model import ExecutionModelConfig
from app.trading.runner import StrategyRunner
from app.trading.types import Candle, OrderPurpose, OrderStatus, Quote
from tests.helpers import at, option

TERMINAL = ("COMPLETE", "CANCELLED", "REJECTED")


class ReadTimeout(Exception):
    """Same class name as requests' timeout: the request may or may not have reached Zerodha."""


class FakeZerodha:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.book: dict[str, dict] = {}
        self.net: dict[str, int] = {}
        self.reject_next: Exception | None = None  # raise this on the next place_order (nothing is placed)
        self.timeout_next = False  # place the next order, then raise a timeout (as if the reply was lost)
        self._seq = 0

    # -- Kite Connect methods used by the live broker
    def place_order(self, **kw):
        self.calls.append(("place_order", kw))
        if self.reject_next is not None:
            exc, self.reject_next = self.reject_next, None
            raise exc
        self._seq += 1
        oid = f"Z{self._seq}"
        self.book[oid] = {**kw, "order_id": oid, "status": "TRIGGER PENDING" if kw["order_type"] == "SL" else "OPEN",
                          "filled_quantity": 0, "average_price": 0.0}
        if self.timeout_next:
            self.timeout_next = False
            raise ReadTimeout("read timed out")
        return oid

    def modify_order(self, **kw):
        self.calls.append(("modify_order", kw))
        order = self.book[kw["order_id"]]
        if order["status"] in TERMINAL:
            raise Exception("Order cannot be modified as it is being processed")
        order.update({k: v for k, v in kw.items() if k not in ("variety", "order_id")})
        if order["order_type"] == "LIMIT" and order["status"] == "TRIGGER PENDING":
            order["status"] = "OPEN"

    def cancel_order(self, **kw):
        self.calls.append(("cancel_order", kw))
        order = self.book[kw["order_id"]]
        if order["status"] in TERMINAL:
            raise Exception("Order cannot be cancelled as it is being processed")
        order["status"] = "CANCELLED"

    def orders(self):
        return [dict(o) for o in self.book.values()]

    def positions(self):
        self.calls.append(("positions", {}))
        return {"net": [{"tradingsymbol": s, "exchange": "NFO", "product": "MIS", "quantity": q} for s, q in self.net.items()]}

    # -- test controls
    def fill(self, oid: str, price: float, qty: int | None = None) -> None:
        order = self.book[oid]
        qty = qty or order["quantity"]
        delta = qty - order["filled_quantity"]
        order.update(filled_quantity=qty, average_price=price, status="COMPLETE" if qty == order["quantity"] else "OPEN")
        sign = 1 if order["transaction_type"] == "BUY" else -1
        self.net[order["tradingsymbol"]] = self.net.get(order["tradingsymbol"], 0) + sign * delta

    def placed(self) -> list[dict]:
        return [kw for name, kw in self.calls if name == "place_order"]

    def named(self, method: str) -> list[dict]:
        return [kw for name, kw in self.calls if name == method]


def make_session_row(trading_mode: str = "LIVE") -> int:
    from app.database.models import PaperSession, Strategy, StrategyVersion
    from app.database.session import session_scope

    with session_scope() as db:
        s = Strategy(name="live test", strategy_key=f"live_{uuid.uuid4().hex[:10]}")
        db.add(s)
        db.flush()
        v = StrategyVersion(strategy_id=s.id, version_number=1, config_json={}, config_hash="x")
        db.add(v)
        db.flush()
        row = PaperSession(strategy_version_id=v.id, name="live", data_source="KITE", trading_mode=trading_mode, initial_capital=200000,
                           strategy_config={}, config_hash="x", session_config={"costs": {"enabled": False}}, cash=200000)
        db.add(row)
        db.flush()
        return row.id


class Sink:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.signals: list[tuple[str, str | None]] = []

    def event(self, severity, event_type, message, details=None, ts=None):
        self.events.append(event_type)

    def record_signal(self, signal, outcome, reason, details):
        self.signals.append((outcome, reason))


def build(lots: int = 1):
    """A StrategyRunner on a LiveBroker wired to a fake Zerodha, with a NIFTY 24150 CE quote available."""
    config = copy.deepcopy(DEFAULT_NIFTY_MOMENTUM_CONFIG)
    config["option_selection"]["quantity_lots"] = lots
    strategy = build_strategy(config)
    ce = option(24150, "CE")
    zerodha = FakeZerodha()
    clock = {"now": at(9, 18)}
    broker = LiveBroker(make_session_row(), 200000, CostConfig(enabled=False), ExecutionModelConfig(), lambda: zerodha,
                        lambda: clock["now"], settings=get_settings())
    broker.poll_seconds = 0  # tests poll on demand
    alerts: list[tuple[str, str]] = []
    broker.alert = lambda severity, event_type, message, details=None, ts=None: alerts.append((severity, event_type))
    quotes = {ce.key: Quote(ce.key, clock["now"], ltp=120, bid=119.5, ask=120, source="TEST")}

    class Quotes:
        def get_quote(self, inst, now):
            return quotes.get(inst.key)

        def data_freshness(self, now):
            return True, None

    sink = Sink()
    runner = StrategyRunner(strategy, broker, ListCatalog([ce]), Quotes(), sink, "LS")

    def poll(seconds: float = 0.0) -> None:
        clock["now"] = clock["now"] + timedelta(seconds=seconds)
        broker.on_time(clock["now"])

    def signal() -> None:
        # Bullish 3-minute candle: range 125 > 100, body 100 > 80 -> buy the 24150 CALL.
        runner.on_candle(Candle(at(9, 15), 180, 24000, 24120, 23995, 24100), clock["now"])

    return SimpleNamespace(broker=broker, runner=runner, zerodha=zerodha, ce=ce, clock=clock, quotes=quotes, alerts=alerts,
                           sink=sink, poll=poll, signal=signal)


def enter(t) -> tuple[str, str]:
    """Signal, fill the entry at 120.5 and return (entry order id, stop-loss order id) at Zerodha."""
    t.signal()
    entry_id = next(o["order_id"] for o in t.zerodha.book.values() if o["transaction_type"] == "BUY")
    t.zerodha.fill(entry_id, 120.5)
    t.poll()
    sl_id = next(o["order_id"] for o in t.zerodha.book.values() if o["order_type"] == "SL")
    return entry_id, sl_id


def test_entry_is_a_protected_mis_limit_order_and_stop_rests_at_zerodha(database):
    t = build()
    t.signal()
    [entry] = t.zerodha.placed()
    assert entry["tradingsymbol"] == t.ce.tradingsymbol and entry["exchange"] == "NFO"
    assert entry["transaction_type"] == "BUY" and entry["quantity"] == 65 and entry["product"] == "MIS"
    assert entry["order_type"] == "LIMIT" and entry["price"] == pytest.approx(122.4)  # ask 120 + 2 %, never a market order
    assert entry["tag"].startswith("AL") and len(entry["tag"]) <= 20
    assert not t.broker.open_positions()  # nothing is booked until Zerodha reports a fill

    t.zerodha.fill("Z1", 120.5)
    t.poll()
    [position] = t.broker.open_positions()
    assert position.quantity == 65 and position.avg_entry_fill == pytest.approx(120.5)
    entry_order, stop_order = t.zerodha.placed()
    # Stop-loss at Zerodha: SL (stop-limit) sell, trigger = fill - 50, limit 5 % below the trigger.
    assert stop_order["order_type"] == "SL" and stop_order["transaction_type"] == "SELL" and stop_order["product"] == "MIS"
    assert stop_order["trigger_price"] == pytest.approx(70.5) and stop_order["price"] == pytest.approx(66.95)
    # The target (fill + 80) is watched by the app, not sent to Zerodha.
    target = next(o for o in t.broker.active_orders() if o.meta.get("virtual"))
    assert target.limit_price == pytest.approx(200.5)
    assert len(t.zerodha.placed()) == 2


def test_target_converts_the_stop_into_the_exit_so_the_position_is_never_sold_twice(database):
    t = build()
    _, sl_id = enter(t)
    t.runner.on_quote(Quote(t.ce.key, t.clock["now"], ltp=201, bid=201, ask=201.5, source="TEST"), t.clock["now"])
    [modify] = t.zerodha.named("modify_order")
    assert modify["order_id"] == sl_id and modify["order_type"] == "LIMIT" and modify["price"] == pytest.approx(196.95)
    assert len(t.zerodha.placed()) == 2  # no second sell order
    t.zerodha.fill(sl_id, 201.0)
    t.poll()
    [trade] = t.broker.trades
    assert trade.exit_reason == "TAKE_PROFIT" and trade.net_pnl == pytest.approx((201.0 - 120.5) * 65)
    assert not t.broker.open_positions() and not t.broker.active_orders()
    assert t.zerodha.net[t.ce.tradingsymbol] == 0


def test_stop_loss_filled_at_zerodha_closes_the_trade(database):
    t = build()
    _, sl_id = enter(t)
    t.zerodha.fill(sl_id, 70.0)
    t.poll()
    [trade] = t.broker.trades
    assert trade.exit_reason == "STOP_LOSS" and trade.exit_price == pytest.approx(70.0)
    assert not t.broker.active_orders()  # the app-side target was dropped with the position


def test_triggered_stop_that_does_not_fill_is_repriced(database):
    t = build()
    _, sl_id = enter(t)
    t.zerodha.book[sl_id]["status"] = "OPEN"  # triggered, but the price fell through the limit
    t.quotes[t.ce.key] = Quote(t.ce.key, t.clock["now"], ltp=60, bid=60, ask=61, source="TEST")
    t.broker.on_quote(t.quotes[t.ce.key], t.clock["now"])
    t.poll(seconds=4)
    [modify] = t.zerodha.named("modify_order")
    assert modify["order_id"] == sl_id and modify["order_type"] == "LIMIT" and modify["price"] == pytest.approx(57.6)  # bid 60 - 4 %
    t.zerodha.fill(sl_id, 59.9)
    t.poll()
    assert t.broker.trades[0].exit_reason == "STOP_LOSS" and not t.broker.open_positions()


def test_position_closed_in_kite_app_is_not_sold_again(database):
    t = build()
    _, sl_id = enter(t)
    t.zerodha.net[t.ce.tradingsymbol] = 0  # the user exited in the Kite app
    t.runner.close_all("SESSION_STOPPED", t.clock["now"], force=True)
    assert len(t.zerodha.placed()) == 2 and not t.zerodha.named("modify_order")  # no sell order sent
    assert [c["order_id"] for c in t.zerodha.named("cancel_order")] == [sl_id]  # the resting stop is cancelled
    [trade] = t.broker.trades
    assert "EXTERNAL_EXIT" in trade.flags
    assert ("WARNING", "LIVE_EXTERNAL_EXIT") in t.alerts


def test_rejected_stop_loss_exits_immediately(database):
    t = build()
    t.signal()
    t.zerodha.reject_next = Exception("Trigger price for stoploss sell orders should be lower than the last traded price")
    t.zerodha.fill("Z1", 120.5)
    t.poll()
    assert ("CRITICAL", "LIVE_STOP_MISSING") in t.alerts
    exit_order = t.zerodha.placed()[-1]
    assert exit_order["transaction_type"] == "SELL" and exit_order["order_type"] == "LIMIT" and exit_order["quantity"] == 65


def test_failed_exit_is_retried_but_not_on_every_clock_tick(database):
    t = build()
    t.signal()
    t.zerodha.fill("Z1", 120.5)
    t.zerodha.reject_next = Exception("RMS: blocked")  # the stop-loss is rejected...
    t.poll()
    exits_after_first_try = len(t.zerodha.placed())
    for _ in range(5):  # ...and the runner asks to exit again on every 0.5 s tick
        t.runner.close_position(t.broker.open_positions()[0], "SESSION_END", t.clock["now"])
    assert len(t.zerodha.placed()) == exits_after_first_try
    t.poll(seconds=4)  # after the re-price interval the exit is still working, so nothing new is needed
    assert len([o for o in t.zerodha.placed() if o["transaction_type"] == "SELL" and o["order_type"] == "LIMIT"]) == 1


def test_lot_cap_blocks_orders_above_live_max_lots(database):
    t = build(lots=2)
    t.signal()
    assert t.zerodha.placed() == []  # rejected before reaching Zerodha
    assert any(outcome == "BLOCKED" and "LIVE_MAX_LOTS" in (reason or "") for outcome, reason in t.sink.signals)


def test_unconfirmed_placement_is_found_by_tag_and_not_duplicated(database):
    t = build()
    t.zerodha.timeout_next = True
    t.signal()
    assert ("WARNING", "LIVE_ORDER_UNCONFIRMED") in t.alerts
    t.zerodha.fill("Z1", 120.5)
    t.poll()
    assert [p.quantity for p in t.broker.open_positions()] == [65]
    assert sum(1 for o in t.zerodha.placed() if o["transaction_type"] == "BUY") == 1


def test_unfilled_entry_is_cancelled_after_timeout(database):
    t = build()
    t.signal()
    t.poll(seconds=11)
    assert [c["order_id"] for c in t.zerodha.named("cancel_order")] == ["Z1"]
    t.poll()
    entry = next(o for o in t.broker.orders.values() if o.purpose is OrderPurpose.ENTRY)
    assert entry.status is OrderStatus.CANCELLED and not t.broker.open_positions()
    assert t.sink.signals[-1][0] == "SKIPPED"


def test_partly_filled_entry_keeps_and_protects_the_filled_quantity(database):
    # F&O fills come in whole lots, so a partial fill needs a multi-lot order (cap raised to 2 for this test).
    t = build(lots=2)
    t.broker.max_lots = 2
    t.signal()
    t.zerodha.fill("Z1", 120.5, qty=65)  # 1 of 2 lots filled
    t.poll(seconds=11)  # entry timeout -> cancel the rest
    t.poll()
    [position] = t.broker.open_positions()
    assert position.quantity == 65
    stop = next(o for o in t.zerodha.book.values() if o["order_type"] == "SL")
    assert stop["quantity"] == 65 and stop["transaction_type"] == "SELL"


def test_live_session_stays_stopping_until_zerodha_confirms_flat(database, monkeypatch):
    from app.database.models import PaperSession
    from app.database.session import SessionLocal
    from app.market_data.hub import MarketDataHub
    from app.market_data.kite_client import kite_sessions
    from app.paper_trading.engine import PaperSessionRunner
    from app.trading.types import InstrumentRef

    zerodha = FakeZerodha()
    monkeypatch.setattr(kite_sessions, "order_client", lambda: zerodha)
    sid = make_session_row()
    with SessionLocal() as db:
        row = db.get(PaperSession, sid)
        row.strategy_config = DEFAULT_NIFTY_MOMENTUM_CONFIG
        ce = option(24150, "CE")
        underlying = InstrumentRef(key="NSE:NIFTY 50", exchange="NSE", tradingsymbol="NIFTY 50", instrument_type="INDEX")
        session = PaperSessionRunner(row, MarketDataHub(), ListCatalog([ce]), underlying, None)
    assert session.live and isinstance(session.broker, LiveBroker)
    session.broker.poll_seconds = 0
    finished = []
    session.on_finished = finished.append
    session.start(restore=False, runtime_state=None)

    from app.trading.broker import Order
    from app.trading.types import OrderType, Side

    now = session.clock()
    session.broker.submit_order(Order("E1", ce, Side.BUY, OrderType.MARKET, 65, OrderPurpose.ENTRY, now),
                                now, Quote(ce.key, now, ltp=120, bid=119.5, ask=120, source="TEST"))
    zerodha.fill("Z1", 120.5)
    session.on_time(now)  # poll -> entry filled -> stop-loss placed at Zerodha
    sl_id = next(o["order_id"] for o in zerodha.book.values() if o["order_type"] == "SL")

    session.stop("test stop")
    assert session.status == "STOPPING" and finished == []  # still open at Zerodha
    zerodha.fill(sl_id, 121.0)  # the converted exit fills
    session.on_time(session.clock())
    assert session.status == "STOPPED" and finished == [sid]
    with SessionLocal() as db:
        assert db.get(PaperSession, sid).status == "STOPPED"


def test_api_requires_confirmation_kite_data_and_login_for_real_money(client):
    templates = client.get("/api/strategies/templates").json()
    key = f"live_api_{uuid.uuid4().hex[:8]}"
    created = client.post("/api/strategies", json={"name": "Live API", "strategy_key": key,
                                                  "config": {**templates["nifty_momentum"], "strategy_id": key, "name": "Live API"}})
    assert created.status_code == 201, created.text
    version_id = created.json()["latest_version"]["id"]
    body = {"strategy_version_id": version_id, "name": "real", "data_source": "KITE", "trading_mode": "LIVE"}

    r = client.post("/api/paper-sessions", json=body)
    assert r.status_code == 400 and "confirm" in r.text
    r = client.post("/api/paper-sessions", json={**body, "confirm_live": True, "data_source": "REPLAY", "dataset_id": 1})
    assert r.status_code == 400 and "live Kite data" in r.text
    r = client.post("/api/paper-sessions", json={**body, "confirm_live": True})
    assert r.status_code == 400 and "Log in to Kite" in r.text


def test_replay_feed_is_refused_while_a_real_money_session_runs(client, monkeypatch):
    from app.services.runtime import paper_service

    monkeypatch.setattr(paper_service, "has_live_sessions", lambda: True)
    r = client.post("/api/market-data/feed/start", json={"source": "REPLAY", "dataset_id": 1})
    assert r.status_code == 409 and "real-money" in r.text
