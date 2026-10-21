import pytest

from app.trading import fill_model as fm
from app.trading.broker import Order, SimulatedBroker
from app.trading.costs import CostConfig, SlippageConfig, compute_charges
from app.trading.fill_model import ExecutionModelConfig
from app.trading.types import OrderPurpose, OrderStatus, OrderType, Quote, Side
from tests.helpers import at, bar, option

INST = option(24050, "CE")
NO_COSTS = CostConfig(enabled=False)
NO_SLIP = ExecutionModelConfig(slippage=SlippageConfig(points=0))


def entry(cid="E1", qty=65, otype=OrderType.MARKET, limit=None):
    return Order(cid, INST, Side.BUY, otype, qty, OrderPurpose.ENTRY, at(9, 18), limit_price=limit)


def exit_order(cid, otype, qty=65, trigger=None, limit=None, oco="G"):
    return Order(cid, INST, Side.SELL, otype, qty, OrderPurpose.EXIT, at(9, 18), trigger_price=trigger, limit_price=limit,
                 oco_group=oco, exit_reason="STOP_LOSS" if otype is OrderType.STOP else "TAKE_PROFIT")


def quote(ltp=None, bid=None, ask=None, ts=None):
    return Quote(INST.key, ts or at(9, 18), ltp=ltp, bid=bid, ask=ask, source="TEST")


def test_prd_gross_pnl_example():
    b = SimulatedBroker(100000, NO_COSTS, NO_SLIP)
    b.submit_order(entry(), at(9, 18), quote(bid=199.5, ask=200))
    b.submit_order(Order("X", INST, Side.SELL, OrderType.MARKET, 65, OrderPurpose.EXIT, at(9, 30), exit_reason="TAKE_PROFIT"),
                   at(9, 30), quote(bid=280, ask=280.5, ts=at(9, 30)))
    t = b.trades[0]
    assert t.gross_pnl == pytest.approx((280 - 200) * 65) == 5200
    assert t.net_pnl == pytest.approx(5200)
    assert b.cash == pytest.approx(105200)


def test_slippage_not_double_counted_and_charges_applied():
    costs = CostConfig()
    b = SimulatedBroker(100000, costs, ExecutionModelConfig(slippage=SlippageConfig(points=0.5)))
    b.submit_order(entry(), at(9, 18), quote(bid=199, ask=200))
    assert b.orders["E1"].average_fill_price == 200.5
    b.submit_order(Order("X", INST, Side.SELL, OrderType.MARKET, 65, OrderPurpose.EXIT, at(9, 30), exit_reason="X"),
                   at(9, 30), quote(bid=280, ask=281, ts=at(9, 30)))
    t = b.trades[0]
    charges = compute_charges(costs, Side.BUY, 200.5, 65, "CE").total + compute_charges(costs, Side.SELL, 279.5, 65, "CE").total
    assert t.gross_pnl == pytest.approx((280 - 200) * 65)
    assert t.slippage_cost == pytest.approx(1.0 * 65)
    assert t.entry_charges + t.exit_charges == pytest.approx(charges)
    assert t.net_pnl == pytest.approx((279.5 - 200.5) * 65 - charges)
    assert b.equity() == pytest.approx(100000 + t.net_pnl)
    assert abs(b.reconciliation_error()) < 1e-6


def test_option_charges_breakdown():
    c = compute_charges(CostConfig(), Side.SELL, 280, 65, "CE")
    turnover = 280 * 65
    assert c.brokerage == 20
    assert c.stt == pytest.approx(turnover * 0.001)
    assert c.stamp_duty == 0
    buy = compute_charges(CostConfig(), Side.BUY, 280, 65, "CE")
    assert buy.stt == 0 and buy.stamp_duty == pytest.approx(turnover * 0.00003)
    assert buy.gst == pytest.approx((20 + turnover * 0.0003503 + turnover * 0.000001) * 0.18)


def _broker_with_position(entry_price=200.0):
    b = SimulatedBroker(100000, NO_COSTS, NO_SLIP)
    b.submit_order(entry(), at(9, 18), quote(ltp=entry_price, bid=entry_price, ask=entry_price))
    b.submit_order(exit_order("SL", OrderType.STOP, trigger=150), at(9, 18))
    b.submit_order(exit_order("TP", OrderType.LIMIT, limit=280), at(9, 18))
    return b


