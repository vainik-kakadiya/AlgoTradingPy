"""Static description of supported underlyings.

Only naming conventions live here. Lot sizes, tick sizes, strikes and expiries always come
from instrument metadata (Kite instrument master or dataset metadata), never from here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UnderlyingSpec:
    symbol: str  # name used in strategy configs and as the derivative "name" in the instrument master
    index_exchange: str
    index_tradingsymbol: str  # Kite tradingsymbol of the spot index
    derivative_exchange: str

    @property
    def index_key(self) -> str:
        return f"{self.index_exchange}:{self.index_tradingsymbol}"


SUPPORTED_UNDERLYINGS: dict[str, UnderlyingSpec] = {
    "NIFTY": UnderlyingSpec("NIFTY", "NSE", "NIFTY 50", "NFO"),
    "BANKNIFTY": UnderlyingSpec("BANKNIFTY", "NSE", "NIFTY BANK", "NFO"),
    "FINNIFTY": UnderlyingSpec("FINNIFTY", "NSE", "NIFTY FIN SERVICE", "NFO"),
    "MIDCPNIFTY": UnderlyingSpec("MIDCPNIFTY", "NSE", "NIFTY MID SELECT", "NFO"),
    "SENSEX": UnderlyingSpec("SENSEX", "BSE", "SENSEX", "BFO"),
}


def get_underlying(symbol: str) -> UnderlyingSpec:
    try:
        return SUPPORTED_UNDERLYINGS[symbol.upper()]
    except KeyError as exc:
        raise ValueError(f"Unsupported underlying '{symbol}'") from exc
