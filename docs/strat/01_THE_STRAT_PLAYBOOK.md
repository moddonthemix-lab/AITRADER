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

## Entries, stops and targets — as thestrat.ai/docs states them

Trader trades these exactly; the quotes are the docs' own words. (The full pages are saved word for word in
his Strat documents: `trader docs add-thestrat`.)

| Signal | Trigger | Stop | Target (magnitude) |
|---|---|---|---|
| 2-1-2 reversal | "the inside bar's extreme, reversal side" | "the other side of the 1" | "the first candle's extreme" |
| 2-1-2 continuation | "the inside bar's break, trend side" | "the other side of the 1" | "the leg before the pause, projected forward" (measured move) |
| 3-1-2 | "the extreme of the inside bar" | "the other side of the 1, one penny under the inside bar's low (bullish) or over its high (bearish)" | "the far side of the 3" |
| 2-2 reversal | "the first 2's extreme, on the reversal side" | "one penny beyond the low (bullish) or high (bearish) of the entry bar" | "the bar before the pattern" |
| 1-2-2 (rev strat) | "the failed-break bar's extreme" | "one penny under the entry bar's low" | "the candle before the inside bar" |
| 3-2-2 | "the middle 2's extreme" | "one penny beyond the low (bullish) or high (bearish) of the entry bar" | "the 3's extreme" |
| Hammer / shooter | "the hammer's high" / "the shooter's low" | "one penny under the entry bar's low, not the hammer's wick" | "the prior candle's high" / "low" |
| 3-2 | "the opposite side of the outside bar" (reversal) | entry bar | "carries no magnitude of its own" — the next prior level |
| 2-2 momentum stack | the 2's extreme | "one penny beyond the entry bar's low (or high)" | "First objective is the prior extreme" |

- **Entry: "The break is the confirmation."** Trader enters the moment price trades through the trigger —
  the bar's high or low, wicks included. (`TRADER_ENTRY_CONFIRM=5m` waits for a 5-minute close instead.)
- **Hammer:** "a large lower wick whose body closes in the top 33% of its range". **Shooter:** "a large upper
  wick that opens and closes in the bottom 33% of its range".
- **An equal high or low is still a 1:** "Matching an extreme without exceeding it 'didn't break the goal
  line'"; "exceeding an extreme by even two cents fully qualifies a 2".
- **Entry hours: 9:30–15:45 New York.** Scanning starts at the 9:30 open.
- **The opposite side first cancels it.** If price breaks the stop side before the trigger, the plan is
  cancelled. "A signal is only in force while its triggering bar is open."
- **Your documents have a say.** Before a plan is armed, Trader checks it against your Strat documents.

## The game plan (before the open and after the close)

- **After the close (16:10):** the day's bars are finished. Trader lists the inside bars that just formed
  (daily, 4-hour, and weekly once the week is done), with both triggers, the stops, the targets, and the side
  the higher timeframes (Q, M, W) favour.
- **Before the open (9:00):** the same read again, fresh for the bell.
- A timeframe whose next bar hasn't opened yet has no direction yet. Its continuity is decided at the open,
  and the trade is taken only if continuity agrees at the moment of the trigger.

## Don't chase

A setup that already triggered is only taken while price is within a quarter of the planned risk past the
trigger (`TRADER_MAX_CHASE_R`). Otherwise wait for the next bar's trigger.
