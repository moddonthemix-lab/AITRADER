"""The scan: read every market with the Strat, rank the setups, and either plan a trade or pass — with a
market reason.

Grades (only A and B are ever traded; C never):
    A  full timeframe continuity (FTFC) in the trade's direction, net R:R after fees ≥ TRADER_MIN_RR
    B  the bias leans the trade's way (most timeframes agree), net R:R after fees ≥ TRADER_MIN_RR
       — taken only when Trader is behind pace for the weekly minimum (2 trades)
Ranking inside a grade: inside-bar setups first (TRADER_PRIORITY=inside), then the learned edge of that
pattern, then reward:risk, then volume, then the bigger timeframe.

Pass discipline: a pass needs a MARKET reason —
    chop                 inside/outside bars on the setup timeframes and no continuity
    conflicting_mtf      timeframes disagree (no continuity, or every setup is against it)
    no_setup_in_window   nothing actionable formed on the setup timeframes during the window
    rr_after_fees        the setup's target doesn't pay enough after fees
    stop_too_wide        the Strat stop is further than the -10% hard stop allows
    no_chase             it already ran past the trigger
A data-feed problem is never a pass: Trader retries, falls back to Hyperliquid's own candles, and if a
market still can't be read it is logged as an INCIDENT (and reported as such), not as a pass.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from . import hyperliquid as hl
from . import learning, risk, strat
from .config import settings
from .hyperliquid import Instrument
from .journal import Journal
from .market import DataFeedError

log = logging.getLogger(__name__)

MARKET_REASONS = {
    "chop": "chop — inside/outside bars on the setup timeframes and no continuity",
    "conflicting_mtf": "conflicting timeframes — no continuity behind any setup",
    "no_setup_in_window": "no setup in the window — nothing actionable formed",
    "rr_after_fees": "the targets don't pay enough after fees",
    "stop_too_wide": "the Strat stop is wider than the -10% hard stop allows",
    "no_chase": "it already ran past the trigger — no chasing",
}
# Never acceptable as a pass reason. These are incidents.
DATA_WORDS = ("data feed", "no data", "market data", "api ", "timeout", "timed out", "offline", "unreachable",
              "http", "network", "yahoo", "rate limit", "connection", "couldn't read")
GRADE_RANK = {"A": 2, "B": 1, "C": 0}
MAX_BASIS = 0.03  # the stock chart and the perp must agree within 3%, or Trader reads the perp's own chart


class NotAMarketReason(ValueError):
    pass


def validate_pass(reason: str, detail: str = "") -> None:
    """Only market reasons may justify a pass (the boss's pass discipline)."""
    if reason not in MARKET_REASONS:
        raise NotAMarketReason(f"'{reason}' is not a market reason; passes need one of: {', '.join(MARKET_REASONS)}")
    if any(w in detail.lower() for w in DATA_WORDS):
        raise NotAMarketReason("a pass can't be explained by a data problem — that's an incident")


@dataclass
class Candidate:
    inst: Instrument
    setup: strat.Setup
    levels: risk.Levels
    grade: str
    entry_type: str            # stop (wait for the trigger) | market (triggered, close enough to take now)
    edge: float
    source: str                # chart | hyperliquid
    basis: float               # perp price / chart price (levels are scaled by this)
    thesis: str
    bias: str

    @property
    def key(self) -> tuple:
        s = self.setup
        inside_first = s.inside if settings.priority == "inside" else False
        vol = 0
        if s.rvol is not None:  # an inside bar on lighter volume is a coil; a breakout bar wants volume
            vol = 1 if (s.inside and s.rvol < 1) or (not s.inside and s.rvol >= 1.2) else 0
        return (GRADE_RANK[self.grade], inside_first, round(self.edge, 2), self.levels.rr, vol,
                -strat.TF_ORDER.get(s.tf, 9))

    def summary(self) -> str:
        s, lv = self.setup, self.levels
        fmt = strat.fmt_factory(lv.entry)
        how = f"on a break {'above' if s.direction == 'long' else 'below'} {fmt(lv.entry)}" \
            if self.entry_type == "stop" else f"now near {fmt(lv.entry)} (just triggered)"
        vol = f", setup-bar volume x{s.rvol:g}" if s.rvol else ""
        return (f"{self.grade}-grade {s.direction} {self.inst.coin}: {strat.TF_NAMES[s.tf]} {s.name} {how}; "
                f"stop {fmt(lv.stop)}, target {fmt(lv.target)} ({lv.target_kind}), hard stop {fmt(lv.hard_stop)}, "
                f"R:R after fees {lv.rr:g}{', inside bar' if s.inside else ''}{vol}. Bias {self.bias}.")


@dataclass
class Read:
    inst: Instrument
    analysis: dict[str, Any] | None
    source: str
    basis: float
    candidates: list[Candidate] = field(default_factory=list)
    pass_reason: str | None = None
    pass_detail: str = ""
    incident: str | None = None


@dataclass
class Scan:
    reads: list[Read]
    ranked: list[Candidate]
    allowed_grades: tuple[str, ...]
    behind_pace: bool

    @property
    def tradeable(self) -> list[Candidate]:
        return [c for c in self.ranked if c.grade in self.allowed_grades]

    @property
    def incidents(self) -> list[str]:
        return [r.incident for r in self.reads if r.incident]

    def pass_summary(self) -> tuple[str, str] | None:
        """The day's market reason when nothing is tradeable: (reason code, plain-English detail)."""
        if self.tradeable:
            return None
        reasons = Counter(r.pass_reason for r in self.reads if r.pass_reason)
        read_ok = [r for r in self.reads if r.analysis is not None]
        if not read_ok:
            return None  # nothing could be read: an incident, not a pass
        if self.ranked:  # there were setups, just not good enough for the grades allowed today
            best = self.ranked[0]
            return ("conflicting_mtf", f"best setup was only grade {best.grade} ({best.summary()}); "
                                       f"today Trader takes grade {'/'.join(self.allowed_grades)} only")
        code, n = reasons.most_common(1)[0] if reasons else ("no_setup_in_window", len(read_ok))
        examples = "; ".join(f"{r.inst.ticker}: {r.pass_detail}" for r in self.reads if r.pass_reason == code)[:400]
        return code, f"{n} of {len(read_ok)} markets — {MARKET_REASONS[code]}. {examples}"


def read_market(inst: Instrument, chart_source: Callable | None = None, perp_source: Callable | None = None) -> Read:
    """The Strat read for one market: the stock's chart first, the perp's own candles if that fails or
    doesn't match the perp's price."""
    problems = []
    try:
        a = strat.analyze(inst.chart, source=chart_source)
        basis = inst.mark / a["price"] if a["price"] else 0
        if inst.mark and abs(basis - 1) <= MAX_BASIS:
            return Read(inst, a, "chart", basis)
        problems.append(f"chart {inst.chart} doesn't match the perp's price (x{basis:.3f})")
    except (DataFeedError, ValueError, KeyError, TypeError) as e:
        problems.append(f"chart {inst.chart}: {e}")
    try:
        a = strat.analyze(inst.coin, source=perp_source or hl.candles)
        return Read(inst, a, "hyperliquid", 1.0)
    except (DataFeedError, ValueError, KeyError, TypeError) as e:
        problems.append(f"perp {inst.coin}: {e}")
    return Read(inst, None, "none", 0, incident=f"couldn't read {inst.coin} ({'; '.join(problems)[:300]})")


def grade_of(a: dict[str, Any], s: strat.Setup, lv: risk.Levels) -> str:
    if lv.problem or not s.with_continuity:
        return "C"
    if a["continuity"]["ftfc"] == ("up" if s.direction == "long" else "down"):
        return "A"
    return "B"


def evaluate(r: Read, j: Journal, price: float | None = None) -> Read:
    """Candidates for one market (or its market-based pass reason)."""
    a = r.analysis
    if a is None:
        return r
    px = (price if price is not None else r.inst.mark) / (r.basis or 1)  # the perp's price on the chart's scale
    setups: list[strat.Setup] = [s for s in a["_setups"] if s.tf in strat.SETUP_TFS]
    live = [s for s in setups if s.status in ("waiting", "triggered")]
    aligned = [s for s in live if s.with_continuity]
    problems: Counter = Counter()
    for s in aligned:
        risk_pts = abs(s.trigger - s.stop)
        if s.status == "triggered":
            beyond = (px - s.trigger) * (1 if s.direction == "long" else -1)
            if beyond > settings.max_chase_r * risk_pts or (px - s.stop) * (1 if s.direction == "long" else -1) <= 0:
                problems["no_chase"] += 1
                continue
            entry, how = px, "market"
        else:
            entry, how = s.trigger, "stop"
        if learning.benched(j, s.family, s.tf):
            continue
        b = r.basis or 1
        lv = risk.levels(s.direction, entry * b, s.stop * b, None if s.target is None else s.target * b,
                         min(settings.leverage, r.inst.max_leverage))
        if lv.problem:
            problems[lv.problem] += 1
            continue
        e, _ = learning.edge(j, s.family, s.tf)
        r.candidates.append(Candidate(r.inst, s, lv, grade_of(a, s, lv), how, e, r.source, b,
                                      strat.thesis(a), a["bias"]))
    if not r.candidates:
        if strat.is_chop(a):
            r.pass_reason, r.pass_detail = "chop", f"W/D bars {_types(a)}, bias {a['bias']}"
        elif problems:
            code = problems.most_common(1)[0][0]
            r.pass_reason, r.pass_detail = code, MARKET_REASONS[code]
        elif live and not aligned:
            r.pass_reason, r.pass_detail = "conflicting_mtf", f"bias {a['bias']} ({a['why']}); setups are against it"
        elif a["bias"] == "neutral":
            r.pass_reason, r.pass_detail = "conflicting_mtf", a["why"]
        else:
            r.pass_reason, r.pass_detail = "no_setup_in_window", f"bias {a['bias']}, bars {_types(a)}"
    return r


def _types(a: dict[str, Any]) -> str:
    return " ".join(f"{tf}:{a['timeframes'][tf]['type']}" for tf in ("W", "D", "4H") if tf in a["timeframes"])


def scan(instruments: list[Instrument], j: Journal, today: date, trades_this_week: int,
         chart_source: Callable | None = None, perp_source: Callable | None = None) -> Scan:
    reads = [evaluate(read_market(i, chart_source, perp_source), j) for i in instruments]
    ranked = sorted((c for r in reads for c in r.candidates), key=lambda c: c.key, reverse=True)
    behind = risk.behind_pace(trades_this_week, today)
    allowed = ("A", "B") if behind else ("A",)
    for r in reads:
        if r.incident:
            log.warning("incident: %s", r.incident)
    return Scan(reads, ranked, allowed, behind)
