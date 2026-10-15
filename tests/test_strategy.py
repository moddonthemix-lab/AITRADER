from datetime import date

import pytest
from conftest import DAILY_INSIDE, bars, failing_source, inst, source_for

from trader import strategy

WED = date(2026, 10, 14)

# Split timeframes: weekly below its open, daily above — no continuity, bars inside/outside (chop)
CHOP_DAILY = bars([(100, 110, 90, 100), (100, 112, 88, 101), (101, 111, 89, 100), (100, 108, 92, 101),
                   (101, 107, 93, 102)])
WEEKLY_DOWN = bars([(120, 125, 95, 100), (100, 115, 85, 105), (105, 116, 84, 103), (110, 114, 86, 102)], step=7 * 86400)


def test_inside_bar_with_continuity_is_an_a_grade_candidate(journal):
    sc = strategy.scan([inst()], journal, WED, 0, chart_source=source_for(DAILY_INSIDE), perp_source=failing_source)
    best = sc.ranked[0]
    assert best.grade == "A" and best.setup.inside and best.setup.direction == "long"
    assert best.entry_type == "stop" and best.levels.entry == pytest.approx(110)
    assert sc.tradeable and sc.pass_summary() is None


def test_inside_bars_rank_first(journal):
    from trader import risk, strat

    a = strat.analyze("NVDA", source=source_for(DAILY_INSIDE))
    s_inside = next(s for s in a["_setups"] if s.inside and s.with_continuity)
    s_other = strat.Setup("D", "2U-2U continuation", "long", 110, 130, 104, "waiting", True, inside=False,
                          family="2-2 continuation")
    lv_hi = risk.levels("long", 110, 104, 130)  # better R:R, but not an inside bar
    lv_lo = risk.levels("long", 110, 106, 120)
    c1 = strategy.Candidate(inst(), s_other, lv_hi, "A", "stop", 0.1, "chart", 1, "", "bullish")
    c2 = strategy.Candidate(inst(), s_inside, lv_lo, "A", "stop", 0.1, "chart", 1, "", "bullish")
    assert sorted([c1, c2], key=lambda c: c.key, reverse=True)[0] is c2


def test_chop_is_a_market_reason_to_pass(journal):
    sc = strategy.scan([inst(mark=102, max_leverage=1)], journal, WED, 0,
                       chart_source=source_for(CHOP_DAILY, weekly=WEEKLY_DOWN),
                       perp_source=failing_source)
    assert not sc.tradeable
    code, detail = sc.pass_summary()
    assert code in ("chop", "conflicting_mtf")
    strategy.validate_pass(code, detail)


def test_a_data_feed_failure_is_an_incident_never_a_pass(journal):
    sc = strategy.scan([inst()], journal, WED, 0, chart_source=failing_source, perp_source=failing_source)
    assert sc.incidents and "couldn't read xyz:NVDA" in sc.incidents[0]
    assert sc.pass_summary() is None  # nothing could be read → no pass is recorded at all


def test_pass_discipline_rejects_data_reasons():
    with pytest.raises(strategy.NotAMarketReason):
        strategy.validate_pass("data_feed", "Yahoo was down")
    with pytest.raises(strategy.NotAMarketReason):
        strategy.validate_pass("no_setup_in_window", "the data feed timed out")
    strategy.validate_pass("conflicting_mtf", "weekly down, daily up")


def test_falls_back_to_the_perps_own_candles(journal):
    sc = strategy.scan([inst()], journal, WED, 0, chart_source=failing_source, perp_source=source_for(DAILY_INSIDE))
    assert sc.reads[0].source == "hyperliquid" and sc.ranked


def test_a_chart_that_doesnt_match_the_perp_is_not_used(journal):
    sc = strategy.scan([inst(mark=150)], journal, WED, 0, chart_source=source_for(DAILY_INSIDE),
                       perp_source=failing_source)
    assert sc.incidents and "doesn't match" in sc.incidents[0]


def test_b_grades_only_when_behind_pace(journal):
    sc = strategy.scan([], journal, date(2026, 10, 12), 0)  # Monday
    assert sc.allowed_grades == ("A",)
    sc = strategy.scan([], journal, WED, 0)
    assert sc.allowed_grades == ("A", "B")
