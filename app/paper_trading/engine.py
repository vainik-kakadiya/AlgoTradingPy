"""Paper-trading sessions driven by live (Kite) or replayed market data.

Each session owns a StrategyRunner (the same one backtests use) wired to a PaperBroker
(simulated fills, persisted) and to the MarketDataHub. Sessions run inside the backend
process and do not depend on any browser being connected. On restart, RUNNING and
ENTRIES_STOPPED sessions are restored from the database.

Stop semantics:
* stop-entries: no new entries; open positions keep their stop-loss, target and
  session-end square-off.
* stop: cancels pending orders and closes every open simulated position at the current
  quote (or the last known price, flagged, if no fresh quote exists), then ends the session.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.analytics.performance import trade_metrics
from app.core.safety import validate_execution_mode
from app.core.timeutil import utcnow
from app.database.models import (
    Candle as CandleRow,
    EquitySnapshot,
    Instrument,
    PaperSession,
    PaperTrade,
    SignalRecord,
    StrategyVersion,
)
from app.database.session import SessionLocal, session_scope
from app.instruments.contract_resolver import ListCatalog
from app.instruments.instrument_master import DbCatalog, index_instrument, to_ref
from app.market_data.hub import MarketDataHub
from app.services.event_bus import bus, record_event, to_jsonable
from app.strategies.registry import build_strategy
from app.trading.costs import CostConfig
from app.trading.fill_model import ExecutionModelConfig
from app.trading.runner import StrategyRunner
from app.trading.types import Candle, InstrumentRef, Quote, Signal
from app.paper_trading.broker import PaperBroker

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("RUNNING", "ENTRIES_STOPPED")


class PaperSink:
    def __init__(self, session_id: int, strategy_id: int | None) -> None:
        self.session_id = session_id
        self.strategy_id = strategy_id

    def event(self, severity: str, event_type: str, message: str, details: dict[str, Any] | None = None, ts: datetime | None = None) -> None:
        details = dict(details or {})
        if ts is not None:
            details["market_ts"] = ts
        record_event(severity, event_type, "paper_trading", message, details, paper_session_id=self.session_id, strategy_id=self.strategy_id)

    def record_signal(self, signal: Signal, outcome: str, reason: str | None, details: dict[str, Any]) -> None:
        with session_scope() as db:
            row = db.scalar(select(SignalRecord).where(SignalRecord.paper_session_id == self.session_id,
                                                       SignalRecord.signal_id == signal.signal_id))
            if row is None:
                row = SignalRecord(
                    paper_session_id=self.session_id, signal_id=signal.signal_id, ts=signal.ts, strategy_key=signal.strategy_key,
                    direction=signal.direction.value, action=signal.action.value, reference_price=signal.reference_price,
                    candle=signal.candle.to_dict(), explanation=to_jsonable(signal.explanation),
                    outcome=outcome, outcome_reason=reason, details=to_jsonable(details),
                )
                db.add(row)
            else:
                row.outcome, row.outcome_reason, row.details = outcome, reason, to_jsonable(details)
        bus.publish("signal", {"paper_session_id": self.session_id, "signal_id": signal.signal_id, "ts": signal.ts,
                               "action": signal.action.value, "reference_price": signal.reference_price,
                               "outcome": outcome, "outcome_reason": reason})


class PaperQuoteSource:
    def __init__(self, session: PaperSessionRunner) -> None:
        self.session = session

    def get_quote(self, instrument: InstrumentRef, now: datetime) -> Quote | None:
        hub, s = self.session.hub, self.session
        if hub.source != s.data_source:
            return None
        quote = hub.latest.get(instrument.key)
        if quote is not None and (now - quote.ts).total_seconds() <= s.execution.max_quote_age_seconds:
            return quote
        # Not streaming yet (e.g. a contract selected moments ago): ask the feed for a point-in-time quote.
        return hub.snapshot_quote(instrument.key) or quote

    def data_freshness(self, now: datetime) -> tuple[bool, str | None]:
        hub, s = self.session.hub, self.session
        if hub.source != s.data_source:
            return False, f"Feed source is {hub.source or 'not running'}; session expects {s.data_source}"
        return hub.data_freshness(s.underlying.key, now, s.strategy.config.risk.stale_data_seconds)


class PaperSessionRunner:
    def __init__(self, row: PaperSession, hub: MarketDataHub, catalog: Any, underlying: InstrumentRef, strategy_id: int | None) -> None:
        self.session_id = row.id
        self.hub = hub
        self.data_source = row.data_source
        self.status = row.status
        self.strategy = build_strategy(row.strategy_config)
        validate_execution_mode(self.strategy.config.execution.execution_mode)
        cfg = row.session_config
        self.execution = ExecutionModelConfig(**cfg.get("execution", {}))
        self.broker = PaperBroker(row.id, row.initial_capital, CostConfig(**cfg.get("costs", {})), self.execution)
        self.underlying = underlying
        self.sink = PaperSink(row.id, strategy_id)
        self.runner = StrategyRunner(self.strategy, self.broker, catalog, PaperQuoteSource(self), self.sink, id_prefix=f"PS{row.id}")
        self.runner.on_instrument_selected = self._subscribe_contract
        self.peak_equity = row.initial_capital
        self._last_persist = 0.0
        self._last_equity_ts: datetime | None = None
        self._trade_count = 0
        self.last_market_ts: datetime | None = row.last_market_ts

    # ------------------------------------------------------------------ lifecycle
    def start(self, restore: bool, runtime_state: dict[str, Any] | None) -> None:
        if restore:
            self.broker.load_from_db()
            self.runner.import_state(runtime_state or {})
            self._trade_count = len(self.broker.trades)
        if self.status == "ENTRIES_STOPPED":
            self.runner.risk.disable_entries("New entries stopped by user")
        self.hub.subscribe(self.session_id, self.underlying, {self.strategy.timeframe_seconds})
        for position in self.broker.open_positions():
            self._subscribe_contract(position.instrument)
        for order in self.broker.active_orders():
            self._subscribe_contract(order.instrument)
        if self.data_source == "KITE":
            self.runner.seed_history(self._stored_candles())
        self.hub.add_listener(self)

    def _stored_candles(self) -> list[Candle]:
        if self.underlying.instrument_id is None:
            return []
        with SessionLocal() as db:
            ids = [i for (i,) in db.execute(select(Instrument.id).where(
                Instrument.exchange == self.underlying.exchange, Instrument.tradingsymbol == self.underlying.tradingsymbol))]
            rows = db.scalars(select(CandleRow).where(
                CandleRow.instrument_id.in_(ids), CandleRow.interval_seconds == self.strategy.timeframe_seconds,
                CandleRow.source == "LIVE", CandleRow.start_time >= utcnow() - timedelta(days=5),
            ).order_by(CandleRow.start_time.desc()).limit(self.runner.history.maxlen or 300)).all()
        seen: dict[datetime, Candle] = {}
        for r in rows:
            seen.setdefault(r.start_time, Candle(r.start_time, r.interval_seconds, r.open, r.high, r.low, r.close, r.volume,
                                                 r.is_complete, tuple(r.quality_flags or ())))
        return [seen[k] for k in sorted(seen)]

    def _subscribe_contract(self, inst: InstrumentRef) -> None:
        self.hub.subscribe(self.session_id, inst)

    def detach(self) -> None:
        self.hub.remove_listener(self)
        self.hub.unsubscribe_owner(self.session_id)

    # ------------------------------------------------------------------ hub callbacks
    def _active(self) -> bool:
        return self.hub.source == self.data_source and self.status in ACTIVE_STATUSES

    def on_quote(self, quote: Quote, now: datetime) -> None:
        if not self._active():
            return
        self.last_market_ts = quote.ts
        if quote.instrument_key != self.underlying.key:
            self.runner.on_quote(quote, now)

    def on_candle(self, instrument_key: str, candle: Candle, now: datetime) -> None:
        if not self._active() or instrument_key != self.underlying.key or candle.interval_seconds != self.strategy.timeframe_seconds:
            return
        self.runner.on_candle(candle, now)
        bus.publish("strategy_candle", {"paper_session_id": self.session_id, **candle.to_dict()})
        self.persist(now, force=True)

    def on_time(self, now: datetime) -> None:
        if not self._active():
            return
        self.runner.on_time(now)
        self.persist(now)

    # ------------------------------------------------------------------ controls
    def stop_entries(self, reason: str) -> None:
        self.runner.stop_entries(reason)
        self.status = "ENTRIES_STOPPED"
        self.persist(self.hub.now(), force=True)

    def stop(self, reason: str) -> None:
        now = self.hub.now() if self.hub.source == self.data_source else utcnow()
        self.runner.stop_entries(reason)
        self.runner.close_all("SESSION_STOPPED", now, force=True)
        for order in self.broker.active_orders():
            self.broker.cancel(order.client_order_id, now, "SESSION_STOPPED")
        self.status = "STOPPED"
        self.persist(now, force=True, stop_reason=reason)
        self.detach()

    # ------------------------------------------------------------------ persistence
    def summary(self) -> dict[str, Any]:
        realized = self.broker.realized_pnl()
        unrealized = self.broker.unrealized_pnl()
        return {
            "cash": self.broker.cash,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "net_pnl": realized + unrealized,
            "equity": self.broker.initial_capital + realized + unrealized,
            "entries_blocked_reason": self.runner.risk.blocked_reason,
            "open_positions": len(self.broker.open_positions()),
            "pending_orders": len(self.broker.active_orders()),
            "day_realized": self.runner.risk.day_realized,
        }

    def persist(self, now: datetime, force: bool = False, stop_reason: str | None = None) -> None:
        mono = _time.monotonic()
        trades_changed = len(self.broker.trades) != self._trade_count
        if not force and not trades_changed and mono - self._last_persist < 2.0:
            return
        self._last_persist = mono
        s = self.summary()
        with session_scope() as db:
            row = db.get(PaperSession, self.session_id)
            if row is None:
                return
            row.cash, row.realized_pnl, row.unrealized_pnl, row.net_pnl = s["cash"], s["realized_pnl"], s["unrealized_pnl"], s["net_pnl"]
            row.status = self.status
            row.entries_enabled = self.runner.risk.entries_enabled
            row.entries_blocked_reason = s["entries_blocked_reason"]
            row.runtime_state = to_jsonable(self.runner.export_state())
            row.last_market_ts = self.last_market_ts
            if stop_reason is not None:
                row.stopped_at, row.stop_reason = utcnow(), stop_reason
            equity_due = (
                trades_changed or force or self._last_equity_ts is None
                or (now - self._last_equity_ts).total_seconds() >= (60 if self.broker.open_positions() else 300)
            )
            if equity_due:
                self.peak_equity = max(self.peak_equity, s["equity"])
                dd = self.peak_equity - s["equity"]
                db.add(EquitySnapshot(
                    paper_session_id=self.session_id, ts=now, cash=s["cash"], realized_pnl=s["realized_pnl"],
                    unrealized_pnl=s["unrealized_pnl"], equity=s["equity"], peak_equity=self.peak_equity, drawdown=dd,
                    drawdown_pct=dd / self.peak_equity * 100 if self.peak_equity else 0.0,
                ))
                self._last_equity_ts = now
        self._trade_count = len(self.broker.trades)
        bus.publish("session", {"id": self.session_id, "status": self.status, **s})


class PaperTradingService:
    def __init__(self, hub: MarketDataHub) -> None:
        self.hub = hub
        self.runners: dict[int, PaperSessionRunner] = {}

    def _catalog_and_underlying(self, db: Any, data_source: str, dataset_id: int | None, underlying: str) -> tuple[Any, InstrumentRef]:
        if data_source == "REPLAY":
            if dataset_id is None:
                raise ValueError("REPLAY sessions require a dataset_id")
            rows = db.scalars(select(Instrument).where(Instrument.dataset_id == dataset_id)).all()
            idx = index_instrument(db, underlying, dataset_id=dataset_id)
            if idx is None:
                raise ValueError(f"Dataset {dataset_id} has no {underlying} index instrument")
            return ListCatalog([to_ref(r) for r in rows if r.instrument_type != "INDEX"],
                               {to_ref(r).key: r.listed_from for r in rows if r.listed_from}), idx
        idx = index_instrument(db, underlying)
        if idx is None:
            raise ValueError("No Kite instrument master snapshot found; refresh instruments first")
        return DbCatalog(SessionLocal), idx

    def create(self, version_id: int, name: str, initial_capital: float, data_source: str, dataset_id: int | None,
               costs: CostConfig, execution: ExecutionModelConfig) -> int:
        data_source = data_source.upper()
        if data_source not in ("KITE", "REPLAY"):
            raise ValueError("data_source must be KITE or REPLAY")
        with session_scope() as db:
            version = db.get(StrategyVersion, version_id)
            if version is None:
                raise LookupError(f"Strategy version {version_id} not found")
            if not version.strategy.is_enabled:
                raise ValueError("Strategy is disabled; enable it before starting a paper session")
            strategy = build_strategy(version.config_json)
            validate_execution_mode(strategy.config.execution.execution_mode)
            catalog, underlying = self._catalog_and_underlying(db, data_source, dataset_id, strategy.underlying)
            row = PaperSession(
                strategy_version_id=version.id, name=name, status="RUNNING", data_source=data_source,
                initial_capital=initial_capital, strategy_config=version.config_json, config_hash=version.config_hash,
                session_config={"costs": costs.model_dump(), "execution": execution.model_dump(), "dataset_id": dataset_id},
                cash=initial_capital, started_at=utcnow(),
            )
            db.add(row)
            db.flush()
            session_id = row.id
            strategy_id = version.strategy_id
            runner = PaperSessionRunner(row, self.hub, catalog, underlying, strategy_id)
        runner.start(restore=False, runtime_state=None)
        self.runners[session_id] = runner
        record_event("INFO", "SESSION_STARTED", "paper_trading", f"Paper session {session_id} '{name}' started ({data_source})",
                     {"strategy_version_id": version_id}, paper_session_id=session_id, strategy_id=strategy_id)
        runner.persist(self.hub.now(), force=True)
        return session_id

    def restore_all(self) -> int:
        restored = 0
        with SessionLocal() as db:
            ids = list(db.scalars(select(PaperSession.id).where(PaperSession.status.in_(ACTIVE_STATUSES))))
        for session_id in ids:
            try:
                with SessionLocal() as db:
                    row = db.get(PaperSession, session_id)
                    assert row is not None
                    version = db.get(StrategyVersion, row.strategy_version_id)
                    catalog, underlying = self._catalog_and_underlying(
                        db, row.data_source, (row.session_config or {}).get("dataset_id"), build_strategy(row.strategy_config).underlying)
                    runner = PaperSessionRunner(row, self.hub, catalog, underlying, version.strategy_id if version else None)
                    runtime_state, data_source = row.runtime_state, row.data_source
                runner.start(restore=True, runtime_state=runtime_state)
                self.runners[session_id] = runner
                restored += 1
                record_event("INFO", "SESSION_RESTORED", "paper_trading",
                             f"Paper session {session_id} restored after restart with {len(runner.broker.open_positions())} open position(s)",
                             paper_session_id=session_id)
                if runner.broker.open_positions() and self.hub.source != data_source:
                    record_event("WARNING", "POSITIONS_UNMONITORED", "paper_trading",
                                 f"Session {session_id} has open simulated positions but its {data_source} feed is not running",
                                 paper_session_id=session_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to restore session %s", session_id)
                with session_scope() as db:
                    row = db.get(PaperSession, session_id)
                    if row is not None:
                        row.status, row.stop_reason = "FAILED", f"Restore failed: {exc}"
                record_event("ERROR", "SESSION_RESTORE_FAILED", "paper_trading", f"Session {session_id} could not be restored: {exc}",
                             paper_session_id=session_id)
        return restored

    def get(self, session_id: int) -> PaperSessionRunner | None:
        return self.runners.get(session_id)

    def stop_entries(self, session_id: int) -> None:
        runner = self._require(session_id)
        runner.stop_entries("New entries stopped by user")
        record_event("INFO", "SESSION_ENTRIES_STOPPED", "paper_trading", f"New entries stopped for session {session_id}",
                     paper_session_id=session_id)

    def stop(self, session_id: int, reason: str = "Stopped by user") -> None:
        runner = self._require(session_id)
        runner.stop(reason)
        self.runners.pop(session_id, None)
        record_event("INFO", "SESSION_STOPPED", "paper_trading", f"Paper session {session_id} stopped: {reason}",
                     paper_session_id=session_id)

    def _require(self, session_id: int) -> PaperSessionRunner:
        runner = self.runners.get(session_id)
        if runner is None:
            raise LookupError(f"Paper session {session_id} is not active")
        return runner

    def live_summary(self, session_id: int) -> dict[str, Any] | None:
        runner = self.runners.get(session_id)
        if runner is None:
            return None
        fresh, reason = PaperQuoteSource(runner).data_freshness(self.hub.now())
        return {**runner.summary(), "data_fresh": fresh, "stale_reason": reason,
                "pending_entries": len(runner.runner.pending_entries), "underlying": runner.underlying.key}

    def session_metrics(self, session_id: int, initial_capital: float) -> dict[str, Any]:
        with SessionLocal() as db:
            trades = db.scalars(select(PaperTrade).where(PaperTrade.paper_session_id == session_id)).all()
            rows = [{c: getattr(t, c) for c in ("net_pnl", "gross_pnl", "entry_charges", "exit_charges", "slippage_cost")} for t in trades]
        return trade_metrics(rows, initial_capital)
