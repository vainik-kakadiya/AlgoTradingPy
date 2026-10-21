from __future__ import annotations

from collections.abc import Iterable
from datetime import date


def monthly_expiries(expiries: Iterable[date]) -> list[date]:
    """The last listed expiry of each calendar month."""
    by_month: dict[tuple[int, int], date] = {}
    for e in expiries:
        key = (e.year, e.month)
        if key not in by_month or e > by_month[key]:
            by_month[key] = e
    return sorted(by_month.values())


def select_expiry(expiries: Iterable[date], as_of: date, policy: str, min_days_to_expiry: int = 0) -> date | None:
    """Pick an expiry from the expiries listed as of ``as_of``.

    ``NEAREST_ELIGIBLE``: earliest expiry at least ``min_days_to_expiry`` days away.
    ``NEXT_AFTER_NEAREST``: the one after that.
    ``NEAREST_MONTHLY``: earliest eligible monthly (last-of-month) expiry.
    """
    listed = sorted(set(expiries))
    eligible = [e for e in listed if (e - as_of).days >= min_days_to_expiry]
    if policy == "NEAREST_ELIGIBLE":
        return eligible[0] if eligible else None
    if policy == "NEXT_AFTER_NEAREST":
        return eligible[1] if len(eligible) > 1 else None
    if policy == "NEAREST_MONTHLY":
        monthly = [e for e in monthly_expiries(listed) if (e - as_of).days >= min_days_to_expiry]
        return monthly[0] if monthly else None
    raise ValueError(f"Unknown expiry policy {policy}")
