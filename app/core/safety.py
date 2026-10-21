"""Paper-only safety boundary.

This module is the single place that defines which execution modes exist and which
Kite Connect methods the application may call. Real-order placement is not a mode
that can be switched on: there is no LIVE execution mode, and the Kite wrapper only
forwards an explicit allowlist of market-data / session methods.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ExecutionMode(str, Enum):
    PAPER = "PAPER"
    BACKTEST = "BACKTEST"


ALLOWED_EXECUTION_MODES = frozenset(m.value for m in ExecutionMode)


class SafetyViolation(RuntimeError):
    """Raised when code attempts something the paper-only architecture forbids."""


def validate_execution_mode(mode: Any) -> ExecutionMode:
    value = mode.value if isinstance(mode, Enum) else str(mode).upper()
    if value not in ALLOWED_EXECUTION_MODES:
        raise SafetyViolation(
            f"Unsupported execution mode {mode!r}. Only simulated modes {sorted(ALLOWED_EXECUTION_MODES)} exist."
        )
    return ExecutionMode(value)


# Kite Connect methods this application is permitted to call. Everything else - including
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


class MarketDataOnlyKite:
    """Wraps a KiteConnect client and exposes only allowlisted, non-trading methods."""

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name: str) -> Any:
        if name in KITE_ALLOWED_METHODS:
            return getattr(object.__getattribute__(self, "_client"), name)
        raise SafetyViolation(
            f"Kite method '{name}' is not available: this application is paper-only and only uses market data."
        )

    def __setattr__(self, name: str, value: Any) -> None:
        raise SafetyViolation("MarketDataOnlyKite is immutable")
