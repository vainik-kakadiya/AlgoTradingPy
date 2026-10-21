import copy

import pytest

from app.strategies.config_schema import DEFAULT_NIFTY_MOMENTUM_CONFIG, parse_config, validate_config


def cfg(**changes):
    c = copy.deepcopy(DEFAULT_NIFTY_MOMENTUM_CONFIG)
    for path, value in changes.items():
        node = c
        keys = path.split("__")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
    return c


def test_prd_example_config_is_valid():
    result = validate_config(DEFAULT_NIFTY_MOMENTUM_CONFIG)
    assert result.valid, result.errors
    assert result.config_hash and len(result.config_hash) == 64
    assert any("BUY CALL" in line for line in result.summary)
    assert any("daily loss" in w for w in result.warnings)  # SL × lot can exceed the ₹2,000 daily limit


def test_hash_is_stable_and_sensitive():
    a = validate_config(DEFAULT_NIFTY_MOMENTUM_CONFIG).config_hash
    b = validate_config(copy.deepcopy(DEFAULT_NIFTY_MOMENTUM_CONFIG)).config_hash
    c = validate_config(cfg(exits__stop_loss_points=40)).config_hash
    assert a == b != c


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"execution__allow_real_orders": True}, "Real-order"),
        ({"mode": "live"}, "paper"),
        ({"execution__execution_mode": "LIVE"}, "PAPER"),
        ({"entry__range_gt": -5}, "greater than"),
        ({"underlying": "XYZ"}, "Unsupported underlying"),
        ({"timeframe_seconds": 150}, "whole minutes"),
        ({"exits__stop_loss_points": 0}, "greater than"),
        ({"option_selection__quantity_lots": 9}, "max_lots_per_trade"),
        ({"session__square_off_time": "16:00"}, "square_off_time"),
        ({"unexpected_field": 1}, "Extra inputs"),
        ({"direction__bullish": "BUY_PUT"}, "BUY_CALL"),
    ],
)
def test_invalid_configs_rejected(changes, fragment):
    result = validate_config(cfg(**changes))
    assert not result.valid
    assert any(fragment.lower() in (e["msg"] + e["loc"]).lower() for e in result.errors), result.errors


def test_real_orders_cannot_be_enabled_by_any_value():
    for value in (True, "true", 1, "yes"):
        assert not validate_config(cfg(execution__allow_real_orders=value)).valid


def test_rule_based_config_requires_an_entry_rule():
    base = {"strategy_type": "rule_based", "strategy_id": "x_rules", "name": "x", "rules": {}}
    assert not validate_config(base).valid
    ok = {**base, "rules": {"long_entry": {"op": "gt", "left": {"type": "field", "name": "close"}, "right": 0}}}
    assert validate_config(ok).valid


def test_strategy_type_inferred_for_prd_config():
    assert parse_config(DEFAULT_NIFTY_MOMENTUM_CONFIG).strategy_type == "nifty_momentum"
