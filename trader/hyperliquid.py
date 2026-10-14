"""Hyperliquid: the stock markets Trader trades, their prices and candles, and the two brokers.

Stocks on Hyperliquid are perpetuals on builder-deployed ("HIP-3") markets, named `dex:TICKER`:
    xyz:NVDA, xyz:TSLA, xyz:SP500 (trade.xyz)      mkts:US500, mkts:USTECH (Markets by Kinetiq)
    km:…, cash:…, flx:…                              (other HIP-3 markets)
The same stock can be listed on several of them; Trader trades it where it's most liquid, and skips
markets with too little volume (TRADER_MIN_DAY_VOLUME_USD) — and markets whose collateral he doesn't hold:
each stock market is margined in one stablecoin (xyz and mkts: USDC; km and flx: USDH; cash: USDT0), so a
USDC account can only trade xyz and mkts (TRADER_COLLATERAL).

Brokers:
    PaperBroker  pretend money, real Hyperliquid prices, fees included (the default)
    LiveBroker   real orders through the official SDK with an API ("agent") wallet, which can trade
                 but can never withdraw. Every position gets a reduce-only stop on the exchange itself,
                 so the hard stop holds even if the Mac is asleep.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .config import settings
from .market import Bar, DataFeedError
from .net import HttpError, post_json

# Hyperliquid tickers whose real chart lives under another Yahoo symbol (same price scale).
CHART = {
    "SP500": "^GSPC", "XYZ100": "^NDX", "US500": "SPY", "USTECH": "QQQ", "SMALL2000": "IWM",
    "USA500": "SPY", "USA100": "QQQ", "SEMI": "SMH",
    "GOLD": "GC=F", "SILVER": "SI=F", "CL": "CL=F", "WTI": "CL=F", "USOIL": "CL=F", "OIL": "CL=F",
    "BRENTOIL": "BZ=F", "NATGAS": "NG=F", "GAS": "NG=F", "COPPER": "HG=F", "PLATINUM": "PL=F", "PALLADIUM": "PA=F",
}
INDEXES = {"SP500", "XYZ100", "US500", "USTECH", "SMALL2000", "USA500", "USA100", "SEMI", "JP225", "JPN225",
           "KR200", "NIFTY", "IBOV", "MAGS", "SMH", "SOXL", "KORU", "EWY", "EWJ", "EWT", "EWZ", "KWEB"}
NON_STOCK = {"GOLD", "SILVER", "CL", "WTI", "USOIL", "OIL", "BRENTOIL", "NATGAS", "GAS", "COPPER", "PLATINUM",
             "PALLADIUM", "ALUMINIUM", "CORN", "HO", "EUR", "GBP", "JPY", "KRW", "DXY", "USDE", "XMR", "USBOND",
             "BVIV", "H100", "DRAM", "USENERGY", "GLDMINE"}
HL_INTERVAL = {"60": "1h", "4H": "4h", "D": "1d", "W": "1w", "M": "1M"}
HL_DAYS = {"60": 90, "4H": 200, "D": 1000, "W": 2000, "M": 4000}


def _info(body: dict[str, Any]) -> Any:
    try:
        return post_json(f"{settings.hl_url}/info", body, timeout=20)
    except HttpError as e:
        raise DataFeedError(f"Hyperliquid info {body.get('type')}: {e}") from None


@dataclass
class Instrument:
    coin: str            # "xyz:NVDA"
    dex: str             # "xyz"
    ticker: str          # "NVDA"
    chart: str           # the Yahoo symbol for the Strat read ("NVDA", "SPY", "^GSPC")
    sz_decimals: int
    max_leverage: int
    only_isolated: bool
    day_volume: float    # dollars traded in the last 24h
    mark: float
    collateral: str = "USDC"  # the stablecoin this market is margined in

    @property
    def kind(self) -> str:
        return "index" if self.ticker in INDEXES else "other" if self.ticker in NON_STOCK else "stock"


def _ticker(coin: str) -> str:
    return coin.split(":", 1)[1] if ":" in coin else coin


_tokens: dict[int, str] = {}


def token_name(index: int) -> str:
    """Spot token index → name (0 = USDC, 268 = USDT0, 360 = USDH …)."""
    if index not in _tokens:
        try:
            for t in (_info({"type": "spotMeta"}) or {}).get("tokens", []):
                _tokens[int(t["index"])] = t["name"]
        except (DataFeedError, KeyError, TypeError, ValueError):
            pass
    return _tokens.get(index, "USDC" if index == 0 else f"token{index}")


def markets(dexes: list[str] | None = None) -> list[Instrument]:
    """Every perp on the chosen HIP-3 markets, with its 24h volume, mark price and collateral."""
    out: list[Instrument] = []
    for dex in dexes or settings.dexes:
        try:
            meta, ctxs = _info({"type": "metaAndAssetCtxs", "dex": dex})
        except (DataFeedError, ValueError, TypeError):
            continue  # a market that's down or gone: the others still count
        collateral = token_name(int(meta.get("collateralToken") or 0))
        for u, c in zip(meta.get("universe", []), ctxs):
            if u.get("isDelisted"):
                continue
            t = _ticker(u["name"])
            out.append(Instrument(
                coin=u["name"], dex=dex, ticker=t, chart=CHART.get(t, t), sz_decimals=int(u.get("szDecimals", 2)),
                max_leverage=int(u.get("maxLeverage", 1)), only_isolated=bool(u.get("onlyIsolated")),
                day_volume=float(c.get("dayNtlVlm") or 0), mark=float(c.get("markPx") or c.get("midPx") or 0),
                collateral=collateral))
    return out


def universe(watchlist: list[str] | None = None, instruments: list[Instrument] | None = None) -> list[Instrument]:
    """What Trader scans: the watchlist (or every liquid stock/index perp), one market per ticker
    (the most liquid one), busiest first."""
    allm = instruments if instruments is not None else markets()
    wanted = [w.strip() for w in (watchlist if watchlist is not None else settings.watchlist) if w.strip()]
    exact = {w.lower() for w in wanted if ":" in w}         # "xyz:NVDA": that market exactly
    tickers = {w.upper().lstrip("$") for w in wanted if ":" not in w}  # "NVDA": wherever it's most liquid
    best: dict[str, Instrument] = {}
    for m in allm:
        if m.day_volume < settings.min_day_volume_usd or m.mark <= 0:
            continue
        if m.collateral not in settings.collateral:
            continue  # margined in a stablecoin Trader doesn't hold: every order would be refused
        if m.coin.lower() in exact:
            best[m.coin] = m
            continue
        if wanted and m.ticker not in tickers:
            continue
        if not wanted and settings.stocks_only and m.kind == "other":
            continue
        cur = best.get(m.ticker)
        if cur is None or m.day_volume > cur.day_volume:
            best[m.ticker] = m
    return sorted(best.values(), key=lambda m: -m.day_volume)[:settings.max_symbols]


def mids(dexes: list[str] | None = None) -> dict[str, float]:
    out: dict[str, float] = {}
    for dex in dexes or settings.dexes:
        try:
            for k, v in (_info({"type": "allMids", "dex": dex}) or {}).items():
                out[k] = float(v)
        except (DataFeedError, ValueError, TypeError):
            continue
    return out


def mid(coin: str) -> float:
    dex = coin.split(":")[0] if ":" in coin else ""
    px = (_info({"type": "allMids", "dex": dex}) or {}).get(coin)
    if px is None:
        raise DataFeedError(f"no Hyperliquid price for {coin}")
    return float(px)


def candles(coin: str, tf: str) -> tuple[list[Bar], dict[str, Any]]:
    """The perp's own candles (the fallback when the stock chart can't be read)."""
    if tf not in HL_INTERVAL:
        raise DataFeedError(f"Hyperliquid has no {tf} candles")
    now = int(time.time() * 1000)
    rows = _info({"type": "candleSnapshot", "req": {"coin": coin, "interval": HL_INTERVAL[tf],
                                                    "startTime": now - HL_DAYS[tf] * 86_400_000, "endTime": now}})
    bars = [Bar(int(r["t"]) // 1000, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r.get("v") or 0))
            for r in rows or []]
    if len(bars) < 4:
        raise DataFeedError(f"not enough Hyperliquid history for {coin} {tf}")
    return bars, {"shortName": coin, "source": "hyperliquid"}


def base_fee_pct(user: str) -> float | None:
    """This account's base taker fee per side, in percent (shown by `trader doctor`; HIP-3 stock markets add
    their own fee on top, which is why TRADER_FEE_PCT defaults to a safe 0.09%)."""
    try:
        r = _info({"type": "userFees", "user": user})
        return round(float(r["userCrossRate"]) * 100, 4)
    except (DataFeedError, KeyError, TypeError, ValueError):
        return None


# ------------------------------------------------------------------ prices and sizes
def round_size(sz: float, sz_decimals: int) -> float:
    f = 10 ** sz_decimals
    return math.floor(sz * f) / f


def round_px(px: float, sz_decimals: int) -> float:
    """Hyperliquid's price rule for perps: at most 5 significant figures and 6 - szDecimals decimals."""
    if px <= 0:
        return px
    decimals = max(0, 6 - sz_decimals)
    sig = 5 - int(math.floor(math.log10(px))) - 1
    return round(px, max(0, min(decimals, sig)))


