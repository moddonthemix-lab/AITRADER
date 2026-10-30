"""Entries (a 5-minute close beyond the Strat trigger, from 9:40), finished bars outside the session, the game
plan before the open / after the close, and the boss's documents having a say."""

from datetime import date, datetime, time as dtime, timedelta, timezone

import pytest
from conftest import DAILY_INSIDE, FakeBrain, FakeHub, failing_source, inst, source_for

from trader import prep, session, strat
from trader.config import settings
from trader.engine import Engine, entries_open
from trader.hyperliquid import PaperBroker
from trader.journal import NY
from trader.market import Bar


def sessions_ending(last: date, rows):
    """Daily bars stamped at 9:30 New York on consecutive weekdays, the last one on `last`."""
    days, d = [], last
    while len(days) < len(rows):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    return [Bar(int(datetime.combine(day, dtime(9, 30), NY).timestamp()), *r, v=1000.0) for day, r in zip(days, rows)]


# Thu Oct 15 2026 was an inside day inside Wed's 2U bar: a 2U-1 → long above 110, short below 106.
THU = date(2026, 10, 15)
DAILY = sessions_ending(THU, [(90, 96, 88, 95), (95, 101, 94, 100), (100, 120, 99, 118), (109, 110, 106, 108)])
def mondays(last_monday: date, rows):
    """Weekly bars, each stamped at its Monday's open."""
    weeks = [last_monday - timedelta(weeks=len(rows) - 1 - i) for i in range(len(rows))]
    return [Bar(int(datetime.combine(w, dtime(9, 30), NY).timestamp()), *r, v=5000.0) for w, r in zip(weeks, rows)]


WEEKLY = mondays(date(2026, 10, 12), [(80, 90, 78, 88), (88, 100, 85, 98), (98, 121, 95, 110), (100, 120, 99, 108)])
MONTHLY = [Bar(int(datetime.combine(date(2026, m, 1), dtime(9, 30), NY).timestamp()), *r, v=9000.0)
           for m, r in zip((7, 8, 9, 10), [(60, 70, 58, 68), (68, 80, 65, 78), (78, 121, 75, 100), (90, 120, 88, 108)])]


def recent_source(symbol, tf):
    data = {"D": DAILY, "W": WEEKLY, "M": MONTHLY}
    if tf not in data:
        raise ValueError("not in this test")
    return list(data[tf]), {"shortName": symbol}


# ------------------------------------------------------------------ reading finished bars
def test_after_the_close_todays_bar_is_the_setup_bar():
    after_close = datetime(2026, 10, 15, 16, 30, tzinfo=NY)
    a = strat.analyze("NVDA", source=session.session_source(recent_source, after_close))
    d = a["_frames"]["D"]
    assert d.waiting and d.prev_type == "1"  # Thursday's inside bar, tomorrow not open yet
    longs = [s for s in a["_setups"] if s.tf == "D" and s.direction == "long"]
    assert longs[0].name == "2U-1-2U continuation" and longs[0].trigger == 110 and longs[0].status == "waiting"
    assert not a["_frames"]["W"].waiting  # mid-week: the weekly bar is still live
    # Without the session reading, Thursday would wrongly be the "live" bar and setups built on Wednesday
    raw = strat.analyze("NVDA", source=recent_source)
    assert not any(s.trigger == 110 and s.inside for s in raw["_setups"] if s.tf == "D")


def test_before_the_open_a_placeholder_for_today_is_dropped():
    pre = datetime(2026, 10, 16, 9, 0, tzinfo=NY)  # Friday before the bell
    placeholder = Bar(int(datetime.combine(date(2026, 10, 16), dtime(9, 30), NY).timestamp()), 108, 108, 108, 108)
    out = session.prepare(DAILY + [placeholder], "D", pre)
    assert out[-1].synthetic and out[-2].c == 108 and len(out) == len(DAILY) + 1


def test_the_week_is_finished_from_fridays_close():
    fri = datetime(2026, 10, 16, 16, 30, tzinfo=NY)
    wed = datetime(2026, 10, 14, 16, 30, tzinfo=NY)
    weekly = WEEKLY  # the latest weekly bar is this week (from Monday Oct 12)
    assert session.prepare(weekly, "W", fri)[-1].synthetic
    assert not session.prepare(weekly, "W", wed)[-1].synthetic


