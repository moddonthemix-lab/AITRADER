"""TheStrat (Rob Smith), computed from real candles — no guessing. Forked from AMO's strat.py.

The rules (docs/strat/ in this repo is the written playbook; this file is that playbook in code):
- Truth 1: every bar is a 1 (inside: takes neither prior high nor low), 2U/2D (takes one side) or
  3 (outside: takes both). "The break decides the number, never the color."
- Truth 2: price trades in the direction of the most 2s across timeframes. Full Timeframe
  Continuity (FTFC): price above every timeframe's open = up; below every open = down.
- Truth 3: price discovery is a broadening formation — 3s expand the range.
- Setups trigger on the break of the last completed bar's high/low; the stop is the other side of
  that bar; targets are prior highs/lows (the previous range).
- Trader's priority: INSIDE BARS first (3-1-2, 2-1-2, 1-1-2) — a 1 is a coiled spring with a tight,
  well-defined stop. Volume is a tie-breaker, never the reason.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .market import Bar, candles, relative_volume

TF_NAMES = {"Q": "Quarterly", "M": "Monthly", "W": "Weekly", "D": "Daily", "4H": "4-hour", "60": "60-minute"}
THESIS_TFS = ("M", "W", "D")            # the timeframes the thesis is written around (must have data)
SETUP_TFS = ("W", "D", "4H")            # where Trader takes setups (monthly ones are too wide for a -10% stop)
CONTINUITY_TFS = ("Q", "M", "W", "D", "4H", "60")
TF_ORDER = {"M": 0, "W": 1, "D": 2, "4H": 3, "60": 4}

CandleSource = Callable[[str, str], "tuple[list[Bar], dict[str, Any]]"]


def scenario(prev: Bar, cur: Bar) -> str:
    up, down = cur.h > prev.h, cur.l < prev.l
    if up and down:
        return "3"
    if up:
        return "2U"
    if down:
        return "2D"
    return "1"


def label(bars: list[Bar]) -> list[str | None]:
    return [None] + [scenario(a, b) for a, b in zip(bars, bars[1:])]


def candle_shape(b: Bar) -> str | None:
    """Hammer (long lower wick, closes high) or shooter (long upper wick, closes low)."""
    rng = b.h - b.l
    if rng <= 0:
        return None
    body = abs(b.c - b.o)
    lower, upper = min(b.o, b.c) - b.l, b.h - max(b.o, b.c)
    if lower >= 0.6 * rng and lower >= 2 * body:
        return "hammer"
    if upper >= 0.6 * rng and upper >= 2 * body:
        return "shooter"
    return None


@dataclass
class Setup:
    tf: str
    name: str
    direction: str          # long | short
    trigger: float
    target: float | None
    stop: float
    status: str             # waiting | triggered | target hit
    with_continuity: bool | None = None
    inside: bool = False    # built on an inside bar (Trader's first priority)
    rvol: float | None = None   # volume of the setup bar vs its 20-bar average
    family: str = ""        # the pattern without the direction words, for learning ("3-1-2", "2-2 reversal")

    def describe(self, fmt: Callable[[float | None], str]) -> str:
        verb = "above" if self.direction == "long" else "below"
        t = f", target {fmt(self.target)}" if self.target is not None else ""
        s = {"waiting": f"triggers {verb} {fmt(self.trigger)}",
             "triggered": f"TRIGGERED {verb} {fmt(self.trigger)}",
             "target hit": f"hit its target {fmt(self.target)}"}[self.status]
        side = "" if self.with_continuity is None else (" — with continuity" if self.with_continuity
                                                         else " — against continuity")
        return f"{TF_NAMES[self.tf]} {self.name} ({self.direction}): {s}{t}, stop {fmt(self.stop)}{side}"


@dataclass
class Timeframe:
    tf: str
    open: float
    high: float
    low: float
    price: float
    type: str | None             # current (live) bar's scenario
    prev_type: str | None        # last completed bar's scenario
    prev_high: float
    prev_low: float
    sequence: list[str]          # last few completed scenarios, oldest first
    shape: str | None            # hammer/shooter on the last completed bar
    setups: list[Setup] = field(default_factory=list)
    waiting: bool = False        # the next bar hasn't opened yet (before the open / after the close)

    @property
    def up(self) -> bool:
        return self.price > self.open

    @property
    def arrow(self) -> str:
        if self.waiting:
            return "…"  # next bar not open yet
        return "▲" if self.price > self.open else "▼" if self.price < self.open else "="


def _target_above(bars: list[Bar], level: float, first: float | None = None) -> float | None:
    """Nearest prior high above `level` (the previous range), preferring `first` if valid."""
    if first is not None and first > level:
        return first
    highs = [b.h for b in reversed(bars[-40:]) if b.h > level]
    return min(highs) if highs else None


def _target_below(bars: list[Bar], level: float, first: float | None = None) -> float | None:
    if first is not None and first < level:
        return first
    lows = [b.l for b in reversed(bars[-40:]) if b.l < level]
    return max(lows) if lows else None


def family_of(name: str) -> str:
    """'3-1-2U + hammer' → '3-1-2'; '2D-1-2U reversal' → '2-1-2 reversal'; for learning by pattern."""
    base = name.split(" + ")[0].replace("(compound inside)", "").strip()
    for d in ("2U", "2D"):
        base = base.replace(d, "2")
    return " ".join(base.split())


def find_setups(tf: str, bars: list[Bar], types: list[str | None]) -> list[Setup]:
    """Setups built on the last COMPLETED bar (B), triggered by the live bar breaking it."""
    if len(bars) < 4:
        return []
    a, b, cur = bars[-3], bars[-2], bars[-1]
    ta, tb = types[-3], types[-2]
    history = bars[:-1]
    rvol = relative_volume(bars, len(bars) - 2)
    setups: list[Setup] = []

    def add(name: str, direction: str, first_target: float | None) -> None:
        if direction == "long":
            trigger, stop = b.h, b.l
            target = _target_above(history[:-1], trigger, first_target)
            hit = cur.h > trigger
            done = target is not None and cur.h >= target
        else:
            trigger, stop = b.l, b.h
            target = _target_below(history[:-1], trigger, first_target)
            hit = cur.l < trigger
            done = target is not None and cur.l <= target
        status = "target hit" if hit and done else "triggered" if hit else "waiting"
        setups.append(Setup(tf, name, direction, round(trigger, 4), None if target is None else round(target, 4),
                            round(stop, 4), status, inside=tb == "1", rvol=rvol, family=family_of(name)))

    if tb == "1":  # inside bar: 2-1-2 / 3-1-2 — break of the inside bar decides
        if ta == "3":
            add("3-1-2U", "long", a.h)
            add("3-1-2D", "short", a.l)
        elif ta == "2U":
            add("2U-1-2U continuation", "long", a.h)
            add("2U-1-2D reversal", "short", a.l)
        elif ta == "2D":
            add("2D-1-2U reversal", "long", a.h)
            add("2D-1-2D continuation", "short", a.l)
        else:
            add("1-1-2U (compound inside)", "long", a.h)
            add("1-1-2D (compound inside)", "short", a.l)
    elif tb == "2D":
        prefix = {"1": "1-2D-2U Rev Strat", "3": "3-2D-2U reversal", "2D": "2D-2D-2U reversal"}.get(ta, "2D-2U reversal")
        add(prefix, "long", a.h)
        add("2D-2D continuation" if ta != "2D" else "2D-2D-2D continuation", "short", None)
    elif tb == "2U":
        prefix = {"1": "1-2U-2D Rev Strat", "3": "3-2U-2D reversal", "2U": "2U-2U-2D reversal"}.get(ta, "2U-2D reversal")
        add(prefix, "short", a.l)
        add("2U-2U continuation" if ta != "2U" else "2U-2U-2U continuation", "long", None)
    elif tb == "3":
        add("3-2U", "long", None)
        add("3-2D", "short", None)

    shape = candle_shape(b)
    if shape == "hammer" and not any(s.direction == "long" and s.trigger == round(b.h, 4) for s in setups):
        add("hammer", "long", None)
    elif shape == "shooter" and not any(s.direction == "short" and s.trigger == round(b.l, 4) for s in setups):
        add("shooter", "short", None)
    if shape:
        for s in setups:
            if (shape == "hammer" and s.direction == "long") or (shape == "shooter" and s.direction == "short"):
                if shape not in s.name:
                    s.name += f" + {shape}"
    return setups


def analyze_tf(tf: str, bars: list[Bar]) -> Timeframe:
    types = label(bars)
    cur, prev = bars[-1], bars[-2]
    t = Timeframe(
        tf=tf, open=cur.o, high=cur.h, low=cur.l, price=cur.c, type=types[-1], prev_type=types[-2],
        prev_high=prev.h, prev_low=prev.l, sequence=[x for x in types[-5:-1] if x],
        shape=candle_shape(prev),
    )
    t.setups = find_setups(tf, bars, types)
    t.waiting = bars[-1].synthetic
    return t


def bias_of(frames: dict[str, Timeframe]) -> tuple[str, str, str | None, list[str], list[str]]:
    """(bias, why, ftfc, ups, downs) from each timeframe's price vs its open. A timeframe whose next bar
    hasn't opened yet has no direction yet, so it doesn't count (it's decided at the open)."""
    frames = {tf: f for tf, f in frames.items() if not f.waiting} or frames
    ups = [tf for tf, f in frames.items() if f.price > f.open]
    downs = [tf for tf, f in frames.items() if f.price < f.open]
    ftfc = "up" if len(ups) == len(frames) else "down" if len(downs) == len(frames) else None
    higher = [frames[t].up for t in ("M", "W") if t in frames]
    lower = [frames[t].up for t in ("D", "60") if t in frames]
    if ftfc:
        bias, why = ("bullish", "full timeframe continuity is UP") if ftfc == "up" else \
                    ("bearish", "full timeframe continuity is DOWN")
    elif len(lower) == 2 and len(set(lower)) == 1 and set(higher) and lower[0] not in higher:
        bias = "bullish" if lower[0] else "bearish"
        why = "the Daily and 60-minute agree against the higher timeframes (lower timeframes in control — override)"
    elif len(ups) > len(downs):
        bias, why = "leaning bullish", f"{len(ups)} of {len(frames)} timeframes are above their open"
    elif len(downs) > len(ups):
        bias, why = "leaning bearish", f"{len(downs)} of {len(frames)} timeframes are below their open"
    else:
        bias, why = "neutral", "timeframes are split — no continuity"
    return bias, why, ftfc, ups, downs


def analyze(symbol: str, source: CandleSource | None = None) -> dict[str, Any]:
    """The full multi-timeframe read. `source(symbol, tf)` gives candles (default: the stock's chart)."""
    get = source or candles
    frames: dict[str, Timeframe] = {}
    meta: dict[str, Any] = {}
    bars_by_tf: dict[str, list[Bar]] = {}
    for tf in CONTINUITY_TFS:
        try:
            bars, m = get(symbol, tf)
        except Exception:  # noqa: BLE001 — e.g. no intraday data for some symbols
            if tf in THESIS_TFS:
                raise
            continue
        meta = meta or m
        if len(bars) >= 4:
            frames[tf] = analyze_tf(tf, bars)
            bars_by_tf[tf] = bars
    if not all(tf in frames for tf in THESIS_TFS):
        raise ValueError(f"not enough history for {symbol}")

    price = frames["D"].price
    for f in frames.values():  # every timeframe's "price" is the latest print
        f.price = price

    bias, why, ftfc, ups, downs = bias_of(frames)

    def two_dir(f: Timeframe) -> str | None:
        if f.type == "2U" or (f.type == "3" and f.price > f.open):
            return "up"
        if f.type == "2D" or (f.type == "3" and f.price < f.open):
            return "down"
        return None

    twos_up = [tf for tf, f in frames.items() if two_dir(f) == "up"]
    twos_down = [tf for tf, f in frames.items() if two_dir(f) == "down"]

    want = "long" if "bullish" in bias else "short" if "bearish" in bias else None
    setups: list[Setup] = []
    for tf in (t for t in SETUP_TFS if t in frames):
        for s in frames[tf].setups:
            s.with_continuity = None if want is None else (s.direction == want)
            setups.append(s)

    return {
        "symbol": symbol,
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        "price": price,
        "as_of": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "timeframes": {tf: {**{k: v for k, v in asdict(f).items() if k != "setups"},
                            "up": f.up, "arrow": f.arrow} for tf, f in frames.items()},
        "continuity": {"ftfc": ftfc, "up": ups, "down": downs},
        "most_2s": {"up": twos_up, "down": twos_down},
        "bias": bias,
        "why": why,
        "setups": [asdict(s) for s in setups],
        "_setups": setups,
        "_frames": frames,
        "_bars": bars_by_tf,
    }


# ------------------------------------------------------------------ reading it
def fmt_factory(price: float) -> Callable[[float | None], str]:
    decimals = 2 if price >= 1 else 4
    return lambda v: "—" if v is None else f"{v:,.{decimals}f}"


def risk_reward(s: Setup) -> float | None:
    if s.target is None:
        return None
    risk = abs(s.trigger - s.stop)
    return round(abs(s.target - s.trigger) / risk, 2) if risk else None


def is_chop(a: dict[str, Any]) -> bool:
    """Chop: the setup timeframes are all inside or outside bars and nothing has continuity."""
    frames: dict[str, Timeframe] = a["_frames"]
    live = [frames[t].type for t in ("W", "D") if t in frames]
    return a["continuity"]["ftfc"] is None and bool(live) and all(t in ("1", "3") for t in live) \
        and a["bias"] in ("neutral", "leaning bullish", "leaning bearish")


def thesis(a: dict[str, Any]) -> str:
    """The written multi-timeframe read, for the journal and for the boss."""
    fmt = fmt_factory(a["price"])
    frames: dict[str, Timeframe] = a["_frames"]
    setups: list[Setup] = a["_setups"]
    lines = [f"{a['symbol']} — The Strat read (price {fmt(a['price'])})"]
    cont = " · ".join(f"{'60m' if tf == '60' else tf} {frames[tf].arrow}" for tf in CONTINUITY_TFS if tf in frames)
    ftfc = {"up": "FULL TIMEFRAME CONTINUITY UP", "down": "FULL TIMEFRAME CONTINUITY DOWN"}.get(
        a["continuity"]["ftfc"], "no full continuity")
    lines.append(f"Continuity (price vs each open): {cont} → {ftfc}")
    twos_up, twos_down = a["most_2s"]["up"], a["most_2s"]["down"]
    lines.append(f"Most 2s: {len(twos_up)} up ({', '.join(twos_up) or '—'}) vs {len(twos_down)} down "
                 f"({', '.join(twos_down) or '—'})")
    for tf in (t for t in ("M",) + SETUP_TFS if t in frames):
        f = frames[tf]
        seq = "-".join(f.sequence[-3:] + [f"[{f.type}]"])
        if f.waiting:
            lines.append(f"{TF_NAMES[tf]}: {'-'.join(f.sequence[-4:])} — last bar complete "
                         f"({fmt(f.prev_low)}–{fmt(f.prev_high)}); the next one hasn't opened yet.")
            continue
        where = ("inside last bar's range" if f.type == "1" else
                 "broke last bar's HIGH" if f.type == "2U" else
                 "broke last bar's LOW" if f.type == "2D" else "broke BOTH sides (outside bar)")
        shape = f", last bar was a {f.shape}" if f.shape else ""
        lines.append(f"{TF_NAMES[tf]}: {seq} — current bar {where}{shape}. "
                     f"Open {fmt(f.open)} ({'above' if f.up else 'below'}).")
    lines.append(f"Bias: {a['bias'].upper()} — {a['why']}.")
    live = [s for s in setups if s.status == "triggered" and s.with_continuity is not False]
    waiting = [s for s in setups if s.status == "waiting" and s.with_continuity is not False]
    if live:
        lines.append("In force: " + "; ".join(s.describe(fmt) for s in live[:3]) + ".")
    if waiting:
        lines.append("Watching: " + "; ".join(s.describe(fmt) for s in waiting[:4]) + ".")
    return "\n".join(lines)
