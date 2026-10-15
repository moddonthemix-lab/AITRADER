"""Trader's settings, read from environment variables (and an optional .env file).

Every trading rule the boss set is a named setting here, so it is written down in one place:

    TRADER_HARD_STOP_PCT=10        a position is closed before it loses more than 10% (fees included)
    TRADER_MIN_TRADES_WEEK=2       at least 2 trades a week (B-grade setups count when behind; never C)
    TRADER_PRIORITY=inside         inside-bar setups (3-1-2, 2-1-2, 1-1-2) come first
    take profit at the Strat target, and a trailing stop that only ever locks in a profit after fees
    passes need a market reason (chop, conflicting timeframes, no setup in the window) — never a data feed
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no python-dotenv needed). Variables already set win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def env_file() -> Path:
    """The .env in use: $TRADER_ENV_FILE, else ./.env, else the one in the Trader folder."""
    if os.environ.get("TRADER_ENV_FILE"):
        return Path(os.environ["TRADER_ENV_FILE"])
    local = Path(".env")
    return local if local.is_file() else PROJECT_ROOT / ".env"


_load_dotenv(env_file())


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _flag(key: str, default: str) -> bool:
    return _env(key, default).strip().lower() in ("1", "true", "yes", "on")


MAX_LEVERAGE = 1000  # "max": no cap of our own, each market's maximum applies


def _leverage(value: str) -> int:
    v = value.strip().lower()
    return MAX_LEVERAGE if v in ("max", "maximum", "") else max(1, int(float(v)))


def _list(key: str, default: str) -> list[str]:
    return [x.strip() for x in _env(key, default).split(",") if x.strip()]


@dataclass
class Settings:
    home: str = field(default_factory=lambda: _env("TRADER_HOME", "~/.trader"))
    db_path: str = field(default_factory=lambda: _env("TRADER_DB_PATH", ""))  # blank = <home>/trader.db
    owner_name: str = field(default_factory=lambda: _env("TRADER_OWNER_NAME", "Modd"))
    timezone: str = field(default_factory=lambda: _env("TRADER_TIMEZONE", "America/New_York"))

    # ---- money mode: paper (pretend) until the boss says otherwise
    mode: str = field(default_factory=lambda: _env("TRADER_MODE", "paper").strip().lower())
    # Live only: "each" = every real-money trade waits for the boss's 👍 in #trades (the hub's rule for Trader);
    # "auto" = Trader trades on his own and posts each plan to #trades for the record. The boss decides.
    live_approval: str = field(default_factory=lambda: _env("TRADER_LIVE_APPROVAL", "each").strip().lower())
    paper_equity: float = field(default_factory=lambda: float(_env("TRADER_PAPER_EQUITY", "1000")))

    # ---- Hyperliquid: where Trader trades (stock perps on the HIP-3 markets)
    hl_url: str = field(default_factory=lambda: _env("HL_API_URL", "https://api.hyperliquid.xyz"))
    # The account that holds Trader's money (best: a sub-account just for him) and the API ("agent")
    # wallet key that may trade it. An API wallet can never withdraw: that's the point.
    hl_account: str = field(default_factory=lambda: _env("HL_ACCOUNT_ADDRESS", "").strip())
    hl_agent_key: str = field(default_factory=lambda: _env("HL_AGENT_PRIVATE_KEY", "").strip())
    dexes: list[str] = field(default_factory=lambda: _list("TRADER_DEXES", "xyz,mkts,km,cash,flx"))
    # The stablecoin(s) Trader's account holds. Each stock market is margined in one: xyz and mkts in USDC,
    # km and flx in USDH, cash in USDT0. Markets in a coin he doesn't hold are skipped.
    collateral: list[str] = field(default_factory=lambda: _list("TRADER_COLLATERAL", "USDC"))
    # Blank = every liquid stock / index perp on those markets. Or e.g. "NVDA,TSLA,xyz:AAPL,mkts:US500".
    watchlist: list[str] = field(default_factory=lambda: _list("TRADER_WATCHLIST", ""))
    min_day_volume_usd: float = field(default_factory=lambda: float(_env("TRADER_MIN_DAY_VOLUME_USD", "5000000")))
    max_symbols: int = field(default_factory=lambda: int(_env("TRADER_MAX_SYMBOLS", "25")))
    # Stocks and stock indexes only (no gold, oil or currencies) unless the watchlist names them.
    stocks_only: bool = field(default_factory=lambda: _flag("TRADER_STOCKS_ONLY", "1"))

    # ---- the boss's hard rules
    hard_stop_pct: float = field(default_factory=lambda: float(_env("TRADER_HARD_STOP_PCT", "10")))
    min_trades_week: int = field(default_factory=lambda: int(_env("TRADER_MIN_TRADES_WEEK", "2")))
    priority: str = field(default_factory=lambda: _env("TRADER_PRIORITY", "inside"))

    # ---- sizing and risk
    # "max" = always each ticker's full maximum leverage on Hyperliquid (CBRS 10x, SP500 50x). Or a number.
    leverage: int = field(default_factory=lambda: _leverage(_env("TRADER_LEVERAGE", "max")))
    # If the Strat stop is beyond the liquidation price, the stop is moved inside it — this fraction of the
    # way to liquidation — so Trader's own stop closes the trade, never a liquidation.
    liq_stop_at: float = field(default_factory=lambda: float(_env("TRADER_LIQ_STOP_AT", "0.8")))
    # How much of the account Trader may trade with: the margin of all his open positions together never
    # goes over this % of the account; the rest always stays free as a cushion.
    account_use_pct: float = field(default_factory=lambda: float(_env("TRADER_ACCOUNT_USE_PCT", "75")))
    risk_per_trade_pct: float = field(default_factory=lambda: float(_env("TRADER_RISK_PER_TRADE_PCT", "2")))
    max_open: int = field(default_factory=lambda: int(_env("TRADER_MAX_OPEN", "3")))
    min_rr: float = field(default_factory=lambda: float(_env("TRADER_MIN_RR", "1.5")))
    # Fee per side, in percent of the trade's value. HIP-3 stock perps cost more than crypto perps;
    # 0.09% is a safe assumption. Trader reads the real rate from Hyperliquid when the wallet is set.
    fee_pct: float = field(default_factory=lambda: float(_env("TRADER_FEE_PCT", "0.09")))
    # Trailing stop: once the trade is this far in profit (after fees), the stop moves to lock in a profit,
    # then follows the best price at this distance — never back down below the locked profit.
    # Trailing stop, on as soon as the trade is in profit after fees: when price is TRADER_TRAIL_START_PCT
    # beyond the round-trip fees, the stop jumps to lock in fees + TRADER_TRAIL_LOCK_PCT (a guaranteed green
    # close), then follows the best price TRADER_TRAIL_R (1R = the setup's entry-to-stop distance) behind.
    trail_start_pct: float = field(default_factory=lambda: float(_env("TRADER_TRAIL_START_PCT", "0.15")))
    trail_lock_pct: float = field(default_factory=lambda: float(_env("TRADER_TRAIL_LOCK_PCT", "0.05")))
    trail_r: float = field(default_factory=lambda: float(_env("TRADER_TRAIL_R", "1.0")))
    # Optional: wait until the trade is this many R in profit before trailing (0 = as soon as it's in profit).
    trail_activate_r: float = field(default_factory=lambda: float(_env("TRADER_TRAIL_ACTIVATE_R", "0")))
    # Don't chase: a setup that already triggered is only taken while price is this close to the trigger
    # (as a fraction of the planned risk).
    max_chase_r: float = field(default_factory=lambda: float(_env("TRADER_MAX_CHASE_R", "0.25")))

    # ---- the trading day (New York time, weekdays)
    # Scanning starts at the open; entries from 9:40 (the second 5-minute candle has closed) until 15:45.
    scan_start: str = field(default_factory=lambda: _env("TRADER_SCAN_START", "09:30"))
    window_start: str = field(default_factory=lambda: _env("TRADER_WINDOW_START", "09:40"))
    window_end: str = field(default_factory=lambda: _env("TRADER_WINDOW_END", "15:45"))
    # Entry: "break" = the moment price trades through the setup bar's high/low, wicks included
    # (thestrat.ai: "the break is the confirmation"). "5m" = wait for a 5-minute candle to close beyond it.
    entry_confirm: str = field(default_factory=lambda: _env("TRADER_ENTRY_CONFIRM", "break").strip().lower())
    # Before arming a plan, check it against the boss's Strat documents (when there are any): a setup the
    # documents rule out is skipped, with the document's reason.
    doc_check: bool = field(default_factory=lambda: _flag("TRADER_DOC_CHECK", "1"))
    # The game plan: inside bars ready for the next session, after the close and again before the open.
    prep_after_close: str = field(default_factory=lambda: _env("TRADER_PREP_AFTER_CLOSE", "16:10"))
    prep_pre_market: str = field(default_factory=lambda: _env("TRADER_PREP_PRE_MARKET", "09:00"))
    scan_minutes: int = field(default_factory=lambda: int(_env("TRADER_SCAN_MINUTES", "15")))
    report_time: str = field(default_factory=lambda: _env("TRADER_REPORT_TIME", "16:30"))

    # ---- the Strat documents: Trader's playbook (docs/strat in this repo) plus the boss's own folder
    strat_docs_dir: str = field(default_factory=lambda: _env("TRADER_STRAT_DOCS_DIR", "~/Documents/Trader Strat Docs"))

    # ---- brains (the Family Hub's shared brain decides cloud or local, inside the hub's budget)
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))
    local_model: str = field(default_factory=lambda: _env("LOCAL_MODEL", "qwen3:8b"))
    hub_enabled: bool = field(default_factory=lambda: _flag("TRADER_HUB", "1"))

    # ---- the dashboard (http://localhost:8767). This Mac only, unless TRADER_DASH_HOST=0.0.0.0 (your Wi-Fi),
    # which needs TRADER_DASH_KEY.
    dash_enabled: bool = field(default_factory=lambda: _flag("TRADER_DASH", "1"))
    dash_host: str = field(default_factory=lambda: _env("TRADER_DASH_HOST", "127.0.0.1"))
    dash_port: int = field(default_factory=lambda: int(_env("TRADER_DASH_PORT", "8767")))
    dash_key: str = field(default_factory=lambda: _env("TRADER_DASH_KEY", "").strip())

    @property
    def db_file(self) -> str:
        return self.db_path or str(Path(self.home).expanduser() / "trader.db")

    @property
    def live(self) -> bool:
        return self.mode == "live"


settings = Settings()


def reload() -> Settings:
    """Re-read the environment in place (tests change it), so every `from .config import settings` sees it."""
    settings.__dict__.update(Settings().__dict__)
    return settings
