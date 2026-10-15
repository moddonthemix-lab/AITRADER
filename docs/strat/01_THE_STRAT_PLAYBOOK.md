# The Strat — Trader's playbook

Forked from AMO's Strat rules (AMOAI `amo/strat.py`), which follow Rob Smith's The Strat: thestrat.ai's
*3 Universal Truths* and thestrat-indicators.com. Trader's code (`trader/strat.py`, `trader/strategy.py`,
`trader/risk.py`) is this document in code. Every number is calculated from candles, never guessed.

## The three universal truths

1. **Every bar is a 1, a 2 or a 3** — compared to the bar before it.
   - **1 (inside bar):** takes out neither the previous high nor the previous low. Consolidation, a coil.
   - **2U / 2D (directional bar):** takes out one side only. 2U breaks the previous high, 2D the previous low.
   - **3 (outside bar):** takes out both sides. Broadening, price discovery.
   - *The break decides the number, never the colour.* A red 2U is still a 2U.
2. **Price trades in the direction of the most 2s across timeframes.** Full Timeframe Continuity (FTFC):
   when price is above the open of every timeframe (Quarterly, Monthly, Weekly, Daily, 4-hour, 60-minute)
   continuity is **up**; below every open, **down**. Trade with it, not against it.
3. **Price discovery is a broadening formation.** 3s expand the range; previous highs and lows are the
   magnets (targets).

## Continuity and bias

- **FTFC up / down:** every timeframe's current bar is green / red relative to its open. Strongest.
- **Leaning:** most timeframes agree. Weaker; the higher timeframes against the trade are a warning.
- **Lower-timeframe override:** when the Daily and the 60-minute agree against the higher timeframes,
  the lower timeframes are in control for now — a lower-timeframe play, kept tight.
- **Neutral:** timeframes split. No continuity, no trade.

## Setups (trigger = break of the last completed bar)

Every setup is built on the **last completed bar**. It **triggers** when the live bar breaks that bar's
high (long) or low (short). The **stop** is the other side of that bar. The **target** is the previous
range — the nearest prior high (long) or low (short) beyond the trigger.

| Pattern | Bars | Meaning |
|---|---|---|
| **3-1-2** | outside, inside, break | Broadening then a coil; the break of the inside bar decides direction. |
| **2-1-2 continuation** | 2U-1-2U / 2D-1-2D | A pause (inside bar) in a move, then the move continues. |
| **2-1-2 reversal** | 2D-1-2U / 2U-1-2D | A pause that resolves the other way. |
| **1-1-2** | inside, inside, break | Compound inside bar — an even tighter coil. |
| **Rev Strat (1-2-2)** | inside, 2, then the opposite 2 | A failed break of an inside bar, reversing. |
| **2-2 reversal** | 2D then 2U (or 2U then 2D) | Failed direction, reverse back through the range. |
| **2-2 continuation** | 2U-2U / 2D-2D | Trend continuation. |
| **3-2 / 3-2-2** | outside, then a 2 (and a reversal 2) | Direction picked after a broadening bar. |
| **Hammer / shooter** | long lower / upper wick | Rejection candle; adds weight to a long / short trigger. |

*"Take reversals back through a previous range, in the direction of FTFC."*

## Trader's priorities

1. **Inside bars first** — 3-1-2, 2-1-2 and 1-1-2. An inside bar is a coiled spring with a tight,
   well-defined stop (the other side of the inside bar), so the reward:risk is usually best. Trader's
   backtests agree: on NVDA and SPY daily charts since 2016, inside-bar setups earned clearly more per trade
   than the other patterns.
2. **With continuity.** A-grade = FTFC in the trade's direction. B-grade = the bias leans that way.
   C-grade (against continuity, or the numbers don't work) is never traded.
3. **Reward:risk after fees** at least 1.5 to 1 (`TRADER_MIN_RR`).
4. **Volume is a tie-breaker, never the reason.** An inside bar on *lighter* volume than usual is a quiet
   coil (good); a breakout bar on *heavier* volume (1.2× its 20-bar average or more) shows participation.
5. **Bigger timeframe wins ties** (Weekly over Daily over 4-hour). Monthly setups are watched for
   context but are too wide for the -10% hard stop.

## Entries: when and how

- **The trigger is the Strat trigger:** the break of the setup bar's high (long) or low (short).
- **Confirmation: a 5-minute close.** A poke through the trigger is not an entry. Trader enters only when a
  5-minute candle (on the market he trades) **closes beyond the trigger**. A wick through that closes back
  inside is ignored.
- **Entry hours: 9:40–15:45 New York.** The scan starts at the 9:30 open, but the first entry can only come
  at 9:40, when the second 5-minute candle closes. No new entries after 15:45.
- **The opposite side first cancels it.** If price breaks the stop side before the trigger confirms, the
  plan is cancelled. That's a different setup now.
- **Your documents have a say.** Before a plan is armed, Trader checks it against the Strat documents you
  uploaded. If they rule the setup out, he skips it and quotes the rule.

## The game plan (before the open and after the close)

- **After the close (16:10):** the day's bars are finished. Trader lists the inside bars that just formed
  (daily, 4-hour, and weekly once the week is done), with both triggers, the stops, the targets, and the side
  the higher timeframes (Q, M, W) favour.
- **Before the open (9:00):** the same read again, fresh for the bell.
- A timeframe whose next bar hasn't opened yet has no direction yet. Its continuity is decided at the open,
  and the trade is taken only if continuity agrees at the moment of the 5-minute confirmation.

## Don't chase

A setup that already triggered is only taken while price is within a quarter of the planned risk past the
trigger (`TRADER_MAX_CHASE_R`). Otherwise wait for the next bar's trigger.
