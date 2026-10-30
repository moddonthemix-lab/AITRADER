"""Trade plans from the hub: only active tasks, newest wins, older ones replaced, results after the close."""

from datetime import datetime, timezone
import json
from types import SimpleNamespace

from trader import hubplans
from trader.journal import NY


class FakeHub:
    def __init__(self, tasks):
        self._tasks = tasks
        self.updates, self.results = [], []

    def tasks(self, assignee=None, status=None):
        wanted = set((status or "").split(","))
        assert assignee == "trader"
        return [t for t in self._tasks if t["status"] in wanted]

    def update_task(self, task_id, status, note=""):
        self.updates.append((task_id, status, note))

    def post_result(self, task_id, summary="", channel="tasks", form=None, **_):
        self.results.append({"task": task_id, "summary": summary, "channel": channel, "form": form})


RESULT_AFTER = datetime(2026, 10, 29, 18, 30, tzinfo=NY).timestamp()  # the hub's time for this plan (from the task)


def plan(task_id, status="open", items=None, title="Trade plan"):
    return {"id": task_id, "title": title, "status": status, "channel": "trades", "expects_form": "trade_report",
            "form": {"name": "trade_setup", "items": items or []}, "result_after": RESULT_AFTER}


def engine_for(journal, now):
    engine = SimpleNamespace(j=journal, broker=SimpleNamespace(live=False), clock=lambda: now)
    engine.make_plan = lambda c, when: 1
    return engine


def setup_item(symbol="NOPE1"):
    return {"symbol": symbol, "timeframe": "daily", "pattern": "2-1-2 (inside bar)", "direction": "long",
            "trigger": 10.0, "stop": 9.0, "targets": [12.0], "why": "test"}


def test_only_active_trade_plans_count(journal):
    now = datetime(2026, 10, 28, 20, 0, tzinfo=NY)
    hub = FakeHub([plan(5, status="done", items=[setup_item()]), plan(6, status="declined", items=[setup_item()])])
    tp = hubplans.TradePlans(engine_for(journal, now), hub, lambda: {})
    tp.sync()
    assert hub.updates == [] and hub.results == []


def test_newest_plan_is_worked_and_older_ones_are_replaced(journal):
    now = datetime(2026, 10, 28, 20, 0, tzinfo=NY)
    hub = FakeHub([plan(7, items=[setup_item()]), plan(9, items=[setup_item()])])
    tp = hubplans.TradePlans(engine_for(journal, now), hub, lambda: {})
    tp.sync()
    assert [r["task"] for r in hub.results] == [7]  # the older one is finished, saying it was replaced
    assert "Replaced by Trade plan #9" in hub.results[0]["summary"]
    assert hub.results[0]["form"] == {"name": "trade_report", "items": [], "note": hub.results[0]["summary"]}
    assert hub.updates[-1][0] == 9 and hub.updates[-1][1] == "in_progress"  # the newest is held in progress


def test_setup_not_on_hyperliquid_is_reported_not_traded(journal, monkeypatch):
    now = datetime(2026, 10, 28, 20, 0, tzinfo=NY)
    hub = FakeHub([plan(11, items=[setup_item("ZZZZ")])])
    tp = hubplans.TradePlans(engine_for(journal, now), hub, lambda: {})
    tp.sync()
    assert hub.results == []  # held until the close
    later = datetime(2026, 10, 29, 18, 35, tzinfo=NY)  # after the hub's result_after
    monkeypatch.setattr(hubplans.time, "time", lambda: later.timestamp())
    tp.settle()
    assert len(hub.results) == 1
    form = hub.results[0]["form"]
    assert form["name"] == "trade_report" and form["items"][0]["outcome"] == "NOT AVAILABLE"
    assert form["items"][0]["symbol"] == "ZZZZ"


def test_setups_are_read_from_the_text_when_there_is_no_form():
    text = ("Amo's plan\n--- From Amo's result ---\n"
            "symbol: AAPL\ntimeframe: daily\npattern: 2-1-2 (inside bar)\ndirection: long\n"
            "trigger: 182.40\nstop: 179.80\ntargets: 185.10, 187.50\nwhy: inside bar\n\n"
            "symbol: NVDA\ntimeframe: 4h\npattern: 3-1-2\ndirection: short\ntrigger: 900\nstop: 910\ntargets: 880\n")
    items = hubplans.setups_from_text(text)
    assert [i["symbol"] for i in items] == ["AAPL", "NVDA"]
    assert items[0]["targets"] == [185.10, 187.50] and items[1]["direction"] == "short"


def test_unreadable_text_blocks_the_plan_instead_of_saying_no_setups(journal):
    now = datetime(2026, 10, 28, 20, 0, tzinfo=NY)
    task = plan(21, items=[])
    task["details"] = "Amo's plan\n--- From Amo's result ---\nLooks good to me, trade the inside bars."
    hub = FakeHub([task])
    tp = hubplans.TradePlans(engine_for(journal, now), hub, lambda: {})
    tp.sync()
    assert hub.results == []
    assert hub.updates[-1][1] == "blocked" and "couldn't read the setups" in hub.updates[-1][2]


