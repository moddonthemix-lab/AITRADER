"""The money rules, as plain functions (no network, easy to test).

Hard stop: a position is closed before its price moves more than TRADER_HARD_STOP_PCT (23%) against it,
    fees included — measured on price, so it means the same at any leverage.
Account use: the margin of all open positions together stays within TRADER_ACCOUNT_USE_PCT (75%) of the
    account; the other 25% is always a free cushion.
Leverage: always each market's full maximum (TRADER_LEVERAGE=max: CBRS 10x, SP500 50x). When the Strat stop
    is beyond the liquidation price at that leverage, the stop moves inside it (TRADER_LIQ_STOP_AT, 80% of the
    way), so Trader's own stop closes the trade, never a liquidation. Leverage doesn't change the dollar risk
    (TRADER_RISK_PER_TRADE_PCT); it changes how little margin each position ties up. The Strat stop (the other side of the setup bar) is used when it's tighter; a setup
    whose Strat stop is further away than the hard stop is skipped — its risk doesn't fit the rule.
Take profit: at the Strat target (the previous range's high/low). With no prior level in the way (price
    discovery) the target is a measured move of 2R.
Trailing stop: on as soon as the trade is in profit after fees (TRADER_TRAIL_START_PCT past the round-trip
    fees). The stop jumps to lock in the fees plus TRADER_TRAIL_LOCK_PCT — a guaranteed green close — then
    follows the best price TRADER_TRAIL_R (1R) behind. It never moves back.
Only setups whose net reward after fees is at least TRADER_MIN_RR times the net risk are taken.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import settings
from .hyperliquid import round_px, round_size

MIN_ORDER_USD = 10.0  # Hyperliquid's smallest order


def fee() -> float:
    return settings.fee_pct / 100


def hard_stop_price(entry: float, side: str) -> float:
    """The furthest a stop may be: the position's price can't go more than the hard-stop % against it
    (fees included). Measured on price, so it means the same at any leverage."""
    move = settings.hard_stop_pct / 100 - 2 * fee()
    return entry * (1 - move) if side == "long" else entry * (1 + move)


def maintenance(max_leverage: int) -> float:
    """Hyperliquid's maintenance margin rate: half the initial margin at the market's maximum leverage."""
    return 1 / (2 * max(1, max_leverage))


def liquidation_distance(leverage: int, max_leverage: int) -> float:
    """How far price can move against an isolated position at this leverage before it's liquidated."""
    return 1 / max(1, leverage) - maintenance(max_leverage)


def leverage_for(max_leverage: int) -> int:
    """The market's full maximum leverage (TRADER_LEVERAGE=max), or the fixed number set, if lower."""
    return max(1, min(settings.leverage, max_leverage))


def liquidation_guard(entry: float, side: str, max_leverage: int) -> float:
    """The furthest a stop may be at this leverage: TRADER_LIQ_STOP_AT (80%) of the way to the liquidation
    price, so Trader's own stop order always closes the trade before a liquidation would."""
    dist = liquidation_distance(leverage_for(max_leverage), max_leverage) * settings.liq_stop_at
    return entry * (1 - dist) if side == "long" else entry * (1 + dist)


def effective_stop(entry: float, side: str, strat_stop: float, max_leverage: int) -> tuple[float, str]:
    """The Strat stop, or — when it's beyond the liquidation price at full leverage — the liquidation guard.
    (The -23% hard stop is always further than both at any leverage above ~2x, and is checked separately.)"""
    guard = liquidation_guard(entry, side, max_leverage)
    tighter = max(strat_stop, guard) if side == "long" else min(strat_stop, guard)
    return tighter, ("strat" if tighter == strat_stop else "liquidation guard")


def lock_price(entry: float, side: str) -> float:
    """The stop that guarantees a profit after both fees (where the trailing stop starts)."""
    m = 2 * fee() + settings.trail_lock_pct / 100
    return entry * (1 + m) if side == "long" else entry * (1 - m)


def activation_gain() -> float:
    """In profit after fees: the gain (as a fraction) at which the trailing stop switches on, always above the
    lock so the new stop sits below the price."""
    return 2 * fee() + max(settings.trail_start_pct, settings.trail_lock_pct + 0.05) / 100


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
    stop_kind: str = "strat"  # strat | liquidation guard


def levels(side: str, entry: float, strat_stop: float, strat_target: float | None, max_leverage: int = 1) -> Levels:
    hard = hard_stop_price(entry, side)
    beyond = strat_stop < hard if side == "long" else strat_stop > hard
    stop, stop_kind = effective_stop(entry, side, strat_stop, max_leverage) if max_leverage > 1 else (strat_stop, "strat")
    if stop_kind != "strat":
        beyond = False  # the guard is tighter than the hard stop
    strat_stop = stop
    risk = abs(entry - strat_stop)
    kind = "strat"
    target = strat_target
    if target is None or (target <= entry if side == "long" else target >= entry):
        target = entry + 2 * risk if side == "long" else entry - 2 * risk
        kind = "measured"
    rr = net_rr(entry, strat_stop, target)
    lv = Levels(entry, strat_stop, target, hard, rr, kind, stop_kind=stop_kind)
    if risk <= 0:
        lv.problem = "no_setup_in_window"
    elif beyond:
        lv.problem = "stop_too_wide"
    elif rr < settings.min_rr:
        lv.problem = "rr_after_fees"
    return lv


def trading_capital(equity: float) -> float:
    """The part of the account Trader trades with (TRADER_ACCOUNT_USE_PCT, 75%)."""
    return equity * settings.account_use_pct / 100


def size_for(equity: float, entry: float, stop: float, sz_decimals: int, leverage: int = 1,
             margin_in_use: float = 0.0) -> float:
    """Position size: risk TRADER_RISK_PER_TRADE_PCT of the trading capital between entry and stop (fees in),
    and never more margin (value ÷ leverage) than what's still free of the 75% Trader may use, counting the
    positions already open."""
    if equity <= 0 or entry <= 0:
        return 0.0
    lev = max(1, leverage)
    capital = trading_capital(equity)
    per_unit = abs(entry - stop) + fee() * (entry + stop)
    by_risk = capital * settings.risk_per_trade_pct / 100 / per_unit if per_unit > 0 else 0
    by_cap = max(0.0, capital - margin_in_use) * lev / entry
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
    start = max(activation_gain(), settings.trail_activate_r * r_unit)
    dist = settings.trail_r * r_unit if r_unit > 0 else 0.01
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
    loss = -((price - entry) / entry * d - 2 * fee())  # on the position's price, fees in
    if loss >= settings.hard_stop_pct / 100 or (price - t["hard_stop"]) * d <= 0:
        reason = "hard_stop"  # the boss's -23% rule, checked on the real loss — whatever the stop says
    elif t.get("target") is not None and (price - t["target"]) * d >= 0:
        reason = "target"
    elif (price - stop) * d <= 0:
        reason = "trail" if active else ("hard_stop" if abs(stop - t["hard_stop"]) < 1e-9 else "stop")
    return Step(stop, peak, active, reason, moved)


