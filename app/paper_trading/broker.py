"""Paper broker: the simulated broker plus durable persistence of every state change.

It inherits all fill logic from ``SimulatedBroker`` and adds nothing that can reach a real
broker - there is no network client in this module.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database.models import PaperOrder, PaperPosition, PaperTrade
from app.database.session import session_scope
from app.services.event_bus import bus, to_jsonable
from app.trading.broker import Order, Position, SimulatedBroker, TradeRecord
from app.trading.costs import CostConfig
from app.trading.fill_model import ExecutionModelConfig

logger = logging.getLogger(__name__)


class _Persistence:
    def __init__(self, session_id: int) -> None:
        self.session_id = session_id

    def on_order_update(self, order: Order) -> None:
        payload = to_jsonable(order.to_dict())
        values = {
            "paper_session_id": self.session_id,
            "client_order_id": order.client_order_id,
            "instrument_id": order.instrument.instrument_id,
            "instrument_key": order.instrument.key,
            "tradingsymbol": order.instrument.tradingsymbol,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "purpose": order.purpose.value,
            "quantity": order.quantity,
            "limit_price": order.limit_price,
            "trigger_price": order.trigger_price,
            "status": order.status.value,
            "filled_quantity": order.filled_quantity,
            "average_fill_price": order.average_fill_price,
            "signal_id": order.signal_id,
            "oco_group": order.oco_group,
            "exit_reason": order.exit_reason,
            "rejection_reason": order.rejection_reason,
            "execution_details": {"fills": payload["fills"], "charges": order.charges,
                                  "average_reference_price": order.average_reference_price},
            "order_payload": payload,
            "created_at": order.created_at,
            "updated_at": order.updated_at or order.created_at,
            "filled_at": order.filled_at,
            "expires_at": order.expires_at,
        }
        stmt = sqlite_insert(PaperOrder).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["paper_session_id", "client_order_id"],
            set_={k: stmt.excluded[k] for k in values if k not in ("paper_session_id", "client_order_id", "created_at")},
        )
        with session_scope() as db:
            db.execute(stmt)
        bus.publish("order", {"paper_session_id": self.session_id, **{k: v for k, v in values.items() if k not in ("order_payload",)}})

    def on_fill(self, order: Order, fill: dict[str, Any]) -> None:
        pass  # fills are persisted as part of the order record

    def on_position_update(self, position: Position) -> None:
        values = {
            "paper_session_id": self.session_id,
            "position_uid": position.position_uid,
            "instrument_id": position.instrument.instrument_id,
            "instrument_key": position.instrument.key,
            "tradingsymbol": position.instrument.tradingsymbol,
            "direction": position.direction.value,
            "net_quantity": position.quantity,
            "average_entry_price": position.avg_entry_fill,
            "realized_pnl": position.realized_net,
            "unrealized_pnl": position.unrealized(),
            "last_price": position.last_price,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "status": position.status,
            "signal_id": position.signal_id,
            "opened_at": position.opened_at,
            "closed_at": position.closed_at,
            "state": to_jsonable(position.to_dict()),
        }
        stmt = sqlite_insert(PaperPosition).values(values)
        stmt = stmt.on_conflict_do_update(index_elements=["position_uid"],
                                          set_={k: stmt.excluded[k] for k in values if k != "position_uid"})
        with session_scope() as db:
            db.execute(stmt)
        bus.publish("position", {k: v for k, v in values.items() if k != "state"})

    def on_trade_closed(self, trade: TradeRecord, position: Position) -> None:
        row = trade.to_row()
        row["details"] = to_jsonable(row["details"])
        with session_scope() as db:
            db.execute(sqlite_insert(PaperTrade).values({**row, "paper_session_id": self.session_id}).on_conflict_do_nothing())
        bus.publish("trade", {"paper_session_id": self.session_id, **{k: v for k, v in row.items() if k != "details"}})


class PaperBroker(SimulatedBroker):
    def __init__(self, session_id: int, initial_capital: float, costs: CostConfig, execution: ExecutionModelConfig) -> None:
        self.session_id = session_id
        self.persistence = _Persistence(session_id)
        super().__init__(initial_capital, costs, execution, listeners=[self.persistence])

    def submit_order(self, order: Order, now: datetime, quote: Any = None) -> Order:
        # Idempotency across restarts: a client order id that was ever persisted is never re-created.
        if order.client_order_id not in self.orders:
            with session_scope() as db:
                existing = db.scalar(select(PaperOrder).where(
                    PaperOrder.paper_session_id == self.session_id, PaperOrder.client_order_id == order.client_order_id))
                if existing is not None and existing.order_payload:
                    logger.info("Duplicate simulated order %s suppressed", order.client_order_id)
                    return Order.from_dict(existing.order_payload)
        return super().submit_order(order, now, quote)

    def load_from_db(self) -> None:
        """Rebuild broker state after a restart. Cash is recomputed from every persisted fill."""
        with session_scope() as db:
            all_orders = [Order.from_dict(o.order_payload) for o in db.scalars(select(PaperOrder).where(
                PaperOrder.paper_session_id == self.session_id)) if o.order_payload]
            positions = [Position.from_dict(p.state) for p in db.scalars(select(PaperPosition).where(
                PaperPosition.paper_session_id == self.session_id, PaperPosition.status == "OPEN")) if p.state]
            realized = sum(t.net_pnl for t in db.scalars(select(PaperTrade).where(PaperTrade.paper_session_id == self.session_id)))
        cash = self.initial_capital
        for order in all_orders:
            for fill in order.fills:
                cash -= order.side.sign * fill["fill_price"] * fill["quantity"] + fill["charges"]["total"]
        self.restore(cash, realized, [o for o in all_orders if o.is_active], positions)
