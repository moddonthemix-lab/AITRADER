"""Backtest: replay history bar by bar with the SAME rules Trader trades with.

For each completed bar, the Strat setups built on it (strat.find_setups) are checked against the next
bars: entry on the break of the trigger (or the open, if it gapped through), the Strat stop, the -23%
hard stop, the fee-aware target and the trailing stop (risk.manage), fees on both sides. Continuity is
measured at the moment of the trigger, against the higher timeframes' opens (W and M for daily setups;
M and Q for weekly ones) — so grades match the live scanner: A = full continuity, B = leaning.

It's deliberately cautious: when a bar touches both the stop and the target, the stop is assumed to
have come first. Results per pattern feed Trader's learning (learning.record_backtest).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from . import learning, risk, strat
from .journal import Journal
from .market import Bar, aggregate, candles

HIGHER = {"D": ("W", "M"), "W": ("M", "Q")}
MAX_HOLD = 60  # bars


@dataclass
class BtTrade:
    symbol: str
    tf: str
    setup: str
    family: str
    side: str
    grade: str
    inside: bool
    rvol: float | None
    opened: str
    closed: str
    entry: float
    stop: float
    target: float
    exit: float
    exit_reason: str
    r: float
    pnl_pct: float


def _date(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).date().isoformat()


def _open_of(period_bars: list[Bar], t: int) -> float | None:
    """The open of the higher-timeframe bar that contains time t."""
    cur = None
    for b in period_bars:
        if b.t <= t:
            cur = b
        else:
            break
    return cur.o if cur else None


def run(symbol: str, tf: str = "D", source: Callable | None = None, start: str | None = None,
        grades: tuple[str, ...] = ("A", "B")) -> dict[str, Any]:
    if tf not in HIGHER:
        raise ValueError("backtests run on D (daily) or W (weekly) setups")
    get = source or candles
    daily, meta = get(symbol, "D")
    offset = meta.get("gmtoffset", 0) if isinstance(meta, dict) else 0
    bars = daily if tf == "D" else aggregate(daily, "W", offset)
    higher = {h: aggregate(daily, h, offset) for h in HIGHER[tf]}
    trades: list[BtTrade] = []
    busy_until = -1
    first = 30
    if start:
        first = max(first, next((i for i, b in enumerate(bars) if _date(b.t) >= start), len(bars)))

    for i in range(first, len(bars) - 1):
        if i <= busy_until:
            continue
        done = bars[: i + 1]
        live = Bar(bars[i + 1].t, done[-1].c, done[-1].c, done[-1].c, done[-1].c, 0, synthetic=True)  # not traded yet
        hist = done + [live]
        setups = [s for s in strat.find_setups(tf, hist, strat.label(hist)) if s.status == "waiting"]
        nxt = bars[i + 1]
        best = None
        for s in setups:
            d = 1 if s.direction == "long" else -1
            if (nxt.h > s.trigger) if d == 1 else (nxt.l < s.trigger):
                entry = max(s.trigger, nxt.o) if d == 1 else min(s.trigger, nxt.o)
                opens = [nxt.o] + [_open_of(higher[h], nxt.t) for h in HIGHER[tf]]
                agree = sum(1 for o in opens if o is not None and (entry - o) * d > 0)
                grade = "A" if agree == len(opens) else "B" if agree >= 2 else "C"
                if grade not in grades:
                    continue
                # Daily bars can't show where the entry bar's low/high was when the trigger broke, so the backtest
                # keeps the setup bar's far side as the stop (wider, so cautious); live trading uses the real one.
                stop = s.stop
                lv = risk.levels(s.direction, entry, stop, s.target)
                if lv.problem:
                    continue
                key = (grade == "A", s.inside, lv.rr)  # the live scanner's order: grade, inside bars, R:R
                if best is None or key > best[0]:
                    best = (key, s, lv, grade, entry)
        if best is None:
            continue
        _, s, lv, grade, entry = best
        t, exit_i = _simulate(bars, i + 1, s, lv, entry)
        busy_until = exit_i
        d = 1 if s.direction == "long" else -1
        f = risk.fee()
        pnl_unit = (t["exit"] - entry) * d - f * (entry + t["exit"])
        trades.append(BtTrade(symbol, tf, s.name, s.family, s.direction, grade, s.inside, s.rvol, _date(nxt.t),
                              _date(bars[exit_i].t), round(entry, 4), round(lv.stop, 4), round(lv.target, 4),
                              round(t["exit"], 4), t["reason"], round(pnl_unit / abs(entry - lv.stop), 3),
                              round(pnl_unit / entry * 100, 3)))
    return summarize(symbol, tf, trades)


def _simulate(bars: list[Bar], k: int, s: strat.Setup, lv: risk.Levels, entry: float) -> tuple[dict[str, Any], int]:
    """Walk forward from the entry bar k. Stop before target inside a bar (cautious)."""
    trade = {"side": s.direction, "entry": entry, "stop": lv.stop, "initial_stop": lv.stop, "hard_stop": lv.hard_stop,
             "target": lv.target, "peak": entry, "trail_active": 0}
    d = 1 if s.direction == "long" else -1
    for j in range(k, min(len(bars), k + MAX_HOLD)):
        b = bars[j]
        worst, best = (b.l, b.h) if d == 1 else (b.h, b.l)
        o = b.o if j > k else entry
        if (worst - trade["stop"]) * d <= 0:  # stopped (at the open if it gapped through)
            px = o if (o - trade["stop"]) * d < 0 else trade["stop"]
            return {"exit": px, "reason": "trail" if trade["trail_active"] else "stop"}, j
        if (best - trade["target"]) * d >= 0:
            px = o if (o - trade["target"]) * d > 0 else trade["target"]
            return {"exit": px, "reason": "target"}, j
        step = risk.manage(trade, best)  # trail on the bar's best price; the new stop applies from the next bar
        trade.update(stop=step.stop, peak=step.peak, trail_active=int(step.trail_active))
    j = min(len(bars), k + MAX_HOLD) - 1
    return {"exit": bars[j].c, "reason": "time"}, j


def summarize(symbol: str, tf: str, trades: list[BtTrade]) -> dict[str, Any]:
    def stats(rows: list[BtTrade]) -> dict[str, Any]:
        rs = [t.r for t in rows]
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        peak = dd = run_r = 0.0
        for r in rs:
            run_r += r
            peak = max(peak, run_r)
            dd = min(dd, run_r - peak)
        return {"trades": len(rs), "win_rate": round(len(wins) / len(rs), 3) if rs else None,
                "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "total_r": round(sum(rs), 2),
                "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else None,
                "max_drawdown_r": round(dd, 2),
                "return_pct_1x": round(sum(t.pnl_pct for t in rows), 2)}

    groups: dict[str, list[BtTrade]] = defaultdict(list)
    for t in trades:
        groups[t.family].append(t)
    return {
        "symbol": symbol, "tf": tf,
        "from": trades[0].opened if trades else None, "to": trades[-1].closed if trades else None,
        "all": stats(trades),
        "inside_bars": stats([t for t in trades if t.inside]),
        "other_setups": stats([t for t in trades if not t.inside]),
        "grade_A": stats([t for t in trades if t.grade == "A"]),
        "grade_B": stats([t for t in trades if t.grade == "B"]),
        "by_pattern": {k: stats(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))},
        "exits": {r: sum(1 for t in trades if t.exit_reason == r) for r in ("target", "trail", "stop", "time")},
        "trades": [asdict(t) for t in trades],
    }


def learn(j: Journal, result: dict[str, Any]) -> None:
    """Feed a backtest into Trader's learning (per pattern and timeframe)."""
    by: dict[tuple[str, str], list[float]] = defaultdict(list)
    for t in result["trades"]:
        by[(t["family"], t["tf"])].append(t["r"])
    learning.record_backtest(j, by, f"backtest:{result['symbol']}")


