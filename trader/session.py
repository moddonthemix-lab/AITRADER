"""Which bars are finished, by the US stock session clock (New York, 9:30–16:00 on weekdays).

The Strat code reads the newest bar as the live one. That's right while the market is open, but
before the open and after the close the newest bar is FINISHED. Then Trader adds a placeholder for the
next bar (flat at the last close, marked `synthetic`), so the finished bar becomes the setup bar —
exactly how a Strat trader reads a chart at night: "today was an inside day; tomorrow it triggers on
a break of its high or low". A timeframe whose next bar hasn't opened yet doesn't count toward
continuity (its direction is decided at the open).

    daily, 4-hour, 60-minute   finished whenever the market is closed
    weekly                     finished from Friday's close until Monday's open
    monthly / quarterly        finished from the last session of the month / quarter until the next opens
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable

from .config import is_crypto, settings
from .journal import NY
from .market import Bar, candles

OPEN, CLOSE = dtime(9, 30), dtime(16, 0)
INTRADAY = ("60", "4H")
FRESH_DAYS = 5  # only touch data that's current (old test data / delisted tickers are left alone)


def is_open(now: datetime) -> bool:
    n = now.astimezone(NY)
    return n.weekday() < 5 and OPEN <= n.time() < CLOSE


def next_session(now: datetime) -> date:
    """The date of the next (or current) session's open."""
    n = now.astimezone(NY)
    d = n.date()
    if n.weekday() < 5 and n.time() < CLOSE:
        return d
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _day(b: Bar) -> date:
    return datetime.fromtimestamp(b.t, NY).date()


def _period(d: date, tf: str) -> tuple:
    if tf == "W":
        iso = d.isocalendar()
        return (iso[0], iso[1])
    if tf == "M":
        return (d.year, d.month)
    if tf == "Q":
        return (d.year, (d.month - 1) // 3)
    return (d,)


def _placeholder(last: Bar, step: int) -> Bar:
    return Bar(last.t + step, last.c, last.c, last.c, last.c, 0.0, synthetic=True)


STEP = {"60": 3600, "4H": 14400, "D": 86400, "W": 7 * 86400, "M": 31 * 86400, "Q": 92 * 86400}


def prepare(bars: list[Bar], tf: str, now: datetime) -> list[Bar]:
    """These bars, read the way a trader reads them at `now` (see the module notes)."""
    if len(bars) < 3:
        return bars
    n = now.astimezone(NY)
    last_day = _day(bars[-1])
    if (n.date() - last_day).days > FRESH_DAYS + 31 * (tf in ("M", "Q")) + 7 * (tf == "W"):
        return bars  # old data: nothing to say about which bar is live
    nxt = next_session(n)
    out = list(bars)
    if not is_open(n):
        # Before the open, drop any placeholder bar a data source made for today's not-yet-open session.
        if tf in INTRADAY or tf == "D":
            while out and _day(out[-1]) >= nxt and n.date() == nxt:
                out.pop()
        elif out and _day(out[-1]) >= nxt and _day(out[-1]) == n.date():
            out.pop()  # a weekly/monthly bar that only exists because of today's placeholder day
        if len(out) < 3:
            return bars
        if tf in INTRADAY or _period(_day(out[-1]), tf) != _period(nxt, tf):
            out.append(_placeholder(out[-1], STEP[tf]))
    elif tf == "D" and _day(out[-1]) < n.date():
        out.append(_placeholder(out[-1], STEP[tf]))  # first minutes of the session: today's bar isn't in yet
    return out


def session_source(base: Callable[[str, str], tuple[list[Bar], dict[str, Any]]] | None,
                   now: datetime) -> Callable[[str, str], tuple[list[Bar], dict[str, Any]]]:
    """Wrap a candle source so every timeframe is read correctly for `now`."""
    get = base or candles

    def src(symbol: str, tf: str) -> tuple[list[Bar], dict[str, Any]]:
        bars, meta = get(symbol, tf)
        if is_crypto(symbol):  # crypto trades around the clock: its days are UTC days, not the NY session
            return bars, meta
        return prepare(bars, tf, now), meta
    return src
