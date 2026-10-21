"""Configurable transaction-cost and slippage assumptions.

Default rates approximate Zerodha / NSE F&O charges at the time of writing. Statutory
charges change; every rate is configurable per backtest and per paper session, and the
values used are stored with the results.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.trading.types import Side


class CostConfig(BaseModel):
    brokerage_per_order: float = Field(20.0, ge=0, description="Flat brokerage per executed order (INR)")
    brokerage_pct_cap: float | None = Field(
        0.03, ge=0,
        description="Futures/equity brokerage is the lower of the flat fee and this % of turnover (None = flat). Options are always flat.",
    )
    # Options (rates in % of premium turnover)
    options_stt_sell_pct: float = Field(0.1, ge=0)
    options_exchange_txn_pct: float = Field(0.03503, ge=0)
    # Futures (rates in % of turnover)
    futures_stt_sell_pct: float = Field(0.02, ge=0)
    futures_exchange_txn_pct: float = Field(0.00173, ge=0)
    # Equity intraday
    equity_stt_sell_pct: float = Field(0.025, ge=0)
    equity_exchange_txn_pct: float = Field(0.00297, ge=0)
    sebi_fee_pct: float = Field(0.0001, ge=0)
    stamp_duty_buy_pct_options: float = Field(0.003, ge=0)
    stamp_duty_buy_pct_futures: float = Field(0.002, ge=0)
    stamp_duty_buy_pct_equity: float = Field(0.003, ge=0)
    gst_pct: float = Field(18.0, ge=0)
    enabled: bool = True


class SlippageConfig(BaseModel):
    """Adverse slippage applied to market and stop fills (never to limit fills)."""

    points: float = Field(0.5, ge=0, description="Fixed adverse price points per fill")
    pct: float = Field(0.0, ge=0, description="Additional adverse % of the reference price")

    def amount(self, reference_price: float) -> float:
        return self.points + reference_price * self.pct / 100.0


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange_txn: float = 0.0
    sebi: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange_txn + self.sebi + self.stamp_duty + self.gst

    def to_dict(self) -> dict[str, float]:
        return {
            "brokerage": round(self.brokerage, 4),
            "stt": round(self.stt, 4),
            "exchange_txn": round(self.exchange_txn, 4),
            "sebi": round(self.sebi, 4),
            "stamp_duty": round(self.stamp_duty, 4),
            "gst": round(self.gst, 4),
            "total": round(self.total, 4),
        }


def compute_charges(cfg: CostConfig, side: Side, price: float, quantity: int, instrument_type: str) -> ChargeBreakdown:
    """Charges for one executed fill."""
    if not cfg.enabled or quantity <= 0:
        return ChargeBreakdown()
    turnover = price * quantity
    if instrument_type in ("CE", "PE"):
        stt_pct, txn_pct, stamp_pct = cfg.options_stt_sell_pct, cfg.options_exchange_txn_pct, cfg.stamp_duty_buy_pct_options
    elif instrument_type == "FUT":
        stt_pct, txn_pct, stamp_pct = cfg.futures_stt_sell_pct, cfg.futures_exchange_txn_pct, cfg.stamp_duty_buy_pct_futures
    else:
        stt_pct, txn_pct, stamp_pct = cfg.equity_stt_sell_pct, cfg.equity_exchange_txn_pct, cfg.stamp_duty_buy_pct_equity

    brokerage = cfg.brokerage_per_order
    if cfg.brokerage_pct_cap is not None and instrument_type not in ("CE", "PE"):
        brokerage = min(brokerage, turnover * cfg.brokerage_pct_cap / 100.0)
    stt = turnover * stt_pct / 100.0 if side is Side.SELL else 0.0
    exchange_txn = turnover * txn_pct / 100.0
    sebi = turnover * cfg.sebi_fee_pct / 100.0
    stamp = turnover * stamp_pct / 100.0 if side is Side.BUY else 0.0
    gst = (brokerage + exchange_txn + sebi) * cfg.gst_pct / 100.0
    return ChargeBreakdown(brokerage, stt, exchange_txn, sebi, stamp, gst)
