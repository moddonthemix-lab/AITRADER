"""Candles with volume, for the Strat read.

Structure comes from the real stock's chart (Yahoo Finance, free, no key): that's the chart the Strat
documents describe, with proper sessions — no weekend or overnight bars making fake inside bars.
If Yahoo can't be reached, Trader falls back to the Hyperliquid perp's own candles. A data problem is
never a reason to pass on a trade: when both sources fail it's an *incident* (see strategy.py).

Timeframes: 60, 4H, D, W, M, Q (4H from hourly; W/M/Q from daily, on calendar periods).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .net import HttpError, get_json


class DataFeedError(Exception):
    """No candles from any source. Never a reason to pass — an incident to fix."""


@dataclass
class Bar:
    t: int        # unix seconds (bar start)
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"t": self.t, "o": self.o, "h": self.h, "l": self.l, "c": self.c, "v": self.v}


INTRADAY = ("60m", "3mo")  # three months of hourly bars — also builds the 4-hour candles
DAILY = ("1d", "10y")
_cache: dict[str, tuple[float, Any]] = {}
CACHE_SECONDS = 60

# A stand-in for tests: (symbol, interval, range) -> (bars, meta)
_fetcher: Callable[[str, str, str], tuple[list[Bar], dict[str, Any]]] | None = None


def set_fetcher(f: Callable[[str, str, str], tuple[list[Bar], dict[str, Any]]] | None) -> None:
    global _fetcher
    _fetcher = f
    _cache.clear()


def _span(interval: str) -> int:
    return {"60m": 3600, "1d": 86400}.get(interval, 86400)


def yahoo(symbol: str, interval: str, rng: str) -> tuple[list[Bar], dict[str, Any]]:
    params = {"interval": interval, "range": rng, "includePrePost": "false"}
    data = None
    for host in ("query1", "query2"):
        try:
            data = get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}", params)
            break
        except HttpError as e:
            if e.status == 404:
                raise DataFeedError(f"no market data for {symbol} — check the ticker") from None
            data = None
    if data is None:
        raise DataFeedError(f"Yahoo unreachable for {symbol}")
    res = ((data or {}).get("chart", {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        raise DataFeedError(f"no market data for {symbol}")
    q = res["indicators"]["quote"][0]
    vols = q.get("volume") or [0] * len(res["timestamp"])
    bars = [Bar(t, o, h, l, c, float(v or 0))
            for t, o, h, l, c, v in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"], vols)
            if None not in (o, h, l, c)]
    # Yahoo sometimes appends a zero-range "last trade" bar after the real latest bar — drop it.
    if len(bars) > 1 and bars[-1].h == bars[-1].l and bars[-1].t - bars[-2].t < _span(interval):
        bars.pop()
    return bars, res.get("meta", {})


def _fetch(symbol: str, interval: str, rng: str) -> tuple[list[Bar], dict[str, Any]]:
    return (_fetcher or yahoo)(symbol, interval, rng)


def candles(symbol: str, tf: str = "D") -> tuple[list[Bar], dict[str, Any]]:
    """Candles for a timeframe: 60, 4H, D, W, M or Q. Cached for a minute."""
    key = f"{symbol}:{tf}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    if tf == "60":
        out = _fetch(symbol, *INTRADAY)
    elif tf == "4H":
        hourly, meta = candles(symbol, "60")
        out = (four_hour(hourly, meta), meta)
    elif tf == "D":
        out = _fetch(symbol, *DAILY)
    elif tf in ("W", "M", "Q"):
        daily, meta = candles(symbol, "D")
        out = (aggregate(daily, tf, meta.get("gmtoffset", 0)), meta)
    else:
        raise ValueError(f"unknown timeframe {tf} (use 60, 4H, D, W, M or Q)")
    _cache[key] = (time.time(), out)
    return out


def four_hour(hourly: list[Bar], meta: dict[str, Any] | None = None) -> list[Bar]:
    """Stock 4-hour candles: each day's session in blocks of four hours from the open (9:30–13:30, 13:30–16:00)."""
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo((meta or {}).get("exchangeTimezoneName") or "America/New_York")
    except Exception:  # noqa: BLE001 — unknown zone name
        tz = ZoneInfo("America/New_York")
    count: dict[Any, int] = {}
    out: list[Bar] = []
    current = None
    for b in hourly:
        d = datetime.fromtimestamp(b.t, tz).date()
        k = (d, count.get(d, 0) // 4)
        count[d] = count.get(d, 0) + 1
        if k != current:
            out.append(Bar(b.t, b.o, b.h, b.l, b.c, b.v))
            current = k
        else:
            last = out[-1]
            last.h, last.l, last.c, last.v = max(last.h, b.h), min(last.l, b.l), b.c, last.v + b.v
    return out


def _period_key(ts: int, tf: str, offset: int) -> tuple:
    d = datetime.fromtimestamp(ts + offset, tz=timezone.utc)
    if tf == "W":
        iso = d.isocalendar()
        return (iso[0], iso[1])
    if tf == "M":
        return (d.year, d.month)
    return (d.year, (d.month - 1) // 3)  # Q


def aggregate(daily: list[Bar], tf: str, offset: int = 0) -> list[Bar]:
    """Weekly (W), monthly (M) or quarterly (Q) candles from daily ones, on calendar periods in the
    exchange's time zone (offset = seconds from UTC)."""
    out: list[Bar] = []
    current = None
    for b in daily:
        key = _period_key(b.t, tf, offset)
        if key != current:
            out.append(Bar(b.t, b.o, b.h, b.l, b.c, b.v))
            current = key
        else:
            p = out[-1]
            p.h, p.l, p.c, p.v = max(p.h, b.h), min(p.l, b.l), b.c, p.v + b.v
    return out


def relative_volume(bars: list[Bar], index: int = -1, lookback: int = 20) -> float | None:
    """This bar's volume vs the average of the `lookback` bars before it (1.0 = normal)."""
    if len(bars) < 3:
        return None
    i = index if index >= 0 else len(bars) + index
    prior = [b.v for b in bars[max(0, i - lookback):i] if b.v]
    if not prior or not bars[i].v:
        return None
    return round(bars[i].v / (sum(prior) / len(prior)), 2)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def days_ago_ms(days: float) -> int:
    return int((utc_now() - timedelta(days=days)).timestamp() * 1000)
