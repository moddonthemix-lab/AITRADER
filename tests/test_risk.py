from datetime import date

import pytest

from trader import risk
from trader.config import settings


def trade(**kw):
    t = {"side": "long", "entry": 100.0, "stop": 96.0, "initial_stop": 96.0, "hard_stop": risk.hard_stop_price(100, "long"),
         "target": 110.0, "peak": 100.0, "trail_active": 0}
    t.update(kw)
    return t


def test_hard_stop_is_ten_percent_of_the_price_with_fees():
    entry = 100.0
    stop = risk.hard_stop_price(entry, "long")
    loss = -((stop - entry) / entry - 2 * risk.fee())
    assert loss == pytest.approx(settings.hard_stop_pct / 100)
    assert risk.hard_stop_price(entry, "short") > entry


def test_hard_stop_fires_on_the_real_loss_even_past_the_stop():
    st = risk.manage(trade(stop=80.0), 89.0)  # a stop set too wide must still not lose more than 10%
    assert st.exit_reason == "hard_stop"


def test_take_profit_at_target():
    assert risk.manage(trade(), 110.0).exit_reason == "target"


def test_plain_stop():
    st = risk.manage(trade(), 95.9)
    assert st.exit_reason == "stop" and not st.trail_active


def test_trailing_stop_locks_a_profit_after_fees_and_never_moves_back():
    t = trade(target=150.0)
    st = risk.manage(t, 106.5)  # +1.625R: trailing switches on (1.5R)
    assert st.trail_active and st.moved
    assert st.stop > 100 * (1 + 2 * risk.fee())  # stopped out from here = still a profit after both fees
    t.update(stop=st.stop, peak=st.peak, trail_active=1)
    higher = risk.manage(t, 112.0)
    assert higher.stop > st.stop
    t.update(stop=higher.stop, peak=higher.peak)
    lower = risk.manage(t, 109.0)
    assert lower.stop == higher.stop  # never moves back
    out = risk.manage(t, higher.stop - 0.01)
    assert out.exit_reason == "trail"
    net = (higher.stop - 100) - risk.fee() * (100 + higher.stop)
    assert net > 0


def test_short_trades_mirror():
    t = trade(side="short", entry=100.0, stop=104.0, initial_stop=104.0, hard_stop=risk.hard_stop_price(100, "short"),
              target=90.0)
    assert risk.manage(t, 90.0).exit_reason == "target"
    assert risk.manage(t, 104.1).exit_reason == "stop"


def test_levels_reject_stops_wider_than_the_hard_stop_and_thin_targets():
    assert risk.levels("long", 100, 85, 130).problem == "stop_too_wide"
    assert risk.levels("long", 100, 96, 101).problem == "rr_after_fees"
    ok = risk.levels("long", 100, 96, 110)
    assert ok.problem is None and ok.rr >= settings.min_rr and ok.target_kind == "strat"
    measured = risk.levels("long", 100, 96, None)
    assert measured.target == 108 and measured.target_kind == "measured"


def test_position_size_uses_75_percent_of_the_account():
    capital = 1000 * settings.account_use_pct / 100
    size = risk.size_for(1000, 100, 96, 3, leverage=20)
    risk_usd = size * (4 + risk.fee() * 196)
    assert risk_usd == pytest.approx(capital * settings.risk_per_trade_pct / 100, rel=0.01)  # leverage doesn't change risk
    tight = risk.size_for(1000, 100, 99.9, 3, leverage=1)  # a very tight stop at 1x: all 75% as margin, no more
    assert tight * 100 == pytest.approx(capital, rel=0.01)
    assert risk.size_for(1000, 100, 99.9, 3, leverage=1, margin_in_use=capital) == 0  # 75% already in use
    half = risk.size_for(1000, 100, 99.9, 3, leverage=1, margin_in_use=capital / 2)
    assert half * 100 == pytest.approx(capital / 2, rel=0.01)
    assert risk.size_for(20, 100, 96, 3) == 0  # below Hyperliquid's $10 minimum


def test_max_leverage_but_liquidation_always_beyond_the_stop():
    # 2% stop on a 20x market: 20x liquidates ~2.5% away — not 2x beyond the stop+fees (4.4%), so lowered to 14x
    lev = risk.leverage_for(100, 98, 20)
    assert lev == 14
    assert risk.liquidation_distance(lev, 20) >= settings.liq_buffer * (0.02 + 2 * risk.fee())
    assert risk.liquidation_distance(20, 20) < settings.liq_buffer * (0.02 + 2 * risk.fee())
    # a 1% stop on the same market can use the full 20x (liquidation 2.5% away vs 2x 1.18%)
    assert risk.leverage_for(100, 99, 20) == 20
    assert risk.liquidation_distance(20, 20) >= settings.liq_buffer * (0.002 + 2 * risk.fee())
    # a wide stop falls back toward 1x, never below
    assert risk.leverage_for(100, 91, 50) >= 1
    # a fixed number caps it
    old = settings.leverage
    settings.leverage = 3
    try:
        assert risk.leverage_for(100, 99.8, 20) == 3
    finally:
        settings.leverage = old


def test_weekly_pace():
    mon, wed, fri = date(2026, 10, 12), date(2026, 10, 14), date(2026, 10, 16)
    assert not risk.behind_pace(0, mon)
    assert risk.behind_pace(0, wed) and not risk.behind_pace(1, wed)
    assert risk.behind_pace(1, fri) and not risk.behind_pace(2, fri)
