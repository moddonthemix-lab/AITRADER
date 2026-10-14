"""The money rules, as plain functions (no network, easy to test).

Hard stop: a position is closed before it loses more than TRADER_HARD_STOP_PCT (10%) of the money in it,
    fees included. The Strat stop (the other side of the setup bar) is used when it's tighter; a setup
    whose Strat stop is further away than the hard stop is skipped — its risk doesn't fit the rule.
Take profit: at the Strat target (the previous range's high/low). With no prior level in the way (price
    discovery) the target is a measured move of 2R.
Trailing stop: once the trade is TRADER_TRAIL_ACTIVATE_R in profit (1.5R; R = the setup's entry-to-stop
    distance), the stop jumps to lock in a profit AFTER fees and follows the best price TRADER_TRAIL_R (1R)
    behind. It never moves back, and never below that locked profit — so once it's active, the trade can only
    close green. (TRADER_TRAIL_MODE=pct uses fixed percentages instead.)
Only setups whose net reward after fees is at least TRADER_MIN_RR times the net risk are taken.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from .config import settings
from .hyperliquid import round_px, round_size

MIN_ORDER_USD = 10.0  # Hyperliquid's smallest order


def fee() -> float:
    return settings.fee_pct / 100


def hard_stop_price(entry: float, side: str, leverage: int | None = None) -> float:
    """The furthest a stop may be: losing more than the hard-stop % of the margin (fees included) is impossible."""
    lev = max(1, leverage or settings.leverage)
    move = settings.hard_stop_pct / 100 / lev - 2 * fee()
    return entry * (1 - move) if side == "long" else entry * (1 + move)


def lock_price(entry: float, side: str) -> float:
    """The stop that guarantees a profit after both fees (where the trailing stop starts)."""
    m = 2 * fee() + settings.trail_lock_pct / 100
    return entry * (1 + m) if side == "long" else entry * (1 - m)


def activation_gain() -> float:
    """How far in profit (as a fraction) before the trailing stop switches on; always above the lock."""
    return max(settings.trail_activate_pct / 100, 2 * fee() + settings.trail_lock_pct / 100 + 0.0025)


def net_rr(entry: float, stop: float, target: float) -> float:
    """Reward-to-risk after paying the fee on the way in and out."""
    f = fee()
    risk = abs(entry - stop) + f * (entry + stop)
    reward = abs(target - entry) - f * (entry + target)
    return round(reward / risk, 2) if risk > 0 else 0.0


@dataclass
class Levels:
    entry: float
    stop: float
    target: float
    hard_stop: float
    rr: float
    target_kind: str          # strat | measured
    problem: str | None = None  # why it can't be traded (a market reason)


def levels(side: str, entry: float, strat_stop: float, strat_target: float | None, leverage: int | None = None) -> Levels:
    hard = hard_stop_price(entry, side, leverage)
    beyond = strat_stop < hard if side == "long" else strat_stop > hard
    risk = abs(entry - strat_stop)
    kind = "strat"
    target = strat_target
    if target is None or (target <= entry if side == "long" else target >= entry):
        target = entry + 2 * risk if side == "long" else entry - 2 * risk
        kind = "measured"
    rr = net_rr(entry, strat_stop, target)
    lv = Levels(entry, strat_stop, target, hard, rr, kind)
    if risk <= 0:
        lv.problem = "no_setup_in_window"
    elif beyond:
        lv.problem = "stop_too_wide"
    elif rr < settings.min_rr:
        lv.problem = "rr_after_fees"
    return lv


def size_for(equity: float, entry: float, stop: float, sz_decimals: int, max_leverage: int = 50) -> float:
    """Position size: risk TRADER_RISK_PER_TRADE_PCT of the account between entry and stop (fees in),
    never more than TRADER_MAX_POSITION_PCT of the account (times leverage) in one position."""
    if equity <= 0 or entry <= 0:
        return 0.0
    lev = max(1, min(settings.leverage, max_leverage))
    per_unit = abs(entry - stop) + fee() * (entry + stop)
    by_risk = equity * settings.risk_per_trade_pct / 100 / per_unit if per_unit > 0 else 0
    by_cap = equity * settings.max_position_pct / 100 * lev / entry
    size = round_size(min(by_risk, by_cap), sz_decimals)
    return size if size * entry >= MIN_ORDER_USD else 0.0


@dataclass
class Step:
    stop: float
    peak: float
    trail_active: bool
    exit_reason: str | None   # target | trail | stop | hard_stop | None (hold)
    moved: bool               # the stop changed (move the exchange order too)


def manage(t: dict[str, Any], price: float, sz_decimals: int = 2) -> Step:
    """One check of an open trade at `price`: take profit, stop out, or trail the stop up."""
    side, entry = t["side"], t["entry"]
    d = 1 if side == "long" else -1
    peak = max(t["peak"] or entry, price) if d == 1 else min(t["peak"] or entry, price)
    stop, active = t["stop"], bool(t["trail_active"])

    gain = (peak - entry) / entry * d
    r_unit = abs(entry - (t.get("initial_stop") or t["stop"])) / entry  # the setup's own risk, as a fraction
    if settings.trail_mode == "r" and r_unit > 0:
        start, dist = max(activation_gain(), settings.trail_activate_r * r_unit), settings.trail_r * r_unit
    else:
        start, dist = activation_gain(), settings.trail_pct / 100
    if gain >= start:
        lock = lock_price(entry, side)
        trail = peak * (1 - d * dist)
        candidate = max(lock, trail) if d == 1 else min(lock, trail)
        candidate = round_px(candidate, sz_decimals)
        if (candidate - stop) * d > 0:
            stop = candidate
        active = True
    moved = stop != t["stop"]

    reason = None
    lev = max(1, settings.leverage)
    loss_on_margin = -((price - entry) / entry * d * lev - 2 * fee() * lev)
    if loss_on_margin >= settings.hard_stop_pct / 100 or (price - t["hard_stop"]) * d <= 0:
        reason = "hard_stop"  # the boss's -10% rule, checked on the real loss — whatever the stop says
    elif t.get("target") is not None and (price - t["target"]) * d >= 0:
        reason = "target"
    elif (price - stop) * d <= 0:
        reason = "trail" if active else ("hard_stop" if abs(stop - t["hard_stop"]) < 1e-9 else "stop")
    return Step(stop, peak, active, reason, moved)


# ------------------------------------------------------------------ the weekly minimum
def expected_by(day: date) -> int:
    """Trades Trader should have by this weekday to stay on pace for the weekly minimum
    (2/week: Mon–Tue 0, Wed–Thu 1, Fri 2)."""
    wd = min(day.weekday(), 4)
    return math.floor(settings.min_trades_week * wd / 4)


def behind_pace(trades_this_week: int, day: date) -> bool:
    return trades_this_week < expected_by(day)
