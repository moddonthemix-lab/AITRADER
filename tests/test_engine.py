import json
from datetime import timedelta

import pytest
from conftest import DAILY_INSIDE, FakeBrain, FakeHub, FakeLiveBroker, failing_source, inst, source_for

from trader.engine import Engine
from trader.hyperliquid import PaperBroker


def paper(journal, prices, now, hub=None, brain=None):
    clock = {"now": now}
    e = Engine(journal, PaperBroker(journal, price_fn=lambda c: prices[c]), hub=hub, brain=brain or FakeBrain(),
               instruments=lambda: [inst()], chart_source=source_for(DAILY_INSIDE), perp_source=failing_source,
               clock=lambda: clock["now"])
    return e, clock


def test_paper_trade_from_plan_to_trailing_profit(journal, wednesday):
    prices = {"xyz:NVDA": 108.5}
    e, clock = paper(journal, prices, wednesday)
    e.scan(wednesday)
    plan = journal.one("SELECT * FROM plans")
    assert plan["status"] == "armed" and json.loads(plan["data"])["entry"] == pytest.approx(110)

    e.watch_plans(wednesday)
    assert not journal.open_trades()  # not triggered yet
    prices["xyz:NVDA"] = 110.1
    e.watch_plans(wednesday)
    t = journal.open_trades()[0]
    assert t["side"] == "long" and t["stop"] == 106 and t["target"] == 120 and t["fees"] > 0

    prices["xyz:NVDA"] = 117.0  # > 1.5R in profit: the trailing stop switches on
    e.manage_positions()
    t = journal.trade(t["id"])
    assert t["trail_active"] == 1 and t["stop"] > t["entry"]
    prices["xyz:NVDA"] = t["stop"] - 0.05
    e.manage_positions()
    done = journal.trade(t["id"])
    assert done["exit_reason"] == "trail" and done["pnl"] > 0  # once in profit, it closes in profit
    assert journal.all("SELECT * FROM lessons")


def test_take_profit_at_target(journal, wednesday):
    prices = {"xyz:NVDA": 108.5}
    e, _ = paper(journal, prices, wednesday)
    e.scan(wednesday)
    prices["xyz:NVDA"] = 110.05
    e.watch_plans(wednesday)
    prices["xyz:NVDA"] = 120.2
    e.manage_positions()
    t = journal.all("SELECT * FROM trades")[0]
    assert t["exit_reason"] == "target" and t["pnl"] > 0
    text, pnl_day, n, _ = e.report_text(wednesday.date())
    assert n == 1 and pnl_day == t["pnl"] and "CLOSED long xyz:NVDA" in text  # opened + closed today = one trade


def test_plan_cancelled_when_the_stop_side_breaks_first(journal, wednesday):
    prices = {"xyz:NVDA": 108.5}
    e, _ = paper(journal, prices, wednesday)
    e.scan(wednesday)
    prices["xyz:NVDA"] = 105.9
    e.watch_plans(wednesday)
    assert journal.one("SELECT status FROM plans")["status"] == "invalid" and not journal.open_trades()


def test_plans_expire_when_the_bar_closes(journal, wednesday):
    prices = {"xyz:NVDA": 108.5}
    e, _ = paper(journal, prices, wednesday)
    e.scan(wednesday)
    e.expire_plans(wednesday.replace(hour=16, minute=1))
    assert journal.one("SELECT status FROM plans")["status"] == "expired"


def test_no_chasing_past_the_trigger(journal, wednesday):
    prices = {"xyz:NVDA": 108.5}
    e, _ = paper(journal, prices, wednesday)
    e.scan(wednesday)
    prices["xyz:NVDA"] = 112.0  # ran 2 points past a 4-point-risk trigger
    e.watch_plans(wednesday)
    assert journal.one("SELECT status FROM plans")["status"] == "invalid" and not journal.open_trades()


def test_live_plans_wait_for_the_boss(journal, wednesday):
    from conftest import FakeHub

    hub = FakeHub()
    prices = {"xyz:NVDA": 108.5}
    broker = FakeLiveBroker(prices)
    e = Engine(journal, broker, hub=hub, brain=FakeBrain(), instruments=lambda: [inst()],
               chart_source=source_for(DAILY_INSIDE), perp_source=failing_source, clock=lambda: wednesday)
    e.scan(wednesday)
    plan = journal.one("SELECT * FROM plans")
    assert plan["status"] == "waiting_approval"
    req = hub.tasks_requested[0]
    assert req["title"].startswith(f"[plan {plan['id']}] LIVE long xyz:NVDA") and req["assignee"] == "trader"
    assert req["channel"] == "trades" and "10%" in req["details"]

    prices["xyz:NVDA"] = 110.1
    e.watch_plans(wednesday)
    assert not broker.orders  # nothing happens without the boss

    assert e.on_approved({"id": req["id"], "title": req["title"]})
    e.watch_plans(wednesday)
    assert broker.orders[0][:3] == ("open", "xyz:NVDA", True) and broker.stops  # stop placed on the exchange
    assert any(u[1] == "in_progress" and "Filled" in u[2] for u in hub.updates)


