"""Trade plans from the Family Hub: Amo's "Trade plan" tasks, Trader's part of docs/TRADING_WORKFLOW.md (section 4).

- Only active tasks count: hub.tasks(assignee="trader", status="open,accepted,in_progress"). Done and declined
  tasks are history, never work.
- Each plan is worked from its own task["form"]["items"]. The newest (highest id) wins. Any older active plan is
  finished with a result saying it was replaced.
- The plan is armed for the next session and held in_progress. After the close, the result is posted in the
  form the task asks for (task["expects_form"]), one item per setup.
- Paper only. Nothing is remembered between events except the pending plans in the journal: the hub is asked again
  every time something arrives.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time as dtime
from typing import Any, Callable

from . import risk, strat, strategy
from .config import settings
from .journal import NY, now_iso
from .session import is_crypto, next_session

log = logging.getLogger(__name__)

TITLE = "trade plan"
ACTIVE = "open,accepted,in_progress"
PENDING = "hubplans:pending"          # kv: {task id: record}, the plans still waiting for their close
SETTLE_AFTER = dtime(16, 5)           # results go out a few minutes after the close
TF_OF = {"monthly": "M", "weekly": "W", "daily": "D", "4h": "4H"}
MAX_BASIS = 0.03                      # Amo's price must be within 3% of Hyperliquid's, or it's not the same market


def is_trade_plan(task: dict[str, Any]) -> bool:
    return (task.get("title") or "").strip().lower().startswith(TITLE)


SECTION = re.compile(r"---\s*From Amo's result\s*---?", re.I)
KEYS = re.compile(r"^\s*(symbol|ticker|timeframe|pattern|direction|trigger|stop|targets?|invalidation|continuity|"
                  r"confidence|why)\s*[:=]\s*(.+?)\s*$", re.I | re.M)


def setups_from_text(details: str) -> list[dict[str, Any]]:
    """Setups written as key: value lines (symbol, timeframe, pattern, direction, trigger, stop, targets, why), one
    block per setup, a new block starting at each 'symbol:' line. Anything else is not guessed at: the plan is blocked."""
    body = SECTION.split(details, maxsplit=1)[-1]
    blocks: list[dict[str, str]] = []
    for key, value in KEYS.findall(body):
        k = key.lower().rstrip("s") if key.lower() == "targets" else key.lower()
        if k in ("symbol", "ticker") or not blocks:
            blocks.append({})
        blocks[-1][k] = value
    out = []
    for b in blocks:
        try:
            targets = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", b.get("target", ""))]
            item = {"symbol": b.get("symbol", b.get("ticker", "")), "timeframe": b.get("timeframe", "").lower(),
                    "pattern": b.get("pattern", ""), "direction": b.get("direction", "").lower(),
                    "trigger": float(b["trigger"]), "stop": float(b["stop"]), "targets": targets,
                    "why": b.get("why", ""), "continuity": b.get("continuity", "")}
        except (KeyError, ValueError):
            continue  # a block without a number for trigger or stop is not a setup
        if item["symbol"] and item["timeframe"] in TF_OF and item["direction"] in ("long", "short") and targets:
            out.append(item)
    return out


class TradePlans:
    def __init__(self, engine: Any, hub: Any, known: Callable[[], dict[str, Any]]):
        self.engine, self.hub, self.known = engine, hub, known
        self.j = engine.j

    # ------------------------------------------------------------------ the record in the journal
    def _load(self) -> dict[str, Any]:
        raw = self.j.get(PENDING)
        return json.loads(raw) if raw else {}

    def _save(self, pending: dict[str, Any]) -> None:
        self.j.set(PENDING, json.dumps(pending))

    # ------------------------------------------------------------------ asked every time something arrives
    def sync(self) -> None:
        active = [t for t in self.hub.tasks(assignee="trader", status=ACTIVE) or [] if is_trade_plan(t)]
        if not active:
            return
        active.sort(key=lambda t: -int(t["id"]))
        newest, older = active[0], active[1:]
        for old in older:
            self.replace(old, newest["id"])
        if str(newest["id"]) not in self._load():
            self.start(newest)

    def replace(self, task: dict[str, Any], newer_id: Any) -> None:
        pending = self._load()
        record = pending.pop(str(task["id"]), None)
        for row in (record or {}).get("rows", []):
            if row.get("plan_id"):
                self.j.update("plans", row["plan_id"], {"status": "invalid", "note": f"replaced by plan task {newer_id}",
                                                        "updated_at": now_iso()})
        self._save(pending)
        note = f"Replaced by Trade plan #{newer_id}. Not traded."
        self._post(task, [], note)

    def start(self, task: dict[str, Any]) -> None:
        pid = str(task["id"])
        items = (task.get("form") or {}).get("items") or []
        if not items:  # no form yet: the setups are in the task's text (after "--- From Amo's result")
            items = setups_from_text(task.get("details") or "")
        if not items:  # never "no setups" without checking both places: the plan can't be read, so say that
            self.hub.update_task(task["id"], "blocked", "I couldn't read the setups in this plan (no form, and the text "
                                                          "isn't in the key: value format). Amo needs to resend it.")
            return
        rows = [self.plan_item(item, task) for item in items[:5]]
        self.hub.update_task(task["id"], "in_progress", f"Holding {len(rows)} setup(s) for the next session.")
        pending = self._load()
        pending[pid] = {"task": {"id": task["id"], "channel": task.get("channel"), "expects_form": task.get("expects_form"),
                                 "title": task.get("title")},
                        "rows": rows, "closes": self.next_close().isoformat()}
        self._save(pending)

    # ------------------------------------------------------------------ one setup from the plan
    def plan_item(self, item: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        symbol = str(item.get("symbol") or "").strip().upper()
        row: dict[str, Any] = {"symbol": symbol, "pattern": item.get("pattern"), "direction": item.get("direction"),
                               "plan_id": None, "outcome": None, "reason": None}
        if self.engine.broker.live:
            return {**row, "outcome": "BLOCKED", "reason": "live trading from hub plans isn't switched on (paper only)"}
        inst = self.known().get(symbol)
        if inst is None or inst.collateral not in settings.collateral:
            return {**row, "outcome": "NOT AVAILABLE", "reason": f"{symbol} isn't tradable on Hyperliquid"}
        try:
            tf = TF_OF[str(item.get("timeframe") or "").lower()]
            trigger, stop = float(item["trigger"]), float(item["stop"])
            targets = [float(x) for x in item.get("targets") or []]
        except (KeyError, TypeError, ValueError):
            return {**row, "outcome": "BLOCKED", "reason": "the setup is missing a timeframe, trigger, stop or target"}
        direction = str(item.get("direction") or "").lower()
        pattern = str(item.get("pattern") or "")
        setup = strat.Setup(tf=tf, name=pattern, direction=direction, trigger=trigger,
                            target=targets[0] if targets else None, stop=stop, status="waiting",
                            with_continuity=True, inside="inside" in pattern.lower() or pattern.startswith("1-"),
                            family=strat.family_of(pattern))
        if not strat.in_focus(setup, crypto=is_crypto(symbol)):
            return {**row, "outcome": "PASSED", "reason": "no_setup_in_window: outside Trader's focus (daily inside bars, "
                                                          "4-hour 3-1 setups, BTC daily)"}
        if inst.mark <= 0 or abs(inst.mark - trigger) / trigger > MAX_BASIS:
            return {**row, "outcome": "NOT AVAILABLE", "reason": f"Hyperliquid's price ({inst.mark:g}) is far from the plan's "
                                                                 f"trigger ({trigger:g}): not the same market"}
        lv = risk.levels(direction, trigger, stop, setup.target, inst.max_leverage)
        if lv.problem:
            reason = strategy.MARKET_REASONS.get(lv.problem)
            if reason:
                return {**row, "outcome": "PASSED", "reason": f"{lv.problem}: {reason}"}
            return {**row, "outcome": "BLOCKED", "reason": lv.problem}
        cand = strategy.Candidate(inst, setup, lv, "A", "stop", 0.0, "hub", 1.0, str(item.get("why") or ""),
                                  str(item.get("continuity") or ""))
        pid = self.engine.make_plan(cand, self.engine.clock())
        self.j.update("plans", pid, {"task_id": task["id"], "expires_at": self.next_close().isoformat()})
        return {**row, "plan_id": pid, "outcome": "PENDING"}

    def next_close(self) -> datetime:
        return datetime.combine(next_session(self.engine.clock()), dtime(16, 0), NY)

    # ------------------------------------------------------------------ after the close
    def settle(self) -> None:
        """Post each plan's result once its session has closed. Results go in the task's own form."""
        now = self.engine.clock().astimezone(NY)
        pending = self._load()
        for pid, record in list(pending.items()):
            if now < datetime.fromisoformat(record["closes"]) or now.time() < SETTLE_AFTER:
                continue
            rows = [self.outcome(row) for row in record["rows"]]
            pnl = round(sum(r.get("pnl_usd") or 0 for r in rows), 2)
            taken = sum(1 for r in rows if r["outcome"] == "TAKEN")
            note = f"{len(rows)} setup(s): {taken} taken, P&L {pnl:+.2f} USD after fees (paper)."
            self._post(record["task"], rows, note)
            del pending[pid]
            self._save(pending)

    def outcome(self, row: dict[str, Any]) -> dict[str, Any]:
        if row["outcome"] != "PENDING":
            return {k: row.get(k) for k in ("symbol", "outcome", "reason", "mode")} | {"mode": "paper"}
        trade = self.j.one("SELECT * FROM trades WHERE plan_id = ?", (row["plan_id"],))
        if trade:
            return {"symbol": row["symbol"], "outcome": "TAKEN", "reason": None, "entry": trade["entry"],
                    "exit": trade["exit"], "pnl_usd": trade["pnl"], "fees_usd": trade["fees"],
                    "r_multiple": trade["r_multiple"], "mode": trade["mode"]}
        plan = self.j.one("SELECT status, note FROM plans WHERE id = ?", (row["plan_id"],)) or {}
        if plan.get("status") == "invalid":
            return {"symbol": row["symbol"], "outcome": "BLOCKED", "reason": plan.get("note"), "mode": "paper"}
        return {"symbol": row["symbol"], "outcome": "NOT TRIGGERED", "reason": None, "mode": "paper"}

    # ------------------------------------------------------------------ posting in the task's format
    def _post(self, task: dict[str, Any], items: list[dict[str, Any]], note: str) -> None:
        pending = self._load()
        pending.pop(str(task["id"]), None)
        self._save(pending)
        channel = task.get("channel") or "tasks"
        form_name = task.get("expects_form")
        if form_name:  # the hub's own form for this task: its shape, its name
            self.hub.post_result(task["id"], summary=note, channel=channel,
                                 form={"name": form_name, "items": items, "note": note})
        else:  # no form asked for: a plain line per setup
            lines = [f"{r.get('symbol')}: {r.get('outcome')}" + (f" ({r['reason']})" if r.get("reason") else "")
                     for r in items]
            self.hub.post_result(task["id"], summary="\n".join(lines + [note]), channel=channel)
        log.info("trade plan task %s posted (%s)", task["id"], note)
