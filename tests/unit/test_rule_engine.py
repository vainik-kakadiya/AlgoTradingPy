from datetime import timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from app.strategies import indicators
from app.strategies.rule_engine import Condition, RuleValidationError, check_complexity, describe, evaluate, required_lookback
from app.trading.types import Candle
from tests.helpers import at

parse = TypeAdapter(Condition).validate_python


def series(closes: list[float], volume: float | None = None) -> list[Candle]:
    return [Candle(at(9, 15) + timedelta(minutes=3 * i), 180, c, c + 1, c - 1, c, volume) for i, c in enumerate(closes)]


def test_comparisons_and_fields():
    cs = [Candle(at(9, 15), 180, 100, 130, 95, 120)]
    assert evaluate(parse({"op": "gt", "left": {"type": "field", "name": "range"}, "right": 30}), cs) is True
    assert evaluate(parse({"op": "gte", "left": {"type": "field", "name": "range"}, "right": 35}), cs) is True
    assert evaluate(parse({"op": "lt", "left": {"type": "field", "name": "body"}, "right": 20}), cs) is False
    assert evaluate(parse({"op": "lte", "left": {"type": "field", "name": "abs_body"}, "right": 20}), cs) is True
    assert evaluate(parse({"op": "eq", "left": {"type": "field", "name": "close"}, "right": 120}), cs) is True
    assert evaluate(parse({"op": "neq", "left": {"type": "field", "name": "open"}, "right": 120}), cs) is True


def test_change_fields_need_previous_candle():
    rule = parse({"op": "gt", "left": {"type": "field", "name": "change_pct"}, "right": 1})
    assert evaluate(rule, series([100])) is None
    assert evaluate(rule, series([100, 102])) is True


def test_logic_is_three_valued():
    unknown = {"op": "gt", "left": {"type": "indicator", "name": "sma", "period": 5}, "right": 0}
    false = {"op": "lt", "left": {"type": "field", "name": "close"}, "right": 0}
    true = {"op": "gt", "left": {"type": "field", "name": "close"}, "right": 0}
    cs = series([100, 101])
    assert evaluate(parse({"op": "not", "condition": unknown}), cs) is None  # unknown never becomes a signal
    assert evaluate(parse({"op": "and", "conditions": [unknown, false]}), cs) is False
    assert evaluate(parse({"op": "and", "conditions": [unknown, true]}), cs) is None
    assert evaluate(parse({"op": "or", "conditions": [unknown, true]}), cs) is True
    assert evaluate(parse({"op": "or", "conditions": [unknown, false]}), cs) is None


def test_crosses_above_and_below():
    above = parse({"op": "crosses_above", "left": {"type": "field", "name": "close"}, "right": 100})
    below = parse({"op": "crosses_below", "left": {"type": "field", "name": "close"}, "right": 100})
    assert evaluate(above, series([99, 101])) is True
    assert evaluate(above, series([101, 102])) is False
    assert evaluate(below, series([101, 99])) is True
    assert evaluate(above, series([101])) is None


def test_indicators_values():
    closes = [float(x) for x in range(1, 21)]
    assert indicators.sma(closes, 5) == pytest.approx(18.0)
    assert indicators.ema(closes, 20) == pytest.approx(10.5)
    assert indicators.rsi(closes, 14) == 100.0
    assert indicators.rsi([10, 9, 8, 7], 3) == pytest.approx(0.0)
    assert indicators.session_vwap(series([100, 102], volume=10)) == pytest.approx(101.0)
    assert indicators.session_vwap(series([100, 102])) is None  # index candles have no volume


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "exec", "left": 1, "right": 2},
        {"op": "gt", "left": {"type": "field", "name": "__import__"}, "right": 1},
        {"op": "gt", "left": "__import__('os').system('x')", "right": 1},
        {"op": "gt", "left": {"type": "indicator", "name": "sma"}, "right": 1},
        {"op": "gt", "left": {"type": "field", "name": "close", "extra": 1}, "right": 1},
        {"op": "and", "conditions": []},
        {"op": "gt", "left": True, "right": 1},
    ],
)
def test_malformed_rules_rejected(bad):
    with pytest.raises(ValidationError):
        parse(bad)


def test_complexity_limits():
    node: dict = {"op": "gt", "left": 1, "right": 0}
    for _ in range(15):
        node = {"op": "not", "condition": node}
    with pytest.raises(RuleValidationError):
        check_complexity(parse(node))


def test_describe_and_lookback():
    rule = parse({"op": "crosses_above", "left": {"type": "indicator", "name": "ema", "period": 9},
                  "right": {"type": "indicator", "name": "ema", "period": 21}})
    assert describe(rule) == "EMA(9) crosses above EMA(21)"
    assert required_lookback(rule) == 22
