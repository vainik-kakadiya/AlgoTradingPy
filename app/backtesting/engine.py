"""Backtest engine (pure: no database access).

Uses the same StrategyRunner, risk manager, contract resolver and simulated broker as
paper trading. Only the data source (historical bars), the quote source (bar opens) and
the result sink differ.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.analytics.performance import max_drawdown, trade_metrics
from app.backtesting.replay import BarQuoteSource, ReplayData
from app.core.timeutil import ist_date
from app.instruments.contract_resolver import ListCatalog
from app.market_data.candle_builder import BarAggregator
from app.strategies.base import BaseStrategy
from app.trading.broker import SimulatedBroker, TradeRecord
from app.trading.costs import CostConfig
from app.trading.fill_model import ExecutionModelConfig
from app.trading.runner import StrategyRunner
from app.trading.types import ExitReason, Signal

logger = logging.getLogger(__name__)
MAX_STORED_EVENTS = 2000


class BacktestCancelled(Exception):
    pass


@dataclass
class SignalOutcome:
    signal: Signal
    outcome: str
    reason: str | None
    details: dict[str, Any]


@dataclass
class EquityPoint:
    ts: datetime
    cash: float
    realized: float
    unrealized: float
    equity: float


@dataclass
class BacktestResult:
    trades: list[TradeRecord]
    signals: list[SignalOutcome]
    equity: list[EquityPoint]
    events: list[dict[str, Any]]
    event_counts: dict[str, int]
    metrics: dict[str, Any]
    report: dict[str, Any]


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.counts: Counter[str] = Counter()
        self.signals: dict[str, SignalOutcome] = {}

    def event(self, severity: str, event_type: str, message: str, details: dict[str, Any] | None = None, ts: datetime | None = None) -> None:
        self.counts[event_type] += 1
        if event_type in ("SIGNAL", "TRADING_DAY"):
            return  # recorded elsewhere / too noisy
        if len(self.events) < MAX_STORED_EVENTS:
            self.events.append({"ts": ts.isoformat() if ts else None, "severity": severity, "event_type": event_type,
                                "message": message, "details": details})

    def record_signal(self, signal: Signal, outcome: str, reason: str | None, details: dict[str, Any]) -> None:
        self.signals[signal.signal_id] = SignalOutcome(signal, outcome, reason, details)


@dataclass
class BacktestParams:
    initial_capital: float
    costs: CostConfig = field(default_factory=CostConfig)
    execution: ExecutionModelConfig = field(default_factory=ExecutionModelConfig)
    start_date: date | None = None
    end_date: date | None = None


def run_backtest(
    strategy: BaseStrategy,
    data: ReplayData,
    params: BacktestParams,
    progress: Callable[[float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    listed_from: dict[str, date] | None = None,
) -> BacktestResult:
    cfg = strategy.config
    session = cfg.session
    if strategy.timeframe_seconds % data.base_interval_seconds != 0:
        raise ValueError(
            f"Dataset granularity {data.base_interval_seconds}s cannot build {strategy.timeframe_seconds}s candles"
        )
    sink = _CollectingSink()
    broker = SimulatedBroker(params.initial_capital, params.costs, params.execution)
    quotes = BarQuoteSource(data.contract_bars)
    runner = StrategyRunner(strategy, broker, ListCatalog(data.instruments, listed_from), quotes, sink, id_prefix="BT")
    aggregator = BarAggregator(strategy.timeframe_seconds, data.base_interval_seconds)

    timeline = [
        t for t in data.timeline(session.market_open, session.market_close)
        if (params.start_date is None or ist_date(t) >= params.start_date) and (params.end_date is None or ist_date(t) <= params.end_date)
    ]
    equity: list[EquityPoint] = []
    base = timedelta(seconds=data.base_interval_seconds)
    contract_gap_minutes = 0
    incomplete_candles = 0
    total = len(timeline)
    last_trade_count = 0
    prev_day: date | None = None

    def snapshot(ts: datetime) -> None:
        realized = broker.realized_pnl()
        unrealized = broker.unrealized_pnl()
        equity.append(EquityPoint(ts, broker.cash, realized, unrealized, broker.initial_capital + realized + unrealized))

    if timeline:
        snapshot(timeline[0])

    for i, t in enumerate(timeline):
        day = ist_date(t)
        if prev_day is not None and day != prev_day:
            _end_of_day(runner, broker, t, sink)
            snapshot(t)
        prev_day = day

        # 1) traded-contract bars starting at t
        active_keys = {p.instrument.key for p in broker.open_positions()}
        active_keys |= {o.instrument.key for o in broker.active_orders()}
        active_keys |= {p.instrument.key for p in runner.pending_entries.values()}
        for key in active_keys:
            bar = data.contract_bars.get(key, {}).get(t)
            if bar is not None:
                runner.on_bar(key, bar)
            elif key in broker.positions:
                contract_gap_minutes += 1

        # 2) underlying bar [t, t+i) closes at t+i
        ubar = data.underlying_bars.get(t)
        if ubar is not None:
            close_ts = t + base
            runner.on_time(close_ts)
            for candle in aggregator.add(ubar):
                if not candle.complete:
                    incomplete_candles += 1
                runner.on_candle(candle, close_ts)
            if broker.open_positions() or len(broker.trades) != last_trade_count:
                snapshot(close_ts)
                last_trade_count = len(broker.trades)

        if i % 500 == 0:
            if should_cancel and should_cancel():
                raise BacktestCancelled()
            if progress and total:
                progress(i / total)

    for candle in aggregator.flush():
        if not candle.complete:
            incomplete_candles += 1
    if timeline:
        end_ts = timeline[-1] + base
        for position in list(broker.open_positions()):
            runner.close_position(position, ExitReason.END_OF_DATA.value, end_ts, force=True)
        for intent in list(runner.pending_entries.values()):
            sink.record_signal(intent.signal, "SKIPPED", "End of data before entry", intent.details)
        snapshot(end_ts)

    trades = broker.trades
    trade_rows = [t.to_row() for t in trades]
    metrics = trade_metrics(trade_rows, params.initial_capital)
    dd, dd_pct = max_drawdown([(p.ts, p.equity) for p in equity], params.initial_capital)
    metrics.update(max_drawdown=dd, max_drawdown_pct=dd_pct)

    warnings = _warnings(sink, trades, incomplete_candles, contract_gap_minutes, broker)
    outcomes = Counter(s.outcome for s in sink.signals.values())
    report = {
        "metrics": metrics,
        "signals": dict(outcomes),
        "event_counts": dict(sink.counts),
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
        "data_quality": {
            "timeline_points": total,
            "incomplete_strategy_candles": incomplete_candles,
            "contract_bar_gaps_while_in_position": contract_gap_minutes,
            "ambiguous_intrabar_exits": sum(1 for t in trades if "AMBIGUOUS_INTRABAR" in t.flags),
            "gap_through_stop_exits": sum(1 for t in trades if "GAP_THROUGH_STOP" in t.flags),
        },
        "execution_assumptions": params.execution.describe(),
        "warnings": warnings,
        "reconciliation_error": round(broker.reconciliation_error(), 6),
    }
    if progress:
        progress(1.0)
    return BacktestResult(trades, list(sink.signals.values()), equity, sink.events, dict(sink.counts), metrics, report)


def _end_of_day(runner: StrategyRunner, broker: SimulatedBroker, now: datetime, sink: _CollectingSink) -> None:
    """Close positions that the session-end square-off could not close (no bar after square-off time)."""
    if not runner.cfg.exits.square_off_at_session_end:
        return
    for position in list(broker.open_positions()):
        sink.event("WARNING", "SQUARE_OFF_LAST_PRICE",
                   f"No bar after square-off time for {position.instrument.tradingsymbol}; closed at last known price",
                   {"position": position.position_uid}, ts=now)
        position.flags.append("NO_BAR_AFTER_SQUARE_OFF")
        last_ts = position.last_price_ts or now
        runner.close_position(position, ExitReason.SESSION_END.value, last_ts, force=True)


def _warnings(sink: _CollectingSink, trades: list[TradeRecord], incomplete: int, gaps: int, broker: SimulatedBroker) -> list[str]:
    out = []
    if incomplete:
        out.append(f"{incomplete} strategy candle(s) were incomplete (missing source bars) and were not evaluated.")
    skipped = sum(1 for s in sink.signals.values() if s.outcome == "SKIPPED")
    if skipped:
        out.append(f"{skipped} signal(s) were skipped (no listed contract or no option price at entry time).")
    if gaps:
        out.append(f"{gaps} timeline step(s) had no bar for a held contract; stops/targets could not be evaluated at those times.")
    amb = sum(1 for t in trades if "AMBIGUOUS_INTRABAR" in t.flags)
    if amb:
        out.append(f"{amb} exit(s) had both stop and target inside one bar; the stop was assumed first (conservative).")
    forced = sum(1 for t in trades if "FORCED_AT_LAST_KNOWN_PRICE" in t.flags)
    if forced:
        out.append(f"{forced} exit(s) used the last known price because no later bar existed.")
    if abs(broker.reconciliation_error()) > 0.01:
        out.append(f"Accounting reconciliation error of {broker.reconciliation_error():.4f}.")
    return out
