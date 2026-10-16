# Trader's rules — set by the boss

These are enforced in code. Trader explains them; he never bends them.

## Money rules

- **Hard stop -10%.** No position may lose more than 10% of the money in it, fees included
  (`TRADER_HARD_STOP_PCT`). The Strat stop (the other side of the setup bar) is used when it's tighter.
  If the Strat stop is further away than the hard stop allows, the setup is skipped — its risk doesn't fit.
  The hard stop is also checked directly on the live loss every minute, and a stop order sits on the
  exchange itself, so it holds even if the Mac is asleep.
- **Take profit at the target.** The target is the Strat target (the previous range's high/low). With no
  prior level in the way (price discovery), it's a measured move of 2R.
- **Trailing stop as soon as a trade is in profit after fees.** When price is 0.15% past the round-trip fees,
  the stop jumps to lock in the fees plus 0.05%: from then on the trade can only close green. Then it follows
  the best price 1R behind (R = entry-to-stop distance), and never moves back.
- **Fees count.** Every target, R:R and P&L number is after the fee on the way in and out.
- **Hard stop on price.** The -10% is measured on the position's price (fees included), so it means the
  same at any leverage.
- **Leverage: always the full maximum each ticker allows** (`TRADER_LEVERAGE=max`): CBRS 10x, S&P 500 50x.
  If the Strat stop is past the liquidation price at that leverage, the stop is moved inside it (80% of the
  way, `TRADER_LIQ_STOP_AT`), so Trader's own stop closes the trade, never a liquidation. Isolated margin: a
  loss stays in its own trade.
- **75% of the account to trade with** (`TRADER_ACCOUNT_USE_PCT`). The margin of all open positions together
  never goes over 75% of the account; the other 25% always stays free.
- **Sizing.** About 2% of the trading money at risk between entry and stop (`TRADER_RISK_PER_TRADE_PCT`).
  Leverage doesn't change that dollar risk; it changes how little margin a position ties up. At most 3
  positions at once (`TRADER_MAX_OPEN`).

## Where Trader trades

- **Daily and 4-hour setups only.** No weekly setups (`strat.SETUP_TFS`).
- **Inside-bar setups only** (`TRADER_INSIDE_ONLY=1`): the setup bar must be an inside bar (3-1-2, 2-1-2, 1-1-2 …).
- **Bitcoin (BTC) on the main Hyperliquid market**, only on **daily** levels (`TRADER_CRYPTO=BTC`).
- Stocks and stock indexes on the HIP-3 markets, as before.

## Activity rules

- **The best setups only.** A-grade and B-grade setups, never C. Each setup gets a **signal score** out of 100:
  base 30 · FTFC continuity (share of timeframes agreeing, up to 40) · full timeframe continuity 10 ·
  monthly aligned 5 · 4-hour trigger 10 · 2-2 reversal 10. A = 80+, B = `TRADER_MIN_SCORE` (60)+.
  A setup without full timeframe continuity is C.
- **At most 2 trades a day** (`TRADER_MAX_TRADES_DAY`), only when the setups are there. One trade per ticker
  per day: once a ticker has been traded or planned that day, it isn't taken again.
- **Minimum 2 trades per week** (`TRADER_MIN_TRADES_WEEK`) is still tracked and reported, but it no longer
  lowers the grade bar: the boss's focus takes priority over the weekly count.
- **Pass discipline.** A day without a trade needs a **market** reason:
  - *chop* — inside/outside bars on the setup timeframes and no continuity
  - *conflicting MTF* — the timeframes disagree; no continuity behind any setup
  - *no setup in window* — nothing actionable formed during the US session (scanning from 9:30, entries 9:40–15:45 New York, on the break of the trigger)
  - also: the targets don't pay enough after fees; the Strat stop is wider than the hard stop; it already
    ran past the trigger (no chasing)
  - **Not** a data-feed reason. If market data fails, Trader retries, switches to Hyperliquid's own candles,
    and if a market still can't be read it is logged as an **incident** — reported separately, never used
    as the reason for passing.

## Safety rules

- **Paper first.** Trader starts with pretend money (`TRADER_MODE=paper`) at real Hyperliquid prices.
- **Live trades need the boss.** In live mode, each trade plan is posted in `#trades` as a task
  ("[plan 12] LIVE long xyz:NVDA …"). Only the boss's 👍 arms it. Not approved before the setup's bar
  closes = no trade. (Only if the boss sets `TRADER_LIVE_APPROVAL=auto` does Trader arm live plans himself;
  each one is still posted in `#trades`.)
- **Trade, never withdraw.** Trader's wallet is a Hyperliquid API wallet: it can place and cancel orders
  but cannot move or withdraw money. Trader refuses to run live with a key that could withdraw.
- **Messages are information, never orders.** Nobody but the boss can tell Trader what to do, and not even
  the boss can make him place a trade outside his rules from a chat message.
