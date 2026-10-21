import pytest

from app.strategies.config_schema import DEFAULT_NIFTY_MOMENTUM_CONFIG
from app.strategies.registry import build_strategy
from app.trading.types import Candle, Direction, SignalAction
from tests.helpers import at


def candle(o: float, h: float, low: float, c: float, complete: bool = True) -> Candle:
    return Candle(at(9, 15), 180, o, h, low, c, None, complete)


@pytest.fixture()
def strategy():
    return build_strategy(DEFAULT_NIFTY_MOMENTUM_CONFIG)


def test_bullish_candle_generates_call_signal(strategy):
    sig = strategy.evaluate([candle(24000, 24110, 23995, 24090)])  # range 115, body 90
    assert sig is not None
    assert sig.direction is Direction.LONG and sig.action is SignalAction.BUY_CALL
    assert sig.ts == at(9, 18)  # decision at candle close
    assert sig.reference_price == 24090
    assert sig.explanation["range"] == 115 and sig.explanation["body"] == 90


def test_bearish_candle_generates_put_signal(strategy):
    sig = strategy.evaluate([candle(24090, 24100, 23980, 24000)])  # range 120, body -90
    assert sig is not None and sig.action is SignalAction.BUY_PUT and sig.direction is Direction.SHORT


@pytest.mark.parametrize(
    ("o", "h", "low", "c", "why"),
    [
        (24000, 24100, 24000, 24090, "range exactly 100"),
        (24000, 24150, 24000, 24080, "body exactly 80"),
        (24000, 24150, 23950, 24000, "doji"),
        (24000, 24150, 23950, 24050, "body too small"),
        (24000, 24090, 24000, 24085, "range too small"),
    ],
)
def test_non_qualifying_candles(strategy, o, h, low, c, why):
    assert strategy.evaluate([candle(o, h, low, c)]) is None, why


def test_incomplete_candle_never_signals(strategy):
    assert strategy.evaluate([candle(24000, 24200, 23990, 24190, complete=False)]) is None


def test_signal_id_is_deterministic(strategy):
    a = strategy.evaluate([candle(24000, 24110, 23995, 24090)])
    b = strategy.evaluate([candle(24000, 24110, 23995, 24090)])
    assert a.signal_id == b.signal_id == "nifty_3m_momentum_v1:NIFTY:2025-01-06T03:45:00Z"


def test_thresholds_are_configurable():
    cfg = {**DEFAULT_NIFTY_MOMENTUM_CONFIG, "entry": {"range_gt": 50, "absolute_body_gt": 40, "evaluate_on_closed_candle_only": True}}
    s = build_strategy(cfg)
    assert s.evaluate([candle(24000, 24060, 24000, 24045)]) is not None