def test_old_data_is_left_alone():
    assert session.prepare(DAILY_INSIDE, "D", datetime(2026, 10, 15, 20, 0, tzinfo=NY)) == DAILY_INSIDE


# ------------------------------------------------------------------ the game plan
def test_game_plan_lists_tomorrows_inside_bars_with_both_triggers():
    plan = prep.build([inst()], datetime(2026, 10, 15, 16, 10, tzinfo=NY), recent_source)
    assert plan["session"] == "2026-10-16" and plan["kind"] == "after the close"
    row = next(r for r in plan["inside"] if r["tf"] == "D")
    assert row["sides"]["long"]["trigger"] == 110 and row["sides"]["short"]["trigger"] == 106
    assert row["favoured"] == "long"  # the higher timeframes point up
    text = prep.text(plan)
    assert "Game plan for Fri Oct 16" in text and "long above 110" in text and "← favoured" in text


def test_game_plan_runs_once_after_the_close_and_once_before_the_open(journal):
    hub = FakeHub()
    e = Engine(journal, PaperBroker(journal, price_fn=lambda c: 108.0), hub=hub, brain=FakeBrain(),
               instruments=lambda: [inst()], chart_source=recent_source, perp_source=failing_source)
    thu_1610 = datetime(2026, 10, 15, 16, 10, tzinfo=NY)
    assert prep.due(datetime(2026, 10, 15, 15, 0, tzinfo=NY), journal) is None  # market open
    assert prep.due(thu_1610, journal) == "after the close"
    e.clock = lambda: thu_1610
    e.step()
    assert prep.due(thu_1610 + timedelta(minutes=5), journal) is None  # done for tonight
    assert prep.due(datetime(2026, 10, 16, 9, 0, tzinfo=NY), journal) == "before the open"
    assert prep.due(datetime(2026, 10, 17, 9, 0, tzinfo=NY), journal) is None  # Saturday
    assert hub.findings and "Game plan" in hub.findings[0]["claim"]
    assert prep.latest(journal)["inside"]


# ------------------------------------------------------------------ the 5-minute confirmation
@pytest.fixture()
def five_min(monkeypatch):
    monkeypatch.setattr(settings, "entry_confirm", "5m")


def engine_with(journal, wednesday, prices, candle):
    clock = {"now": wednesday}
    e = Engine(journal, PaperBroker(journal, price_fn=lambda c: prices[c]), brain=FakeBrain(), instruments=lambda: [inst()],
               chart_source=source_for(DAILY_INSIDE), perp_source=failing_source, clock=lambda: clock["now"],
               confirm_source=lambda coin, now: candle["bar"])
    return e, clock


def five(now, close):
    start = now.astimezone(NY) - timedelta(minutes=5)
    return Bar(int(start.timestamp()), close, close, close, close)


def test_a_touch_is_not_an_entry_a_5_minute_close_is(journal, wednesday, five_min, monkeypatch):
    from trader import engine as engine_module
    monkeypatch.setattr(engine_module, "now_iso", lambda: wednesday.astimezone(timezone.utc).isoformat(timespec="seconds"))
    prices, candle = {"xyz:NVDA": 108.5}, {"bar": None}
    e, _ = engine_with(journal, wednesday, prices, candle)
    e.scan(wednesday)
    prices["xyz:NVDA"] = 110.3                   # price pokes through the trigger…
    candle["bar"] = five(wednesday, 109.8)       # …but the 5-minute candle closed back below it
    e.watch_plans(wednesday)
    assert not journal.open_trades()
    candle["bar"] = five(wednesday, 110.4)       # a 5-minute close above 110
    e.watch_plans(wednesday)
    assert journal.open_trades() and journal.open_trades()[0]["side"] == "long"


def test_yesterdays_candle_doesnt_confirm_today(journal, wednesday, five_min):
    prices, candle = {"xyz:NVDA": 110.3}, {"bar": five(wednesday - timedelta(days=1), 111)}
    e, _ = engine_with(journal, wednesday, prices, candle)
    e.scan(wednesday)
    e.watch_plans(wednesday)
    assert not journal.open_trades()


