"""How Trader learns — always inside the rules, never around them.

1. From his own trades and from backtests: each Strat pattern on each timeframe gets an "edge" — its
   average result in R (1R = what was risked), shrunk toward a cautious prior until there's enough data.
   Real/paper trades count twice as much as backtest trades. The edge reorders setups of the same
   grade; a pattern that keeps losing (≥ 30 samples, edge below -0.15R) is benched and reported.
   Learning can never create a trade the rules don't allow, or loosen the hard stop.
2. Lessons: after every closed trade, Trader writes one short, specific lesson (what the Strat said,
   what happened, what to do next time) — read back into his thinking.
3. The Strat documents (knowledge.py): the playbook and the boss's own Strat files.
"""

from __future__ import annotations

from typing import Any

from .journal import Journal, now_iso

PRIOR_R, PRIOR_N = 0.1, 10          # assume a small edge until the data says otherwise
TRADE_WEIGHT, MAX_BACKTEST_N = 2.0, 200
BENCH_N, BENCH_R = 30, -0.15


def edge(j: Journal, family: str, tf: str) -> tuple[float, float]:
    """(expected R per trade, effective sample size) for a pattern on a timeframe."""
    rows = j.all("SELECT r_multiple FROM trades WHERE family = ? AND tf = ? AND r_multiple IS NOT NULL",
                 (family, tf))
    n_t = len(rows)
    sum_t = sum(r["r_multiple"] for r in rows)
    bt = j.all("SELECT n, sum_r FROM setup_stats WHERE family = ? AND tf = ?", (family, tf))
    n_b = sum(r["n"] for r in bt)
    sum_b = sum(r["sum_r"] for r in bt)
    if n_b > MAX_BACKTEST_N:  # don't let old backtests drown out real results
        sum_b, n_b = sum_b * MAX_BACKTEST_N / n_b, MAX_BACKTEST_N
    n_eff = n_t * TRADE_WEIGHT + n_b
    total = sum_t * TRADE_WEIGHT + sum_b + PRIOR_R * PRIOR_N
    return round(total / (n_eff + PRIOR_N), 3), n_eff


def benched(j: Journal, family: str, tf: str) -> bool:
    e, n = edge(j, family, tf)
    return n >= BENCH_N and e < BENCH_R


def record_backtest(j: Journal, by_pattern: dict[tuple[str, str], list[float]], source: str) -> None:
    """Save a backtest's R results per (family, tf) — replaces that source's previous numbers."""
    with j.tx() as c:
        for (family, tf), rs in by_pattern.items():
            c.execute("INSERT INTO setup_stats(family, tf, source, n, wins, sum_r, updated_at) VALUES (?,?,?,?,?,?,?) "
                      "ON CONFLICT(family, tf, source) DO UPDATE SET n=excluded.n, wins=excluded.wins, "
                      "sum_r=excluded.sum_r, updated_at=excluded.updated_at",
                      (family, tf, source, len(rs), sum(1 for r in rs if r > 0), round(sum(rs), 4), now_iso()))


def table(j: Journal) -> list[dict[str, Any]]:
    """Every pattern Trader has data on, best first."""
    keys = {(r["family"], r["tf"]) for r in j.all("SELECT DISTINCT family, tf FROM setup_stats")}
    keys |= {(r["family"], r["tf"]) for r in j.all(
        "SELECT DISTINCT family, tf FROM trades WHERE family IS NOT NULL AND r_multiple IS NOT NULL")}
    out = []
    for family, tf in keys:
        e, n = edge(j, family, tf)
        out.append({"family": family, "tf": tf, "edge_r": e, "samples": n, "benched": n >= BENCH_N and e < BENCH_R})
    return sorted(out, key=lambda r: -r["edge_r"])


# ------------------------------------------------------------------ lessons
def add_lesson(j: Journal, text: str, trade_id: int | None = None, source: str = "review") -> int:
    return j.insert("lessons", {"trade_id": trade_id, "text": text.strip()[:600], "source": source,
                                "created_at": now_iso()})


def recent_lessons(j: Journal, n: int = 8) -> list[str]:
    return [r["text"] for r in j.all("SELECT text FROM lessons ORDER BY id DESC LIMIT ?", (n,))]


def lesson_prompt(trade: dict[str, Any], playbook: str) -> str:
    return (
        "You are reviewing one of your own closed trades. Write ONE lesson in 2-3 sentences: what the Strat "
        "said, and what actually happened, using ONLY the numbers given below. Do not invent price levels, and do "
        "not propose new stop, target or trigger numbers. Do not suggest breaking the rules (the -23% hard stop, "
        "fee-aware targets, inside bars first, the 5-minute invalidation).\n\n"
        f"Trade: {trade['side']} {trade['coin']} — {trade['tf']} {trade['setup']} (grade {trade['grade']}, "
        f"{'inside bar' if trade['inside'] else 'not an inside bar'}, "
        f"{'with' if trade['with_continuity'] else 'without'} continuity, setup-bar volume x{trade['rvol'] or '?'}).\n"
        f"Entry {trade['entry']}, first stop {trade['initial_stop']}, target {trade['target']}, "
        f"exit {trade['exit']} ({trade['exit_reason']}). P&L ${trade['pnl']} after ${trade['fees']} fees, "
        f"{trade['r_multiple']}R.\nThesis at entry:\n{(trade.get('thesis') or '')[:1200]}\n\n"
        f"From the Strat documents:\n{playbook[:1500]}"
    )
