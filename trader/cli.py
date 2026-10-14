"""`trader` — the command line.

    trader doctor                 check everything (hub, brains, market data, Hyperliquid, wallet, documents)
    trader run                    start Trader: the hub connection and the trading loop (leave it running)
    trader markets                the stock markets Trader would scan right now (liquid ones only)
    trader scan                   one scan: ranked setups, or the market reason to pass (no orders)
    trader read NVDA              The Strat read for a ticker
    trader backtest NVDA TSLA     replay history with Trader's rules (--weekly, --from 2023-01-01)
    trader positions | stats | report [--post] | lessons
    trader close <trade id>       close a position by hand
    trader wallet new | status | enable-stock-markets
    trader docs [search words]    the Strat documents Trader reads
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from typing import Any

from .config import settings


def make_engine(hub: Any = "auto") -> Any:
    from .brain import Brain
    from .engine import Engine
    from .hyperliquid import LiveBroker, PaperBroker
    from .journal import Journal

    j = Journal(settings.db_file)
    if hub == "auto":
        hub = None
        if os.environ.get("HUB_TOKEN") and settings.hub_enabled:
            from .family.hub_client import HubClient

            hub = HubClient(timeout=10)
    broker = LiveBroker() if settings.live else PaperBroker(j)
    return Engine(j, broker, hub=hub, brain=Brain(hub))


def _print(obj: Any) -> None:
    print(obj if isinstance(obj, str) else json.dumps(obj, indent=2, default=str))


# ------------------------------------------------------------------ commands
def cmd_doctor(_: argparse.Namespace) -> int:
    ok = True

    def line(good: bool | None, what: str) -> None:
        nonlocal ok
        mark = "✅" if good else ("⚠️ " if good is None else "❌")
        ok = ok and good is not False
        print(f"{mark} {what}")

    line(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]} (3.10+ needed)")
    line(True, f"Mode: {settings.mode.upper()} ({'real money — every trade waits for the boss' if settings.live else 'pretend money'})")
    line(True, f"Rules: hard stop -{settings.hard_stop_pct:g}%, min {settings.min_trades_week} trades/week, "
               f"min R:R {settings.min_rr:g} after {settings.fee_pct:g}%/side fees, inside bars first, "
               f"leverage {settings.leverage}x, risk {settings.risk_per_trade_pct:g}%/trade")
    # the hub
    if os.environ.get("HUB_TOKEN"):
        try:
            from .family.hub_client import HubClient

            h = HubClient(timeout=5)
            me = h.me()
            line((me.get("member") or {}).get("id") == "trader",
                 f"Family Hub at {os.environ.get('HUB_URL')}: connected as {(me.get('member') or {}).get('id')}")
            b = h.budget()
            line(True, f"Cloud budget this month: {json.dumps(b)[:160]}")
        except Exception as e:  # noqa: BLE001
            line(False, f"Family Hub: can't connect ({type(e).__name__}). Check HUB_URL / HUB_TOKEN.")
    else:
        line(None, "Family Hub: no HUB_TOKEN yet (make one on the Chromebook: python3 -m hub agent token trader)")
    # brains
    try:
        import anthropic  # noqa: F401

        line(True if os.environ.get("ANTHROPIC_API_KEY") else None, "Cloud brain: anthropic installed"
             + ("" if os.environ.get("ANTHROPIC_API_KEY") else " but ANTHROPIC_API_KEY isn't set"))
    except ImportError:
        line(None, "Cloud brain: not installed (pip install -e '.[cloud]') — local only")
    try:
        from .net import get_json

        tags = get_json(f"{settings.ollama_url}/api/tags", timeout=3, retries=0)
        names = [m["name"] for m in tags.get("models", [])]
        line(settings.local_model in names or None, f"Ollama: running; {settings.local_model} "
             + ("ready" if settings.local_model in names else f"not pulled (ollama pull {settings.local_model})"))
    except Exception:  # noqa: BLE001
        line(None, "Ollama: not running (only needed when the cloud isn't allowed)")
    # market data
    try:
        from . import market

        bars, _ = market.candles("SPY", "D")
        line(len(bars) > 100, f"Stock charts (Yahoo): {len(bars)} daily SPY candles, with volume")
    except Exception as e:  # noqa: BLE001
        line(False, f"Stock charts (Yahoo): {e} — Trader will fall back to Hyperliquid's candles")
    try:
        from . import hyperliquid as hl

        u = hl.universe()
        line(bool(u), f"Hyperliquid: {len(u)} liquid markets on {', '.join(settings.dexes)} — "
                      + ", ".join(m.coin for m in u[:8]))
    except Exception as e:  # noqa: BLE001
        line(False, f"Hyperliquid: {e}")
    # wallet
    try:
        from . import wallet

        st = wallet.status()
        if st["agent"]:
            line(st["approved"] is not False and not st["problems"],
                 f"Wallet: Trader's API wallet {st['agent']} for {st['account']}"
                 + (f", approved until {st['valid_until']}" if st["valid_until"] else ""))
        for p in st["problems"]:
            line(None if not settings.live else False, p)
        if settings.hl_account:
            fee = hl.base_fee_pct(settings.hl_account)
            if fee is not None:
                line(True, f"Account base fee {fee:g}%/side (stock markets add theirs; planning uses {settings.fee_pct:g}%)")
    except Exception as e:  # noqa: BLE001
        line(None if not settings.live else False, f"Wallet: {e}")
    # documents
    from .knowledge import library

    s = library().summary()
    line(bool(s["files"]), f"Strat documents: {len(s['files'])} files ({', '.join(s['files'][:5])})")
    print("\nAll good." if ok else "\nFix the ❌ items above.")
    return 0 if ok else 1


def cmd_run(_: argparse.Namespace) -> int:
    from .connector import Connector

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine = make_engine()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    conn = None
    if engine.hub is not None:
        conn = Connector(engine)
        conn.start()
        logging.info("connected to the Family Hub")
    else:
        logging.info("no hub connection (HUB_TOKEN not set) — trading %s only, reports stay local", engine.mode)
    logging.info("Trader is running in %s mode. Ctrl+C to stop.", engine.mode.upper())
    try:
        while not stop.is_set():
            try:
                engine.step()
            except Exception:  # noqa: BLE001 — one bad minute never stops Trader
                logging.exception("step failed")
            stop.wait(60)
    except KeyboardInterrupt:
        pass
    if conn:
        conn.stop()
    return 0


def cmd_markets(_: argparse.Namespace) -> int:
    from . import hyperliquid as hl

    for m in hl.universe():
        print(f"{m.coin:16} {m.kind:6} chart {m.chart:7} price {m.mark:>10,.2f}  24h ${m.day_volume / 1e6:,.1f}M  "
              f"max {m.max_leverage}x{' isolated' if m.only_isolated else ''}")
    return 0


def cmd_scan(_: argparse.Namespace) -> int:
    from . import hyperliquid as hl
    from . import strategy
    from .journal import Journal, ny_today, week_start

    j = Journal(settings.db_file)
    sc = strategy.scan(hl.universe(), j, ny_today(), j.trades_opened_since(week_start(), settings.mode))
    print(f"Grades taken today: {'/'.join(sc.allowed_grades)}{' (behind pace for the weekly minimum)' if sc.behind_pace else ''}")
    for c in sc.ranked[:10]:
        print("•", c.summary())
    ps = sc.pass_summary()
    if ps:
        print(f"\nPASS — {ps[0]}: {ps[1]}")
    for i in sc.incidents:
        print("INCIDENT (not a pass):", i)
    return 0


def cmd_read(a: argparse.Namespace) -> int:
    from . import strat

    for t in a.tickers:
        print(strat.thesis(strat.analyze(_chart(t))), "\n")
    return 0


def _chart(t: str) -> str:
    from . import hyperliquid as hl

    t = t.strip()
    tick = t.split(":")[-1].upper()
    try:
        for m in hl.markets():
            if m.coin.lower() == t.lower() or m.ticker == tick:
                return m.chart
    except Exception:  # noqa: BLE001
        pass
    return hl.CHART.get(tick, tick)


def cmd_backtest(a: argparse.Namespace) -> int:
    from . import backtest
    from .journal import Journal

    j = Journal(settings.db_file)
    for t in a.tickers:
        res = backtest.run(_chart(t), "W" if a.weekly else "D", start=a.start)
        if not a.no_learn:
            backtest.learn(j, res)
        print(backtest.text(res), "\n")
        if a.json:
            _print(res)
    return 0


def cmd_positions(_: argparse.Namespace) -> int:
    from .journal import Journal

    rows = Journal(settings.db_file).open_trades()
    if not rows:
        print("No open positions.")
    for t in rows:
        print(f"#{t['id']} {t['mode']} {t['side']} {t['coin']} {t['size']:g} @ {t['entry']:g} — stop {t['stop']:g}"
              f"{' (trailing)' if t['trail_active'] else ''}, target {t['target']:g}, hard stop {t['hard_stop']:g}")
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    from . import learning
    from .journal import Journal

    j = Journal(settings.db_file)
    _print(j.stats(mode=settings.mode))
    for r in learning.table(j)[:20]:
        print(f"{r['tf']:3} {r['family']:30} edge {r['edge_r']:+.2f}R  samples {r['samples']:.0f}"
              f"{'  BENCHED' if r['benched'] else ''}")
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    from .journal import ny_today

    e = make_engine(hub="auto" if a.post else None)
    print(e.report(ny_today()) if a.post else e.report_text(ny_today())[0])
    return 0


def cmd_lessons(_: argparse.Namespace) -> int:
    from . import learning
    from .journal import Journal

    for x in learning.recent_lessons(Journal(settings.db_file), 20):
        print("•", x)
    return 0


def cmd_close(a: argparse.Namespace) -> int:
    e = make_engine(hub=None)
    t = e.j.trade(a.trade_id)
    if not t or t["closed_at"]:
        print("No open trade with that id.")
        return 1
    done = e.close(t, "manual")
    print(f"Closed: {done['pnl']:+.2f} USD, {done['r_multiple']}R." if done else "Couldn't close it — try again.")
    return 0 if done else 1


def cmd_wallet(a: argparse.Namespace) -> int:
    from . import wallet

    if a.action == "new":
        if settings.hl_agent_key and not a.yes:
            print("Trader already has an API wallet. Make a new one (rotate)? Run again with --yes.\n"
                  "After rotating: approve the new address in Hyperliquid and remove the old one.")
            return 1
        address, key = wallet.new_key()
        path = wallet.save_key(key)
        print(f"Trader's new API wallet: {address}\n(the private key was saved to {path} — never share it)\n\n"
              "Now approve it in Hyperliquid — see docs/HYPERLIQUID_SETUP.md:\n"
              "  app.hyperliquid.xyz → More → API → name it 'Trader', paste the address above, Authorize.\n"
              "  (If Trader trades a sub-account, pick that sub-account there.)\n"
              "This wallet can trade but can never withdraw. Then run: trader wallet status")
        return 0
    if a.action == "enable-stock-markets":
        _print(wallet.enable_stock_markets())
        return 0
    _print(wallet.status())
    return 0


def cmd_docs(a: argparse.Namespace) -> int:
    from .knowledge import library

    if a.words:
        for h in library().search(" ".join(a.words), k=4):
            print(f"[{h['file']}] ({h['score']})\n{h['text'][:700]}\n")
    else:
        _print(library().summary())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="trader", description="Trader — The Strat on Hyperliquid's stock markets")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("doctor", cmd_doctor), ("run", cmd_run), ("markets", cmd_markets), ("scan", cmd_scan),
                     ("positions", cmd_positions), ("stats", cmd_stats), ("lessons", cmd_lessons)):
        sub.add_parser(name).set_defaults(fn=fn)
    s = sub.add_parser("read")
    s.add_argument("tickers", nargs="+")
    s.set_defaults(fn=cmd_read)
    s = sub.add_parser("backtest")
    s.add_argument("tickers", nargs="+")
    s.add_argument("--weekly", action="store_true")
    s.add_argument("--from", dest="start")
    s.add_argument("--no-learn", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_backtest)
    s = sub.add_parser("report")
    s.add_argument("--post", action="store_true", help="post it to the hub and record the day's P&L")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("close")
    s.add_argument("trade_id", type=int)
    s.set_defaults(fn=cmd_close)
    s = sub.add_parser("wallet")
    s.add_argument("action", nargs="?", default="status", choices=["new", "status", "enable-stock-markets"])
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_wallet)
    s = sub.add_parser("docs")
    s.add_argument("words", nargs="*")
    s.set_defaults(fn=cmd_docs)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
