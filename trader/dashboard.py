"""Trader's dashboard: http://localhost:8767 (AMO is 8765, Scholar 8766).

What it shows: money mode, account, today's and total P&L, the weekly minimum, the P&L chart, open
positions (with a Close button), armed plans, the last scan (ranked setups, the market reason to pass,
incidents), his journal, what he's learned per pattern, his lessons — and the Strat documents.

What you can do: upload Strat documents (drag in PDFs, Word docs, notes — Trader reads them right away),
delete them, search them, run a backtest, get a Strat read on a ticker, scan now (read-only: places
nothing), and ask Trader a question.

Safety: it listens on this Mac only (127.0.0.1) unless TRADER_DASH_HOST is set. Anything other than
this Mac must send TRADER_DASH_KEY (open the page once as http://<mac>:8767/?key=… and it's remembered).
Placing trades isn't possible from here — only closing one.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import backtest, learning, prep, strat, strategy
from .config import settings
from .journal import NY, money, ny_today, week_start
from .knowledge import BUILTIN, DOC_EXT, MAX_FILE_BYTES, library

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"
SAFE_NAME = re.compile(r"[^\w .()&+,-]+")


def docs_folder() -> Path:
    f = Path(settings.strat_docs_dir).expanduser()
    f.mkdir(parents=True, exist_ok=True)
    return f


def safe_filename(name: str) -> str:
    """A plain file name for the docs folder: no folders, no hidden files, a document type Trader can read."""
    base = Path(urllib.parse.unquote(name or "")).name
    base = SAFE_NAME.sub("_", base).strip(" ._")
    if not base:
        raise ValueError("the file needs a name")
    if Path(base).suffix.lower() not in DOC_EXT:
        raise ValueError(f"Trader reads {', '.join(sorted(DOC_EXT))} files")
    return base[:120]


class Dashboard:
    """The data behind the page (kept separate from HTTP so it's easy to test)."""

    def __init__(self, engine: Any):
        self.engine = engine
        self._connector = None
        self._last_scan: dict[str, Any] | None = None

    # ------------------------------------------------------------------ reading
    def state(self) -> dict[str, Any]:
        e, j, mode = self.engine, self.engine.j, self.engine.mode
        today = ny_today()
        try:
            equity = e.broker.equity()
        except Exception:  # noqa: BLE001
            equity = None
        opened = []
        for t in j.open_trades(mode=mode):
            row = {k: t[k] for k in ("id", "coin", "side", "size", "entry", "stop", "target", "hard_stop", "setup",
                                     "tf", "grade", "inside", "trail_active", "opened_at", "leverage")}
            try:
                px = e.broker.mid(t["coin"])
                d = 1 if t["side"] == "long" else -1
                row["price"] = px
                row["unrealized"] = round((px - t["entry"]) * d * t["size"] - (t["fees"] or 0), 2)
            except Exception:  # noqa: BLE001
                row["price"] = row["unrealized"] = None
            opened.append(row)
        plans = []
        for p in j.all("SELECT * FROM plans WHERE status IN ('waiting_approval','armed') ORDER BY id"):
            d = json.loads(p["data"])
            plans.append({"id": p["id"], "status": p["status"], "summary": d["summary"], "expires_at": p["expires_at"]})
        trades = j.all("SELECT id, coin, side, setup, tf, grade, inside, entry, size, leverage, exit, exit_reason, "
                       "pnl, r_multiple, peak, fees, opened_at, closed_at FROM trades WHERE mode = ? "
                       "ORDER BY id DESC LIMIT 30", (mode,))
        for t in trades:
            t.update(money(t))
        today_pnl = round(sum(t["pnl"] or 0 for t in j.closed_on(today) if t["mode"] == mode), 2)
        return {
            "mode": mode, "equity": equity, "today_pnl": today_pnl, "total_pnl": j.realized_pnl(mode),
            "week_trades": j.trades_opened_since(week_start(today), mode), "min_week": settings.min_trades_week,
            "rules": {"hard_stop_pct": settings.hard_stop_pct, "min_rr": settings.min_rr, "fee_pct": settings.fee_pct,
                      "risk_pct": settings.risk_per_trade_pct, "leverage": "max" if settings.leverage >= 1000 else settings.leverage,
                      "trail": f"on once in profit after fees (locks +{settings.trail_lock_pct:g}% after fees), "
                               f"{settings.trail_r:g}R behind",
                      "window": f"{settings.window_start}–{settings.window_end} New York"},
            "hub": e.hub is not None,
            "positions": opened, "plans": plans, "trades": trades,
            "pnl_series": self.pnl_series(),
            "stats": j.stats(mode=mode),
            "edges": learning.table(j)[:15],
            "lessons": learning.recent_lessons(j, 8),
            "passes": j.passes_on(today), "incidents": j.incidents_on(today),
            "scan": self._last_scan,
            "gameplan": prep.latest(j),
            "docs": self.docs(),
            "as_of": datetime.now(NY).isoformat(timespec="seconds"),
        }

    def pnl_series(self) -> list[dict[str, Any]]:
        """Realized P&L per New York day, with the running total."""
        by_day: dict[str, float] = {}
        for t in self.engine.j.closed_between("0000", mode=self.engine.mode):
            day = datetime.fromisoformat(t["closed_at"]).astimezone(NY).date().isoformat()
            by_day[day] = by_day.get(day, 0.0) + (t["pnl"] or 0)
        total, out = 0.0, []
        for day in sorted(by_day):
            total += by_day[day]
            out.append({"day": day, "pnl": round(by_day[day], 2), "total": round(total, 2)})
        return out

    def docs(self) -> list[dict[str, Any]]:
        out = []
        for root, mine in ((BUILTIN, False), (docs_folder(), True)):
            if root.is_dir():
                for p in sorted(root.rglob("*")):
                    if p.is_file() and p.suffix.lower() in DOC_EXT and not p.name.startswith((".", "~$")):
                        out.append({"name": p.relative_to(root).as_posix(), "size": p.stat().st_size, "yours": mine,
                                    "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()})
        return out

    # ------------------------------------------------------------------ the Strat documents
    def upload(self, name: str, data: bytes) -> dict[str, Any]:
        if not data:
            raise ValueError("the file is empty")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("files up to 20 MB")
        fname = safe_filename(name)
        dest = docs_folder() / fname
        stem, ext, n = dest.stem, dest.suffix, 2
        while dest.exists():  # never overwrite: "notes (2).md"
            dest = dest.with_name(f"{stem} ({n}){ext}")
            n += 1
        dest.write_bytes(data)
        pieces = library().refresh()
        read = sum(1 for p in library().pieces if p["file"] == dest.name)
        return {"saved": dest.name, "pieces": read, "total_pieces": pieces,
                "note": "" if read else "Saved, but no text could be read from it (a scanned PDF?)."}

    def delete_doc(self, name: str) -> dict[str, Any]:
        folder = docs_folder().resolve()
        target = (folder / name).resolve()
        if folder not in target.parents or not target.is_file():
            raise ValueError("only your own uploaded documents can be deleted")
        target.unlink()
        library().refresh()
        return {"deleted": name}

    def add_url(self, url: str) -> dict[str, Any]:
        """Save a web page into the Strat documents, word for word."""
        from . import knowledge

        dest = knowledge.save_page(url.strip(), docs_folder())
        library().refresh()
        return {"saved": dest.relative_to(docs_folder()).as_posix()}

    def add_thestrat(self) -> dict[str, Any]:
        """Every thestrat.ai docs page + the TrendSpider guide, in the background."""
        from . import knowledge

        if getattr(self, "_saving", False):
            return {"started": False, "note": "already saving"}
        self._saving = True

        def run() -> None:
            try:
                knowledge.save_thestrat(docs_folder())
            finally:
                self._saving = False
        threading.Thread(target=run, name="trader-docs", daemon=True).start()
        return {"started": True}

    def search_docs(self, q: str) -> list[dict[str, Any]]:
        return [{"file": h["file"], "text": h["text"][:700], "score": h["score"]} for h in library().search(q, k=5)]

    # ------------------------------------------------------------------ actions
    def scan(self) -> dict[str, Any]:
        """A read-only scan: what's setting up, or the market reason to pass. Places nothing."""
        e = self.engine
        today = ny_today()
        sc = strategy.scan(e.universe(), e.j, today, e.trades_this_week(), e.chart_source, e.perp_source)
        ps = sc.pass_summary()
        self._last_scan = {"at": datetime.now(NY).isoformat(timespec="minutes"),
                           "grades": list(sc.allowed_grades), "behind_pace": sc.behind_pace,
                           "setups": [{"coin": c.inst.coin, "grade": c.grade, "inside": c.setup.inside,
                                       "score": c.score, "parts": c.parts, "summary": c.summary()}
                                      for c in sc.ranked[:10]],
                           "pass": {"reason": ps[0], "detail": ps[1]} if ps else None,
                           "incidents": sc.incidents}
        return self._last_scan

    def prep(self) -> dict[str, Any]:
        """Make the game plan now (as of this moment: after the close, before the open, or mid-session)."""
        e = self.engine
        plan = prep.build(e.universe(), datetime.now(NY), e.chart_source)
        prep.save(e.j, plan)
        return plan

    def read(self, ticker: str) -> dict[str, Any]:
        from .cli import _chart

        from .session import session_source

        a = strat.analyze(_chart(ticker), source=session_source(self.engine.chart_source, datetime.now(NY)))
        return {"ticker": ticker, "thesis": strat.thesis(a), "bias": a["bias"]}

    def backtest(self, tickers: list[str], weekly: bool = False) -> dict[str, Any]:
        from .cli import _chart

        out = []
        for t in tickers[:4]:
            res = backtest.run(_chart(t), "W" if weekly else "D")
            backtest.learn(self.engine.j, res)
            out.append(backtest.text(res))
        return {"text": "\n\n".join(out) + "\n\nSaved to Trader's learning."}

    def close(self, trade_id: int) -> dict[str, Any]:
        e = self.engine
        with e.lock:
            t = e.j.trade(trade_id)
            if not t or t["closed_at"]:
                raise ValueError("no open trade with that id")
            done = e.close(t, "manual")
        if not done:
            raise ValueError("couldn't close it right now — the exchange stop still protects it; try again")
        return {"pnl": done["pnl"], "r": done["r_multiple"]}

    def ask(self, question: str) -> dict[str, Any]:
        from .connector import Connector

        if self._connector is None:
            self._connector = Connector(self.engine, self.engine.hub)
        summary, sources, brain = self._connector.do(question, local_only=False, boss=True)
        return {"answer": summary or "That needs research and I'm offline or out of cloud budget.",
                "brain": brain, "sources": sources}


# ------------------------------------------------------------------ HTTP
def make_handler(dash: Dashboard) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "Trader"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            log.debug("dash: " + fmt, *args)

        def _local(self) -> bool:
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _authorized(self) -> bool:
            if self._local():
                return True
            key = settings.dash_key
            given = self.headers.get("X-Trader-Key") or ""
            return bool(key) and hmac.compare_digest(given, key)

        def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _json_body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 100_000:
                raise ValueError("too big")
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def _route(self, method: str) -> None:
            url = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(url.query))
            path = url.path
            if method == "GET" and path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if method == "GET" and path == "/api/health":
                return self._send(200, {"ok": True, "agent": "trader", "mode": dash.engine.mode})
            if not path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(401, {"error": "this needs the dashboard key (TRADER_DASH_KEY)"})
            if method in ("POST", "DELETE") and self.headers.get("Origin") and not self._same_origin():
                return self._send(403, {"error": "cross-site request refused"})
            try:
                if method == "GET" and path == "/api/state":
                    return self._send(200, dash.state())
                if method == "GET" and path == "/api/docs/search":
                    return self._send(200, {"hits": dash.search_docs(q.get("q", ""))})
                if method == "GET" and path == "/api/read":
                    return self._send(200, dash.read(q.get("ticker", "")))
                if method == "POST" and path == "/api/docs":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_FILE_BYTES:
                        return self._send(413, {"error": "files up to 20 MB"})
                    return self._send(200, dash.upload(self.headers.get("X-Filename", ""), self.rfile.read(n)))
                if method == "POST" and path == "/api/docs/url":
                    return self._send(200, dash.add_url(str(self._json_body().get("url", ""))))
                if method == "POST" and path == "/api/docs/thestrat":
                    return self._send(200, dash.add_thestrat())
                if method == "DELETE" and path == "/api/docs":
                    return self._send(200, dash.delete_doc(q.get("name", "")))
                if method == "POST" and path == "/api/prep":
                    return self._send(200, dash.prep())
                if method == "POST" and path == "/api/scan":
                    return self._send(200, dash.scan())
                if method == "POST" and path == "/api/backtest":
                    b = self._json_body()
                    tickers = [t for t in re.split(r"[\s,]+", str(b.get("tickers", ""))) if t][:4]
                    if not tickers:
                        raise ValueError("which tickers?")
                    return self._send(200, dash.backtest(tickers, bool(b.get("weekly"))))
                if method == "POST" and path == "/api/close":
                    return self._send(200, dash.close(int(self._json_body().get("id", 0))))
                if method == "POST" and path == "/api/ask":
                    question = str(self._json_body().get("question", "")).strip()
                    if not question:
                        raise ValueError("ask something")
                    return self._send(200, dash.ask(question[:2000]))
                return self._send(404, {"error": "not found"})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001 — the type only; never details that could hold a key
                log.exception("dashboard error")
                return self._send(500, {"error": f"something went wrong ({type(e).__name__})"})

        def _same_origin(self) -> bool:
            origin = urllib.parse.urlparse(self.headers.get("Origin", ""))
            return origin.netloc == (self.headers.get("Host") or "")

        def do_GET(self) -> None:  # noqa: N802
            self._route("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._route("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._route("DELETE")

    return Handler


def serve(engine: Any, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    """Start the dashboard in the background; returns the server (call .shutdown() to stop)."""
    host = host or settings.dash_host
    port = port if port is not None else settings.dash_port
    if host not in ("127.0.0.1", "localhost", "::1") and not settings.dash_key:
        raise RuntimeError("Set TRADER_DASH_KEY before opening the dashboard beyond this Mac (TRADER_DASH_HOST).")
    srv = ThreadingHTTPServer((host, port), make_handler(Dashboard(engine)))
    threading.Thread(target=srv.serve_forever, name="trader-dash", daemon=True).start()
    log.info("dashboard: http://%s:%s", "localhost" if host in ("127.0.0.1", "::1") else host, srv.server_address[1])
    return srv
