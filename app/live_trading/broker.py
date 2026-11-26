"""Live broker for real-money (LIVE) sessions: places real orders on your Zerodha account.

It plugs into the same ``StrategyRunner`` as paper sessions and reuses the paper broker's
accounting and persistence, so orders, positions and trades appear in the same tables and pages.

How it trades
-------------
* **Fills come from Zerodha.** Orders are placed with Kite Connect; their state is read back by
  polling ``orders()`` on a worker thread (never blocking the event loop). Each newly filled
  quantity is booked at Zerodha's average fill price.
* **No market orders.** Entries and exits are "marketable" limit orders: best ask/bid ± a
  buffer, so a fill is near-immediate but slippage is bounded. Unfilled entries are cancelled
  after a timeout; unfilled exits are re-priced more aggressively until they fill.
* **The stop-loss rests at Zerodha** as an SL (stop-limit) order as soon as the entry fills, so
  the position stays protected if this server stops. (Exchanges reject SL-M on options.)
* **The target is watched by the app.** When it is reached, the resting stop-loss order is
  *modified* into the exit order. Every exit works this way, so a position never has more than
  one sell order at Zerodha and can't be sold twice.
* **Zerodha's position is checked before every exit.** If it is already flat (closed in the
  Kite app, or by Zerodha's auto square-off), nothing is sent and the position is closed here,
  flagged ``EXTERNAL_EXIT``.
* **Intraday only (MIS)** and at most ``LIVE_MAX_LOTS`` lots per order.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time as _time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.paper_trading.broker import PaperBroker
from app.trading import fill_model as fm
from app.trading.broker import Order, OrderRejected, Position
from app.trading.costs import CostConfig
from app.trading.fill_model import ExecutionModelConfig, FillDecision
from app.trading.types import ExitReason, OrderPurpose, OrderStatus, OrderType, Quote, Side

logger = logging.getLogger(__name__)

VARIETY = "regular"
PRODUCT = "MIS"
# Exception class names that mean "the request may or may not have reached Zerodha".
NETWORK_ERRORS = {"NetworkException", "ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout"}
MAX_REPRICES = 10  # alert after this many re-prices of an unfilled exit (re-pricing continues)
MAX_EXIT_ATTEMPTS = 5  # alert after this many attempts to get an exit order working
MAX_BUFFER_PCT = 20.0

Alert = Callable[..., None]


def kite_tag(client_order_id: str) -> str:
    """Zerodha order tag (alphanumeric, at most 20 characters) that finds our order even if placement timed out."""
    return "AL" + hashlib.sha1(client_order_id.encode()).hexdigest()[:16]


def _is_network_error(exc: BaseException) -> bool:
    return any(cls.__name__ in NETWORK_ERRORS for cls in type(exc).__mro__)


def _log_alert(severity: str, event_type: str, message: str, details: dict[str, Any] | None = None, ts: datetime | None = None) -> None:
    logger.log(logging.WARNING if severity in ("WARNING", "ERROR", "CRITICAL") else logging.INFO, "%s %s", event_type, message)


class LiveBroker(PaperBroker):
    """Sends real orders to Zerodha. Only used by sessions started in LIVE trading mode."""

    is_live = True

    def __init__(
        self,
        session_id: int,
        initial_capital: float,
        costs: CostConfig,
        execution: ExecutionModelConfig,
        kite_factory: Callable[[], Any],
        now_fn: Callable[[], datetime],
        settings: Settings | None = None,
    ) -> None:
        super().__init__(session_id, initial_capital, costs, execution)
        s = settings or get_settings()
        self.kite_factory = kite_factory
        self.now_fn = now_fn
        self.max_lots = s.live_max_lots
        self.buffer_pct = s.live_limit_buffer_pct
        self.stop_buffer_pct = s.live_stop_limit_buffer_pct
        self.entry_timeout = s.live_entry_timeout_seconds
        self.reprice_seconds = s.live_exit_reprice_seconds
        self.poll_seconds = s.live_order_poll_seconds
        self.latest_quotes: dict[str, Quote] = {}
        # Positions being exited: position_uid -> {"reason", "attempts", "details", "last_try"}.
        self.exiting: dict[str, dict[str, Any]] = {}
        self.alert: Alert = _log_alert
        self._poll_inflight = False
        self._last_poll = 0.0
        self._last_poll_error = 0.0

    # ================================================================== order placement
    def _validate(self, order: Order) -> None:
        super()._validate(order)
        lot = order.instrument.lot_size or 1
        if order.quantity > self.max_lots * lot:
            raise OrderRejected(f"Quantity {order.quantity} exceeds the real-money cap of {self.max_lots} lot(s) (LIVE_MAX_LOTS)")

    def submit_order(self, order: Order, now: datetime, quote: Any = None) -> Order:
        if order.client_order_id in self.orders:
            return self.orders[order.client_order_id]
        quote = quote or self.latest_quotes.get(order.instrument.key)
        # Validates and persists the order as PENDING. No quote is passed, so nothing is simulated.
        result = super().submit_order(order, now, None)
        if result is not order or order.status is not OrderStatus.PENDING:
            return result
        if (order.purpose is OrderPurpose.EXIT and order.order_type is OrderType.LIMIT and order.exit_reason == ExitReason.TAKE_PROFIT.value
                and not order.meta.get("exit_working")):
            order.meta["virtual"] = True  # the target is watched by the app and never sent to Zerodha
            self._emit_order(order)
            return order
        self._send(order, now, quote)
        if order.status is OrderStatus.REJECTED and order.purpose is OrderPurpose.EXIT and order.order_type is OrderType.STOP:
            self._protection_failed(order, now)
        return order

    def _send(self, order: Order, now: datetime, quote: Quote | None) -> None:
        inst, tick = order.instrument, order.instrument.tick_size
        params: dict[str, Any] = {
            "variety": VARIETY,
            "exchange": inst.exchange,
            "tradingsymbol": inst.tradingsymbol,
            "transaction_type": order.side.value,
            "quantity": order.quantity,
            "product": PRODUCT,
            "validity": "DAY",
            "tag": kite_tag(order.client_order_id),
        }
        if order.order_type is OrderType.STOP:
            assert order.trigger_price is not None
            trigger = fm.round_to_tick(order.trigger_price, tick)
            params.update(order_type="SL", trigger_price=trigger, price=self._stop_limit(order.side, trigger, tick))
            reference = trigger
        else:
            if order.order_type is OrderType.LIMIT:
                price, reference = order.limit_price, order.meta.get("reference_price") or order.limit_price
            else:
                price = self._marketable(order.side, quote, tick, 0)
                reference = self._touch(order.side, quote)
            if price is None:
                self._reject(order, now, "No current price for the contract, so no protected limit order could be placed")
                return
            params.update(order_type="LIMIT", price=price)
        order.meta.update(kite_tag=params["tag"], kite_request=params, placed_at=now.isoformat(), last_modified=now.isoformat(),
                          reference_price=reference, kite_filled=0, kite_avg=0.0)
        try:
            kite_order_id = self.kite_factory().place_order(**params)
        except Exception as exc:  # noqa: BLE001
            if _is_network_error(exc):
                # It may have reached Zerodha: the next poll looks it up by its tag.
                order.meta["unconfirmed"] = True
                self._emit_order(order)
                self.alert("WARNING", "LIVE_ORDER_UNCONFIRMED", f"Zerodha did not confirm {order.side.value} {inst.tradingsymbol} ({exc}); checking its status",
                           {"order": order.client_order_id}, now)
                return
            self._reject(order, now, f"Zerodha rejected the order: {exc}")
            return
        order.meta["kite_order_id"] = str(kite_order_id)
        self._emit_order(order)
        price_text = f"trigger {params['trigger_price']} limit {params['price']}" if params["order_type"] == "SL" else f"limit {params['price']}"
        self.alert("INFO", "LIVE_ORDER_PLACED", f"Zerodha order {kite_order_id}: {order.side.value} {order.quantity} {inst.tradingsymbol} {params['order_type']} ({price_text})",
                   {"order": order.client_order_id, "kite_order_id": str(kite_order_id), "purpose": order.purpose.value}, now)

    def _reject(self, order: Order, now: datetime, reason: str) -> None:
        order.status = OrderStatus.REJECTED
        order.rejection_reason = reason
        order.updated_at = now
        self._emit_order(order)
        self.alert("WARNING", "LIVE_ORDER_REJECTED", f"{order.instrument.tradingsymbol}: {reason}", {"order": order.client_order_id}, now)

    def cancel(self, client_order_id: str, now: datetime, reason: str = "CANCELLED") -> Order | None:
        order = self.orders.get(client_order_id)
        if order is None or not order.is_active or order.meta.get("virtual") or order.meta.get("external"):
            return super().cancel(client_order_id, now, reason)
        if order.meta.get("cancel_requested"):
            return order
        order.meta["cancel_requested"] = reason
        kite_order_id = order.meta.get("kite_order_id")
        if kite_order_id is not None:
            try:
                self.kite_factory().cancel_order(variety=VARIETY, order_id=kite_order_id)
            except Exception as exc:  # noqa: BLE001
                # Usually it already filled or was cancelled; the next poll settles it.
                order.meta.pop("cancel_requested", None)
                self.alert("WARNING", "LIVE_CANCEL_FAILED", f"Could not cancel Zerodha order {kite_order_id} ({exc})", {"order": client_order_id}, now)
        # The order only becomes CANCELLED once Zerodha confirms it: it could still fill until then.
        self._emit_order(order)
        return order

    def expire_orders(self, now: datetime) -> list[Order]:
        expired = []
        for order in self.active_orders():
            if order.expires_at is None or now < order.expires_at:
                continue
            if order.meta.get("kite_order_id") or order.meta.get("unconfirmed"):
                self.cancel(order.client_order_id, now, "EXPIRED")  # confirmed by the next poll
            else:
                order.status, order.updated_at = OrderStatus.EXPIRED, now
                self._emit_order(order)
                expired.append(order)
        return expired

    # ================================================================== prices
    @staticmethod
    def _touch(side: Side, quote: Quote | None) -> float | None:
        """The price a market order would trade at: best ask to buy, best bid to sell (LTP if no depth)."""
        if quote is None:
            return None
        price = quote.ask if side is Side.BUY else quote.bid
        price = price if price and price > 0 else quote.ltp
        return price if price and price > 0 else None

    def _marketable(self, side: Side, quote: Quote | None, tick: float, attempt: int, fallback: float | None = None) -> float | None:
        base = self._touch(side, quote) or fallback
        if not base or base <= 0:
            return None
        pct = min(self.buffer_pct * (1 + attempt), MAX_BUFFER_PCT) / 100
        if side is Side.BUY:
            return fm.round_to_tick(base * (1 + pct), tick, "up")
        return max(tick, fm.round_to_tick(base * (1 - pct), tick, "down"))

    def _stop_limit(self, side: Side, trigger: float, tick: float) -> float:
        pct = self.stop_buffer_pct / 100
        if side is Side.SELL:
            return max(tick, fm.round_to_tick(trigger * (1 - pct), tick, "down"))
        return fm.round_to_tick(trigger * (1 + pct), tick, "up")

    # ================================================================== market data
    def on_quote(self, quote: Quote, now: datetime) -> None:
        """Track prices and watch targets. Fills never come from quotes here, only from Zerodha."""
        self.latest_quotes[quote.instrument_key] = quote
        position = self.positions.get(quote.instrument_key)
        if position is None or not position.is_open:
            return
        price, _ = fm.executable_price(position.direction.entry_side.opposite(), quote, self.execution)
        if price is None:
            return
        position.last_price, position.last_price_ts = price, quote.ts
        if position.position_uid in self.exiting:
            return
        for order in self.active_orders(quote.instrument_key):
            if order.meta.get("virtual") and order.limit_price is not None:
                reached = price >= order.limit_price if order.side is Side.SELL else price <= order.limit_price
                if reached:
                    self.exit_position(position, order.exit_reason or ExitReason.TAKE_PROFIT.value, now, quote)
                    return

    def on_bar(self, instrument_key: str, bar: Any) -> None:
        raise NotImplementedError("Live sessions trade on live quotes, not historical bars")

    # ================================================================== exits
    def _working_exit(self, instrument_key: str) -> Order | None:
        return next((o for o in self.active_orders(instrument_key)
                     if o.purpose is OrderPurpose.EXIT and o.meta.get("exit_working") and not o.meta.get("virtual")), None)

    def exit_position(self, position: Position, reason: str, now: datetime, quote: Quote | None = None,
                      details: dict[str, Any] | None = None) -> bool:
        """Start exiting a position at Zerodha. Returns True once an exit is working (or not needed)."""
        if not position.is_open:
            return True
        if self._working_exit(position.instrument.key) is not None:
            return True
        inst = position.instrument
        previous = self.exiting.get(position.position_uid)
        if previous is not None and self._seconds_since(previous.get("last_try"), now) < self.reprice_seconds:
            return True  # tried moments ago (time exits are re-requested every clock tick): don't hammer Zerodha
        state = self.exiting.setdefault(position.position_uid, {"reason": reason, "attempts": 0, "details": details or {}})
        state["attempts"] += 1
        state["last_try"] = now.isoformat()
        quote = quote or self.latest_quotes.get(inst.key)
        side = position.direction.entry_side.opposite()

        held = self._held_at_broker(position, now)
        if held == 0:
            self._close_externally(position, reason, now)
            return True
        qty = position.quantity if held is None else min(position.quantity, held)

        price = self._marketable(side, quote, inst.tick_size, 0, fallback=position.last_price or position.avg_entry_fill)
        reference = self._touch(side, quote) or position.last_price or price
        if price is None:
            self.alert("ERROR", "LIVE_EXIT_NO_PRICE", f"No price to exit {inst.tradingsymbol}; retrying", {"position": position.position_uid}, now)
            return False
        for order in self.active_orders(inst.key):
            if order.meta.get("virtual"):
                super().cancel(order.client_order_id, now, f"REPLACED_BY_{reason}")

        resting = next((o for o in self.active_orders(inst.key) if o.purpose is OrderPurpose.EXIT and not o.meta.get("virtual")), None)
        if resting is not None:
            if resting.meta.get("cancel_requested") or not resting.meta.get("kite_order_id"):
                return True  # settles on the next poll; the exit is retried then
            return self._convert_to_exit(resting, price, reference, reason, qty, now, details)

        # A limit order at the marketable price worked out above (which falls back to the last known
        # price when there is no fresh quote, e.g. right after a restart).
        order = Order(
            client_order_id=f"{position.position_uid}:LX:{reason}:{int(now.timestamp() * 1000)}",
            instrument=inst,
            side=side,
            order_type=OrderType.LIMIT,
            quantity=qty,
            purpose=OrderPurpose.EXIT,
            created_at=now,
            limit_price=price,
            signal_id=position.signal_id,
            exit_reason=reason,
            meta={"details": details or {}, "exit_working": True, "reference_price": reference},
        )
        self.submit_order(order, now, quote)
        if order.status is OrderStatus.REJECTED:
            self.alert("CRITICAL", "LIVE_EXIT_FAILED", f"Exit order for {inst.tradingsymbol} was rejected: {order.rejection_reason}. Check the position in Kite.",
                       {"position": position.position_uid}, now)
            return False
        self.alert("INFO", "LIVE_EXIT_SENT", f"Exiting {inst.tradingsymbol} ({reason}) with a limit order @ {order.meta.get('kite_request', {}).get('price')}",
                   {"position": position.position_uid}, now)
        return True

    def _convert_to_exit(self, stop: Order, price: float, reference: float, reason: str, qty: int, now: datetime,
                         details: dict[str, Any] | None) -> bool:
        """Turn the resting stop-loss order into the exit order (one order per position, never two sells)."""
        params: dict[str, Any] = {"variety": VARIETY, "order_id": stop.meta["kite_order_id"], "order_type": "LIMIT", "price": price}
        if qty < stop.quantity and stop.filled_quantity == 0:
            params["quantity"] = qty
        try:
            self.kite_factory().modify_order(**params)
        except Exception as exc:  # noqa: BLE001
            # Most likely the stop has just filled or been cancelled; the poll settles it and the exit is retried.
            self.alert("WARNING", "LIVE_EXIT_RETRY", f"Could not turn the stop-loss for {stop.instrument.tradingsymbol} into an exit ({exc}); retrying",
                       {"order": stop.client_order_id}, now)
            return True
        stop.order_type, stop.limit_price, stop.trigger_price = OrderType.LIMIT, price, None
        if "quantity" in params:
            stop.quantity = qty
        stop.exit_reason = reason
        stop.updated_at = now
        stop.meta.update(exit_working=True, converted_from_stop=True, last_modified=now.isoformat(), reprice_attempts=0,
                         reference_price=reference, details=details or {})
        self._emit_order(stop)
        self.alert("INFO", "LIVE_EXIT_SENT", f"Exiting {stop.instrument.tradingsymbol} ({reason}): stop-loss order changed to a limit order @ {price}",
                   {"order": stop.client_order_id}, now)
        return True

    def _held_at_broker(self, position: Position, now: datetime) -> int | None:
        """Quantity of this position Zerodha holds (MIS). None if it can't be read right now."""
        inst = position.instrument
        try:
            data = self.kite_factory().positions() or {}
        except Exception as exc:  # noqa: BLE001
            self.alert("WARNING", "LIVE_POSITIONS_UNAVAILABLE", f"Could not read positions from Zerodha ({exc}); exiting the quantity on record",
                       {"position": position.position_uid}, now)
            return None
        for row in data.get("net", []):
            if row.get("tradingsymbol") == inst.tradingsymbol and row.get("exchange") == inst.exchange and row.get("product") == PRODUCT:
                return max(0, position.direction.sign * int(row.get("quantity") or 0))
        return 0

    def _close_externally(self, position: Position, reason: str, now: datetime) -> None:
        """Zerodha is already flat: cancel our resting orders and close the position here for the records."""
        inst = position.instrument
        for order in self.active_orders(inst.key):
            self.cancel(order.client_order_id, now, "POSITION_CLOSED_OUTSIDE_APP")
        price = position.last_price or position.avg_entry_fill
        order = Order(
            client_order_id=f"{position.position_uid}:EXT:{int(now.timestamp() * 1000)}",
            instrument=inst,
            side=position.direction.entry_side.opposite(),
            order_type=OrderType.MARKET,
            quantity=position.quantity,
            purpose=OrderPurpose.EXIT,
            created_at=now,
            signal_id=position.signal_id,
            exit_reason=reason,
            status=OrderStatus.PENDING,
            meta={"external": True},
        )
        self.orders[order.client_order_id] = order
        self.alert("WARNING", "LIVE_EXTERNAL_EXIT", f"{inst.tradingsymbol} is already flat at Zerodha (closed outside this app). "
                   f"Recorded as closed at the last price {price}; check the actual exit price in Kite.", {"position": position.position_uid}, now)
        self._fill(order, order.quantity, FillDecision(price, price, ["EXTERNAL_EXIT"]), now)

    def _protection_failed(self, order: Order, now: datetime) -> None:
        position = self.positions.get(order.instrument.key)
        if position is None or not position.is_open:
            return
        self.alert("CRITICAL", "LIVE_STOP_MISSING", f"The stop-loss for {order.instrument.tradingsymbol} is not active at Zerodha "
                   f"({order.rejection_reason or order.status.value}); exiting the position now", {"order": order.client_order_id}, now)
        self.exit_position(position, ExitReason.RISK_EXIT.value, now)

    def protect_open_positions(self, now: datetime) -> None:
        """After a restart: every open position must have a working order at Zerodha; place a stop-loss if not."""
        for position in self.open_positions():
            if any(o.purpose is OrderPurpose.EXIT and not o.meta.get("virtual") for o in self.active_orders(position.instrument.key)):
                continue
            if position.stop_loss is None:
                self.alert("CRITICAL", "LIVE_STOP_MISSING", f"{position.instrument.tradingsymbol} has no stop-loss at Zerodha and none is configured",
                           {"position": position.position_uid}, now)
                continue
            self.submit_order(Order(
                client_order_id=f"{position.position_uid}:SL:R{int(now.timestamp() * 1000)}", instrument=position.instrument,
                side=position.direction.entry_side.opposite(), order_type=OrderType.STOP, quantity=position.quantity,
                purpose=OrderPurpose.EXIT, created_at=now, trigger_price=position.stop_loss, signal_id=position.signal_id,
                oco_group=f"{position.position_uid}:OCO", exit_reason=ExitReason.STOP_LOSS.value,
            ), now)

    # ================================================================== order status from Zerodha
    def _needs_poll(self) -> bool:
        return bool(self.exiting) or any(not (o.meta.get("virtual") or o.meta.get("external")) for o in self.active_orders())

    def on_time(self, now: datetime) -> None:
        """Clock tick: read order status from Zerodha (at most every LIVE_ORDER_POLL_SECONDS)."""
        if self._poll_inflight or not self._needs_poll():
            return
        mono = _time.monotonic()
        if mono - self._last_poll < self.poll_seconds:
            return
        self._poll_inflight, self._last_poll = True, mono
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._apply_poll(self._fetch_orders())  # no event loop (tests, scripts): poll inline
            return
        future = loop.run_in_executor(None, self._fetch_orders)
        future.add_done_callback(lambda f: self._apply_poll(f.result()))  # runs back on the event loop

    def _fetch_orders(self) -> tuple[list[dict[str, Any]] | None, BaseException | None]:
        try:
            return list(self.kite_factory().orders() or []), None
        except Exception as exc:  # noqa: BLE001
            return None, exc

    def _apply_poll(self, result: tuple[list[dict[str, Any]] | None, BaseException | None]) -> None:
        self._poll_inflight = False
        orders, error = result
        now = self.now_fn()
        if error is not None or orders is None:
            if _time.monotonic() - self._last_poll_error > 60:
                self._last_poll_error = _time.monotonic()
                self.alert("WARNING", "LIVE_POLL_FAILED", f"Could not read order status from Zerodha ({error}); retrying", None, now)
            return
        try:
            by_id = {str(o.get("order_id")): o for o in orders}
            by_tag = {}
            for o in orders:
                for tag in [o.get("tag"), *(o.get("tags") or [])]:
                    if tag:
                        by_tag[tag] = o
            for order in list(self.active_orders()):
                if order.meta.get("virtual") or order.meta.get("external") or not order.is_active:
                    continue
                kite_order_id = order.meta.get("kite_order_id")
                row = by_id.get(kite_order_id) if kite_order_id else by_tag.get(order.meta.get("kite_tag"))
                if row is None:
                    if kite_order_id is None and self._seconds_since(order.meta.get("placed_at"), now) > self.entry_timeout:
                        self._reject(order, now, "Zerodha has no record of this order: it was not placed")
                    continue
                if kite_order_id is None:
                    order.meta["kite_order_id"] = str(row.get("order_id"))
                    order.meta.pop("unconfirmed", None)
                    pending_cancel = order.meta.pop("cancel_requested", None)
                    if pending_cancel:
                        self.cancel(order.client_order_id, now, pending_cancel)
                self._apply_kite_order(order, row, now)
            self._housekeeping(now)
        except Exception:  # noqa: BLE001
            logger.exception("Applying Zerodha order status failed")
            self.alert("ERROR", "LIVE_SYNC_ERROR", "Error while applying order status from Zerodha; see the backend log", None, now)

    @staticmethod
    def _seconds_since(iso: str | None, now: datetime) -> float:
        return (now - datetime.fromisoformat(iso)).total_seconds() if iso else 0.0

    def _apply_kite_order(self, order: Order, row: dict[str, Any], now: datetime) -> None:
        status = str(row.get("status") or "").upper()
        order.meta["kite_status"] = status
        if row.get("status_message"):
            order.meta["kite_message"] = row["status_message"]
        filled = int(row.get("filled_quantity") or 0)
        booked = int(order.meta.get("kite_filled") or 0)
        average = float(row.get("average_price") or 0)
        if filled > booked and average > 0:
            delta = min(filled - booked, order.remaining)
            prev_average = float(order.meta.get("kite_avg") or 0)
            price = round((average * filled - prev_average * booked) / (filled - booked), 4)
            order.meta["kite_filled"], order.meta["kite_avg"] = filled, average
            position = self.positions.get(order.instrument.key)
            if order.purpose is OrderPurpose.EXIT and (position is None or not position.is_open):
                self.alert("CRITICAL", "LIVE_UNEXPECTED_FILL", f"Zerodha filled {order.side.value} {filled} {order.instrument.tradingsymbol} "
                           "but no position is open here. Check your positions in Kite.", {"order": order.client_order_id}, now)
                order.status, order.updated_at = OrderStatus.FILLED, now
                self._emit_order(order)
                return
            if delta > 0:
                reference = float(order.meta.get("reference_price") or price)
                self._fill(order, delta, FillDecision(reference, price, ["LIVE_FILL"]), now)
        if not order.is_active:
            return
        if status == "COMPLETE":
            # Zerodha reports it done; book it as filled with whatever filled.
            if order.filled_quantity > 0:
                order.quantity, order.status, order.updated_at = order.filled_quantity, OrderStatus.FILLED, now
                self._emit_order(order)
        elif status in ("CANCELLED", "REJECTED"):
            self._closed_unfilled(order, status, str(row.get("status_message") or status.lower()), now)

    def _closed_unfilled(self, order: Order, status: str, message: str, now: datetime) -> None:
        if order.purpose is OrderPurpose.ENTRY and order.filled_quantity > 0:
            # Part of the entry filled before it was cancelled: keep that quantity and protect it.
            order.quantity, order.status, order.updated_at = order.filled_quantity, OrderStatus.FILLED, now
            self._emit_order(order)
            for listener in self.listeners:
                listener.on_fill(order, order.fills[-1])
            return
        requested = order.meta.get("cancel_requested")
        if status == "REJECTED":
            order.status, order.rejection_reason = OrderStatus.REJECTED, f"Zerodha: {message}"
        else:
            order.status = OrderStatus.EXPIRED if requested == "EXPIRED" else OrderStatus.CANCELLED
            order.rejection_reason = requested or f"Zerodha: {message}"
        order.updated_at = now
        self._emit_order(order)
        if order.purpose is OrderPurpose.ENTRY:
            if status == "REJECTED":
                self.alert("WARNING", "LIVE_ORDER_REJECTED", f"Zerodha rejected the entry in {order.instrument.tradingsymbol}: {message}",
                           {"order": order.client_order_id}, now)
            return
        position = self.positions.get(order.instrument.key)
        if position is None or not position.is_open:
            return
        if order.meta.get("exit_working"):
            self.alert("WARNING", "LIVE_EXIT_RETRY", f"Exit order for {order.instrument.tradingsymbol} ended unfilled ({message}); sending a new one",
                       {"order": order.client_order_id}, now)
        elif not requested:
            self._protection_failed(order, now)  # the resting stop-loss was rejected or cancelled outside the app

    def _housekeeping(self, now: datetime) -> None:
        """Entry timeouts, re-pricing of unfilled exits, and retrying exits that are not working."""
        for order in list(self.active_orders()):
            if (order.meta.get("virtual") or order.meta.get("external") or not order.meta.get("kite_order_id")
                    or order.meta.get("cancel_requested")):
                continue
            if order.purpose is OrderPurpose.ENTRY:
                if self._seconds_since(order.meta.get("placed_at"), now) > self.entry_timeout:
                    self.alert("INFO", "LIVE_ENTRY_TIMEOUT", f"Entry in {order.instrument.tradingsymbol} not filled within "
                               f"{self.entry_timeout:g}s; cancelling it", {"order": order.client_order_id}, now)
                    self.cancel(order.client_order_id, now, "ENTRY_TIMEOUT")
                continue
            # An exit that is working, or a stop-loss that triggered but did not fill (price moved past its limit).
            working = order.meta.get("exit_working") or order.meta.get("kite_status") == "OPEN"
            if working and self._seconds_since(order.meta.get("last_modified"), now) >= self.reprice_seconds:
                self._reprice(order, now)
        for uid, state in list(self.exiting.items()):
            position = next((p for p in self.open_positions() if p.position_uid == uid), None)
            if position is None:
                del self.exiting[uid]
                continue
            if self._working_exit(position.instrument.key) is None and self._seconds_since(state.get("last_try"), now) >= self.reprice_seconds:
                if state["attempts"] == MAX_EXIT_ATTEMPTS:
                    self.alert("CRITICAL", "LIVE_EXIT_STUCK", f"Exit of {position.instrument.tradingsymbol} has not worked after "
                               f"{MAX_EXIT_ATTEMPTS} attempts; still retrying. Check the position in Kite.", {"position": uid}, now)
                self.exit_position(position, state["reason"], now, details=state.get("details"))

    def _reprice(self, order: Order, now: datetime) -> None:
        attempts = int(order.meta.get("reprice_attempts") or 0) + 1
        inst = order.instrument
        position = self.positions.get(inst.key)
        price = self._marketable(order.side, self.latest_quotes.get(inst.key), inst.tick_size, attempts,
                                 fallback=(position.last_price if position else None) or order.limit_price)
        order.meta["last_modified"] = now.isoformat()
        if price is None:
            return
        try:
            self.kite_factory().modify_order(variety=VARIETY, order_id=order.meta["kite_order_id"], order_type="LIMIT", price=price)
        except Exception as exc:  # noqa: BLE001
            self.alert("WARNING", "LIVE_REPRICE_FAILED", f"Could not re-price the exit for {inst.tradingsymbol} ({exc})", {"order": order.client_order_id}, now)
            return
        order.order_type, order.limit_price, order.trigger_price, order.updated_at = OrderType.LIMIT, price, None, now
        order.meta.update(reprice_attempts=attempts, exit_working=True)
        if position is not None:
            self.exiting.setdefault(position.position_uid, {"reason": order.exit_reason or ExitReason.STOP_LOSS.value, "attempts": 1,
                                                            "details": {}, "last_try": now.isoformat()})
        self._emit_order(order)
        severity, event = ("CRITICAL", "LIVE_EXIT_NOT_FILLING") if attempts == MAX_REPRICES else ("INFO", "LIVE_EXIT_REPRICED")
        self.alert(severity, event, f"Exit for {inst.tradingsymbol} not filled yet; re-priced to {price} (attempt {attempts})",
                   {"order": order.client_order_id}, now)

    # ================================================================== restart
    def load_from_db(self) -> None:
        super().load_from_db()
        for order in self.active_orders():
            position = self.positions.get(order.instrument.key)
            if order.meta.get("exit_working") and position is not None:
                self.exiting[position.position_uid] = {"reason": order.exit_reason or ExitReason.RISK_EXIT.value, "attempts": 1,
                                                       "details": order.meta.get("details", {}), "last_try": order.meta.get("last_modified")}