def test_no_result_time_from_the_hub_means_no_guessing(journal):
    now = datetime(2026, 10, 28, 20, 0, tzinfo=NY)
    task = plan(31, items=[setup_item("ZZZZ")])
    task.pop("result_after")
    hub = FakeHub([task])
    hubplans.TradePlans(engine_for(journal, now), hub, lambda: {}).sync()
    assert hub.updates[-1][1] == "blocked" and "result time" in hub.updates[-1][2]


def test_a_409_from_the_hub_is_kept_and_not_retried_in_a_loop(journal, monkeypatch):
    class Refused(Exception):
        status, message = 409, "accepted from Friday 6:30 PM"

    now = datetime(2026, 10, 28, 20, 0, tzinfo=NY)
    hub = FakeHub([plan(41, items=[setup_item("ZZZZ")])])
    tp = hubplans.TradePlans(engine_for(journal, now), hub, lambda: {})
    tp.sync()
    monkeypatch.setattr(hubplans.time, "time", lambda: now.timestamp() + 10 ** 6)
    hub.post_result = lambda *a, **k: (_ for _ in ()).throw(Refused())
    tp.settle()
    tp.settle()  # the next minute: still refused, nothing re-sent
    record = json.loads(journal.get(hubplans.PENDING))["41"]
    assert record["next_try"] > now.timestamp() + 10 ** 6  # waiting to be checked again, not dropped


class HubWithTasks(FakeHub):
    def __init__(self, tasks, by_id):
        super().__init__(tasks)
        self.by_id, self.asked, self.posts = by_id, [], 0

    def task(self, task_id):
        return self.by_id.get(task_id, {})

    def ask(self, channel, text, to=None, reply_to=None):
        self.asked.append(text)


def test_an_old_record_takes_its_result_time_from_the_hub(journal, monkeypatch):
    now = datetime(2026, 10, 28, 20, 0, tzinfo=NY)
    tp = hubplans.TradePlans(engine_for(journal, now), HubWithTasks([], {}), lambda: {})
    old = {"51": {"task": {"id": 51, "channel": "trades", "expects_form": "trade_report"}, "rows": [], "closes": "x"}}
    journal.set(hubplans.PENDING, json.dumps(old))
    tp.hub.by_id[51] = {"id": 51, "status": "in_progress", "result_after": RESULT_AFTER, "expects_form": "trade_report"}
    monkeypatch.setattr(hubplans.time, "time", lambda: datetime(2026, 10, 29, 18, 35, tzinfo=NY).timestamp())
    tp.settle()
    assert tp.hub.results and tp.hub.results[0]["task"] == 51


def test_an_old_record_the_hub_marks_done_is_dropped_quietly(journal, monkeypatch):
    now = datetime(2026, 10, 28, 20, 0, tzinfo=NY)
    hub = HubWithTasks([], {52: {"id": 52, "status": "done"}})
    tp = hubplans.TradePlans(engine_for(journal, now), hub, lambda: {})
    journal.set(hubplans.PENDING, json.dumps({"52": {"task": {"id": 52}, "rows": [], "closes": "x"}}))
    tp.settle()
    assert hub.results == [] and json.loads(journal.get(hubplans.PENDING)) == {}


def test_a_409_waits_and_is_not_sent_again_straight_away(journal, monkeypatch):
    class TooEarly(Exception):
        status, message = 409, "accepted from Friday 6:30 PM"

    now = datetime(2026, 10, 29, 18, 35, tzinfo=NY)
    monkeypatch.setattr(hubplans.time, "time", lambda: now.timestamp())
    hub = HubWithTasks([], {})
    calls = {"n": 0}

    def refuse(*a, **k):
        calls["n"] += 1
        raise TooEarly()
    hub.post_result = refuse
    tp = hubplans.TradePlans(engine_for(journal, now), hub, lambda: {})
    journal.set(hubplans.PENDING, json.dumps({"53": {"task": {"id": 53}, "rows": [], "result_after": RESULT_AFTER}}))
    tp.settle()
    tp.settle()  # the very next loop: waiting, not sending again
    assert calls["n"] == 1 and json.loads(journal.get(hubplans.PENDING))["53"]["next_try"] > now.timestamp()


def test_three_other_errors_end_in_a_question_to_the_boss(journal, monkeypatch):
    now = datetime(2026, 10, 29, 18, 35, tzinfo=NY)
    monkeypatch.setattr(hubplans.time, "time", lambda: now.timestamp() + 10 ** 6)
    hub = HubWithTasks([], {})

    def broken(*a, **k):
        raise OSError("the hub is down")
    hub.post_result = broken
    tp = hubplans.TradePlans(engine_for(journal, now), hub, lambda: {})
    journal.set(hubplans.PENDING, json.dumps({"54": {"task": {"id": 54}, "rows": [], "result_after": RESULT_AFTER}}))
    for _ in range(3):
        tp.settle()
        rec = json.loads(journal.get(hubplans.PENDING))["54"]
        rec["next_try"] = 0  # the next check comes around (each one is 10 minutes apart in real use)
        journal.set(hubplans.PENDING, json.dumps({"54": rec}))
    assert "OSError" in json.loads(journal.get(hubplans.PENDING))["54"]["failed"]
    assert len(hub.asked) == 1 and "54" in hub.asked[0]
