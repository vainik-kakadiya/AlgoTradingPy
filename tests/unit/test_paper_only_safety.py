"""Paper-only guarantees (PRD §12.1 / §14)."""

import ast
from pathlib import Path

import pytest

from app.core.safety import (
    KITE_ALLOWED_METHODS,
    KITE_FORBIDDEN_METHODS,
    MarketDataOnlyKite,
    SafetyViolation,
    validate_execution_mode,
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


def test_no_source_file_calls_a_kite_trading_method():
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in KITE_FORBIDDEN_METHODS:
                offenders.append(f"{path.name}:{node.lineno} .{node.attr}")
    assert offenders == []


def test_execution_modules_do_not_import_kiteconnect():
    for package in ("trading", "backtesting", "paper_trading", "risk", "strategies"):
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
