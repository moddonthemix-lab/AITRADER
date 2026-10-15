"""The game plan: inside bars ready for the next session, made after the close and again before the open.

After the close (TRADER_PREP_AFTER_CLOSE, 16:10 New York): the day's bars are finished, so Trader reads
every market and lists the inside bars that just formed (daily, 4-hour, weekly when the week is done),
with both triggers, the stop and the targets, and which side the higher timeframes favour.
Before the open (TRADER_PREP_PRE_MARKET, 9:00): the same read again (weekly bars finish on Friday;
overnight the perps keep trading), so the plan is fresh at 9:30.

The plan is posted in #trades and shown on the dashboard. At 9:30 the live scan trades it: an inside bar
triggers when price breaks its high or low, and it's taken only if continuity agrees at that moment —
the rules don't change, the plan just means Trader (and the boss) know the levels before the bell.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable

from . import risk, strat
from .config import settings
from .hyperliquid import Instrument
from .journal import NY, Journal, now_iso
from .market import DataFeedError
from .session import is_open, next_session, session_source

log = logging.getLogger(__name__)
TF_ORDER = {"W": 0, "D": 1, "4H": 2}


def higher_tf_side(a: dict[str, Any]) -> tuple[str | None, str]:
    """Which way the finished-and-open higher timeframes point (Q, M, W), e.g. ("long", "Q▲ M▲ W▲")."""
    frames = a["_frames"]
    hts = [tf for tf in ("Q", "M", "W") if tf in frames and not frames[tf].waiting]
    arrows = " ".join(f"{tf}{frames[tf].arrow}" for tf in ("Q", "M", "W") if tf in frames)
    ups = [tf for tf in hts if frames[tf].price > frames[tf].open]
    downs = [tf for tf in hts if frames[tf].price < frames[tf].open]
    if hts and len(ups) == len(hts):
        return "long", arrows
    if hts and len(downs) == len(hts):
        return "short", arrows
    return None, arrows


def plan_for(inst: Instrument, a: dict[str, Any]) -> list[dict[str, Any]]:
    """The inside bars in one market, both sides, with levels and the favoured side."""
    side, arrows = higher_tf_side(a)
    out: dict[str, dict[str, Any]] = {}
    for s in a["_setups"]:
        if not s.inside or s.tf not in TF_ORDER or s.status != "waiting":
            continue
        frame = a["_frames"][s.tf]
        if not frame.waiting and s.tf != "W":
            continue  # a live intraday bar isn't a finished setup yet
        row = out.setdefault(s.tf, {"coin": inst.coin, "ticker": inst.ticker, "tf": s.tf, "pattern": "",
                                    "favoured": side, "higher_tfs": arrows, "rvol": s.rvol, "sides": {}})
        lv = risk.levels(s.direction, s.trigger, s.stop, s.target)
        row["sides"][s.direction] = {"name": s.name, "trigger": s.trigger, "stop": s.stop, "target": lv.target,
                                     "target_kind": lv.target_kind, "rr": lv.rr, "ok": lv.problem is None,
                                     "problem": lv.problem}
        row["pattern"] = s.family.replace(" continuation", "").replace(" reversal", "")
    return sorted(out.values(), key=lambda r: TF_ORDER[r["tf"]])


def build(instruments: list[Instrument], now: datetime, chart_source: Callable | None = None,
          kind: str | None = None) -> dict[str, Any]:
    """Read every market as of `now` and collect the inside bars for the next session."""
    src = session_source(chart_source, now)
    rows, problems = [], []
    for inst in instruments:
        try:
            a = strat.analyze(inst.chart, source=src)
        except (DataFeedError, ValueError, KeyError, TypeError) as e:
            problems.append(f"{inst.ticker}: couldn't read the chart ({str(e)[:80]})")
            continue
        rows.extend(plan_for(inst, a))
    # favoured side first, then the bigger timeframe, then the busiest market (instrument order)
    rows.sort(key=lambda r: (r["favoured"] is None, TF_ORDER[r["tf"]]))
    session = next_session(now)
    return {"made_at": now.astimezone(NY).isoformat(timespec="minutes"), "session": session.isoformat(),
            "kind": kind or ("after the close" if now.astimezone(NY).hour >= 12 else "before the open"),
            "inside": rows, "markets": len(instruments), "problems": problems}


def text(plan: dict[str, Any], limit: int = 12) -> str:
    day = datetime.fromisoformat(plan["session"]).strftime("%a %b %d")
    rows = plan["inside"]
    head = (f"Game plan for {day} ({plan['kind']}): {len(rows)} inside bar{'s' if len(rows) != 1 else ''} "
            f"across {plan['markets']} markets. Entries 9:40–15:45: a 5-minute candle must close beyond the inside "
            "bar's high or low, with continuity at that moment.")
    lines = [head]
    for r in rows[:limit]:
        tf = strat.TF_NAMES[r["tf"]]
        sides = []
        for d in ("long", "short"):
            s = r["sides"].get(d)
            if not s:
                continue
            mark = " ← favoured" if r["favoured"] == d else ""
            verb = "above" if d == "long" else "below"
            note = "" if s["ok"] else f" (skip: {s['problem'].replace('_', ' ')})"
            sides.append(f"{d} {verb} {s['trigger']:g} → {s['target']:g}, stop {s['stop']:g}{mark}{note}")
        lines.append(f"• {r['coin']} {tf} {r['pattern']} [{r['higher_tfs']}]: " + "; ".join(sides))
    if len(rows) > limit:
        lines.append(f"…and {len(rows) - limit} more on the dashboard.")
    if not rows:
        lines.append("No inside bars formed — the scan at 9:30 still looks for every other Strat setup.")
    if plan["problems"]:
        lines.append(f"Couldn't read {len(plan['problems'])} market(s) — an incident, not a reason to pass.")
    return "\n".join(lines)


def save(j: Journal, plan: dict[str, Any]) -> None:
    j.set("gameplan", json.dumps(plan))
    j.set(f"gameplan:{plan['session']}:{plan['kind']}", now_iso())


def latest(j: Journal) -> dict[str, Any] | None:
    raw = j.get("gameplan")
    return json.loads(raw) if raw else None


def due(now: datetime, j: Journal) -> str | None:
    """Which prep is due now, if any: "after the close" or "before the open" (weekdays, once each)."""
    n = now.astimezone(NY)
    if n.weekday() >= 5 or is_open(n):
        return None
    hm = n.strftime("%H:%M")
    session = next_session(n).isoformat()
    for kind, at, until in (("before the open", settings.prep_pre_market, "09:30"),
                            ("after the close", settings.prep_after_close, "23:59")):
        if at <= hm < until and not j.get(f"gameplan:{session}:{kind}"):
            return kind
    return None
