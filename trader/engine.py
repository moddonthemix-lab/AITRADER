"""Trader at work: scan → plan → (approval when live) → enter on the trigger → manage → exit → learn → report.

Every minute (all day, every day — the perps trade 24/7):
    manage open positions: take profit at the target, trail the stop once in profit, hard stop at -10%
During the US cash session (weekdays, TRADER_WINDOW_START–END, New York time):
    every TRADER_SCAN_MINUTES: scan the markets; plan the best setup(s); when nothing qualifies, record the
    MARKET reason for passing; data problems are recorded as incidents
    armed plans enter when price breaks the trigger (and are cancelled if the stop side breaks first, or
    when the setup's bar closes)
Live money: each plan is first sent to the boss as a task in #trades ("[plan 12] LIVE long …"). Only the
boss's 👍 arms it. Paper plans arm right away.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable

from . import hyperliquid as hl
from . import learning, prep, risk, strat, strategy
from .brain import Brain, system
from .config import settings
from .hyperliquid import Broker, Instrument
from .journal import NY, Journal, now_iso, ny_now, week_start
from .knowledge import library
from .market import Bar, DataFeedError
from .session import session_source

log = logging.getLogger(__name__)

PLAN_RE = re.compile(r"\[plan (\d+)\]")
UNIVERSE_TTL = 3600


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def in_window(now: datetime) -> bool:
    """Scanning hours: from the open (9:30) to the last entry time (15:45), weekdays."""
    now = now.astimezone(NY)
    return now.weekday() < 5 and _hm(settings.scan_start) <= now.time() <= _hm(settings.window_end)


def entries_open(now: datetime) -> bool:
    """Entry hours: 9:40 (the second 5-minute candle has closed) to 15:45, weekdays."""
    now = now.astimezone(NY)
    return now.weekday() < 5 and _hm(settings.window_start) <= now.time() <= _hm(settings.window_end)


def bar_close(tf: str, now: datetime) -> datetime:
    """When the bar the trigger lives in closes (New York time): the plan expires then."""
    now = now.astimezone(NY)
    close = datetime.combine(now.date(), dtime(16, 0), NY)
    if tf == "4H":
        mid = datetime.combine(now.date(), dtime(13, 30), NY)
        return mid if now < mid else close
    if tf == "W":
        return close + timedelta(days=max(0, 4 - now.weekday()))
    return close


def inst_from(row: dict[str, Any]) -> Instrument:
    return Instrument(coin=row["coin"], dex=row.get("dex") or row["coin"].split(":")[0], ticker=row.get("ticker") or "",
                      chart=row.get("chart") or "", sz_decimals=int(row.get("sz_decimals") or 2),
                      max_leverage=int(row.get("max_leverage") or 1), only_isolated=bool(row.get("only_isolated")),
                      day_volume=0, mark=0, collateral=row.get("collateral") or "USDC")


class Engine:
    def __init__(self, journal: Journal, broker: Broker, hub: Any = None, brain: Brain | None = None,
                 instruments: Callable[[], list[Instrument]] | None = None,
                 chart_source: Callable | None = None, perp_source: Callable | None = None,
                 clock: Callable[[], datetime] = ny_now, confirm_source: Callable | None = None):
        self.j = journal
        self.broker = broker
        self.hub = hub
        self.brain = brain or Brain(hub)
        self._instruments = instruments or hl.universe
        self.chart_source = chart_source
        self.perp_source = perp_source
        self.clock = clock
        # (coin, now) -> the last finished 5-minute candle of the perp (the entry confirmation)
        self.confirm_source = confirm_source or (lambda coin, now: hl.last_closed_candle(
            coin, int(now.timestamp() * 1000)))
        self.memories: list[dict[str, Any]] = []
        self._universe: tuple[float, list[Instrument]] = (0.0, [])
        self.last_scan: strategy.Scan | None = None
        self.lock = threading.RLock()  # the trading loop and the dashboard never touch a position at once

    def stamp(self) -> str:
        """Now, by Trader's clock, as the journal stores it (UTC)."""
        return self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")

    @property
    def mode(self) -> str:
        return "live" if self.broker.live else "paper"

    # ------------------------------------------------------------------ the loop
    def universe(self) -> list[Instrument]:
        at, cached = self._universe
        if cached and time.time() - at < UNIVERSE_TTL:
            return cached
        fresh = self._instruments()
        if fresh:
            self._universe = (time.time(), fresh)
        return fresh or cached

    def step(self) -> None:
        """One minute of work."""
        with self.lock:
            self._step()

    def _step(self) -> None:
        now = self.clock()
        self.manage_positions()
        self.expire_plans(now)
        kind = prep.due(now, self.j)
        if kind:
            self.game_plan(now, kind)
        if in_window(now):
            last = self.j.get("last_scan_at")
            if not last or (now - datetime.fromisoformat(last)).total_seconds() >= settings.scan_minutes * 60 - 5:
                self.scan(now)
                self.j.set("last_scan_at", now.isoformat())
            if entries_open(now):
                self.watch_plans(now)
        self.maybe_report(now)

    # ------------------------------------------------------------------ the game plan (before the open / after the close)
    def game_plan(self, now: datetime | None = None, kind: str | None = None) -> dict[str, Any]:
        now = now or self.clock()
        try:
            instruments = self.universe()
        except DataFeedError as e:
            self.incident(now.date(), f"couldn't list Hyperliquid's markets for the game plan: {e}")
            instruments = []
        plan = prep.build(instruments, now, self.chart_source, kind)
        prep.save(self.j, plan)
        for what in plan["problems"][:5]:
            self.incident(now.date(), what)
        if self.hub is not None:
            try:
                self.hub.brain = "local"
                self.hub.post_finding("trades", prep.text(plan), "reported", 1.0)
            except Exception as e:  # noqa: BLE001 — the outbox keeps it when the hub is away
                log.info("hub: couldn't post the game plan (%s)", type(e).__name__)
        return plan

    # ------------------------------------------------------------------ scanning and planning
    def trades_this_week(self, now: datetime | None = None) -> int:
        return self.j.trades_opened_since(week_start((now or self.clock()).date()), mode=self.mode)

    def active_coins(self) -> set[str]:
        coins = {t["coin"] for t in self.j.open_trades()}
        coins |= {p["coin"] for p in self.j.all("SELECT coin FROM plans WHERE status IN ('waiting_approval','armed')")}
        return coins

    def scan(self, now: datetime | None = None) -> strategy.Scan:
        now = now or self.clock()
        today = now.date()
        try:
            instruments = self.universe()
        except DataFeedError as e:
            self.incident(today, f"couldn't list Hyperliquid's markets: {e}")
            instruments = []
        if not instruments:
            self.incident(today, "no markets to scan (Hyperliquid's market list was empty or unreachable)")
            sc = strategy.Scan([], [], ("A",), False)
            self.last_scan = sc
            return sc
        sc = strategy.scan(instruments, self.j, today, self.trades_this_week(now),
                           session_source(self.chart_source, now), self.perp_source)
        self.last_scan = sc
        key = f"scans:{today.isoformat()}"
        self.j.set(key, str(int(self.j.get(key, "0") or 0) + 1))
        for what in sc.incidents:
            self.incident(today, what)
        taken = self.active_coins()
        slots = settings.max_open - len(self.j.open_trades()) - len(
            self.j.all("SELECT id FROM plans WHERE status IN ('waiting_approval','armed')"))
        planned = 0
        for c in sc.tradeable:
            if planned >= slots:
                break
            if c.inst.coin in taken:
                continue
            taken.add(c.inst.coin)
            ok, why = self.check_documents(c, today)
            if not ok:
                self.record_pass(today, "playbook", f"{c.inst.ticker} {strat.TF_NAMES[c.setup.tf]} {c.setup.name}: {why}")
                continue
            self.make_plan(c, now)
            planned += 1
        if not sc.tradeable:
            ps = sc.pass_summary()
            if ps:
                self.record_pass(today, *ps)
        return sc

    def check_documents(self, c: strategy.Candidate, day: date) -> tuple[bool, str]:
        """Does the boss's Strat documents rule this setup out? Only the boss's own files are consulted (the
        built-in playbook IS the code). Once per setup per day; no brain or a vague answer = the rules decide."""
        if not settings.doc_check:
            return True, ""
        lib = library()
        lib.refresh()
        theirs = [p for p in lib.pieces if not str(p["file"]).startswith(("01_THE_STRAT_PLAYBOOK", "02_TRADER_RULES"))]
        if not theirs:
            return True, ""
        key = f"doccheck:{day.isoformat()}:{c.inst.coin}:{c.setup.tf}:{c.setup.name}:{c.setup.trigger}"
        cached = self.j.get(key)
        if cached is not None:
            return cached == "TAKE", self.j.get(key + ":why") or ""
        docs = lib.context(f"{c.setup.name} {c.setup.family} entry {c.setup.direction} inside bar continuity "
                           f"{strat.TF_NAMES[c.setup.tf]} rules", k=4)
        prompt = (f"Check this planned trade against the boss's Strat documents below.\n\nPlan: {c.summary()}\n\n"
                  f"The read:\n{c.thesis[:1500]}\n\nFrom the documents:\n{docs}\n\n"
                  "Reply on the FIRST line with exactly TAKE or SKIP. Say SKIP only if the documents clearly rule "
                  "this setup out right now; then on the second line quote the rule in under 25 words. "
                  "If the documents don't address it, say TAKE.")
        th = self.brain.think(prompt, job="research", system_prompt=system(learning.recent_lessons(self.j),
                                                                            self.memories))
        verdict, why = "TAKE", ""
        if th is not None and th.text and not th.refused and th.brain != "wait":
            lines = [x.strip() for x in th.text.strip().splitlines() if x.strip()]
            if lines and lines[0].upper().startswith("SKIP"):
                verdict = "SKIP"
                why = (lines[1] if len(lines) > 1 else lines[0][4:].strip(" :-—")) or "the documents rule it out"
        self.j.set(key, verdict)
        self.j.set(key + ":why", why[:200])
        return verdict == "TAKE", why[:200]

    def record_pass(self, day: date, reason: str, detail: str) -> None:
        strategy.validate_pass(reason, detail)  # the boss's pass discipline: market reasons only
        existing = self.j.one("SELECT id FROM passes WHERE day = ? AND reason = ? AND coin IS NULL",
                              (day.isoformat(), reason))
        if existing:
            self.j.update("passes", existing["id"], {"detail": detail[:500], "created_at": now_iso()})
        else:
            self.j.record_pass(day, reason, detail)

    def incident(self, day: date, what: str) -> None:
        if not self.j.one("SELECT id FROM incidents WHERE day = ? AND what = ?", (day.isoformat(), what[:500])):
            self.j.record_incident(day, what)
            log.warning("incident: %s", what)

    def make_plan(self, c: strategy.Candidate, now: datetime) -> int:
        s, lv, inst = c.setup, c.levels, c.inst
        data = {"coin": inst.coin, "ticker": inst.ticker, "chart": inst.chart, "dex": inst.dex,
                "sz_decimals": inst.sz_decimals, "max_leverage": inst.max_leverage,
                "only_isolated": int(inst.only_isolated), "collateral": inst.collateral, "side": s.direction, "setup": s.name, "family": s.family,
                "tf": s.tf, "grade": c.grade, "inside": int(s.inside), "rvol": s.rvol,
                "with_continuity": int(bool(s.with_continuity)), "entry": lv.entry, "stop": lv.stop,
                "target": lv.target, "hard_stop": lv.hard_stop, "rr": lv.rr, "target_kind": lv.target_kind,
                "entry_type": c.entry_type, "source": c.source, "summary": c.summary(), "thesis": c.thesis}
        expires = bar_close(s.tf, now)
        ask = self.broker.live and settings.live_approval != "auto"
        status = "waiting_approval" if ask else "armed"
        pid = self.j.insert("plans", {"mode": self.mode, "coin": inst.coin, "side": s.direction,
                                      "data": json.dumps(data), "status": status, "created_at": now_iso(),
                                      "expires_at": expires.isoformat(), "updated_at": now_iso()})
        if ask:
            self.ask_approval(pid, data, expires)
        elif self.broker.live:
            self.announce(pid, data, expires)
        return pid

    def announce(self, pid: int, data: dict[str, Any], expires: datetime) -> None:
        """Auto mode: no approval needed, but every live plan is still on the record in #trades."""
        if self.hub is None:
            return
        try:
            self.hub.brain = "local"
            self.hub.post_finding("trades", f"[plan {pid}] LIVE (auto) {data['summary']} Armed until "
                                            f"{expires.strftime('%a %H:%M')} New York.", "reported", 1.0)
        except Exception as e:  # noqa: BLE001
            log.info("hub: couldn't post plan %s (%s)", pid, type(e).__name__)

    def ask_approval(self, pid: int, data: dict[str, Any], expires: datetime) -> None:
        """Real money: the boss approves every trade (CONNECTING_AN_AGENT.md, Trader's notes)."""
        if self.hub is None:
            self.j.update("plans", pid, {"status": "declined", "note": "no hub: live trades need the boss's 👍",
                                         "updated_at": now_iso()})
            return
        title = f"[plan {pid}] LIVE {data['side']} {data['coin']} — {strat.TF_NAMES[data['tf']]} {data['setup']}"
        details = (f"{data['summary']}\n\nIf you approve (👍) before {expires.strftime('%a %H:%M')} New York time, I'll "
                   f"{'buy' if data['side'] == 'long' else 'sell short'} when price breaks the trigger, with a stop "
                   f"order on Hyperliquid right away, the take-profit at the target, and a trailing stop once in profit. "
                   f"Hard stop: never more than {settings.hard_stop_pct:g}% against the entry price, fees included. "
                   f"Leverage: the market's maximum, lowered if needed so liquidation stays at least "
                   f"{settings.liq_buffer:g}x further than the stop. At the stop I lose about "
                   f"{settings.risk_per_trade_pct:g}% of my trading money ({settings.account_use_pct:g}% of the account; "
                   f"the rest stays free). "
                   f"Not approved by then = no trade.\n\nThe read:\n{data['thesis'][:1500]}")
        try:
            self.hub.brain = "local"
            r = self.hub.request_task("trades", title[:120], details, "trader", priority="high")
            tid = ((r or {}).get("task") or {}).get("id") or (r or {}).get("task_id")
            if tid:
                self.j.update("plans", pid, {"task_id": tid, "updated_at": now_iso()})
        except Exception as e:  # noqa: BLE001 — no approval request = no live trade
            log.warning("couldn't ask the boss to approve plan %s (%s)", pid, type(e).__name__)
            self.j.update("plans", pid, {"status": "declined", "note": "couldn't reach the hub to ask the boss",
                                         "updated_at": now_iso()})

    # ------------------------------------------------------------------ approvals (from the hub connector)
    def on_approved(self, task: dict[str, Any]) -> bool:
        m = PLAN_RE.search(task.get("title") or "")
        if not m:
            return False
        plan = self.j.one("SELECT * FROM plans WHERE id = ?", (int(m.group(1)),))
        if not plan or plan["status"] != "waiting_approval":
            why = f"it's {plan['status']}" + (f" ({plan['note']})" if plan and plan.get("note") else "") if plan else \
                "I don't have that plan"
            self._task_update(task["id"], "declined", f"No trade: {why}.")
            return True
        if datetime.fromisoformat(plan["expires_at"]) <= self.clock():
            self.j.update("plans", plan["id"], {"status": "expired", "note": "approved after the setup's bar closed",
                                                "updated_at": now_iso()})
            self._task_update(task["id"], "declined", "Approved too late: the setup's bar already closed. No trade.")
            return True
        self.j.update("plans", plan["id"], {"status": "armed", "task_id": task["id"], "updated_at": now_iso()})
        d = json.loads(plan["data"])
        self._task_update(task["id"], "in_progress",
                          f"Armed: {d['side']} {d['coin']} on a break {'above' if d['side'] == 'long' else 'below'} "
                          f"{d['entry']:g} until the bar closes.")
        return True

    def on_declined(self, task: dict[str, Any]) -> None:
        m = PLAN_RE.search(task.get("title") or "")
        if m:
            plan = self.j.one("SELECT * FROM plans WHERE id = ?", (int(m.group(1)),))
            if plan and plan["status"] in ("waiting_approval", "armed"):
                self.j.update("plans", plan["id"], {"status": "declined", "note": "the boss declined",
                                                    "updated_at": now_iso()})

    def _task_update(self, task_id: Any, status: str, note: str) -> None:
        if self.hub is not None and task_id:
            try:
                self.hub.update_task(task_id, status, note)
            except Exception as e:  # noqa: BLE001
                log.info("hub: couldn't update task %s (%s)", task_id, type(e).__name__)

    # ------------------------------------------------------------------ entries
    def expire_plans(self, now: datetime) -> None:
        for p in self.j.all("SELECT * FROM plans WHERE status IN ('waiting_approval','armed')"):
            if datetime.fromisoformat(p["expires_at"]) <= now:
                self.j.update("plans", p["id"], {"status": "expired", "note": "the setup's bar closed without a trigger",
                                                 "updated_at": now_iso()})
                if p["status"] == "armed":  # an approved plan is closed off; an unapproved one just lapses
                    self._task_update(p["task_id"], "done", "Expired without triggering. No trade.")

    def watch_plans(self, now: datetime) -> None:
        for p in self.j.all("SELECT * FROM plans WHERE status = 'armed' ORDER BY id"):
            d = json.loads(p["data"])
            try:
                px = self.broker.mid(d["coin"])
            except Exception as e:  # noqa: BLE001
                self.incident(now.date(), f"no price for {d['coin']} while watching a plan ({type(e).__name__})")
                continue
            sign = 1 if d["side"] == "long" else -1
            if (px - d["stop"]) * sign <= 0:
                self.j.update("plans", p["id"], {"status": "invalid", "note": f"the stop side broke first ({px:g})",
                                                 "updated_at": now_iso()})
                self._task_update(p["task_id"], "done", f"Cancelled: price hit the stop side ({px:g}) before the trigger.")
                continue
            triggered = self.confirmed(d, now, px, sign)
            chase = settings.max_chase_r * abs(d["entry"] - d["stop"])
            if triggered and (px - d["entry"]) * sign <= chase:
                self.enter(p, d, px)
            elif triggered:
                self.j.update("plans", p["id"], {"status": "invalid", "note": f"ran past the trigger ({px:g}) — no chase",
                                                 "updated_at": now_iso()})
                self._task_update(p["task_id"], "done", f"Skipped: it ran past the trigger ({px:g}). No chasing.")

    def confirmed(self, d: dict[str, Any], now: datetime, px: float, sign: int) -> bool:
        """The Strat trigger, confirmed: a finished 5-minute candle CLOSED beyond the trigger during today's
        session (TRADER_ENTRY_CONFIRM=5m). With "touch", the first print through the trigger is enough."""
        if settings.entry_confirm != "5m":
            return d["entry_type"] == "market" or (px - d["entry"]) * sign >= 0
        try:
            bar: Bar | None = self.confirm_source(d["coin"], now)
        except Exception as e:  # noqa: BLE001
            self.incident(now.date(), f"no 5-minute candle for {d['coin']} ({type(e).__name__})")
            return False
        if bar is None:
            return False
        opened = datetime.fromtimestamp(bar.t, NY)
        if opened.date() != now.astimezone(NY).date() or opened.time() < _hm(settings.scan_start):
            return False  # only candles from today's session count
        return (bar.c - d["entry"]) * sign > 0

    def enter(self, plan: dict[str, Any], d: dict[str, Any], px: float) -> int | None:
        if len(self.j.open_trades()) >= settings.max_open:
            return None
        inst = inst_from(d)
        long_ = d["side"] == "long"
        try:
            equity = self.broker.equity(inst)
            lev = risk.leverage_for(px, d["stop"], inst.max_leverage)
            in_use = sum(t["entry"] * t["size"] / max(1, t["leverage"] or 1) for t in self.j.open_trades(mode=self.mode))
            size = risk.size_for(equity, px, d["stop"], inst.sz_decimals, lev, in_use)
        except Exception as e:  # noqa: BLE001
            self.incident(self.clock().date(), f"couldn't size {d['coin']} ({type(e).__name__})")
            return None
        if size <= 0:
            self.j.update("plans", plan["id"], {"status": "invalid", "note": "no room: the account is too small, or "
                                                          f"{settings.account_use_pct:g}% of it is already in use",
                                                "updated_at": now_iso()})
            return None
        try:
            fill = self.broker.open(inst, long_, size, lev)
        except Exception as e:  # noqa: BLE001
            self.j.update("plans", plan["id"], {"status": "invalid", "note": f"order failed: {str(e)[:150]}",
                                                "updated_at": now_iso()})
            self._task_update(plan["task_id"], "blocked", f"The order failed: {str(e)[:150]}")
            return None
        # Levels from the real fill: the hard stop is measured from what was actually paid.
        hard = risk.hard_stop_price(fill.price, d["side"])
        stop = max(d["stop"], hard) if long_ else min(d["stop"], hard)
        stop, hard = hl.round_px(stop, inst.sz_decimals), hl.round_px(hard, inst.sz_decimals)
        target = hl.round_px(d["target"], inst.sz_decimals)
        try:
            ids = self.broker.protect(inst, long_, fill.size, stop, target)
        except Exception as e:  # noqa: BLE001 — never hold an unprotected position
            log.error("couldn't place the stop for %s (%s): closing it", d["coin"], type(e).__name__)
            try:
                self.broker.close(inst, long_, fill.size)
            finally:
                self.j.update("plans", plan["id"], {"status": "invalid", "note": "couldn't place the stop — closed",
                                                    "updated_at": now_iso()})
            return None
        tid = self.j.record_open(
            mode=self.mode, coin=d["coin"], ticker=d["ticker"], chart=d["chart"], dex=d["dex"],
            sz_decimals=inst.sz_decimals, max_leverage=inst.max_leverage, only_isolated=d.get("only_isolated", 0),
            side=d["side"], setup=d["setup"], family=d["family"], tf=d["tf"], grade=d["grade"], inside=d["inside"],
            with_continuity=d["with_continuity"], rvol=d["rvol"], entry=fill.price, size=fill.size, stop=stop,
            initial_stop=stop, hard_stop=hard, target=target, fees=fill.fee, protection=ids, plan_id=plan["id"],
            leverage=lev,
            thesis=d["thesis"], notes=d["summary"], opened_at=self.stamp())
        self.j.update("plans", plan["id"], {"status": "filled", "updated_at": now_iso()})
        self._task_update(plan["task_id"], "in_progress",
                          f"Filled {fill.size:g} at {fill.price:g} with {lev}x leverage. Stop {stop:g} on the exchange, "
                          f"target {target:g}.")
        log.info("opened %s %s %s @ %s", self.mode, d["side"], d["coin"], fill.price)
        return tid

    # ------------------------------------------------------------------ managing and exiting
    def manage_positions(self) -> None:
        for t in self.j.open_trades(mode=self.mode):
            inst = inst_from(t)
            try:
                px = self.broker.mid(t["coin"])
            except Exception as e:  # noqa: BLE001
                self.incident(self.clock().date(), f"no price for open {t['coin']} ({type(e).__name__}); "
                                                   "the exchange stop still protects it")
                continue
            if self.broker.live:  # did the exchange's own stop / take-profit already close it?
                try:
                    if (self.broker.position_size(inst) or 0) <= 0:
                        near_target = abs(px - t["target"]) < abs(px - t["stop"])
                        self.finish(t, t["target"] if near_target else t["stop"],
                                    "target" if near_target else ("trail" if t["trail_active"] else "stop"),
                                    t["size"] * px * settings.fee_pct / 100, closed_by_exchange=True)
                        continue
                except Exception as e:  # noqa: BLE001
                    log.info("couldn't read the position for %s (%s)", t["coin"], type(e).__name__)
            st = risk.manage(t, px, inst.sz_decimals)
            changes: dict[str, Any] = {}
            if st.peak != t["peak"]:
                changes["peak"] = st.peak
            if st.trail_active != bool(t["trail_active"]):
                changes["trail_active"] = int(st.trail_active)
            if st.moved and not st.exit_reason:
                try:
                    ids = self.broker.move_stop(inst, t["side"] == "long", t["size"], st.stop,
                                                json.loads(t["protection"] or "{}"))
                    changes.update(stop=st.stop, protection=json.dumps(ids))
                except Exception as e:  # noqa: BLE001 — the old stop stays in place
                    log.warning("couldn't move the stop on %s (%s)", t["coin"], type(e).__name__)
            if changes:
                self.j.update("trades", t["id"], changes)
            if st.exit_reason:
                self.close(self.j.trade(t["id"]) or t, st.exit_reason)

    def close(self, t: dict[str, Any], reason: str) -> dict[str, Any] | None:
        inst = inst_from(t)
        try:
            self.broker.cancel_protection(inst, json.loads(t["protection"] or "{}"))
            fill = self.broker.close(inst, t["side"] == "long", t["size"])
        except Exception as e:  # noqa: BLE001 — the exchange stop is still there; try again next minute
            log.warning("couldn't close %s (%s)", t["coin"], type(e).__name__)
            return None
        return self.finish(t, fill.price, reason, fill.fee)

    def finish(self, t: dict[str, Any], px: float, reason: str, fee: float,
               closed_by_exchange: bool = False) -> dict[str, Any]:
        done = self.j.record_close(t["id"], px, reason, fee, at=self.stamp())
        if closed_by_exchange:
            self.j.update("trades", t["id"], {"notes": (t.get("notes") or "") + " (closed by the exchange order)"})
        plan = self.j.one("SELECT task_id FROM plans WHERE id = ?", (t.get("plan_id"),)) if t.get("plan_id") else None
        if plan and plan.get("task_id"):
            self._task_update(plan["task_id"], "in_progress",
                              f"Closed at {px:g} ({reason}): {done['pnl']:+.2f} USD after fees, {done['r_multiple']}R.")
        self.review(done)
        return done

    def review(self, t: dict[str, Any]) -> None:
        """One lesson per closed trade, from the trade and the Strat documents."""
        playbook = library().context(f"{t['setup']} {t['tf']} {t['exit_reason']} stop target inside bar continuity")
        th = self.brain.think(learning.lesson_prompt(t, playbook), job="research",
                              system_prompt=system(learning.recent_lessons(self.j), self.memories))
        if th is not None and th.text and not th.refused and th.brain != "wait":
            learning.add_lesson(self.j, th.text, t["id"])
        else:  # no brain: a factual lesson still gets written
            learning.add_lesson(self.j, f"{t['tf']} {t['family']} {t['side']} {t['coin']}: {t['exit_reason']}, "
                                        f"{t['r_multiple']}R ({'inside bar' if t['inside'] else 'not inside'}, "
                                        f"grade {t['grade']}).", t["id"], source="auto")

    # ------------------------------------------------------------------ the daily report
    def maybe_report(self, now: datetime) -> None:
        """If the hub's "Daily trading report" routine didn't arrive, still record the day."""
        if now.weekday() >= 5:
            return
        due = datetime.combine(now.date(), _hm(settings.report_time), NY) + timedelta(minutes=20)
        if now >= due and self.j.get("reported_day") != now.date().isoformat():
            self.report(now.date(), task_id=None)

    def report(self, day: date, task_id: Any = None) -> str:
        text, pnl_day, n, equity = self.report_text(day)
        self.j.set("reported_day", day.isoformat())
        if self.hub is not None:
            try:
                self.hub.brain = "local"
                if task_id:
                    self.hub.post_result(task_id, text, channel="trades")
                else:
                    self.hub.post_finding("trades", text, "reported", 1.0)
                self.hub.report_pnl(pnl_day, mode=self.mode, equity_usd=equity, trades=n,
                                    note=text.split("\n")[0][:200], day=day.isoformat())
            except Exception as e:  # noqa: BLE001 — the outbox keeps it when the hub is away
                log.warning("couldn't post the daily report (%s)", type(e).__name__)
        return text

    def report_text(self, day: date) -> tuple[str, float, int, float | None]:
        opened, closed = self.j.opened_on(day), self.j.closed_on(day)
        closed = [t for t in closed if t["mode"] == self.mode]
        opened = [t for t in opened if t["mode"] == self.mode]
        pnl_day = round(sum(t["pnl"] or 0 for t in closed), 2)
        total = self.j.realized_pnl(self.mode)
        try:
            equity = self.broker.equity()
        except Exception:  # noqa: BLE001
            equity = None
        wk = self.trades_this_week(datetime.combine(day, dtime(12), NY))
        money = "PAPER (pretend money)" if self.mode == "paper" else "LIVE (real money)"
        head = (f"{day.isoformat()} — {money}: today {pnl_day:+.2f} USD after fees, running total {total:+.2f} USD"
                + (f", account {equity:,.2f} USD" if equity is not None else "")
                + f". This week: {wk} trade{'s' if wk != 1 else ''} (minimum {settings.min_trades_week}).")
        lines = [head, ""]
        for t in opened:
            lines.append(f"OPENED {t['side']} {t['coin']} @ {t['entry']:g} — {t['notes'] or t['setup']} "
                         f"Plan: stop {t['initial_stop']:g}, target {t['target']:g}, hard stop {t['hard_stop']:g}.")
        for t in closed:
            lines.append(f"CLOSED {t['side']} {t['coin']} {t['entry']:g} → {t['exit']:g} ({t['exit_reason']}): "
                         f"{t['pnl']:+.2f} USD, {t['r_multiple']}R, fees {t['fees']:.2f}.")
        for t in self.j.open_trades(mode=self.mode):
            lines.append(f"OPEN {t['side']} {t['coin']} from {t['entry']:g}: stop {t['stop']:g}"
                         f"{' (trailing, profit locked)' if t['trail_active'] else ''}, target {t['target']:g}.")
        if not opened and not closed:
            passes = self.j.passes_on(day)
            if passes:
                p = passes[-1]
                lines.append(f"No trade today, because: {strategy.MARKET_REASONS[p['reason']]}. {p['detail']}")
            elif not int(self.j.get(f"scans:{day.isoformat()}", "0") or 0):
                if day.weekday() < 5:  # honest: no scan happened, so there's no market reason to give
                    lines.append("No trade today: I didn't scan during today's window (I wasn't running).")
            elif not self.j.incidents_on(day):
                lines.append("No trade today, because: no setup in the window — nothing actionable formed.")
        incidents = self.j.incidents_on(day)
        if incidents:
            lines.append(f"Incidents (not reasons to pass — being fixed): " + "; ".join(i["what"] for i in incidents[:3]))
        if day.weekday() == 4:
            lines.append(self.week_line(day, wk))
        since = datetime.combine(day, dtime(0), NY).astimezone(timezone.utc).isoformat(timespec="seconds")
        lessons = self.j.all("SELECT text FROM lessons WHERE created_at >= ? ORDER BY id", (since,))
        if lessons:
            lines.append("Lessons: " + " | ".join(x["text"][:200] for x in lessons[:3]))
        lines.append("Not financial advice.")
        touched = {t["id"] for t in opened} | {t["id"] for t in closed}
        return "\n".join(x for x in lines if x is not None).strip(), pnl_day, len(touched), equity

    def week_line(self, day: date, wk: int) -> str:
        if wk >= settings.min_trades_week:
            return f"Week: {wk} trades — the weekly minimum of {settings.min_trades_week} is met."
        start = week_start(day)
        rows = self.j.all("SELECT reason, COUNT(*) AS n FROM passes WHERE day >= ? AND day <= ? GROUP BY reason "
                          "ORDER BY n DESC", (start.isoformat(), day.isoformat()))
        why = ", ".join(f"{strategy.MARKET_REASONS[r['reason']].split(' — ')[0]} ({r['n']} day{'s' if r['n'] != 1 else ''})"
                        for r in rows) or "no setup in the window"
        return (f"Week: {wk} of the {settings.min_trades_week}-trade minimum. Market reasons: {why}. "
                "B-grade setups were allowed once behind pace; no C-grade setup was forced.")
