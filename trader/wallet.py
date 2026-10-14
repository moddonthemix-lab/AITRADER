"""Trader's own wallet on Hyperliquid — an API ("agent") wallet that can trade but can NEVER withdraw.

How it fits together (docs/HYPERLIQUID_SETUP.md walks the boss through it):
  1. The boss's own wallet keeps the money and the withdrawal rights. Best: a sub-account just for
     Trader, funded with what he's allowed to trade with. Its address → HL_ACCOUNT_ADDRESS.
  2. `trader wallet new` makes Trader a fresh key. Only its address is shown; the key goes into .env.
  3. The boss approves that address in the Hyperliquid app (More → API). Hyperliquid lets an approved
     API wallet place and cancel orders, but not move or withdraw funds.
  4. Rotate it any time: `trader wallet new` again, approve the new address, remove the old one.
     Approvals expire (up to 180 days), so the boss renews them periodically anyway.

Trader refuses to trade live with a key that controls the account itself (that key could withdraw).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import env_file, settings

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class WalletError(Exception):
    pass


def _account() -> Any:
    try:
        from eth_account import Account
    except ImportError as e:
        raise WalletError("The wallet tools need the Hyperliquid SDK: pip install -e '.[live]'") from e
    return Account


def address_of(private_key: str) -> str:
    return _account().from_key(private_key).address


def new_key() -> tuple[str, str]:
    """(address, private key) for a brand-new API wallet."""
    acct = _account().create()
    return acct.address, acct.key.hex() if hasattr(acct.key, "hex") else str(acct.key)


def save_key(private_key: str, path: Path | None = None) -> Path:
    """Write HL_AGENT_PRIVATE_KEY into .env (replacing an older one), readable only by this user."""
    path = path or env_file()
    lines = path.read_text().splitlines() if path.is_file() else []
    lines = [ln for ln in lines if not ln.strip().startswith("HL_AGENT_PRIVATE_KEY=")]
    key = private_key if private_key.startswith("0x") else "0x" + private_key
    lines.append(f"HL_AGENT_PRIVATE_KEY={key}")
    path.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def approved_agents(user: str) -> list[dict[str, Any]]:
    from .hyperliquid import _info

    return list(_info({"type": "extraAgents", "user": user}) or [])


def status() -> dict[str, Any]:
    """What `trader wallet` shows: Trader's address, the account, and whether Hyperliquid approved him."""
    out: dict[str, Any] = {"account": settings.hl_account or None, "agent": None, "approved": None,
                           "valid_until": None, "problems": []}
    if not settings.hl_account:
        out["problems"].append("HL_ACCOUNT_ADDRESS isn't set (the account Trader trades for).")
    elif not ADDRESS.match(settings.hl_account):
        out["problems"].append("HL_ACCOUNT_ADDRESS doesn't look like an address (0x + 40 characters).")
    if not settings.hl_agent_key:
        out["problems"].append("No API wallet yet: run `trader wallet new`.")
        return out
    try:
        out["agent"] = address_of(settings.hl_agent_key)
    except WalletError:
        raise
    except Exception:  # noqa: BLE001 — never echo the key
        out["problems"].append("HL_AGENT_PRIVATE_KEY isn't a valid key.")
        return out
    if settings.hl_account and out["agent"].lower() == settings.hl_account.lower():
        out["problems"].append("DANGER: HL_AGENT_PRIVATE_KEY is the account's own key, which can withdraw. "
                               "Use an API wallet (`trader wallet new`) instead, and keep this key private.")
    masters = [a for a in (settings.hl_account, os.environ.get("HL_MASTER_ADDRESS", "").strip()) if a]
    for user in masters:
        try:
            agents = approved_agents(user)
        except Exception:  # noqa: BLE001
            continue
        for a in agents:
            if str(a.get("address", "")).lower() == out["agent"].lower():
                out["approved"] = True
                until = a.get("validUntil")
                if until:
                    out["valid_until"] = datetime.fromtimestamp(int(until) / 1000, timezone.utc).date().isoformat()
        if out["approved"]:
            break
    if out["approved"] is None and masters:
        out["approved"] = False
        out["problems"].append("Hyperliquid hasn't approved this API wallet yet (More → API in the app). "
                               "If Trader trades a sub-account, also set HL_MASTER_ADDRESS to your main address.")
    return out


def require_safe_for_live() -> None:
    """Called before any live order. Refuses anything that could withdraw or isn't set up."""
    if not settings.hl_account or not ADDRESS.match(settings.hl_account):
        raise WalletError("Live trading needs HL_ACCOUNT_ADDRESS (the account Trader trades for).")
    if not settings.hl_agent_key:
        raise WalletError("Live trading needs Trader's API wallet: run `trader wallet new`.")
    if address_of(settings.hl_agent_key).lower() == settings.hl_account.lower():
        raise WalletError("Refusing to trade with the account's own key: it can withdraw. Use an API wallet.")


def enable_stock_markets() -> Any:
    """Let the API wallet use the account's USDC on the HIP-3 stock markets ("dex abstraction")."""
    from .hyperliquid import LiveBroker

    return LiveBroker().ex.agent_enable_dex_abstraction()