def test_stop_gap_fills_at_open_not_stop_price():
    b = _broker_with_position()
    b.on_bar(INST.key, bar(at(9, 20), 140, 145, 130, 135))
    t = b.trades[0]
    assert t.exit_reason == "STOP_LOSS" and t.exit_ref_price == 140 and "GAP_THROUGH_STOP" in t.flags
    assert b.orders["TP"].status is OrderStatus.CANCELLED


def test_ambiguous_bar_assumes_stop_first():
    b = _broker_with_position()
    b.on_bar(INST.key, bar(at(9, 20), 200, 290, 140, 210))
    t = b.trades[0]
    assert t.exit_reason == "STOP_LOSS" and t.exit_ref_price == 150 and "AMBIGUOUS_INTRABAR" in t.flags


def test_target_requires_trade_through_and_gap_up_fills_at_limit():
    b = _broker_with_position()
    b.on_bar(INST.key, bar(at(9, 20), 250, 280, 240, 260))  # touches 280, does not trade through
    assert not b.trades
    b.on_bar(INST.key, bar(at(9, 21), 300, 310, 295, 305))  # gaps above target
    t = b.trades[0]
    assert t.exit_reason == "TAKE_PROFIT" and t.exit_price == 280 and "GAP_FILLED_AT_LIMIT" in t.flags


def test_quote_based_stop_uses_bid_with_slippage():
    b = SimulatedBroker(100000, NO_COSTS, ExecutionModelConfig(slippage=SlippageConfig(points=0.5)))
    b.submit_order(entry(), at(9, 18), quote(bid=199.5, ask=200))
    b.submit_order(exit_order("SL", OrderType.STOP, trigger=150.5), at(9, 18))
    b.on_quote(quote(ltp=152, bid=151, ask=152, ts=at(9, 19)), at(9, 19))
    assert not b.trades
    b.on_quote(quote(ltp=149, bid=148, ask=149, ts=at(9, 20)), at(9, 20))
    t = b.trades[0]
    assert t.exit_ref_price == 148 and t.exit_price == 147.5


def test_stale_quote_cannot_fill_market_order():
    b = SimulatedBroker(100000, NO_COSTS, NO_SLIP)
    o = b.submit_order(entry(), at(9, 18), quote(bid=199, ask=200, ts=at(9, 17)))
    assert o.status is OrderStatus.PENDING and o.meta["last_fill_failure"] == "STALE_QUOTE"


def test_submit_is_idempotent_and_validates():
    b = SimulatedBroker(100000, NO_COSTS, NO_SLIP)
    first = b.submit_order(entry(), at(9, 18), quote(ask=200, bid=199))
    again = b.submit_order(entry(), at(9, 18), quote(ask=300, bid=299))
    assert again is first and len(b.orders) == 1 and len(b.open_positions()) == 1
    bad = b.submit_order(entry("E2", qty=10), at(9, 18))
    assert bad.status is OrderStatus.REJECTED and "lot size" in bad.rejection_reason
    flip = b.submit_order(Order("F", INST, Side.SELL, OrderType.MARKET, 130, OrderPurpose.EXIT, at(9, 18)), at(9, 18))
    assert flip.status is OrderStatus.REJECTED


def test_partial_exit_accounting_reconciles():
    b = SimulatedBroker(100000, CostConfig(), NO_SLIP)
    b.submit_order(entry(qty=130), at(9, 18), quote(bid=100, ask=100))
    b.submit_order(Order("P1", INST, Side.SELL, OrderType.MARKET, 65, OrderPurpose.EXIT, at(9, 20), exit_reason="X"), at(9, 20),
                   quote(bid=110, ask=110, ts=at(9, 20)))
    assert b.open_positions()[0].quantity == 65 and not b.trades
    b.positions[INST.key].last_price = 105
    assert abs(b.reconciliation_error()) < 1e-6
    b.submit_order(Order("P2", INST, Side.SELL, OrderType.MARKET, 65, OrderPurpose.EXIT, at(9, 25), exit_reason="X"), at(9, 25),
                   quote(bid=90, ask=90, ts=at(9, 25)))
    t = b.trades[0]
    assert t.quantity == 130 and t.exit_ref_price == pytest.approx(100)
    assert b.equity() == pytest.approx(100000 + t.net_pnl)


def test_bar_open_quote_marks_fill():
    q = fm.bar_open_quote(INST.key, bar(at(9, 18), 200, 210, 190, 205))
    d = fm.market_fill_quote(Side.BUY, q, at(9, 18), ExecutionModelConfig(), 0.05)
    assert d.reference_price == 200 and d.fill_price == 200.5 and "BAR_OPEN_FILL" in d.flags
