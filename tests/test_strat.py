from conftest import DAILY_INSIDE, bars, source_for

from trader import strat


def test_bar_numbers_follow_the_break_not_the_colour():
    prev = bars([(10, 12, 8, 11)])[0]
    assert strat.scenario(prev, bars([(11, 11.5, 9, 9.5)])[0]) == "1"
    assert strat.scenario(prev, bars([(11, 13, 9, 9)])[0]) == "2U"  # a red 2U is still a 2U
    assert strat.scenario(prev, bars([(11, 11, 7, 10)])[0]) == "2D"
    assert strat.scenario(prev, bars([(11, 13, 7, 10)])[0]) == "3"


def test_inside_bar_setup_has_trigger_stop_target_and_volume():
    types = strat.label(DAILY_INSIDE)
    setups = strat.find_setups("D", DAILY_INSIDE, types)
    long_ = next(s for s in setups if s.direction == "long")
    assert long_.name == "2U-1-2U continuation"
    assert (long_.trigger, long_.stop, long_.target, long_.status) == (110, 106, 120, "waiting")
    assert long_.inside and long_.family == "2-1-2 continuation"
    assert long_.rvol == 1.0


def test_full_timeframe_continuity_and_aligned_setups():
    a = strat.analyze("NVDA", source=source_for(DAILY_INSIDE))
    assert a["continuity"]["ftfc"] == "up" and a["bias"] == "bullish"
    aligned = [s for s in a["_setups"] if s.with_continuity]
    assert aligned and all(s.direction == "long" for s in aligned)
    assert "FULL TIMEFRAME CONTINUITY UP" in strat.thesis(a)


def test_family_names_drop_the_direction():
    assert strat.family_of("3-1-2U + hammer") == "3-1-2"
    assert strat.family_of("2D-1-2U reversal") == "2-1-2 reversal"
    assert strat.family_of("1-1-2D (compound inside)") == "1-1-2"
