"""Execution-safety guarantees.

Simulated execution (strategies, backtests, paper sessions) can never reach a Kite order method.
Real orders exist only for LIVE sessions and only through ``LiveOrderKite`` in the single
module ``app/live_trading/broker.py``."""

import ast
from pathlib import Path

import pytest

from app.core.safety import (
    KITE_ALLOWED_METHODS,
    KITE_FORBIDDEN_METHODS,
    KITE_LIVE_ORDER_METHODS,
    LIVE_ORDER_MODULE,
    LiveOrderKite,
    MarketDataOnlyKite,
    SafetyViolation,
    TradingMode,
    validate_execution_mode,
    validate_trading_mode,
)

APP_DIR = Path(__file__).resolve().parents[2] / "app"


class FakeKite:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def method(*a, **k):
            self.calls.append(name)
            return {}
        return method


def test_wrapper_blocks_every_trading_method():
    fake = FakeKite()
    kite = MarketDataOnlyKite(fake)
    for name in KITE_FORBIDDEN_METHODS:
        with pytest.raises(SafetyViolation):
            getattr(kite, name)
    assert fake.calls == []
    kite.quote(["NSE:NIFTY 50"])
    assert fake.calls == ["quote"]


def test_wrapper_blocks_unknown_methods_and_is_immutable():
    kite = MarketDataOnlyKite(FakeKite())
    with pytest.raises(SafetyViolation):
        kite.some_future_order_api()
    with pytest.raises(SafetyViolation):
        kite._client = object()


def test_allowlist_contains_no_trading_methods():
    assert not (KITE_ALLOWED_METHODS & KITE_FORBIDDEN_METHODS)
    from kiteconnect import KiteConnect

    trading_like = {m for m in dir(KiteConnect) if any(w in m for w in ("order", "gtt", "sip", "position"))}
    assert not (KITE_ALLOWED_METHODS & trading_like)


def test_only_simulated_execution_modes_exist():
    assert validate_execution_mode("PAPER").value == "PAPER"
    for bad in ("LIVE", "REAL", "live", "", "PAPER;LIVE"):
        with pytest.raises(SafetyViolation):
            validate_execution_mode(bad)


def test_only_the_live_broker_calls_kite_order_methods():
    """Order methods appear in exactly one module, and only the regular-order ones (no GTT, MF, conversion)."""
    offenders, live_calls = [], set()
    for path in APP_DIR.rglob("*.py"):
        rel = path.relative_to(APP_DIR.parent).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in KITE_FORBIDDEN_METHODS:
                if rel == LIVE_ORDER_MODULE:
                    live_calls.add(node.attr)
                else:
                    offenders.append(f"{rel}:{node.lineno} .{node.attr}")
    assert offenders == []
    assert live_calls <= KITE_LIVE_ORDER_METHODS


def test_live_order_wrapper_allows_only_regular_orders_and_state():
    fake = FakeKite()
    kite = LiveOrderKite(fake)
    for name in ("place_order", "modify_order", "cancel_order", "orders", "positions"):
        getattr(kite, name)()
    for name in KITE_FORBIDDEN_METHODS - KITE_LIVE_ORDER_METHODS:  # GTT, MF, exit_order, convert_position
        with pytest.raises(SafetyViolation):
            getattr(kite, name)
    for name in ("quote", "instruments", "generate_session", "some_future_api"):
        with pytest.raises(SafetyViolation):
            getattr(kite, name)
    with pytest.raises(SafetyViolation):
        kite._client = object()
    assert fake.calls == ["place_order", "modify_order", "cancel_order", "orders", "positions"]


def test_trading_modes():
    assert validate_trading_mode("live") is TradingMode.LIVE
    assert validate_trading_mode("PAPER") is TradingMode.PAPER
    for bad in ("REAL", "", "LIVE;PAPER", "BACKTEST"):
        with pytest.raises(SafetyViolation):
            validate_trading_mode(bad)


def test_execution_modules_do_not_import_kiteconnect():
    for package in ("trading", "backtesting", "paper_trading", "live_trading", "risk", "strategies"):
        for path in (APP_DIR / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                assert not any(n.startswith("kiteconnect") for n in names), f"{path} imports kiteconnect"


def test_no_real_order_endpoints():
    from app.main import app

    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        mutating = methods & {"POST", "PUT", "PATCH", "DELETE"}
        assert not ("order" in path.lower() and mutating), f"mutating order endpoint: {methods} {path}"
        assert "live" not in path.lower(), path