def text(result: dict[str, Any]) -> str:
    a = result["all"]
    if not a["trades"]:
        return f"{result['symbol']} {result['tf']}: no trades in the test period."

    def line(name: str, s: dict[str, Any]) -> str:
        if not s["trades"]:
            return f"{name}: none"
        return (f"{name}: {s['trades']} trades, {s['win_rate'] * 100:.0f}% winners, avg {s['avg_r']:+.2f}R, "
                f"total {s['total_r']:+.1f}R, PF {s['profit_factor'] or '—'}, worst drawdown {s['max_drawdown_r']}R")

    out = [f"Backtest {result['symbol']} — {strat.TF_NAMES[result['tf']]} Strat setups, {result['from']} → {result['to']} "
           f"(fees in, stop-first when a bar hits both):",
           line("All", a), line("Inside bars", result["inside_bars"]), line("Other setups", result["other_setups"]),
           line("Grade A (full continuity)", result["grade_A"]), line("Grade B", result["grade_B"])]
    top = list(result["by_pattern"].items())[:6]
    out.append("By pattern: " + "; ".join(f"{k} {v['trades']}× avg {v['avg_r']:+.2f}R" for k, v in top))
    out.append("Exits: " + ", ".join(f"{k} {v}" for k, v in result["exits"].items()))
    return "\n".join(out)
