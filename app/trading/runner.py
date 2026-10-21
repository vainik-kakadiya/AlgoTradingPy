"""StrategyRunner: the shared decision pipeline used by backtests and paper sessions.

    completed underlying candle -> strategy.evaluate -> signal
      -> duplicate check -> pre-entry risk checks -> contract resolution
      -> quote (fresh, executable) -> order-level risk checks (stop/target validity, budgets)
      -> simulated market/limit entry -> on fill: protective stop + target (OCO)
    exits: stop / target (broker), session-end square-off, time exit, rule exit, daily-loss policy

The environments differ only in what feeds the runner (historical bars vs live/replay
quotes), the QuoteSource implementation and the sink that records results.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.core.timeutil import ist_date, ist_time
from app.instruments.contract_resolver import InstrumentCatalog, Resolution, resolve_future, resolve_option
from app.risk.limits import RiskCode
from app.risk.risk_manager import RiskManager
from app.strategies.base import BaseStrategy
from app.trading import fill_model as fm
from app.trading.broker import Order, Position, SimulatedBroker, TradeRecord
from app.trading.types import (
    Candle,
    Direction,
    ExitReason,
    InstrumentRef,
    OrderPurpose,
    OrderStatus,
    OrderType,
    Quote,
    Signal,
)

logger = logging.getLogger(__name__)


class RunnerSink(Protocol):
    def event(self, severity: str, event_type: str, message: str, details: dict[str, Any] | None = None, ts: datetime | None = None) -> None: ...

    def record_signal(self, signal: Signal, outcome: str, reason: str | None, details: dict[str, Any]) -> None: ...


class QuoteSource(Protocol):
    def get_quote(self, instrument: InstrumentRef, now: datetime) -> Quote | None: ...

    def data_freshness(self, now: datetime) -> tuple[bool, str | None]: ...


@dataclass
class PendingEntry:
    signal: Signal
    instrument: InstrumentRef
    details: dict[str, Any]
    created_at: datetime
    not_before: datetime
    deadline: datetime
    last_failure: str | None = None


class StrategyRunner:
    def __init__(
        self,
        strategy: BaseStrategy,
        broker: SimulatedBroker,
        catalog: InstrumentCatalog,
        quotes: QuoteSource,
        sink: RunnerSink,
        id_prefix: str,
        history_size: int | None = None,
    ) -> None:
        self.strategy = strategy
        self.cfg = strategy.config
        self.broker = broker
        self.catalog = catalog
        self.quotes = quotes
        self.sink = sink
        self.id_prefix = id_prefix
        self.risk = RiskManager(self.cfg.risk, self.cfg.session)
        self.history: deque[Candle] = deque(maxlen=history_size or max(strategy.lookback + 10, 300))
        self.processed_signals: set[str] = set()
        self.pending_entries: dict[str, PendingEntry] = {}
        self.signals: dict[str, Signal] = {}
        self.last_entry_day: Any = None
        self._exit_seq = 0
        # Called when a contract is selected, so live environments can subscribe to its quotes.
        self.on_instrument_selected: Callable[[InstrumentRef], None] | None = None
        broker.listeners.append(self)

    # ================================================================== inputs
    def seed_history(self, candles: Sequence[Candle]) -> None:
        for c in candles:
            if c.complete:
                self.history.append(c)

    def on_candle(self, candle: Candle, now: datetime) -> Signal | None:
        """A completed signal-timeframe candle of the underlying."""
        if self.history and candle.start <= self.history[-1].start:
            return None  # duplicate or out-of-order candle
        self.history.append(candle)
        self.risk.roll_day(now)
        lateness = (now - candle.end).total_seconds()
        if lateness > max(self.strategy.timeframe_seconds, 60):
            self.sink.event("WARNING", "STALE_CANDLE", f"Candle {candle.start.isoformat()} completed {lateness:.0f}s late and was not evaluated",
                            {"candle_end": candle.end.isoformat()}, ts=now)
            return None
        if not candle.complete:
            self.sink.event("WARNING", "INCOMPLETE_CANDLE", f"Candle {candle.start.isoformat()} is incomplete and was not evaluated",
                            {"flags": list(candle.flags)}, ts=now)
            return None
        if self.strategy.exit_rule is not None and self.broker.open_positions():
            hit, trace = self.strategy.should_exit(list(self.history))
            if hit:
                for pos in list(self.broker.open_positions()):
                    self.close_position(pos, ExitReason.EXIT_RULE.value, now, details={"exit_rule": trace})
        signal = self.strategy.evaluate(list(self.history))
        if signal is not None:
            self._handle_signal(signal, now)
        return signal

    def on_quote(self, quote: Quote, now: datetime) -> None:
        """A live/replay price update for a traded instrument."""
        for intent in [p for p in self.pending_entries.values() if p.instrument.key == quote.instrument_key]:
            if now >= intent.not_before:
                self._try_entry(intent, now, quote)
        self.broker.on_quote(quote, now)

    def on_bar(self, instrument_key: str, bar: Candle) -> None:
        """A historical bar of a traded instrument (backtests)."""
        open_quote = fm.bar_open_quote(instrument_key, bar)
        for intent in [p for p in self.pending_entries.values() if p.instrument.key == instrument_key]:
            if bar.end > intent.not_before and bar.start <= intent.deadline:
                self._try_entry(intent, bar.start, open_quote)
        position = self.broker.positions.get(instrument_key)
        if position is not None and position.is_open:
            reason = self._time_exit_reason(position, bar.start)
            if reason:
                self.close_position(position, reason, bar.start, quote=open_quote)
        self.broker.on_bar(instrument_key, bar)

    def on_time(self, now: datetime) -> None:
        """Clock tick: expiries, delayed entries, time-based exits and daily-loss monitoring."""
        if self.risk.roll_day(now):
            self.sink.event("INFO", "TRADING_DAY", f"New trading day {ist_date(now)}", ts=now)
        for intent in list(self.pending_entries.values()):
            if now > intent.deadline:
                self._finish_intent(intent, "SKIPPED", intent.last_failure or "No valid quote before the entry deadline")
            elif now >= intent.not_before and intent.last_failure is None:
                self._try_entry(intent, now, None)
        for order in self.broker.expire_orders(now):
            if order.purpose is OrderPurpose.ENTRY and order.signal_id in self.signals:
                self.sink.record_signal(self.signals[order.signal_id], "SKIPPED", "Limit entry not filled before timeout",
                                        {"order": order.client_order_id})
        for position in list(self.broker.open_positions()):
            reason = self._time_exit_reason(position, now)
            if reason:
                self.close_position(position, reason, now, force=ist_time(now) >= self.cfg.session.market_close)
        self._check_daily_loss(now)

    # ================================================================== controls
    def stop_entries(self, reason: str) -> None:
        self.risk.disable_entries(reason)
        for intent in list(self.pending_entries.values()):
            self._finish_intent(intent, "BLOCKED", reason)

    def close_all(self, reason: str, now: datetime, force: bool = False) -> None:
        for intent in list(self.pending_entries.values()):
            self._finish_intent(intent, "BLOCKED", reason)
        for order in self.broker.active_orders():
            if order.purpose is OrderPurpose.ENTRY:
                self.broker.cancel(order.client_order_id, now, reason)
        for position in list(self.broker.open_positions()):
            self.close_position(position, reason, now, force=force)

    def close_position(
        self,
        position: Position,
        reason: str,
        now: datetime,
        quote: Quote | None = None,
        force: bool = False,
        details: dict[str, Any] | None = None,
    ) -> bool:
        """Exit an open position at market. Protective orders are only cancelled once a fill is assured."""
        inst = position.instrument
        exit_side = position.direction.entry_side.opposite()
        quote = quote or self.quotes.get_quote(inst, now)
        estimate = self.broker.estimate_market_fill(exit_side, quote, now, inst.tick_size) if quote else "NO_QUOTE"
        if isinstance(estimate, str):
            if not force or position.last_price is None:
                self.sink.event("WARNING", "EXIT_DEFERRED", f"Cannot exit {inst.tradingsymbol} ({reason}) now: {estimate}; protective orders remain active",
                                {"position": position.position_uid, "reason": reason}, ts=now)
                return False
            quote = Quote(inst.key, now, ltp=position.last_price, source=fm.LAST_KNOWN_SOURCE)
            self.sink.event("WARNING", "FORCED_EXIT_LAST_PRICE", f"Closing {inst.tradingsymbol} at last known price {position.last_price} ({reason})",
                            {"position": position.position_uid}, ts=now)
        for order in self.broker.active_orders(inst.key):
            if order.purpose is OrderPurpose.EXIT:
                self.broker.cancel(order.client_order_id, now, f"REPLACED_BY_{reason}")
        self._exit_seq += 1
        order = Order(
            client_order_id=f"{position.position_uid}:EXIT:{reason}:{self._exit_seq}",
            instrument=inst,
            side=exit_side,
            order_type=OrderType.MARKET,
            quantity=position.quantity,
            purpose=OrderPurpose.EXIT,
            created_at=now,
            signal_id=position.signal_id,
            exit_reason=reason,
            meta={"details": details or {}},
        )
        self.broker.submit_order(order, now, quote)
        if order.status is not OrderStatus.FILLED:
            self.sink.event("ERROR", "EXIT_FAILED", f"Exit order for {inst.tradingsymbol} not filled: {order.rejection_reason or order.status.value}",
                            {"order": order.client_order_id}, ts=now)
            self.broker.cancel(order.client_order_id, now, "EXIT_NOT_FILLED")
            self._place_protective_orders(self.broker.positions.get(inst.key), now)
            return False
        return True

    # ================================================================== signal handling
    def _open_exposure(self) -> int:
        pending_orders = [o for o in self.broker.active_orders() if o.purpose is OrderPurpose.ENTRY]
        return len(self.broker.open_positions()) + len(pending_orders) + len(self.pending_entries)

    def _handle_signal(self, signal: Signal, now: datetime) -> None:
        self.signals[signal.signal_id] = signal
        if signal.signal_id in self.processed_signals:
            self.sink.event("INFO", "DUPLICATE_SIGNAL", f"Signal {signal.signal_id} already processed; ignored", ts=now)
            return
        self.processed_signals.add(signal.signal_id)
        self.sink.event("INFO", "SIGNAL", f"{signal.action.value} signal on {signal.underlying} @ {signal.reference_price:.2f}",
                        {"signal_id": signal.signal_id, "explanation": signal.explanation}, ts=now)

        fresh, stale_reason = self.quotes.data_freshness(now)
        decision = self.risk.check_pre_entry(
            now,
            open_positions=self._open_exposure(),
            lots=self.strategy.quantity_lots,
            data_fresh=fresh,
            stale_reason=stale_reason,
            had_entry_today=self.last_entry_day == ist_date(now),
        )
        if not decision.allowed:
            self._risk_block(signal, decision.code, decision.reason, now)
            return

        resolution = self._resolve(signal, now)
        if not resolution.ok:
            self.sink.record_signal(signal, "SKIPPED", resolution.reason, {"resolution": resolution.details})
            self.sink.event("WARNING", "CONTRACT_UNAVAILABLE", f"Signal {signal.signal_id} skipped: {resolution.reason}", resolution.details, ts=now)
            return
        assert resolution.instrument is not None
        if self.on_instrument_selected is not None:
            self.on_instrument_selected(resolution.instrument)
        exe = self.cfg.execution
        delay = timedelta(milliseconds=exe.execution_delay_ms)
        deadline = now + delay + (timedelta(seconds=exe.max_entry_delay_seconds) if exe.missing_quote_policy == "DEFER" else timedelta(0))
        intent = PendingEntry(signal, resolution.instrument, {"resolution": resolution.details}, now, now + delay, deadline)
        self.pending_entries[signal.signal_id] = intent
        self.sink.record_signal(signal, "PENDING", "Awaiting executable quote", intent.details)
        if delay.total_seconds() == 0:
            self._try_entry(intent, now, None)

    def _resolve(self, signal: Signal, now: datetime) -> Resolution:
        as_of = ist_date(now)
        if self.strategy.trade_instrument == "OPTION":
            return resolve_option(self.catalog, signal.underlying, signal.direction, signal.reference_price, as_of,
                                  self.cfg.option_selection)  # type: ignore[union-attr]
        return resolve_future(self.catalog, signal.underlying, as_of, self.cfg.future_selection)  # type: ignore[union-attr]

    def _try_entry(self, intent: PendingEntry, now: datetime, quote: Quote | None) -> None:
        if intent.signal.signal_id not in self.pending_entries:
            return
        signal, inst = intent.signal, intent.instrument
        # Options are always bought (CALL for long, PUT for short); futures follow the signal direction.
        position_direction = signal.direction if self.strategy.trade_instrument == "FUTURE" else Direction.LONG
        side = position_direction.entry_side

        quote = quote or self.quotes.get_quote(inst, now)
        estimate = self.broker.estimate_market_fill(side, quote, now, inst.tick_size) if quote else "NO_QUOTE"
        if isinstance(estimate, str):
            intent.last_failure = {"NO_QUOTE": "No quote available for the selected contract",
                                   "STALE_QUOTE": "Quote for the selected contract is stale",
                                   "NO_EXECUTABLE_PRICE": "Quote has no executable price"}.get(estimate, estimate)
            if self.cfg.execution.missing_quote_policy == "SKIP" or now >= intent.deadline:
                self._finish_intent(intent, "SKIPPED", intent.last_failure)
            return
        assert quote is not None

        # Re-check exposure/time-dependent risk at execution time (state may have changed while deferred).
        self.pending_entries.pop(signal.signal_id, None)
        fresh, stale_reason = self.quotes.data_freshness(now)
        decision = self.risk.check_pre_entry(now, self._open_exposure(), self.strategy.quantity_lots, fresh, stale_reason,
                                             self.last_entry_day == ist_date(now))
        if not decision.allowed:
            self._risk_block(signal, decision.code, decision.reason, now)
            return

        qty = self.strategy.quantity_lots * inst.lot_size
        exe = self.cfg.execution
        if exe.entry_order_type == "LIMIT":
            expected = fm.round_to_tick(estimate.reference_price + side.sign * exe.limit_offset_points, inst.tick_size,
                                        "up" if side.sign > 0 else "down")
        else:
            expected = estimate.fill_price
        stop, target = self._exit_levels(position_direction, expected, inst.tick_size)
        decision = self.risk.check_order(position_direction, expected, stop, target, qty, inst.tick_size)
        if not decision.allowed:
            self._risk_block(signal, decision.code, decision.reason, now, {"expected_entry": expected, "stop": stop, "target": target})
            return

        details = {
            **intent.details,
            "signal_explanation": signal.explanation,
            "signal_reference_price": signal.reference_price,
            "expected_entry": expected,
            "planned_stop": stop,
            "planned_target": target,
            "quote": {"ts": quote.ts.isoformat(), "ltp": quote.ltp, "bid": quote.bid, "ask": quote.ask, "source": quote.source},
        }
        order = Order(
            client_order_id=f"{self.id_prefix}:{signal.signal_id}:ENTRY",
            instrument=inst,
            side=side,
            order_type=OrderType.LIMIT if exe.entry_order_type == "LIMIT" else OrderType.MARKET,
            quantity=qty,
            purpose=OrderPurpose.ENTRY,
            created_at=now,
            limit_price=expected if exe.entry_order_type == "LIMIT" else None,
            expires_at=now + timedelta(seconds=exe.limit_timeout_seconds) if exe.entry_order_type == "LIMIT" else None,
            signal_id=signal.signal_id,
            meta={"signal_ts": signal.ts.isoformat(), "details": details, "position_uid": f"{self.id_prefix}:{signal.signal_id}"},
        )
        self.broker.submit_order(order, now, quote)
        if order.status is OrderStatus.REJECTED:
            self.sink.record_signal(signal, "BLOCKED", f"Order rejected: {order.rejection_reason}", details)
            self.sink.event("WARNING", "ORDER_REJECTED", f"Entry order rejected: {order.rejection_reason}", {"order": order.client_order_id}, ts=now)
        elif order.status is OrderStatus.PENDING:
            self.sink.record_signal(signal, "PENDING", "Limit entry working", details)

    def _finish_intent(self, intent: PendingEntry, outcome: str, reason: str) -> None:
        self.pending_entries.pop(intent.signal.signal_id, None)
        self.sink.record_signal(intent.signal, outcome, reason, intent.details)
        self.sink.event("WARNING", f"ENTRY_{outcome}", f"Signal {intent.signal.signal_id}: {reason}", intent.details, ts=intent.created_at)

    def _risk_block(self, signal: Signal, code: RiskCode | None, reason: str | None, now: datetime, extra: dict[str, Any] | None = None) -> None:
        details = {"risk_code": code.value if code else None, **(extra or {})}
        self.sink.record_signal(signal, "BLOCKED", reason, details)
        self.sink.event("WARNING", "RISK_BLOCK", f"Entry blocked ({code.value if code else 'RISK'}): {reason}",
                        {"signal_id": signal.signal_id, **details}, ts=now)

    def _exit_levels(self, direction: Direction, entry: float, tick: float) -> tuple[float | None, float | None]:
        ex = self.cfg.exits
        sign = direction.sign
        stop = fm.round_to_tick(entry - sign * ex.stop_loss_points, tick) if ex.stop_loss_points else None
        target = fm.round_to_tick(entry + sign * ex.take_profit_points, tick) if ex.take_profit_points else None
        return stop, target

    # ================================================================== broker callbacks
    def on_order_update(self, order: Order) -> None:
        pass

    def on_position_update(self, position: Position) -> None:
        pass

    def on_fill(self, order: Order, fill: dict[str, Any]) -> None:
        if order.purpose is not OrderPurpose.ENTRY or order.status is not OrderStatus.FILLED:
            return
        position = self.broker.positions.get(order.instrument.key)
        if position is None:
            return
        ts = order.filled_at or order.created_at
        self.risk.on_entry(ts)
        self.last_entry_day = ist_date(ts)
        stop, target = self._exit_levels(position.direction, position.avg_entry_fill, order.instrument.tick_size)
        position.stop_loss, position.take_profit = stop, target
        signal = self.signals.get(order.signal_id or "")
        if signal is not None:
            self.sink.record_signal(signal, "ENTERED", None, {
                **order.meta.get("details", {}),
                "entry_fill": position.avg_entry_fill,
                "entry_reference": position.avg_entry_ref,
                "stop_loss": stop,
                "take_profit": target,
                "fill_flags": fill.get("flags", []),
            })
        self.sink.event("INFO", "ENTRY_FILLED", f"Entered {position.direction.value} {order.instrument.tradingsymbol} x{order.filled_quantity} @ {position.avg_entry_fill:.2f}",
                        {"order": order.client_order_id, "stop_loss": stop, "take_profit": target}, ts=ts)
        if stop is not None and stop < order.instrument.tick_size:
            self.sink.event("CRITICAL", "INVALID_STOP_AT_FILL", "Computed stop-loss is not a valid price after fill; exiting immediately", {"stop": stop}, ts=ts)
            position.stop_loss = None
            self.close_position(position, ExitReason.RISK_EXIT.value, ts, force=True)
            return
        self._place_protective_orders(position, ts)

    def _place_protective_orders(self, position: Position | None, now: datetime) -> None:
        if position is None or not position.is_open:
            return
        existing = {o.exit_reason for o in self.broker.active_orders(position.instrument.key) if o.purpose is OrderPurpose.EXIT}
        exit_side = position.direction.entry_side.opposite()
        group = f"{position.position_uid}:OCO"
        self._exit_seq += 1
        if position.stop_loss is not None and ExitReason.STOP_LOSS.value not in existing:
            self.broker.submit_order(Order(
                client_order_id=f"{position.position_uid}:SL:{self._exit_seq}", instrument=position.instrument, side=exit_side,
                order_type=OrderType.STOP, quantity=position.quantity, purpose=OrderPurpose.EXIT, created_at=now,
                trigger_price=position.stop_loss, signal_id=position.signal_id, oco_group=group, exit_reason=ExitReason.STOP_LOSS.value,
            ), now)
        if position.take_profit is not None and ExitReason.TAKE_PROFIT.value not in existing:
            self.broker.submit_order(Order(
                client_order_id=f"{position.position_uid}:TP:{self._exit_seq}", instrument=position.instrument, side=exit_side,
                order_type=OrderType.LIMIT, quantity=position.quantity, purpose=OrderPurpose.EXIT, created_at=now,
                limit_price=position.take_profit, signal_id=position.signal_id, oco_group=group, exit_reason=ExitReason.TAKE_PROFIT.value,
            ), now)

    def on_trade_closed(self, trade: TradeRecord, position: Position) -> None:
        self.risk.on_trade_closed(trade.net_pnl, trade.exit_ts)
        self.sink.event("INFO", "TRADE_CLOSED",
                        f"Closed {trade.instrument.tradingsymbol} ({trade.exit_reason}) net ₹{trade.net_pnl:,.2f}",
                        {"trade_uid": trade.trade_uid, "net_pnl": trade.net_pnl, "flags": trade.flags}, ts=trade.exit_ts)
        self._check_daily_loss(trade.exit_ts)

    # ================================================================== helpers
    def _time_exit_reason(self, position: Position, now: datetime) -> str | None:
        ex, session = self.cfg.exits, self.cfg.session
        if ex.square_off_at_session_end:
            if ist_date(now) > ist_date(position.opened_at) or ist_time(now) >= session.square_off_time:
                return ExitReason.SESSION_END.value
        if ex.max_holding_minutes and now >= position.opened_at + timedelta(minutes=ex.max_holding_minutes):
            return ExitReason.TIME_EXIT.value
        return None

    def _check_daily_loss(self, now: datetime) -> None:
        breach = self.risk.check_daily_loss(now, self.broker.unrealized_pnl())
        if breach is None:
            return
        self.sink.event("WARNING", "DAILY_LOSS_LIMIT", breach, {"policy": self.cfg.risk.on_daily_loss_breach}, ts=now)
        for intent in list(self.pending_entries.values()):
            self._finish_intent(intent, "BLOCKED", breach)
        if self.cfg.risk.on_daily_loss_breach == "SQUARE_OFF":
            for position in list(self.broker.open_positions()):
                self.close_position(position, ExitReason.DAILY_LOSS_LIMIT.value, now)

    # ================================================================== persistence
    def export_state(self) -> dict[str, Any]:
        return {
            "risk": self.risk.to_state(),
            "processed_signals": sorted(self.processed_signals)[-2000:],
            "last_entry_day": self.last_entry_day.isoformat() if self.last_entry_day else None,
            "exit_seq": self._exit_seq,
        }

    def import_state(self, state: dict[str, Any]) -> None:
        from datetime import date

        self.risk.load_state(state.get("risk", {}))
        self.processed_signals = set(state.get("processed_signals", []))
        self.last_entry_day = date.fromisoformat(state["last_entry_day"]) if state.get("last_entry_day") else None
        self._exit_seq = state.get("exit_seq", 0)
