"""Process-wide singletons for the long-running engines."""

from __future__ import annotations

from app.backtesting.service import backtest_jobs
from app.market_data.hub import hub
from app.paper_trading.engine import PaperTradingService

paper_service = PaperTradingService(hub)

__all__ = ["backtest_jobs", "hub", "paper_service"]
