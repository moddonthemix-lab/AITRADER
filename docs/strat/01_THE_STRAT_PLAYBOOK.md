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

## Don't chase

A setup that already triggered is only taken while price is within a quarter of the planned risk past the
trigger (`TRADER_MAX_CHASE_R`). Otherwise wait for the next bar's trigger.
