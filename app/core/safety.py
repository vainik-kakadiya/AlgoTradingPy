"""Execution safety boundary.

This module is the single place that defines which execution modes exist and which Kite
Connect methods the application may call.

* Strategies, backtests and paper sessions are simulated: ``ExecutionMode`` has only
  ``PAPER`` and ``BACKTEST``, and the general-purpose Kite wrapper (``MarketDataOnlyKite``)
  forwards only an allowlist of market-data / session methods.
* Real orders exist only for sessions started in ``TradingMode.LIVE``. They go through
  ``LiveOrderKite``, a separate wrapper that exposes just the order and position methods
  the live broker needs, and which only ``app/live_trading/broker.py`` uses.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ExecutionMode(str, Enum):
    """Execution mode of a strategy configuration. Always simulated."""

    PAPER = "PAPER"
    BACKTEST = "BACKTEST"


ALLOWED_EXECUTION_MODES = frozenset(m.value for m in ExecutionMode)


class TradingMode(str, Enum):
    """How a trading session executes: simulated (PAPER) or with real money on Zerodha (LIVE)."""

    PAPER = "PAPER"
    LIVE = "LIVE"


class SafetyViolation(RuntimeError):
    """Raised when code attempts something the execution boundary forbids."""


def validate_execution_mode(mode: Any) -> ExecutionMode:
    value = mode.value if isinstance(mode, Enum) else str(mode).upper()
    if value not in ALLOWED_EXECUTION_MODES:
        raise SafetyViolation(
            f"Unsupported execution mode {mode!r}. Only simulated modes {sorted(ALLOWED_EXECUTION_MODES)} exist."
        )
    return ExecutionMode(value)


def validate_trading_mode(mode: Any) -> TradingMode:
    value = mode.value if isinstance(mode, Enum) else str(mode).upper()
    try:
        return TradingMode(value)
    except ValueError:
        raise SafetyViolation(f"Unsupported trading mode {mode!r}. Use PAPER or LIVE.") from None


# Kite Connect methods the general application may call. Everything else - including
# place_order, modify_order, cancel_order, exit_order, GTT and MF order methods - is blocked.
KITE_ALLOWED_METHODS = frozenset(
    {
        "login_url",
        "generate_session",
        "set_access_token",
        "invalidate_access_token",
        "profile",
        "instruments",
        "historical_data",
        "quote",
        "ltp",
        "ohlc",
    }
)

# Explicitly known trading methods, used by tests and error messages.
KITE_FORBIDDEN_METHODS = frozenset(
    {
        "place_order",
        "modify_order",
        "cancel_order",
        "exit_order",
        "place_gtt",
        "modify_gtt",
        "delete_gtt",
        "place_mf_order",
        "cancel_mf_order",
        "place_mf_sip",
        "modify_mf_sip",
        "cancel_mf_sip",
        "convert_position",
    }
)

# The only Kite methods the live broker may call: regular orders plus read-only order/position state.
KITE_LIVE_ORDER_METHODS = frozenset({"place_order", "modify_order", "cancel_order", "orders", "positions"})

# The single module allowed to call Kite order methods (enforced by tests).
LIVE_ORDER_MODULE = "app/live_trading/broker.py"


class MarketDataOnlyKite:
    """Wraps a KiteConnect client and exposes only allowlisted, non-trading methods."""

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name: str) -> Any:
        if name in KITE_ALLOWED_METHODS:
            return getattr(object.__getattribute__(self, "_client"), name)
        raise SafetyViolation(
            f"Kite method '{name}' is not available here: only market-data methods are reachable through this client."
        )

    def __setattr__(self, name: str, value: Any) -> None:
        raise SafetyViolation("MarketDataOnlyKite is immutable")


class LiveOrderKite:
    """Wraps a KiteConnect client for real-money sessions: regular orders and order/position state only.

    GTT, mutual-fund, position-conversion and every other method stay unreachable.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name: str) -> Any:
        if name in KITE_LIVE_ORDER_METHODS:
            return getattr(object.__getattribute__(self, "_client"), name)
        raise SafetyViolation(f"Kite method '{name}' is not available to the live broker.")

    def __setattr__(self, name: str, value: Any) -> None:
        raise SafetyViolation("LiveOrderKite is immutable")