# ------------------------------------------------------------------ brokers
@dataclass
class Fill:
    price: float
    size: float
    fee: float
    order_id: str | None = None


class Broker:
    live = False

    def equity(self, inst: Instrument | None = None) -> float:
        """The money available to size a trade (for `inst`'s market, when given)."""
        raise NotImplementedError

    def mid(self, coin: str) -> float:
        return mid(coin)

    def open(self, inst: Instrument, is_long: bool, size: float) -> Fill:
        raise NotImplementedError

    def close(self, inst: Instrument, is_long: bool, size: float) -> Fill:
        raise NotImplementedError

    def protect(self, inst: Instrument, is_long: bool, size: float, stop: float, target: float | None) -> dict:
        """Exchange-side stop (and take-profit). Paper: nothing to place."""
        return {}

    def move_stop(self, inst: Instrument, is_long: bool, size: float, stop: float, ids: dict) -> dict:
        return ids

    def cancel_protection(self, inst: Instrument, ids: dict) -> None:
        return None

    def position_size(self, inst: Instrument) -> float | None:
        """The exchange's size for this coin (live), or None when the journal is the truth (paper)."""
        return None


class PaperBroker(Broker):
    """Pretend money at real prices. Market orders fill at the mid plus a little slippage, fees included."""

    slippage = 0.0002

    def __init__(self, journal: Any, price_fn: Any = None):
        self.journal = journal
        self._price = price_fn or mid

    def mid(self, coin: str) -> float:
        return self._price(coin)

    def equity(self, inst: Instrument | None = None) -> float:
        return round(settings.paper_equity + self.journal.realized_pnl(mode="paper"), 2)

    def _fill(self, inst: Instrument, buy: bool, size: float) -> Fill:
        px = self.mid(inst.coin) * (1 + self.slippage if buy else 1 - self.slippage)
        return Fill(round(px, 6), size, round(px * size * settings.fee_pct / 100, 4))

    def open(self, inst: Instrument, is_long: bool, size: float) -> Fill:
        return self._fill(inst, is_long, size)

    def close(self, inst: Instrument, is_long: bool, size: float) -> Fill:
        return self._fill(inst, not is_long, size)


