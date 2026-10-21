from datetime import date

from app.instruments.contract_resolver import ListCatalog, resolve_option, validate_contract
from app.instruments.expiry_selector import select_expiry
from app.strategies.config_schema import OptionSelection
from app.trading.types import Direction
from tests.helpers import option

E1, E2, E3 = date(2025, 1, 7), date(2025, 1, 14), date(2025, 1, 28)


def catalog() -> ListCatalog:
    insts = []
    for exp in (E1, E2):
        for k in range(23800, 24301, 50):
            insts += [option(k, "CE", exp), option(k, "PE", exp)]
    insts.append(option(24000, "CE", date(2025, 1, 2)))  # expired before the decision date
    return ListCatalog(insts)


def test_call_strike_rounds_away_from_spot():
    r = resolve_option(catalog(), "NIFTY", Direction.LONG, 24012.4, date(2025, 1, 6), OptionSelection())
    assert r.ok and r.instrument.instrument_type == "CE"
    assert r.details["desired_strike"] == 24062.4
    assert r.instrument.strike == 24100  # smallest listed strike >= desired
    assert r.instrument.expiry == E1


def test_put_strike_rounds_away_from_spot():
    r = resolve_option(catalog(), "NIFTY", Direction.SHORT, 24012.4, date(2025, 1, 6), OptionSelection())
    assert r.ok and r.instrument.instrument_type == "PE"
    assert r.instrument.strike == 23950  # largest listed strike <= 23962.4


def test_exact_strike_and_nearest_rounding():
    r = resolve_option(catalog(), "NIFTY", Direction.LONG, 24000, date(2025, 1, 6), OptionSelection())
    assert r.instrument.strike == 24050
    r2 = resolve_option(catalog(), "NIFTY", Direction.LONG, 24020, date(2025, 1, 6), OptionSelection(strike_rounding="NEAREST"))
    assert r2.instrument.strike == 24050  # desired 24070 -> nearest 24050
    r3 = resolve_option(catalog(), "NIFTY", Direction.LONG, 24025, date(2025, 1, 6), OptionSelection(strike_rounding="NEAREST"))
    assert r3.instrument.strike == 24100  # tie 24050/24100 goes away from spot


def test_no_strike_within_deviation_fails():
    r = resolve_option(catalog(), "NIFTY", Direction.LONG, 24400, date(2025, 1, 6), OptionSelection())
    assert not r.ok and "required side" in r.reason
    r2 = resolve_option(catalog(), "NIFTY", Direction.LONG, 24300, date(2025, 1, 6),
                        OptionSelection(strike_rounding="NEAREST", max_strike_deviation_points=10))
    assert not r2.ok and "max" in r2.reason


def test_expired_contracts_never_selected_and_expiry_policies():
    r = resolve_option(catalog(), "NIFTY", Direction.LONG, 23900, date(2025, 1, 8), OptionSelection())
    assert r.instrument.expiry == E2  # E1 expired on the 7th
    assert select_expiry([E1, E2, E3], date(2025, 1, 6), "NEAREST_ELIGIBLE") == E1
    assert select_expiry([E1, E2, E3], date(2025, 1, 6), "NEAREST_ELIGIBLE", min_days_to_expiry=2) == E2
    assert select_expiry([E1, E2, E3], date(2025, 1, 6), "NEXT_AFTER_NEAREST") == E2
    assert select_expiry([E1, E2, E3], date(2025, 1, 6), "NEAREST_MONTHLY") == E3
    assert select_expiry([E1], date(2025, 1, 8), "NEAREST_ELIGIBLE") is None


def test_listing_date_respected():
    cat = ListCatalog([option(24050, "CE", E1)], listed_from={"NFO:NIFTY2510724050CE": date(2025, 1, 7)})
    assert not resolve_option(cat, "NIFTY", Direction.LONG, 24000, date(2025, 1, 6), OptionSelection()).ok


def test_contract_validation_checks_metadata():
    inst = option(24050, "CE", E1)
    assert validate_contract(inst, "NIFTY", "CE", E1, 24050) is None
    assert "underlying" in validate_contract(inst, "BANKNIFTY", "CE", E1, 24050)
    assert "type" in validate_contract(inst, "NIFTY", "PE", E1, 24050)
    assert "strike" in validate_contract(inst, "NIFTY", "CE", E1, 24100)
