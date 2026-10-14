"""Trader's journal (SQLite): every trade, every pass and its market reason, incidents, lessons, backtests.

This is Trader's personal memory. It stays on the Mac — it is never uploaded to the hub.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,                 -- paper | live
    coin TEXT NOT NULL, ticker TEXT, chart TEXT,
    dex TEXT, sz_decimals INTEGER DEFAULT 2, max_leverage INTEGER DEFAULT 1, only_isolated INTEGER DEFAULT 0,
    side TEXT NOT NULL,                 -- long | short
    setup TEXT, family TEXT, tf TEXT, grade TEXT,
    inside INTEGER DEFAULT 0, with_continuity INTEGER, rvol REAL,
    entry REAL NOT NULL, size REAL NOT NULL,
    stop REAL NOT NULL, initial_stop REAL NOT NULL, hard_stop REAL NOT NULL, target REAL,
    fees REAL DEFAULT 0,
    peak REAL, trail_active INTEGER DEFAULT 0,
    protection TEXT DEFAULT '{}',       -- exchange order ids (live)
    opened_at TEXT NOT NULL, closed_at TEXT,
    exit REAL, exit_reason TEXT,        -- target | trail | stop | hard_stop | manual
    pnl REAL, r_multiple REAL,
    plan_id INTEGER, thesis TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL, coin TEXT NOT NULL, side TEXT NOT NULL,
    data TEXT NOT NULL,                 -- the full plan (setup, levels, size)
    status TEXT NOT NULL,               -- waiting_approval | armed | filled | expired | declined | invalid
    task_id INTEGER, note TEXT,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passes (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL, coin TEXT, reason TEXT NOT NULL, detail TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL, what TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY, trade_id INTEGER, text TEXT NOT NULL, source TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS setup_stats (     -- backtest results per pattern, for learning
    family TEXT NOT NULL, tf TEXT NOT NULL, source TEXT NOT NULL,
    n INTEGER NOT NULL, wins INTEGER NOT NULL, sum_r REAL NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (family, tf, source)
);
CREATE TABLE IF NOT EXISTS feedback (
    message_id INTEGER PRIMARY KEY, value INTEGER NOT NULL, text TEXT, rated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ny_now() -> datetime:
    return datetime.now(NY)


def ny_today() -> date:
    return ny_now().date()


def week_start(d: date | None = None) -> date:
    d = d or ny_today()
    return d - timedelta(days=d.weekday())


class Journal:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            path = str(Path(path).expanduser())
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def scalar(self, sql: str, args: tuple = ()) -> Any:
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
        return r[0] if r else None

    def insert(self, table: str, data: dict[str, Any]) -> int:
        cols = ", ".join(data)
        q = ", ".join("?" for _ in data)
        with self.lock:
            return int(self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({q})", tuple(data.values())).lastrowid)

    def update(self, table: str, row_id: int, data: dict[str, Any]) -> None:
        sets = ", ".join(f"{k} = ?" for k in data)
        with self.lock:
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*data.values(), row_id))

    def get(self, key: str, default: str | None = None) -> str | None:
        v = self.scalar("SELECT value FROM kv WHERE key = ?", (key,))
        return default if v is None else v

    def set(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, value))

    # ------------------------------------------------------------------ trades
    def open_trades(self, mode: str | None = None) -> list[dict[str, Any]]:
        if mode:
            return self.all("SELECT * FROM trades WHERE closed_at IS NULL AND mode = ? ORDER BY id", (mode,))
        return self.all("SELECT * FROM trades WHERE closed_at IS NULL ORDER BY id")

    def trade(self, trade_id: int) -> dict[str, Any] | None:
        return self.one("SELECT * FROM trades WHERE id = ?", (trade_id,))

    def record_open(self, **t: Any) -> int:
        t.setdefault("opened_at", now_iso())
        t.setdefault("initial_stop", t["stop"])
        t.setdefault("peak", t["entry"])
        if isinstance(t.get("protection"), dict):
            t["protection"] = json.dumps(t["protection"])
        return self.insert("trades", t)

    def record_close(self, trade_id: int, exit_px: float, reason: str, exit_fee: float,
                     at: str | None = None) -> dict[str, Any]:
        t = self.trade(trade_id)
        if t is None:
            raise ValueError(f"no trade {trade_id}")
        d = 1 if t["side"] == "long" else -1
        fees = round((t["fees"] or 0) + exit_fee, 4)
        pnl = round((exit_px - t["entry"]) * d * t["size"] - fees, 2)
        risk = abs(t["entry"] - t["initial_stop"]) * t["size"]
        r_mult = round(pnl / risk, 2) if risk else None
        self.update("trades", trade_id, {"exit": exit_px, "exit_reason": reason, "closed_at": at or now_iso(),
                                         "fees": fees, "pnl": pnl, "r_multiple": r_mult})
        return self.trade(trade_id)  # type: ignore[return-value]

    def closed_between(self, start: str, end: str | None = None, mode: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM trades WHERE closed_at IS NOT NULL AND closed_at >= ?", [start]
        if end:
            sql += " AND closed_at < ?"
            args.append(end)
        if mode:
            sql += " AND mode = ?"
            args.append(mode)
        return self.all(sql + " ORDER BY closed_at", tuple(args))

    def realized_pnl(self, mode: str = "paper") -> float:
        return round(self.scalar("SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE mode = ? AND pnl IS NOT NULL",
                                 (mode,)) or 0.0, 2)

    def trades_opened_since(self, since: date, mode: str | None = None) -> int:
        start = datetime.combine(since, datetime.min.time(), NY).astimezone(timezone.utc).isoformat(timespec="seconds")
        if mode:
            return int(self.scalar("SELECT COUNT(*) FROM trades WHERE opened_at >= ? AND mode = ?", (start, mode)) or 0)
        return int(self.scalar("SELECT COUNT(*) FROM trades WHERE opened_at >= ?", (start,)) or 0)

    def opened_on(self, day: date) -> list[dict[str, Any]]:
        start = datetime.combine(day, datetime.min.time(), NY).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        return self.all("SELECT * FROM trades WHERE opened_at >= ? AND opened_at < ? ORDER BY id",
                        (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")))

    def closed_on(self, day: date) -> list[dict[str, Any]]:
        start = datetime.combine(day, datetime.min.time(), NY).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        return self.closed_between(start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))

    # ------------------------------------------------------------------ passes and incidents
    def record_pass(self, day: date, reason: str, detail: str, coin: str | None = None) -> int:
        return self.insert("passes", {"day": day.isoformat(), "coin": coin, "reason": reason, "detail": detail[:500],
                                      "created_at": now_iso()})

    def passes_on(self, day: date) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM passes WHERE day = ? ORDER BY id", (day.isoformat(),))

    def record_incident(self, day: date, what: str) -> int:
        return self.insert("incidents", {"day": day.isoformat(), "what": what[:500], "created_at": now_iso()})

    def incidents_on(self, day: date) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM incidents WHERE day = ? ORDER BY id", (day.isoformat(),))

    # ------------------------------------------------------------------ stats
    def stats(self, mode: str | None = None, since: str = "0000") -> dict[str, Any]:
        trades = self.closed_between(since, mode=mode)
        pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
        by_setup: dict[str, dict[str, float]] = {}
        for t in trades:
            key = f"{t['tf']} {t['family'] or t['setup'] or 'untagged'}"
            s = by_setup.setdefault(key, {"trades": 0, "pnl": 0.0, "wins": 0})
            s["trades"] += 1
            s["pnl"] = round(s["pnl"] + (t["pnl"] or 0), 2)
            s["wins"] += 1 if (t["pnl"] or 0) > 0 else 0
        return {
            "trades": len(pnls),
            "net_pnl": round(sum(pnls), 2),
            "fees": round(sum(t["fees"] or 0 for t in trades), 2),
            "win_rate": round(len(wins) / len(pnls), 3) if pnls else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else None,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
            "by_exit": {r: sum(1 for t in trades if t["exit_reason"] == r)
                        for r in ("target", "trail", "stop", "hard_stop", "manual")},
            "by_setup": by_setup,
        }
