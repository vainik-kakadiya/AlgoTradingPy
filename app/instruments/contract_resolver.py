"""Resolve a signal into a concrete, listed, validated contract.

The resolver only chooses among contracts present in an instrument catalog that was
valid at the decision date (the Kite instrument master snapshot for live/paper, or the
dataset's own contract metadata for backtests). It never invents strikes or recovers
expired contracts from the current master.

Strike rounding rules (``option_selection.strike_rounding``):

* ``AWAY_FROM_SPOT`` (default): CALL above spot -> smallest listed strike >= desired;
  PUT below spot -> largest listed strike <= desired. The chosen strike is never closer to
  spot than requested.
* ``TOWARD_SPOT``: the opposite direction.
* ``NEAREST``: closest listed strike; ties go away from spot.

If the selected strike is further than ``max_strike_deviation_points`` from the desired
strike, resolution fails (the entry is skipped and logged).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from app.instruments.expiry_selector import select_expiry
from app.strategies.config_schema import FutureSelection, OptionSelection
from app.trading.types import Direction, InstrumentRef


class InstrumentCatalog(Protocol):
    def contracts(self, underlying: str, instrument_types: tuple[str, ...], as_of: date) -> list[InstrumentRef]: ...


class ListCatalog:
    """Catalog over an in-memory list (backtests, tests). Filters by listing/expiry at ``as_of``."""

    def __init__(self, instruments: list[InstrumentRef], listed_from: dict[str, date] | None = None) -> None:
        self.instruments = instruments
        self.listed_from = listed_from or {}

    def contracts(self, underlying: str, instrument_types: tuple[str, ...], as_of: date) -> list[InstrumentRef]:
        out = []
        for inst in self.instruments:
            if inst.underlying != underlying or inst.instrument_type not in instrument_types:
                continue
            if inst.expiry is None or inst.expiry < as_of:
                continue
            first = self.listed_from.get(inst.key)
            if first is not None and first > as_of:
                continue
            out.append(inst)
        return out


@dataclass
class Resolution:
    instrument: InstrumentRef | None
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.instrument is not None


def validate_contract(inst: InstrumentRef, underlying: str, expected_type: str, expiry: date, strike: float | None) -> str | None:
    if inst.underlying != underlying:
        return f"underlying mismatch ({inst.underlying} != {underlying})"
    if inst.instrument_type != expected_type:
        return f"instrument type mismatch ({inst.instrument_type} != {expected_type})"
    if inst.expiry != expiry:
        return "expiry mismatch"
    if strike is not None and (inst.strike is None or abs(inst.strike - strike) > 1e-9):
        return "strike mismatch"
    if inst.lot_size is None or inst.lot_size <= 0:
        return "invalid lot size"
    if inst.tick_size is None or inst.tick_size <= 0:
        return "invalid tick size"
    return None


def _pick_strike(strikes: list[float], desired: float, side_above: bool, rounding: str) -> float | None:
    if not strikes:
        return None
    above = [s for s in strikes if s >= desired - 1e-9]
    below = [s for s in strikes if s <= desired + 1e-9]
    if rounding == "AWAY_FROM_SPOT":
        pool = above if side_above else below
        return (min(pool) if side_above else max(pool)) if pool else None
    if rounding == "TOWARD_SPOT":
        pool = below if side_above else above
        return (max(pool) if side_above else min(pool)) if pool else None
    # NEAREST, ties away from spot
    return min(strikes, key=lambda s: (abs(s - desired), -(s - desired) if side_above else (s - desired)))


def resolve_option(
    catalog: InstrumentCatalog,
    underlying: str,
    direction: Direction,
    spot: float,
    as_of: date,
    selection: OptionSelection,
) -> Resolution:
    option_type = "CE" if direction is Direction.LONG else "PE"
    side = selection.call_strike_side if option_type == "CE" else selection.put_strike_side
    side_above = side == "ABOVE_SPOT"
    desired = spot + selection.target_strike_offset_points if side_above else spot - selection.target_strike_offset_points
    details: dict[str, Any] = {
        "option_type": option_type,
        "spot": spot,
        "desired_strike": round(desired, 2),
        "strike_side": side,
        "strike_rounding": selection.strike_rounding,
        "expiry_policy": selection.expiry_policy,
    }
    contracts = catalog.contracts(underlying, (option_type,), as_of)
    if not contracts:
        return Resolution(None, f"No listed {underlying} {option_type} contracts available on {as_of}", details)
    expiry = select_expiry({c.expiry for c in contracts if c.expiry}, as_of, selection.expiry_policy, selection.min_days_to_expiry)
    if expiry is None:
        return Resolution(None, f"No expiry satisfies policy {selection.expiry_policy}", details)
    details["expiry"] = expiry.isoformat()
    same_expiry = [c for c in contracts if c.expiry == expiry and c.strike is not None]
    strike = _pick_strike(sorted({c.strike for c in same_expiry}), desired, side_above, selection.strike_rounding)  # type: ignore[misc]
    if strike is None:
        return Resolution(None, f"No listed strike on the required side of {desired:.2f} for expiry {expiry}", details)
    details["selected_strike"] = strike
    details["strike_deviation"] = round(abs(strike - desired), 2)
    if abs(strike - desired) > selection.max_strike_deviation_points:
        return Resolution(
            None,
            f"Nearest listed strike {strike:g} is {abs(strike - desired):.2f} points from desired {desired:.2f} "
            f"(max {selection.max_strike_deviation_points:g})",
            details,
        )
    matches = [c for c in same_expiry if abs((c.strike or 0) - strike) < 1e-9]
    if len(matches) != 1:
        return Resolution(None, f"Ambiguous contract metadata: {len(matches)} contracts for strike {strike:g} {expiry}", details)
    contract = matches[0]
    problem = validate_contract(contract, underlying, option_type, expiry, strike)
    if problem:
        return Resolution(None, f"Contract validation failed: {problem}", details)
    details["tradingsymbol"] = contract.tradingsymbol
    details["lot_size"] = contract.lot_size
    return Resolution(contract, None, details)


def resolve_future(catalog: InstrumentCatalog, underlying: str, as_of: date, selection: FutureSelection) -> Resolution:
    contracts = catalog.contracts(underlying, ("FUT",), as_of)
    details: dict[str, Any] = {"expiry_policy": selection.expiry_policy}
    if not contracts:
        return Resolution(None, f"No listed {underlying} futures available on {as_of}", details)
    expiry = select_expiry({c.expiry for c in contracts if c.expiry}, as_of, selection.expiry_policy, selection.min_days_to_expiry)
    if expiry is None:
        return Resolution(None, f"No futures expiry satisfies policy {selection.expiry_policy}", details)
    matches = [c for c in contracts if c.expiry == expiry]
    if len(matches) != 1:
        return Resolution(None, f"Ambiguous futures metadata for expiry {expiry}", details)
    problem = validate_contract(matches[0], underlying, "FUT", expiry, None)
    if problem:
        return Resolution(None, f"Contract validation failed: {problem}", details)
    details.update(expiry=expiry.isoformat(), tradingsymbol=matches[0].tradingsymbol, lot_size=matches[0].lot_size)
    return Resolution(matches[0], None, details)