def test_no_entries_before_930_or_after_1545(monkeypatch):
    monkeypatch.setattr(settings, "around_the_clock", False)  # the session window (the default is around the clock)
    assert not entries_open(datetime(2026, 10, 14, 9, 25, tzinfo=NY))
    assert entries_open(datetime(2026, 10, 14, 9, 35, tzinfo=NY))
    assert entries_open(datetime(2026, 10, 14, 15, 45, tzinfo=NY))
    assert not entries_open(datetime(2026, 10, 14, 15, 50, tzinfo=NY))


def test_scanning_starts_at_the_open_but_entries_wait(journal, five_min):
    at_932 = datetime(2026, 10, 14, 9, 32, tzinfo=NY)
    prices, candle = {"xyz:NVDA": 110.3}, {"bar": five(at_932, 110.4)}
    e, _ = engine_with(journal, at_932, prices, candle)
    e.step()
    assert journal.one("SELECT status FROM plans")["status"] == "armed" and not journal.open_trades()


# ------------------------------------------------------------------ the boss's documents
def test_the_documents_can_rule_a_setup_out(journal, wednesday, tmp_path, monkeypatch):
    from trader import knowledge

    (tmp_path / "my rules.md").write_text("My rule: never take a 2-1-2 continuation on the daily on a Wednesday.")
    monkeypatch.setattr(knowledge, "_library", knowledge.Library([knowledge.BUILTIN, tmp_path]))
    brain = FakeBrain(text="SKIP\n\"never take a 2-1-2 continuation on the daily on a Wednesday\"")
    e = Engine(journal, PaperBroker(journal, price_fn=lambda c: 108.5), brain=brain, instruments=lambda: [inst()],
               chart_source=source_for(DAILY_INSIDE), perp_source=failing_source, clock=lambda: wednesday)
    e.scan(wednesday)
    assert not journal.all("SELECT * FROM plans")
    p = journal.passes_on(wednesday.date())[0]
    assert p["reason"] == "playbook" and "Wednesday" in p["detail"] and "my rules.md" in brain.calls[0]["prompt"]
    e.scan(wednesday)
    assert len(brain.calls) == 1  # asked once per setup per day


def test_documents_that_dont_object_let_the_rules_decide(journal, wednesday, tmp_path, monkeypatch):
    from trader import knowledge

    (tmp_path / "notes.md").write_text("Inside bars are my favourite setup. Wait for the break.")
    monkeypatch.setattr(knowledge, "_library", knowledge.Library([knowledge.BUILTIN, tmp_path]))
    e = Engine(journal, PaperBroker(journal, price_fn=lambda c: 108.5), brain=FakeBrain(text="TAKE"),
               instruments=lambda: [inst()], chart_source=source_for(DAILY_INSIDE), perp_source=failing_source,
               clock=lambda: wednesday)
    e.scan(wednesday)
    assert journal.one("SELECT status FROM plans")["status"] == "armed"


def test_trades_trigger_overnight_and_premarket_when_around_the_clock(journal, wednesday, five_min, monkeypatch):
    """Around the clock: a plan armed in the session enters on a 5-minute close through the trigger at 2 AM and 7 AM."""
    from trader import engine as engine_module
    monkeypatch.setattr(settings, "around_the_clock", True)
    monkeypatch.setattr(engine_module, "now_iso", lambda: wednesday.astimezone(timezone.utc).isoformat(timespec="seconds"))
    prices, candle = {"xyz:NVDA": 108.5}, {"bar": None}
    e, clock = engine_with(journal, wednesday, prices, candle)
    e.scan(wednesday)
    for hour in (2, 7):  # 2 AM, then premarket
        night = wednesday.replace(hour=hour, minute=0) + timedelta(days=1)
        clock["now"] = night
        prices["xyz:NVDA"] = 110.3
        candle["bar"] = five(night, 110.4)
        e.watch_plans(night)
        if journal.open_trades():
            break
    assert journal.open_trades() and journal.open_trades()[0]["side"] == "long"