def test_live_position_closed_by_the_exchange_stop_is_recorded(journal, wednesday):
    hub = FakeHub()
    prices = {"xyz:NVDA": 108.5}
    broker = FakeLiveBroker(prices)
    e = Engine(journal, broker, hub=hub, brain=FakeBrain(), instruments=lambda: [inst()],
               chart_source=source_for(DAILY_INSIDE), perp_source=failing_source, clock=lambda: wednesday)
    e.scan(wednesday)
    e.on_approved({"id": hub.tasks_requested[0]["id"], "title": hub.tasks_requested[0]["title"]})
    prices["xyz:NVDA"] = 110.1
    e.watch_plans(wednesday)
    broker.sizes["xyz:NVDA"] = 0  # the exchange's stop order fired while we weren't looking
    prices["xyz:NVDA"] = 105.8
    e.manage_positions()
    t = journal.all("SELECT * FROM trades")[0]
    assert t["exit_reason"] == "stop" and t["pnl"] < 0 and "exchange" in t["notes"]


def test_approval_after_expiry_is_refused(journal, wednesday):
    hub = FakeHub()
    clock = {"now": wednesday}
    e = Engine(journal, FakeLiveBroker({"xyz:NVDA": 108.5}), hub=hub, brain=FakeBrain(), instruments=lambda: [inst()],
               chart_source=source_for(DAILY_INSIDE), perp_source=failing_source, clock=lambda: clock["now"])
    e.scan(wednesday)
    clock["now"] = wednesday + timedelta(hours=7)
    req = hub.tasks_requested[0]
    e.on_approved({"id": req["id"], "title": req["title"]})
    assert journal.one("SELECT status FROM plans")["status"] == "expired"
    assert hub.updates[-1][1] == "declined"


def test_daily_report_says_why_no_trade_and_keeps_incidents_separate(journal, wednesday):
    from conftest import WEEKLY_UP, bars

    hub = FakeHub()
    chop = bars([(100, 110, 90, 100), (100, 112, 88, 101), (101, 111, 89, 100), (100, 108, 92, 101), (101, 107, 93, 102)])
    weekly_down = bars([(120, 125, 95, 100), (100, 115, 85, 105), (105, 116, 84, 103), (110, 114, 86, 102)],
                       step=7 * 86400)
    e = Engine(journal, PaperBroker(journal, price_fn=lambda c: 102), hub=hub, brain=FakeBrain(),
               instruments=lambda: [inst(mark=102), inst("TSLA", coin="xyz:TSLA")],
               chart_source=lambda s, tf: (source_for(chop, weekly=weekly_down) if s == "NVDA" else failing_source)(s, tf),
               perp_source=failing_source, clock=lambda: wednesday)
    e.scan(wednesday)
    e.report(wednesday.date(), task_id=77)
    text = hub.results[0]["summary"]
    assert "No trade today, because:" in text and "PAPER" in text
    assert "Incidents (not reasons to pass" in text and "xyz:TSLA" in text
    assert hub.pnl[0]["mode"] == "paper" and hub.pnl[0]["day"] == "2026-10-14"
    assert hub.results[0]["channel"] == "trades"
    assert WEEKLY_UP  # the shared fixtures stay importable


def test_report_falls_back_to_a_finding_when_no_routine(journal, wednesday):
    hub = FakeHub()
    e, _ = paper(journal, {"xyz:NVDA": 108.5}, wednesday, hub=hub)
    e.maybe_report(wednesday.replace(hour=16, minute=55))
    assert hub.findings and hub.findings[0]["channel"] == "trades" and hub.pnl
    e.maybe_report(wednesday.replace(hour=17, minute=10))
    assert len(hub.findings) == 1  # once a day


def test_max_open_positions_respected(journal, wednesday):
    from trader.config import settings

    prices = {"xyz:NVDA": 108.5}
    e, _ = paper(journal, prices, wednesday)
    old = settings.max_open
    settings.max_open = 0
    try:
        e.scan(wednesday)
        assert not journal.all("SELECT * FROM plans")
    finally:
        settings.max_open = old


def test_report_is_honest_when_trader_never_scanned(journal, wednesday):
    e, _ = paper(journal, {"xyz:NVDA": 108.5}, wednesday)
    text = e.report_text(wednesday.date())[0]
    assert "I didn't scan during today's window" in text and "because" not in text
