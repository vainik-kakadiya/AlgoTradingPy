"""Performance metrics with explicit, tested definitions.

* A trade is a *win* if net P&L > 0, a *loss* if < 0, *break-even* if exactly 0
  (within 1e-9). Win rate = wins / all closed trades × 100 (break-evens count in the
  denominator).
* Profit factor = sum of net P&L of winning trades / |sum of net P&L of losing trades|.
  With no losing trades it is reported as unavailable (state ``NO_LOSSES``), never as a
  misleading finite number.
* Return % = net P&L / configured initial capital × 100 (not return on margin/premium).
* Maximum drawdown: running peak starts at initial capital; drawdown at each equity point
  = peak − equity. Max drawdown (₹) and max drawdown (%) = (peak − equity) / peak × 100 are
  the maxima over all points and may occur at different times. Equity points are
  mark-to-market snapshots (candle closes and trade exits); intrabar dips are not captured.
* Daily P&L groups trades by the IST calendar date of the exit.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from app.core.timeutil import ist_date

EPS = 1e-9


def classify(net_pnl: float) -> str:
    if net_pnl > EPS:
        return "WIN"
    if net_pnl < -EPS:
        return "LOSS"
    return "BREAKEVEN"


def trade_metrics(trades: Sequence[dict[str, Any]], initial_capital: float | None) -> dict[str, Any]:
    """``trades`` are dicts with at least net_pnl, gross_pnl, entry_charges, exit_charges, slippage_cost."""
    n = len(trades)
    nets = [float(t["net_pnl"]) for t in trades]
    wins = [p for p in nets if classify(p) == "WIN"]
    losses = [p for p in nets if classify(p) == "LOSS"]
    breakeven = n - len(wins) - len(losses)
    gross = sum(float(t["gross_pnl"]) for t in trades)
    charges = sum(float(t["entry_charges"]) + float(t["exit_charges"]) for t in trades)
    slippage = sum(float(t["slippage_cost"]) for t in trades)
    net = sum(nets)
    sum_wins, sum_losses = sum(wins), sum(losses)
    if n == 0:
        pf, pf_state = None, "NO_TRADES"
    elif not losses:
        pf, pf_state = None, "NO_LOSSES"
    else:
        pf, pf_state = sum_wins / abs(sum_losses), "FINITE"
    return {
        "total_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": breakeven,
        "win_rate": (len(wins) / n * 100) if n else None,
        "gross_pnl": gross,
        "total_charges": charges,
        "total_slippage": slippage,
        "net_pnl": net,
        "return_pct": (net / initial_capital * 100) if initial_capital else None,
        "gross_profit": sum_wins,
        "gross_loss": sum_losses,
        "profit_factor": pf,
        "profit_factor_state": pf_state,
        "avg_win": (sum_wins / len(wins)) if wins else None,
        "avg_loss": (sum_losses / len(losses)) if losses else None,
        "avg_trade": (net / n) if n else None,
        "largest_win": max(wins) if wins else None,
        "largest_loss": min(losses) if losses else None,
    }


def drawdown_series(points: Sequence[tuple[datetime, float]], initial_capital: float) -> list[dict[str, Any]]:
    peak = initial_capital
    out = []
    for ts, equity in points:
        peak = max(peak, equity)
        dd = peak - equity
        out.append({"ts": ts, "equity": equity, "peak": peak, "drawdown": dd, "drawdown_pct": (dd / peak * 100) if peak > 0 else 0.0})
    return out


def max_drawdown(points: Sequence[tuple[datetime, float]], initial_capital: float) -> tuple[float, float]:
    series = drawdown_series(points, initial_capital)
    if not series:
        return 0.0, 0.0
    return max(p["drawdown"] for p in series), max(p["drawdown_pct"] for p in series)


def daily_pnl(trades: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    days: dict[str, dict[str, Any]] = defaultdict(lambda: {"net_pnl": 0.0, "gross_pnl": 0.0, "trades": 0, "charges": 0.0})
    for t in trades:
        key = ist_date(t["exit_ts"]).isoformat()
        d = days[key]
        d["net_pnl"] += float(t["net_pnl"])
        d["gross_pnl"] += float(t["gross_pnl"])
        d["charges"] += float(t["entry_charges"]) + float(t["exit_charges"])
        d["trades"] += 1
    return [{"date": k, **v} for k, v in sorted(days.items())]


def monthly_pnl(trades: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    months: dict[str, dict[str, Any]] = defaultdict(lambda: {"net_pnl": 0.0, "trades": 0})
    for t in trades:
        key = ist_date(t["exit_ts"]).strftime("%Y-%m")
        months[key]["net_pnl"] += float(t["net_pnl"])
        months[key]["trades"] += 1
    return [{"month": k, **v} for k, v in sorted(months.items())]


def group_metrics(trades: Sequence[dict[str, Any]], key_fn, initial_capital: float | None = None) -> list[dict[str, Any]]:  # noqa: ANN001
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        groups[str(key_fn(t))].append(t)
    return [{"group": k, **trade_metrics(v, initial_capital)} for k, v in sorted(groups.items())]


def pnl_distribution(trades: Sequence[dict[str, Any]], buckets: int = 20) -> list[dict[str, Any]]:
    nets = [float(t["net_pnl"]) for t in trades]
    if not nets:
        return []
    lo, hi = min(nets), max(nets)
    if hi - lo < EPS:
        return [{"from": lo, "to": hi, "count": len(nets)}]
    width = (hi - lo) / buckets
    counts = [0] * buckets
    for p in nets:
        counts[min(int((p - lo) / width), buckets - 1)] += 1
    return [{"from": lo + i * width, "to": lo + (i + 1) * width, "count": c} for i, c in enumerate(counts)]