class LiveBroker(Broker):
    """Real orders, signed by the API wallet (HL_AGENT_PRIVATE_KEY) for HL_ACCOUNT_ADDRESS."""

    live = True

    def __init__(self) -> None:
        from . import wallet

        wallet.require_safe_for_live()  # refuses a main-wallet key (it could withdraw)
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
        except ImportError as e:
            raise RuntimeError("Live trading needs the Hyperliquid SDK: pip install -e '.[live]'") from e
        agent = Account.from_key(settings.hl_agent_key)
        dexes = [""] + [d for d in settings.dexes if d]
        self.info = Info(settings.hl_url, skip_ws=True, perp_dexs=dexes)
        self.ex = Exchange(agent, settings.hl_url, account_address=settings.hl_account, perp_dexs=dexes)
        self.account = settings.hl_account
        self._leverage_set: set[str] = set()

    slippage = 0.01  # market orders: an IOC limit at most 1% through the mid

    # ---- the account
    def abstraction(self) -> str:
        """"unifiedAccount" / "portfolioMargin" (one balance backs every market) or "default"/"disabled"
        (standard: each market has its own balance)."""
        try:
            return str(_info({"type": "userAbstraction", "user": self.account}) or "default")
        except DataFeedError:
            return "default"

    def balances(self) -> dict[str, float]:
        """Spot balances by token (in unified mode this is where all the money shows)."""
        st = _info({"type": "spotClearinghouseState", "user": self.account}) or {}
        return {b["coin"]: float(b.get("total") or 0) for b in st.get("balances", [])}

    def dex_value(self, dex: str) -> float:
        try:
            return float(self.info.user_state(self.account, dex)["marginSummary"]["accountValue"])
        except Exception:  # noqa: BLE001 — a market this account never used
            return 0.0

    def equity(self, inst: Instrument | None = None) -> float:
        if self.abstraction() in ("unifiedAccount", "portfolioMargin"):
            bal = self.balances()
            coins = [inst.collateral] if inst else settings.collateral
            return round(sum(bal.get(c, 0.0) for c in coins), 2)
        if inst is not None:  # standard mode: only that market's own balance can margin the trade
            return round(self.dex_value(inst.dex), 2)
        return round(sum(self.dex_value(d) for d in [""] + [d for d in settings.dexes if d]), 2)

    def position_size(self, inst: Instrument) -> float | None:
        st = self.info.user_state(self.account, inst.dex)
        for p in st.get("assetPositions", []):
            pos = p.get("position", {})
            if pos.get("coin") == inst.coin:
                return abs(float(pos.get("szi") or 0))
        return 0.0

    # ---- orders
    def _check(self, r: Any) -> dict:
        if not isinstance(r, dict) or r.get("status") != "ok":
            raise RuntimeError(f"Hyperliquid refused the order: {str(r)[:200]}")
        statuses = r["response"]["data"]["statuses"]
        st = statuses[0] if statuses else {}
        if "error" in st:
            raise RuntimeError(f"Hyperliquid refused the order: {st['error'][:200]}")
        return st

    @staticmethod
    def _check_ok(r: Any) -> None:
        if not isinstance(r, dict) or r.get("status") != "ok":
            raise RuntimeError(f"Hyperliquid refused: {str(r)[:200]}")

    def _leverage(self, inst: Instrument) -> None:
        if inst.coin in self._leverage_set:
            return
        lev = max(1, min(settings.leverage, inst.max_leverage))
        self._check_ok(self.ex.update_leverage(lev, inst.coin, is_cross=False))  # isolated: a loss stays in its trade
        self._leverage_set.add(inst.coin)

    def _market(self, inst: Instrument, is_buy: bool, size: float, reduce_only: bool) -> Fill:
        """A market order as an immediate-or-cancel limit, priced by the PERP rules. (The SDK's own market_open
        treats stock-market asset ids (110000+) as spot and rounds cheap stocks' prices wrongly.)"""
        px = self.mid(inst.coin)
        limit = round_px(px * (1 + self.slippage if is_buy else 1 - self.slippage), inst.sz_decimals)
        size = round_size(size, inst.sz_decimals)
        st = self._check(self.ex.order(inst.coin, is_buy, size, limit, {"limit": {"tif": "Ioc"}},
                                       reduce_only=reduce_only))
        f = st.get("filled") or {}
        if not f:
            raise RuntimeError("the order didn't fill (price moved away)")
        fpx, sz = float(f["avgPx"]), float(f["totalSz"])
        return Fill(fpx, sz, round(fpx * sz * settings.fee_pct / 100, 4), str(f.get("oid")))

    def open(self, inst: Instrument, is_long: bool, size: float) -> Fill:
        self._leverage(inst)
        return self._market(inst, is_long, size, reduce_only=False)

    def close(self, inst: Instrument, is_long: bool, size: float) -> Fill:
        held = self.position_size(inst)
        if held is not None:
            size = min(size, held) if held > 0 else size
        return self._market(inst, not is_long, size, reduce_only=True)

    def _trigger(self, inst: Instrument, is_long: bool, size: float, px: float, kind: str) -> str:
        px = round_px(px, inst.sz_decimals)
        worst = round_px(px * (0.9 if is_long else 1.1), inst.sz_decimals)  # market trigger: worst acceptable fill
        st = self._check(self.ex.order(inst.coin, not is_long, size, worst,
                                       {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": kind}},
                                       reduce_only=True))
        rest = st.get("resting") or {}
        return str(rest.get("oid") or "")

    def protect(self, inst: Instrument, is_long: bool, size: float, stop: float, target: float | None) -> dict:
        ids = {"sl": self._trigger(inst, is_long, size, stop, "sl")}
        if target is not None:
            ids["tp"] = self._trigger(inst, is_long, size, target, "tp")
        return ids

    def move_stop(self, inst: Instrument, is_long: bool, size: float, stop: float, ids: dict) -> dict:
        new = self._trigger(inst, is_long, size, stop, "sl")  # the new one first: never a moment unprotected
        if ids.get("sl"):
            try:
                self.ex.cancel(inst.coin, int(ids["sl"]))
            except Exception:  # noqa: BLE001 — already gone
                pass
        return {**ids, "sl": new}

    def cancel_protection(self, inst: Instrument, ids: dict) -> None:
        for oid in (ids or {}).values():
            if oid:
                try:
                    self.ex.cancel(inst.coin, int(oid))
                except Exception:  # noqa: BLE001
                    pass

    # ---- setup and checks
    def set_unified_account(self) -> Any:
        """One USDC balance backs the regular and the stock markets (Hyperliquid's "unified account").
        An API wallet may switch this on; it moves no money and can't withdraw."""
        r = self.ex.agent_set_abstraction("u")
        self._check_ok(r)
        return r

    def readiness(self, inst: Instrument) -> list[tuple[bool | None, str]]:
        """Everything a real order on `inst` needs, checked against the real account. Places nothing."""
        out: list[tuple[bool | None, str]] = []
        mode = self.abstraction()
        unified = mode in ("unifiedAccount", "portfolioMargin")
        out.append((True if unified else None, f"Account mode: {mode}" + ("" if unified else
                    " — each market has its own balance; run `trader wallet unified-account` so one USDC balance "
                    "backs the stock markets (or move USDC to the stock market in the app)")))
        eq = self.equity(inst)
        out.append((eq >= 20, f"Money usable on {inst.coin} ({inst.collateral}): ${eq:,.2f}"))
        try:
            asset = self.ex.info.name_to_asset(inst.coin)
            out.append((True, f"{inst.coin} is Hyperliquid asset {asset}; max {inst.max_leverage}x"
                              f"{', isolated only' if inst.only_isolated else ''}"))
        except Exception as e:  # noqa: BLE001
            out.append((False, f"{inst.coin} isn't a tradeable market for this account ({type(e).__name__})"))
        px = self.mid(inst.coin)
        size = min_size(px, inst.sz_decimals)
        out.append((True, f"Smallest order: {size:g} at ~{px:g} = ${size * px:,.2f} "
                          f"(buy limit {round_px(px * 1.01, inst.sz_decimals):g})"))
        return out

    def round_trip(self, inst: Instrument) -> list[str]:
        """A REAL test: buy the smallest allowed amount, put a stop on the exchange, cancel it, sell.
        Costs about the fees and the spread (a few cents)."""
        steps = []
        size = min_size(self.mid(inst.coin), inst.sz_decimals)
        f = self.open(inst, True, size)
        steps.append(f"bought {f.size:g} {inst.coin} at {f.price:g}")
        try:
            ids = self.protect(inst, True, f.size, f.price * 0.95, None)
            steps.append(f"stop order placed on the exchange at {round_px(f.price * 0.95, inst.sz_decimals):g} "
                         f"(id {ids.get('sl')})")
            self.cancel_protection(inst, ids)
            steps.append("stop order cancelled")
        finally:
            c = self.close(inst, True, f.size)
            steps.append(f"sold {c.size:g} at {c.price:g}: {(c.price - f.price) * c.size - f.fee - c.fee:+.4f} USD "
                         "after fees")
        left = self.position_size(inst)
        steps.append("position fully closed" if not left else f"WARNING: {left:g} still open — close it in the app")
        return steps


def min_size(px: float, sz_decimals: int, usd: float = 11.0) -> float:
    """The smallest size worth at least `usd` (Hyperliquid's minimum is $10), rounded UP to the size step."""
    step = 10 ** -sz_decimals
    return round(math.ceil(usd / px / step) * step, sz_decimals)
