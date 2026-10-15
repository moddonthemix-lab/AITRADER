import os
import tempfile

os.environ["TRADER_ENV_FILE"] = "/nonexistent"
os.environ["TRADER_ENTRY_CONFIRM"] = "touch"  # most tests move the price; test_entries.py covers the 5-minute close
_tmp = tempfile.mkdtemp(prefix="trader-test-")
os.environ["TRADER_HOME"] = _tmp
os.environ["TRADER_STRAT_DOCS_DIR"] = os.path.join(_tmp, "docs")  # never the boss's real folder
for _k in ("ANTHROPIC_API_KEY", "HUB_TOKEN", "HUB_URL", "HL_ACCOUNT_ADDRESS", "HL_AGENT_PRIVATE_KEY", "HL_MASTER_ADDRESS",
           "TRADER_MODE", "TRADER_WATCHLIST", "TRADER_LEVERAGE", "TRADER_FEE_PCT"):
    os.environ.pop(_k, None)  # tests never reach the cloud, the hub or a real wallet

from datetime import datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from trader.config import reload  # noqa: E402
from trader.hyperliquid import Fill, Instrument, Broker  # noqa: E402
from trader.journal import NY, Journal  # noqa: E402
from trader.market import Bar  # noqa: E402

reload()


def bars(rows, start=1_700_000_000, step=86400, vol=1000.0):
    return [Bar(start + i * step, *r, v=vol) for i, r in enumerate(rows)]


# A daily 2U-1 (inside bar) with full continuity up: a 2U-1-2U long that triggers above 110, stop 106,
# target 120 (the 2U bar's high). Price 108.5.
DAILY_INSIDE = bars([(90, 96, 88, 95), (95, 101, 94, 100), (100, 120, 99, 118), (109, 110, 106, 108), (108, 109, 107, 108.5)])
WEEKLY_UP = bars([(80, 90, 78, 88), (88, 100, 85, 98), (98, 121, 95, 110), (105, 112, 104, 108.5)], step=7 * 86400)
MONTHLY_UP = bars([(60, 70, 58, 68), (68, 80, 65, 78), (78, 121, 75, 100), (90, 112, 88, 108.5)], step=30 * 86400)


def source_for(daily, weekly=WEEKLY_UP, monthly=MONTHLY_UP):
    data = {"D": daily, "W": weekly, "M": monthly}

    def src(symbol, tf):
        if tf not in data:
            raise ValueError("no such timeframe in this test")
        return list(data[tf]), {"shortName": symbol, "gmtoffset": 0}
    return src


def failing_source(symbol, tf):
    from trader.market import DataFeedError

    raise DataFeedError("feed down")


def inst(ticker="NVDA", mark=108.5, coin=None, max_leverage=20):
    return Instrument(coin=coin or f"xyz:{ticker}", dex="xyz", ticker=ticker, chart=ticker, sz_decimals=3,
                      max_leverage=max_leverage, only_isolated=False, day_volume=1e8, mark=mark)


class FakeBrain:
    def __init__(self, text="Lesson: waited for the inside bar to break with continuity; keep doing that."):
        self.calls = []
        self.text = text

    def think(self, prompt, job="research", local_only=False, web=False, system_prompt=""):
        self.calls.append({"prompt": prompt, "job": job, "local_only": local_only, "web": web})
        return SimpleNamespace(text=self.text, brain="local" if local_only else "cloud", refused=False, reason="",
                               sources=[])


class FakeLiveBroker(Broker):
    live = True

    def __init__(self, prices):
        self.prices = prices
        self.orders, self.stops, self.cancels = [], [], []
        self.sizes = {}

    def mid(self, coin):
        return self.prices[coin]

    def equity(self, inst=None):
        return 1000.0

    def open(self, i, is_long, size, leverage=1):
        self.orders.append(("open", i.coin, is_long, size))
        self.leverages = getattr(self, "leverages", []) + [leverage]
        self.sizes[i.coin] = size
        return Fill(self.prices[i.coin], size, 0.05, "1")

    def close(self, i, is_long, size):
        self.orders.append(("close", i.coin, is_long, size))
        self.sizes[i.coin] = 0
        return Fill(self.prices[i.coin], size, 0.05, "2")

    def protect(self, i, is_long, size, stop, target):
        self.stops.append(stop)
        return {"sl": "10", "tp": "11"}

    def move_stop(self, i, is_long, size, stop, ids):
        self.stops.append(stop)
        return {**ids, "sl": str(len(self.stops) + 10)}

    def cancel_protection(self, i, ids):
        self.cancels.append(ids)

    def position_size(self, i):
        return self.sizes.get(i.coin, 0)


class FakeHub:
    def __init__(self):
        self.brain = None
        self.items, self.read, self.posts, self.updates, self.results, self.findings = [], [], [], [], [], []
        self.tasks_requested, self.pnl = [], []
        self.threads = {}
        self.last_event = None
        self.next_id = 1000
        self.fail = {}

    def _id(self):
        self.next_id += 1
        return self.next_id

    def _maybe_fail(self, name):
        if name in self.fail:
            raise self.fail[name]

    def me(self):
        return {"member": {"id": "trader"}, "last_event": 1}

    def memories(self):
        return [{"text": "Trader trades paper until the boss says otherwise.", "local_only": False},
                {"text": "The safe code is 1234.", "local_only": True}]

    def channels(self):
        return [{"name": "trades", "local_only": False}, {"name": "home", "local_only": True}]

    def tasks(self, assignee=None, status=None):
        return []

    def inbox(self, unread=True):
        return [i for i in self.items if i["id"] not in self.read][::-1]

    def mark_read(self, ids=None):
        self.read += ids

    def outbox_size(self):
        return 0

    def thread(self, mid):
        return self.threads.get(mid, {"root": None, "paused": False})

    def post(self, channel, text, reply_to=None, files=None):
        self._maybe_fail("post")
        self.posts.append({"channel": channel, "text": text, "reply_to": reply_to, "brain": self.brain})
        return {"id": self._id()}

    def post_finding(self, channel, claim, status, confidence, sources=None, reply_to=None, files=None):
        self.findings.append({"channel": channel, "claim": claim, "status": status, "reply_to": reply_to})
        return {"id": self._id()}

    def update_task(self, task_id, status, note=""):
        self._maybe_fail("update_task")
        self.updates.append((task_id, status, note))
        return {"id": self._id()}

    def post_result(self, task_id, summary, artifacts=None, files=None, channel="tasks"):
        self.results.append({"task": task_id, "summary": summary, "brain": self.brain, "channel": channel})
        return {"id": self._id()}

    def request_task(self, channel, title, details, assignee, priority="normal", **kw):
        self._maybe_fail("request_task")
        tid = self._id()
        self.tasks_requested.append({"id": tid, "channel": channel, "title": title, "details": details,
                                     "assignee": assignee})
        return {"id": self._id(), "task": {"id": tid, "status": "proposed"}}

    def report_pnl(self, pnl_usd, mode="paper", equity_usd=None, trades=0, note="", day=None):
        self.pnl.append({"pnl": pnl_usd, "mode": mode, "equity": equity_usd, "trades": trades, "day": day})
        return {"ok": True}


@pytest.fixture()
def journal():
    return Journal(":memory:")


@pytest.fixture()
def wednesday():
    return datetime(2026, 10, 14, 10, 0, tzinfo=NY)  # a Wednesday, inside the trading window
