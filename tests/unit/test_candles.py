from datetime import timedelta

import pytest

from app.core.timeutil import floor_to_interval
from app.market_data.candle_builder import BarAggregator, TickCandleBuilder
from tests.helpers import at, bar


def test_three_minute_buckets_align_to_0915_ist():
    assert floor_to_interval(at(9, 15), 180) == at(9, 15)
    assert floor_to_interval(at(9, 17, 59), 180) == at(9, 15)
    assert floor_to_interval(at(9, 18), 180) == at(9, 18)
    assert floor_to_interval(at(15, 29, 30), 180) == at(15, 27)


def test_bar_aggregator_builds_complete_three_minute_candle():
    agg = BarAggregator(180, 60)
    assert agg.add(bar(at(9, 15), 100, 110, 95, 105)) == []
    assert agg.add(bar(at(9, 16), 105, 120, 104, 118)) == []
    out = agg.add(bar(at(9, 17), 118, 119, 90, 92, volume=None))
    assert len(out) == 1
    c = out[0]
    assert (c.start, c.open, c.high, c.low, c.close) == (at(9, 15), 100, 120, 90, 92)
    assert c.end == at(9, 18)
    assert c.complete and c.range == 30 and c.body == -8 and c.abs_body == 8


def test_bar_aggregator_marks_missing_bars_incomplete():
    agg = BarAggregator(180, 60)
    agg.add(bar(at(9, 15), 100, 101, 99, 100))
    out = agg.add(bar(at(9, 18), 100, 101, 99, 100))  # 09:16 and 09:17 missing
    assert len(out) == 1 and not out[0].complete and "MISSING_BARS" in out[0].flags


def test_bar_aggregator_rejects_incompatible_granularity():
    with pytest.raises(ValueError):
        BarAggregator(180, 120)


def test_tick_builder_closes_on_boundary_and_on_timer():
    b = TickCandleBuilder(180, grace_seconds=1)
    b.start_observing(at(9, 14, 50))
    assert b.on_tick(at(9, 15, 1), 100) == []
    b.on_tick(at(9, 16), 130)
    b.on_tick(at(9, 17, 59), 90)
    closed = b.on_tick(at(9, 18, 0), 95)
    assert len(closed) == 1 and closed[0].complete
    assert (closed[0].open, closed[0].high, closed[0].low, closed[0].close) == (100, 130, 90, 90)
    assert b.flush_until(at(9, 21, 0)) == []  # grace not yet elapsed
    flushed = b.flush_until(at(9, 21, 1))
    assert len(flushed) == 1 and flushed[0].start == at(9, 18)


def test_tick_builder_partial_observation_is_incomplete():
    b = TickCandleBuilder(180)
    first = at(9, 16)  # started observing mid-candle
    b.on_tick(first, 100)
    out = b.on_tick(at(9, 18), 101)
    assert not out[0].complete and "PARTIAL_OBSERVATION" in out[0].flags
    out2 = b.on_tick(at(9, 21), 102)
    assert out2[0].complete


def test_tick_builder_gap_marks_candle_incomplete_and_ignores_late_ticks():
    b = TickCandleBuilder(60)
    b.on_tick(at(9, 15), 100)
    b.on_tick(at(9, 16), 100)
    b.mark_gap("FEED_DISCONNECT")
    out = b.on_tick(at(9, 17, 5), 101)
    assert not out[0].complete and "FEED_DISCONNECT" in out[0].flags
    assert b.on_tick(at(9, 15, 30), 99) == []
    assert b.late_ticks == 1


def test_candle_volume_from_cumulative_volume():
    b = TickCandleBuilder(60)
    b.start_observing(at(9, 14))
    b.on_tick(at(9, 15, 1), 100, 1000)
    b.on_tick(at(9, 15, 30), 100, 1500)
    out = b.on_tick(at(9, 16), 100, 1600)
    assert out[0].volume == 500
    assert out[0].end == at(9, 15) + timedelta(minutes=1)


def test_replay_schedule_keeps_ticks_fresh_and_ordered():
    from app.market_data.feeds import replay_schedule

    s = replay_schedule(60)
    offsets = [o for o, _ in s]
    assert offsets[0] == 0 and max(b - a for a, b in zip(offsets, offsets[1:])) <= 5  # never stale vs a 10s threshold
    assert [i for _, i in s][0] == 0 and [i for _, i in s][-1] == 3  # starts at open, ends at close
    assert replay_schedule(1) == [(0.0, 3)]
