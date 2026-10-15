"""The Strat rules as thestrat.ai/docs states them (each test quotes the page it checks)."""

import pytest
from conftest import bars

from trader import risk, strat
from trader.config import settings
from trader.market import Bar


def setups_for(rows, live=True):
    bs = bars(rows)
    if not live:
        last = bs[-1]
        bs[-1] = Bar(last.t, last.o, last.h, last.l, last.c, synthetic=True)
    return strat.find_setups("D", bs, strat.label(bs))


def test_hammer_and_shooter_definitions():
    # docs/hammer: "a large lower wick whose body closes in the top 33% of its range"
    assert strat.candle_shape(bars([(9.8, 10, 7, 9.9)])[0]) == "hammer"
    assert strat.candle_shape(bars([(8.9, 10, 7, 9.9)])[0]) is None  # body reaches below the top third
    # docs/shooter: "a large upper wick that opens and closes in the bottom 33% of its range"
    assert strat.candle_shape(bars([(7.2, 10, 7, 7.1)])[0]) == "shooter"


def test_1_2_2_rev_strat_targets_the_motherbar():
    # docs/1-2-2: "Trigger: the failed-break bar's extreme. Magnitude: the candle before the inside bar."
    rows = [(100, 101, 99, 100), (100, 110, 95, 105),   # the motherbar: high 110
            (104, 106, 98, 102),                         # 1 (inside the motherbar)
            (101, 103, 96, 102),                         # 2D: breaks the inside bar's low (the failed break)
            (102, 102.5, 97, 102)]                       # live bar, not triggered yet
    long_ = next(s for s in setups_for(rows) if s.direction == "long")
    assert long_.name.startswith("1-2D-2U Rev Strat")
    assert long_.trigger == 103 and long_.target == 110  # the motherbar's high, not the inside bar's 106


def test_2_2_reversal_stop_is_the_entry_bars_low():
    # docs/2-2-reversal: "Stop: one penny beyond the low (bullish) or high (bearish) of the entry bar."
    rows = [(100, 101, 99, 100), (100, 104, 98, 103), (103, 105, 101, 104),
            (104, 104.5, 100, 101),                      # 2D
            (101, 103, 100.5, 102)]                      # live (entry) bar: low 100.5
    long_ = next(s for s in setups_for(rows) if s.direction == "long")
    assert long_.trigger == 104.5 and long_.stop == pytest.approx(100.49) and long_.entry_bar_stop
    assert long_.target == 105  # "Magnitude: the bar before the pattern"


def test_inside_bar_stop_stays_the_other_side_of_the_1():
    # docs/3-1-2: "Stop: the other side of the 1"
    rows = [(100, 101, 99, 100), (100, 103, 97, 101),   # 3
            (101, 102, 98, 99),                          # 1
            (99, 101, 98.5, 100.5)]                      # live
    long_ = next(s for s in setups_for(rows) if s.direction == "long")
    assert long_.name == "3-1-2U" and long_.stop == 98 and long_.target == 103 and not long_.entry_bar_stop


def test_2_1_2_continuation_targets_the_measured_move():
    # docs/measured-move: "the distance of the previous move, projected from the trigger"
    rows = [(80, 82, 78.7, 81), (81, 88, 80, 87), (87, 96.68, 86, 95),   # leg: 78.70 → 96.68 (+17.98)
            (95, 96, 92.5, 93.5),                                         # 1 (the pause)
            (93.5, 94, 93, 93.8)]                                         # live
    cont = next(s for s in setups_for(rows) if s.name == "2U-1-2U continuation")
    assert cont.target == pytest.approx(96 + (96.68 - 78.7), abs=0.01)


def test_trailing_starts_once_in_profit_after_fees_and_closes_green():
    t = {"side": "long", "entry": 100.0, "stop": 97.0, "initial_stop": 97.0,
         "hard_stop": risk.hard_stop_price(100, "long"), "target": 120.0, "peak": 100.0, "trail_active": 0}
    just = 100 * (1 + risk.activation_gain())
    st = risk.manage(t, just + 0.01)
    assert st.trail_active and st.stop > 100 * (1 + 2 * risk.fee())  # locked: a stop-out is green after fees
    t.update(stop=st.stop, peak=st.peak, trail_active=1)
    out = risk.manage(t, st.stop - 0.01)
    assert out.exit_reason == "trail"
    pnl = (st.stop - 100) - risk.fee() * (100 + st.stop)
    assert pnl > 0
    assert risk.activation_gain() < 0.01  # well before 1R on a 3-point stop
    assert not risk.manage({**t, "peak": 100.0, "trail_active": 0, "stop": 97.0}, 100.3).trail_active  # fees not covered yet
    assert settings.trail_activate_r == 0


def test_saved_pages_keep_the_pages_own_words(tmp_path, monkeypatch):
    from trader import knowledge

    html = ("<html><head><title>The Hammer - TheStrat</title></head><body><nav>menu</nav><article>"
            "<h1>The Hammer: Trapped Shorts, Tighter Stop</h1><p>A hammer is a bar with a large lower wick whose body "
            "closes in the top 33% of its range.</p><p>Sellers pushed price down and got completely rejected: an "
            "actionable signal of potentially trapped shorts. Its edge is a tighter stop.</p><p>Reading this the whole way through? We send new docs. "
            "Required .</p><p>Stop: one penny under the entry bar’s low.</p><p>Get the next one by email "
            "footer stuff</p></article></body></html>")
    monkeypatch.setattr(knowledge, "fetch_html", lambda url: html + " " * 600)
    p = knowledge.save_page("https://thestrat.ai/docs/hammer/", tmp_path)
    text = p.read_text()
    assert p.name == "hammer.md" and p.parent.name == "thestrat.ai"
    assert "Source: https://thestrat.ai/docs/hammer/" in text
    assert "A hammer is a bar with a large lower wick whose body closes in the top 33% of its range." in text
    assert "Stop: one penny under the entry bar’s low." in text
    assert "menu" not in text and "Reading this" not in text and "footer" not in text
    with pytest.raises(ValueError):
        knowledge.save_page("file:///etc/passwd", tmp_path)
