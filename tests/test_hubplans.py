"""Trade plans from the hub: only active tasks, newest wins, older ones replaced, results after the close."""

from datetime import datetime, timezone
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


def plan(task_id, status="open", items=None, title="Trade plan"):
    return {"id": task_id, "title": title, "status": status, "channel": "trades", "expects_form": "trade_report",
            "form": {"name": "trade_setup", "items": items or []}}


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


def test_setup_not_on_hyperliquid_is_reported_not_traded(journal):
    now = datetime(2026, 10, 28, 20, 0, tzinfo=NY)
    hub = FakeHub([plan(11, items=[setup_item("ZZZZ")])])
    tp = hubplans.TradePlans(engine_for(journal, now), hub, lambda: {})
    tp.sync()
    assert hub.results == []  # held until the close
    later = datetime(2026, 10, 29, 16, 10, tzinfo=NY)
    tp.engine.clock = lambda: later
    tp.settle()
    assert len(hub.results) == 1
    form = hub.results[0]["form"]
    assert form["name"] == "trade_report" and form["items"][0]["outcome"] == "NOT AVAILABLE"
    assert form["items"][0]["symbol"] == "ZZZZ"
