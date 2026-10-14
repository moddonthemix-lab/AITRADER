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
    paper_equity: float = field(default_factory=lambda: float(_env("TRADER_PAPER_EQUITY", "1000")))

    # ---- Hyperliquid: where Trader trades (stock perps on the HIP-3 markets)
    hl_url: str = field(default_factory=lambda: _env("HL_API_URL", "https://api.hyperliquid.xyz"))
    # The account that holds Trader's money (best: a sub-account just for him) and the API ("agent")
    # wallet key that may trade it. An API wallet can never withdraw: that's the point.
    hl_account: str = field(default_factory=lambda: _env("HL_ACCOUNT_ADDRESS", "").strip())
    hl_agent_key: str = field(default_factory=lambda: _env("HL_AGENT_PRIVATE_KEY", "").strip())
    dexes: list[str] = field(default_factory=lambda: _list("TRADER_DEXES", "xyz,mkts,km,cash,flx"))
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
    leverage: int = field(default_factory=lambda: int(_env("TRADER_LEVERAGE", "1")))
    risk_per_trade_pct: float = field(default_factory=lambda: float(_env("TRADER_RISK_PER_TRADE_PCT", "2")))
    max_position_pct: float = field(default_factory=lambda: float(_env("TRADER_MAX_POSITION_PCT", "30")))
    max_open: int = field(default_factory=lambda: int(_env("TRADER_MAX_OPEN", "3")))
    min_rr: float = field(default_factory=lambda: float(_env("TRADER_MIN_RR", "1.5")))
    # Fee per side, in percent of the trade's value. HIP-3 stock perps cost more than crypto perps;
    # 0.09% is a safe assumption. Trader reads the real rate from Hyperliquid when the wallet is set.
    fee_pct: float = field(default_factory=lambda: float(_env("TRADER_FEE_PCT", "0.09")))
    # Trailing stop: once the trade is this far in profit (after fees), the stop moves to lock in a profit,
    # then follows the best price at this distance — never back down below the locked profit.
    trail_activate_pct: float = field(default_factory=lambda: float(_env("TRADER_TRAIL_ACTIVATE_PCT", "1.0")))
    trail_lock_pct: float = field(default_factory=lambda: float(_env("TRADER_TRAIL_LOCK_PCT", "0.25")))
    trail_pct: float = field(default_factory=lambda: float(_env("TRADER_TRAIL_PCT", "1.5")))
    # "r" (default): the trail is measured in the setup's own risk (R = entry-to-stop distance), so a wide
    # weekly setup gets room and a tight inside bar is trailed tightly. "pct": the fixed percentages above.
    # Defaults from a backtest of 12 big stocks since 2018: switch on at +1.5R, follow 1R behind the best
    # price — so the moment it switches on, the stop already locks in +0.5R.
    trail_mode: str = field(default_factory=lambda: _env("TRADER_TRAIL_MODE", "r").strip().lower())
    trail_activate_r: float = field(default_factory=lambda: float(_env("TRADER_TRAIL_ACTIVATE_R", "1.5")))
    trail_r: float = field(default_factory=lambda: float(_env("TRADER_TRAIL_R", "1.0")))
    # Don't chase: a setup that already triggered is only taken while price is this close to the trigger
    # (as a fraction of the planned risk).
    max_chase_r: float = field(default_factory=lambda: float(_env("TRADER_MAX_CHASE_R", "0.25")))

    # ---- when Trader looks for entries (New York time, weekdays): the US cash session
    window_start: str = field(default_factory=lambda: _env("TRADER_WINDOW_START", "09:45"))
    window_end: str = field(default_factory=lambda: _env("TRADER_WINDOW_END", "15:45"))
    scan_minutes: int = field(default_factory=lambda: int(_env("TRADER_SCAN_MINUTES", "15")))
    report_time: str = field(default_factory=lambda: _env("TRADER_REPORT_TIME", "16:30"))

    # ---- the Strat documents: Trader's playbook (docs/strat in this repo) plus the boss's own folder
    strat_docs_dir: str = field(default_factory=lambda: _env("TRADER_STRAT_DOCS_DIR", "~/Documents/Trader Strat Docs"))

    # ---- brains (the Family Hub's shared brain decides cloud or local, inside the hub's budget)
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://localhost:11434"))
    local_model: str = field(default_factory=lambda: _env("LOCAL_MODEL", "qwen3:8b"))
    hub_enabled: bool = field(default_factory=lambda: _flag("TRADER_HUB", "1"))

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
