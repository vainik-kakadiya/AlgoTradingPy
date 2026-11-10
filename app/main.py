"""FastAPI application entry point.

Run locally:  uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import install_error_handlers
from app.api.routes import analytics, auth, backtests, market, paper, strategies
from app.api.websocket import router as ws_router
from app.core.auth import AuthMiddleware, credentials_configured
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.migrate import upgrade_database
from app.database.session import init_engine
from app.services.event_bus import bus, record_event
from app.services.runtime import backtest_jobs, hub, paper_service

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.auth_enabled and settings.auth_required and not credentials_configured():
        raise RuntimeError("AUTH_EMAIL and AUTH_PASSWORD must be set in AlgoTradingPy/.env")
    init_engine()
    if not getattr(app.state, "skip_migrations", False):
        upgrade_database()
    bus.attach_loop(asyncio.get_running_loop())
    record_event("INFO", "BACKEND_STARTED", "system", "Backend started (paper-only execution)")
    if settings.auth_enabled and not credentials_configured():
        record_event("ERROR", "AUTH_NOT_CONFIGURED", "auth",
                     "Login is not configured: set AUTH_EMAIL and AUTH_PASSWORD in .env. Nobody can log in until then.")

    restored = paper_service.restore_all()
    if getattr(app.state, "resume_backtests", True):
        backtest_jobs.resume_interrupted()
    from app.market_data.kite_client import kite_sessions

    kite_sessions_needed = any(r.data_source == "KITE" for r in paper_service.runners.values())
    if settings.auto_start_feed and kite_sessions_needed and kite_sessions.has_token_for_today:
        try:
            await hub.start("KITE")
        except Exception as exc:  # noqa: BLE001
            record_event("ERROR", "FEED_AUTOSTART_FAILED", "market_data", f"Could not auto-start Kite feed: {exc}")
    elif kite_sessions_needed:
        record_event("WARNING", "FEED_NOT_STARTED", "market_data",
                     "Restored KITE paper sessions are waiting for the market-data feed (log in to Kite and start the feed)")
    logger.info("Restored %d paper session(s)", restored)
    try:
        yield
    finally:
        await hub.shutdown()
        backtest_jobs.shutdown()
        record_event("INFO", "BACKEND_STOPPED", "system", "Backend stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Algo Trading Lab API",
        version="1.0.0",
        description="Backtesting and paper trading for Zerodha Kite market data. Paper-only: no real orders can be placed.",
        lifespan=lifespan,
    )
    # Every /api, /ws and docs request needs a login session (added first = runs inside CORS).
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )
    install_error_handlers(app)
    for module in (auth, strategies, market, backtests, paper, analytics):
        app.include_router(module.router)
    app.include_router(ws_router)
    return app


app = create_app()
