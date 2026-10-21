"""Simulated broker: order state machine, fills, positions, cash and round-trip trades.

This is the only component that turns orders into fills. It has no network access and
no reference to any real broker API. The backtester uses it directly (in memory); the
paper broker subclasses it to persist every state change.

Accounting convention
---------------------
* ``cash`` moves with every fill: buys debit ``price × qty + charges``; sells credit
  ``price × qty − charges``.
* ``equity = cash + Σ signed_qty × mark_price`` over open positions.
* Unrealized P&L of an open position = ``direction × open_qty × (mark − avg_entry_fill)``
  minus the not-yet-realized share of entry charges, so that
  ``equity = initial_capital + realized_net + unrealized`` always reconciles.
* Trade P&L: ``gross = direction × qty × (exit_ref − entry_ref)`` on pre-slippage reference
  prices; ``slippage_cost`` is the difference between reference and fill prices;
  ``net = gross − charges − slippage_cost`` (equivalently fill-price P&L minus charges,
  so slippage is never double counted).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.trading import fill_model as fm
from app.trading.costs import CostConfig, compute_charges
from app.trading.fill_model import ExecutionModelConfig, FillDecision
from app.trading.types import (
    Candle,
    Direction,
    InstrumentRef,
    OrderPurpose,
    OrderStatus,
    OrderType,
    Quote,
    Side,
)

logger = logging.getLogger(__name__)
EPS = 1e-9


@dataclass
class Order:
    client_order_id: str
    instrument: InstrumentRef
    side: Side
    order_type: OrderType
    quantity: int
    purpose: OrderPurpose
    created_at: datetime
    limit_price: float | None = None
    trigger_price: float | None = None
    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: int = 0
    average_fill_price: float | None = None
    average_reference_price: float | None = None
    charges: float = 0.0
    updated_at: datetime | None = None
    filled_at: datetime | None = None
    expires_at: datetime | None = None
    signal_id: str | None = None
    oco_group: str | None = None
    exit_reason: str | None = None
    rejection_reason: str | None = None
    position_uid: str | None = None
    fills: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled_quantity

    @property
    def is_active(self) -> bool:
        return not self.status.is_terminal

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "instrument": self.instrument.to_dict(),
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": self.quantity,
            "purpose": self.purpose.value,
            "created_at": self.created_at.isoformat(),
            "limit_price": self.limit_price,
            "trigger_price": self.trigger_price,
            "status": self.status.value,
            "filled_quantity": self.filled_quantity,
            "average_fill_price": self.average_fill_price,
            "average_reference_price": self.average_reference_price,
            "charges": self.charges,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "signal_id": self.signal_id,
            "oco_group": self.oco_group,
            "exit_reason": self.exit_reason,
            "rejection_reason": self.rejection_reason,
            "position_uid": self.position_uid,
            "fills": self.fills,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Order:
        def dt(v: str | None) -> datetime | None:
            return datetime.fromisoformat(v) if v else None

        return cls(
            client_order_id=d["client_order_id"],
            instrument=InstrumentRef.from_dict(d["instrument"]),
            side=Side(d["side"]),
            order_type=OrderType(d["order_type"]),
            quantity=d["quantity"],
            purpose=OrderPurpose(d["purpose"]),
            created_at=datetime.fromisoformat(d["created_at"]),
            limit_price=d.get("limit_price"),
            trigger_price=d.get("trigger_price"),
            status=OrderStatus(d["status"]),
            filled_quantity=d.get("filled_quantity", 0),
            average_fill_price=d.get("average_fill_price"),
            average_reference_price=d.get("average_reference_price"),
            charges=d.get("charges", 0.0),
            updated_at=dt(d.get("updated_at")),
            filled_at=dt(d.get("filled_at")),
            expires_at=dt(d.get("expires_at")),
            signal_id=d.get("signal_id"),
            oco_group=d.get("oco_group"),
            exit_reason=d.get("exit_reason"),
            rejection_reason=d.get("rejection_reason"),
            position_uid=d.get("position_uid"),
            fills=d.get("fills", []),
            meta=d.get("meta", {}),
        )


@dataclass
class Position:
    position_uid: str
    instrument: InstrumentRef
    direction: Direction
    opened_at: datetime
    signal_id: str | None = None
    quantity: int = 0  # currently open quantity
    entry_quantity: int = 0  # total quantity ever entered
    avg_entry_fill: float = 0.0
    avg_entry_ref: float = 0.0
    entry_charges: float = 0.0
    exit_quantity: int = 0
    avg_exit_fill: float = 0.0
    avg_exit_ref: float = 0.0
    exit_charges: float = 0.0
    realized_net: float = 0.0  # realized P&L of exited quantity, net of charges
    stop_loss: float | None = None
    take_profit: float | None = None
    last_price: float | None = None
    last_price_ts: datetime | None = None
    status: str = "OPEN"
    closed_at: datetime | None = None
    exit_reason: str | None = None
    signal_ts: datetime | None = None
    flags: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status == "OPEN"

    def unrealized(self, mark: float | None = None) -> float:
        price = self.last_price if mark is None else mark
        if not self.is_open or self.quantity == 0 or price is None:
            return 0.0
        open_share = self.quantity / self.entry_quantity if self.entry_quantity else 0.0
        return self.direction.sign * self.quantity * (price - self.avg_entry_fill) - self.entry_charges * open_share

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["instrument"] = self.instrument.to_dict()
        d["direction"] = self.direction.value
        for key in ("opened_at", "last_price_ts", "closed_at", "signal_ts"):
            d[key] = d[key].isoformat() if d[key] else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Position:
        d = dict(d)
        d["instrument"] = InstrumentRef.from_dict(d["instrument"])
        d["direction"] = Direction(d["direction"])
        for key in ("opened_at", "last_price_ts", "closed_at", "signal_ts"):
            d[key] = datetime.fromisoformat(d[key]) if d.get(key) else None
        return cls(**d)


@dataclass
class TradeRecord:
    trade_uid: str
    instrument: InstrumentRef
    direction: Direction
    quantity: int
    signal_id: str | None
    signal_ts: datetime | None
    entry_ts: datetime
    entry_price: float
    entry_ref_price: float
    exit_ts: datetime
    exit_price: float
    exit_ref_price: float
    entry_charges: float
    exit_charges: float
    slippage_cost: float
    gross_pnl: float
    net_pnl: float
    exit_reason: str
    flags: list[str]
    details: dict[str, Any]

    @property
    def lots(self) -> float:
        return self.quantity / self.instrument.lot_size if self.instrument.lot_size else float(self.quantity)

    def to_row(self) -> dict[str, Any]:
        """Column values shared by the backtest_trades and paper_trades tables."""
        inst = self.instrument
        return {
            "trade_uid": self.trade_uid,
            "instrument_id": inst.instrument_id,
            "instrument_key": inst.key,
            "tradingsymbol": inst.tradingsymbol,
            "instrument_type": inst.instrument_type,
            "underlying": inst.underlying,
            "strike": inst.strike,
            "expiry": inst.expiry,
            "lot_size": inst.lot_size,
            "lots": self.lots,
            "quantity": self.quantity,
            "direction": self.direction.value,
            "signal_id": self.signal_id,
            "signal_ts": self.signal_ts,
            "entry_ts": self.entry_ts,
            "entry_price": self.entry_price,
            "entry_ref_price": self.entry_ref_price,
            "exit_ts": self.exit_ts,
            "exit_price": self.exit_price,
            "exit_ref_price": self.exit_ref_price,
            "entry_charges": round(self.entry_charges, 4),
            "exit_charges": round(self.exit_charges, 4),
            "slippage_cost": round(self.slippage_cost, 4),
            "gross_pnl": round(self.gross_pnl, 4),
            "net_pnl": round(self.net_pnl, 4),
            "exit_reason": self.exit_reason,
            "flags": self.flags,
            "details": self.details,
        }


class BrokerListener(Protocol):
    def on_order_update(self, order: Order) -> None: ...
    def on_fill(self, order: Order, fill: dict[str, Any]) -> None: ...
    def on_position_update(self, position: Position) -> None: ...
    def on_trade_closed(self, trade: TradeRecord, position: Position) -> None: ...


class OrderRejected(Exception):
    pass


class SimulatedBroker:
    """In-memory simulated broker. Never communicates with a real broker."""

    def __init__(
        self,
        initial_capital: float,
        cost_config: CostConfig | None = None,
        execution_config: ExecutionModelConfig | None = None,
        listeners: list[BrokerListener] | None = None,
    ) -> None:
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.costs = cost_config or CostConfig()
        self.execution = execution_config or ExecutionModelConfig()
        self.listeners: list[BrokerListener] = list(listeners or [])
        self.orders: dict[str, Order] = {}
        self.positions: dict[str, Position] = {}  # open positions by instrument key
        self.closed_positions: list[Position] = []
        self.trades: list[TradeRecord] = []
        self.realized_closed = 0.0
        self._position_seq = 0

    # ------------------------------------------------------------------ queries
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    def active_orders(self, instrument_key: str | None = None) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if o.is_active and (instrument_key is None or o.instrument.key == instrument_key)
        ]

    def realized_pnl(self) -> float:
        return self.realized_closed + sum(p.realized_net for p in self.open_positions())

    def unrealized_pnl(self) -> float:
        return sum(p.unrealized() for p in self.open_positions())

    def equity(self) -> float:
        return self.cash + sum(p.direction.sign * p.quantity * (p.last_price or p.avg_entry_fill) for p in self.open_positions())

    def reconciliation_error(self) -> float:
        """Difference between cash-based equity and P&L-based equity (should be ~0)."""
        return self.equity() - (self.initial_capital + self.realized_pnl() + self.unrealized_pnl())

    # ------------------------------------------------------------------ orders
    def estimate_market_fill(self, side: Side, quote: Quote, now: datetime, tick: float) -> FillDecision | str:
        return fm.market_fill_quote(side, quote, now, self.execution, tick)

    def submit_order(self, order: Order, now: datetime, quote: Quote | None = None) -> Order:
        """Accept a simulated order. Re-submitting an existing client order id is a no-op (idempotent)."""
        existing = self.orders.get(order.client_order_id)
        if existing is not None:
            return existing
        self.orders[order.client_order_id] = order
        order.updated_at = now
        try:
            self._validate(order)
        except OrderRejected as exc:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = str(exc)
            self._emit_order(order)
            return order
        order.status = OrderStatus.PENDING
        self._emit_order(order)
        if quote is not None:
            self._try_fill_with_quote(order, quote, now)
        return order

    def cancel(self, client_order_id: str, now: datetime, reason: str = "CANCELLED") -> Order | None:
        order = self.orders.get(client_order_id)
        if order is None or not order.is_active:
            return order
        order.status = OrderStatus.CANCELLED
        order.rejection_reason = reason
        order.updated_at = now
        self._emit_order(order)
        return order

    def cancel_orders_for(self, instrument_key: str, now: datetime, reason: str) -> None:
        for order in self.active_orders(instrument_key):
            self.cancel(order.client_order_id, now, reason)

    def expire_orders(self, now: datetime) -> list[Order]:
        expired = []
        for order in self.active_orders():
            if order.expires_at is not None and now >= order.expires_at:
                order.status = OrderStatus.EXPIRED
                order.updated_at = now
                expired.append(order)
                self._emit_order(order)
        return expired

    def _validate(self, order: Order) -> None:
        inst = order.instrument
        if order.quantity <= 0:
            raise OrderRejected("Quantity must be positive")
        if inst.lot_size > 1 and order.quantity % inst.lot_size != 0:
            raise OrderRejected(f"Quantity {order.quantity} is not a multiple of lot size {inst.lot_size}")
        if order.order_type is OrderType.LIMIT and (order.limit_price is None or order.limit_price <= 0):
            raise OrderRejected("Limit order requires a positive limit price")
        if order.order_type is OrderType.STOP and (order.trigger_price is None or order.trigger_price <= 0):
            raise OrderRejected("Stop order requires a positive trigger price")
        position = self.positions.get(inst.key)
        if order.purpose is OrderPurpose.ENTRY:
            if position is not None and position.is_open and position.direction.entry_side is not order.side:
                raise OrderRejected("Entry would offset an existing opposite position; reversing is not supported")
        else:
            if position is None or not position.is_open:
                raise OrderRejected("No open position to exit")
            if order.side is position.direction.entry_side:
                raise OrderRejected("Exit order side must reduce the open position")
            if order.quantity > position.quantity:
                raise OrderRejected(f"Exit quantity {order.quantity} exceeds open quantity {position.quantity}")

    # ------------------------------------------------------------------ market events
    def on_quote(self, quote: Quote, now: datetime) -> None:
        position = self.positions.get(quote.instrument_key)
        if position is not None and position.is_open:
            price, _ = fm.executable_price(position.direction.entry_side.opposite(), quote, self.execution)
            if price is not None:
                position.last_price = price
                position.last_price_ts = quote.ts
        for order in sorted(self.active_orders(quote.instrument_key), key=lambda o: o.created_at):
            if order.is_active:  # an OCO sibling may have just been cancelled
                self._try_fill_with_quote(order, quote, now)

    def on_bar(self, instrument_key: str, bar: Candle) -> None:
        """Evaluate active orders against one historical bar (backtests)."""
        # 1) market orders fill at the open
        for order in self.active_orders(instrument_key):
            if order.order_type is OrderType.MARKET:
                self._fill(order, order.remaining, fm.market_fill_bar(order.side, bar, self.execution, order.instrument.tick_size), bar.start)
        # 2) stops / limits, resolving OCO conflicts conservatively
        pending = [o for o in self.active_orders(instrument_key) if o.order_type is not OrderType.MARKET]
        groups: dict[str, list[Order]] = {}
        for order in pending:
            groups.setdefault(order.oco_group or order.client_order_id, []).append(order)
        for members in groups.values():
            self._process_bar_group(members, bar)
        # 3) mark to the bar close
        position = self.positions.get(instrument_key)
        if position is not None and position.is_open:
            position.last_price = bar.close
            position.last_price_ts = bar.end

    def _process_bar_group(self, members: list[Order], bar: Candle) -> None:
        candidates: list[tuple[int, Order, FillDecision]] = []
        for order in members:
            if not order.is_active:
                continue
            if order.order_type is OrderType.STOP:
                assert order.trigger_price is not None
                decision = fm.stop_fill_bar(order.side, order.trigger_price, bar, self.execution, order.instrument.tick_size)
                if decision is None:
                    continue
                priority = 0 if "GAP_THROUGH_STOP" in decision.flags else 2
            else:
                assert order.limit_price is not None
                decision = fm.limit_fill_bar(order.side, order.limit_price, bar, self.execution)
                if decision is None:
                    continue
                priority = 1 if "GAP_FILLED_AT_LIMIT" in decision.flags else 3
            candidates.append((priority, order, decision))
        if not candidates:
            return
        candidates.sort(key=lambda c: c[0])
        _, order, decision = candidates[0]
        if len(candidates) > 1 and order.oco_group and candidates[0][0] >= 2:
            # Both legs reachable within the bar and the open does not decide: assume the stop came first.
            decision.flags.append("AMBIGUOUS_INTRABAR")
        self._fill(order, order.remaining, decision, bar.start)

    def _try_fill_with_quote(self, order: Order, quote: Quote, now: datetime) -> None:
        tick = order.instrument.tick_size
        decision: FillDecision | str | None
        if order.order_type is OrderType.MARKET:
            decision = fm.market_fill_quote(order.side, quote, now, self.execution, tick)
            if isinstance(decision, str):
                order.meta["last_fill_failure"] = decision
                return
        elif order.order_type is OrderType.LIMIT:
            assert order.limit_price is not None
            decision = fm.limit_fill_quote(order.side, order.limit_price, quote, now, self.execution)
        else:
            assert order.trigger_price is not None
            decision = fm.stop_fill_quote(order.side, order.trigger_price, quote, now, self.execution, tick)
        if decision is not None:
            self._fill(order, order.remaining, decision, quote.ts if quote.source == fm.BAR_OPEN_SOURCE else max(now, quote.ts))

    # ------------------------------------------------------------------ fills & accounting
    def _fill(self, order: Order, qty: int, decision: FillDecision, ts: datetime) -> None:
        if qty <= 0 or not order.is_active:
            return
        inst = order.instrument
        charges = compute_charges(self.costs, order.side, decision.fill_price, qty, inst.instrument_type)
        prev_qty = order.filled_quantity
        order.filled_quantity += qty
        order.average_fill_price = ((order.average_fill_price or 0.0) * prev_qty + decision.fill_price * qty) / order.filled_quantity
        order.average_reference_price = (
            (order.average_reference_price or 0.0) * prev_qty + decision.reference_price * qty
        ) / order.filled_quantity
        order.charges += charges.total
        order.status = OrderStatus.FILLED if order.remaining == 0 else OrderStatus.PARTIALLY_FILLED
        order.updated_at = ts
        order.filled_at = ts
        fill = {
            "ts": ts.isoformat(),
            "quantity": qty,
            "reference_price": decision.reference_price,
            "fill_price": decision.fill_price,
            "charges": charges.to_dict(),
            "flags": decision.flags,
        }
        order.fills.append(fill)

        self.cash -= order.side.sign * decision.fill_price * qty
        self.cash -= charges.total

        if order.purpose is OrderPurpose.ENTRY:
            position = self._apply_entry(order, qty, decision, charges.total, ts)
        else:
            position = self._apply_exit(order, qty, decision, charges.total, ts)
        order.position_uid = position.position_uid

        self._emit_order(order)
        for listener in self.listeners:
            listener.on_fill(order, fill)

        if order.status is OrderStatus.FILLED and order.oco_group:
            for sibling in self.active_orders(inst.key):
                if sibling.oco_group == order.oco_group and sibling is not order:
                    self.cancel(sibling.client_order_id, ts, f"OCO_SIBLING_FILLED:{order.client_order_id}")

        if position.is_open:
            self._emit_position(position)
        else:
            self._close_position(position, order)

    def _apply_entry(self, order: Order, qty: int, decision: FillDecision, charges: float, ts: datetime) -> Position:
        key = order.instrument.key
        position = self.positions.get(key)
        if position is None or not position.is_open:
            self._position_seq += 1
            direction = Direction.LONG if order.side is Side.BUY else Direction.SHORT
            position = Position(
                position_uid=order.meta.get("position_uid") or f"{order.client_order_id}:POS",
                instrument=order.instrument,
                direction=direction,
                opened_at=ts,
                signal_id=order.signal_id,
                signal_ts=datetime.fromisoformat(order.meta["signal_ts"]) if order.meta.get("signal_ts") else None,
                details=dict(order.meta.get("details", {})),
            )
            self.positions[key] = position
        total = position.entry_quantity + qty
        position.avg_entry_fill = (position.avg_entry_fill * position.entry_quantity + decision.fill_price * qty) / total
        position.avg_entry_ref = (position.avg_entry_ref * position.entry_quantity + decision.reference_price * qty) / total
        position.entry_quantity = total
        position.quantity += qty
        position.entry_charges += charges
        position.last_price = decision.fill_price
        position.last_price_ts = ts
        for flag in decision.flags:
            if flag not in position.flags:
                position.flags.append(flag)
        return position

    def _apply_exit(self, order: Order, qty: int, decision: FillDecision, charges: float, ts: datetime) -> Position:
        position = self.positions[order.instrument.key]
        share_of_entry_charges = position.entry_charges * qty / position.entry_quantity
        position.realized_net += (
            position.direction.sign * qty * (decision.fill_price - position.avg_entry_fill) - charges - share_of_entry_charges
        )
        total = position.exit_quantity + qty
        position.avg_exit_fill = (position.avg_exit_fill * position.exit_quantity + decision.fill_price * qty) / total
        position.avg_exit_ref = (position.avg_exit_ref * position.exit_quantity + decision.reference_price * qty) / total
        position.exit_quantity = total
        position.exit_charges += charges
        position.quantity -= qty
        position.last_price = decision.fill_price
        position.last_price_ts = ts
        position.exit_reason = order.exit_reason or position.exit_reason
        for flag in decision.flags:
            if flag not in position.flags:
                position.flags.append(flag)
        if position.quantity == 0:
            position.status = "CLOSED"
            position.closed_at = ts
        return position

    def _close_position(self, position: Position, closing_order: Order) -> None:
        for order in self.active_orders(position.instrument.key):
            if order.purpose is OrderPurpose.EXIT:
                self.cancel(order.client_order_id, position.closed_at or closing_order.updated_at or position.opened_at, "POSITION_CLOSED")
        sign = position.direction.sign
        qty = position.entry_quantity
        gross = sign * qty * (position.avg_exit_ref - position.avg_entry_ref)
        slippage = sign * qty * ((position.avg_entry_fill - position.avg_entry_ref) - (position.avg_exit_fill - position.avg_exit_ref))
        charges = position.entry_charges + position.exit_charges
        net = gross - charges - slippage
        if abs(net - position.realized_net) > 1e-6 * max(1.0, abs(net)):
            logger.warning("Trade P&L reconciliation mismatch for %s: %s vs %s", position.position_uid, net, position.realized_net)
        trade = TradeRecord(
            trade_uid=position.position_uid,
            instrument=position.instrument,
            direction=position.direction,
            quantity=qty,
            signal_id=position.signal_id,
            signal_ts=position.signal_ts,
            entry_ts=position.opened_at,
            entry_price=round(position.avg_entry_fill, 4),
            entry_ref_price=round(position.avg_entry_ref, 4),
            exit_ts=position.closed_at or closing_order.updated_at or position.opened_at,
            exit_price=round(position.avg_exit_fill, 4),
            exit_ref_price=round(position.avg_exit_ref, 4),
            entry_charges=position.entry_charges,
            exit_charges=position.exit_charges,
            slippage_cost=slippage,
            gross_pnl=gross,
            net_pnl=net,
            exit_reason=position.exit_reason or "UNKNOWN",
            flags=list(position.flags),
            details={**position.details, "stop_loss": position.stop_loss, "take_profit": position.take_profit},
        )
        self.realized_closed += net
        self.trades.append(trade)
        del self.positions[position.instrument.key]
        self.closed_positions.append(position)
        self._emit_position(position)
        for listener in self.listeners:
            listener.on_trade_closed(trade, position)

    # ------------------------------------------------------------------ restore / emit
    def restore(self, cash: float, realized_closed: float, orders: list[Order], positions: list[Position]) -> None:
        self.cash = cash
        self.realized_closed = realized_closed
        self.orders = {o.client_order_id: o for o in orders}
        self.positions = {p.instrument.key: p for p in positions if p.is_open}

    def _emit_order(self, order: Order) -> None:
        for listener in self.listeners:
            listener.on_order_update(order)

    def _emit_position(self, position: Position) -> None:
        for listener in self.listeners:
            listener.on_position_update(position)
