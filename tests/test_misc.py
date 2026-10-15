"""Wallet safety, the Strat documents, backtests and learning, market selection."""

import pytest
from conftest import bars

from trader import backtest, hyperliquid as hl, learning, wallet
from trader.config import settings
from trader.knowledge import Library, folders


def test_trader_refuses_a_key_that_could_withdraw(monkeypatch):
    pytest.importorskip("eth_account")
    address, key = wallet.new_key()
    monkeypatch.setattr(settings, "hl_account", address)
    monkeypatch.setattr(settings, "hl_agent_key", key)
    with pytest.raises(wallet.WalletError, match="can withdraw"):
        wallet.require_safe_for_live()
    other, _ = wallet.new_key()
    monkeypatch.setattr(settings, "hl_account", other)
    wallet.require_safe_for_live()  # a separate API wallet is fine


def test_wallet_key_saved_privately(tmp_path):
    pytest.importorskip("eth_account")
    env = tmp_path / ".env"
    env.write_text("HUB_URL=x\nHL_AGENT_PRIVATE_KEY=0xold\n")
    _, key = wallet.new_key()
    wallet.save_key(key, env)
    text = env.read_text()
    assert "0xold" not in text and text.count("HL_AGENT_PRIVATE_KEY=") == 1 and "HUB_URL=x" in text
    assert oct(env.stat().st_mode)[-3:] == "600"


def test_the_strat_documents_are_searchable(tmp_path):
    (tmp_path / "my notes.md").write_text("My rule: a 3-1-2 on the weekly is my favourite setup, stop under the inside bar.")
    lib = Library([folders()[0], tmp_path])
    files = lib.summary()["files"]
    assert "01_THE_STRAT_PLAYBOOK.md" in files and "my notes.md" in files
    hits = lib.search("inside bar stop 3-1-2")
    assert hits and any("inside" in h["text"].lower() for h in hits)
    assert "hard stop" in lib.context("what is the hard stop rule").lower()


def test_backtest_runs_the_same_rules_and_feeds_learning(journal):
    import math

    rows, px = [], 100.0
    for i in range(400):  # a rising, swinging market with regular inside bars
        drift = 0.4 + 3 * math.sin(i / 6)
        o = px
        c = px + drift
        h, low = max(o, c) + 1.5, min(o, c) - 1.5
        if i % 7 == 3:  # an inside bar
            prev = rows[-1]
            h, low = prev[1] - 0.2, prev[2] + 0.2
            c = (h + low) / 2
        rows.append((o, h, low, c))
        px = c
    daily = bars(rows)
    res = backtest.run("TEST", "D", source=lambda s, tf: (daily, {"gmtoffset": 0}))
    assert res["all"]["trades"] > 0
    assert set(res["exits"]) == {"target", "trail", "stop", "time"}
    backtest.learn(journal, res)
    table = learning.table(journal)
    assert table and all("edge_r" in r for r in table)
    assert "Backtest TEST" in backtest.text(res)


def test_learning_benches_a_losing_pattern(journal):
    learning.record_backtest(journal, {("2-2 reversal", "D"): [-1.0] * 40}, "backtest:X")
    assert learning.benched(journal, "2-2 reversal", "D")
    learning.record_backtest(journal, {("3-1-2", "D"): [1.2, -1, 2, 0.5] * 10}, "backtest:X")
    assert not learning.benched(journal, "3-1-2", "D")
    assert learning.edge(journal, "3-1-2", "D")[0] > 0


def test_universe_picks_the_most_liquid_market_per_stock(monkeypatch):
    mk = lambda coin, vol, kind_ticker=None: hl.Instrument(coin, coin.split(":")[0], coin.split(":")[1],
                                                           coin.split(":")[1], 3, 10, False, vol, 100.0)
    markets = [mk("xyz:NVDA", 2e8), mk("km:NVDA", 3e6), mk("mkts:NVDA", 0), mk("xyz:GOLD", 9e8), mk("mkts:US500", 2e6)]
    monkeypatch.setattr(settings, "min_day_volume_usd", 1e6)
    u = hl.universe([], markets)
    coins = [m.coin for m in u]
    assert "xyz:NVDA" in coins and "km:NVDA" not in coins and "mkts:NVDA" not in coins
    assert "xyz:GOLD" not in coins  # stocks and indexes only by default
    assert "mkts:US500" in coins
    assert [m.coin for m in hl.universe(["mkts:US500"], markets)] == ["mkts:US500"]
    assert [m.coin for m in hl.universe(["NVDA"], markets)] == ["xyz:NVDA"]


def test_hyperliquid_price_and_size_rounding():
    assert hl.round_px(123.456789, 3) == 123.46  # 5 significant figures
    assert hl.round_px(0.0123456, 0) == 0.012346
    assert hl.round_size(1.23456, 3) == 1.234


def test_only_markets_margined_in_the_collateral_trader_holds(monkeypatch):
    mk = lambda coin, coll: hl.Instrument(coin, coin.split(":")[0], coin.split(":")[1], coin.split(":")[1], 3, 10,
                                          False, 1e8, 100.0, coll)
    markets = [mk("km:NVDA", "USDH"), mk("xyz:NVDA", "USDC"), mk("cash:TSLA", "USDT0")]
    monkeypatch.setattr(settings, "min_day_volume_usd", 1e6)
    monkeypatch.setattr(settings, "collateral", ["USDC"])
    assert [m.coin for m in hl.universe([], markets)] == ["xyz:NVDA"]
    monkeypatch.setattr(settings, "collateral", ["USDC", "USDT0"])
    assert {m.coin for m in hl.universe([], markets)} == {"xyz:NVDA", "cash:TSLA"}


def test_smallest_order_clears_the_ten_dollar_minimum():
    for px, d in ((217.7, 3), (1.12, 1), (29765.0, 4), (8.08, 1)):
        size = hl.min_size(px, d)
        assert size * px >= 10 and round(size, d) == size


def test_live_broker_prices_by_perp_rules_and_reports_fills(monkeypatch):
    """The live order path with a stand-in exchange: IOC limit priced by the perp rule, reduce-only closes,
    exchange-side stop, unified-account balance."""
    from types import SimpleNamespace

    calls = []

    class Ex:
        def order(self, coin, is_buy, sz, px, order_type, reduce_only=False):
            calls.append((coin, is_buy, sz, px, order_type, reduce_only))
            if "trigger" in order_type:
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 77}}]}}}
            return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"avgPx": str(px), "totalSz": str(sz), "oid": 5}}]}}}

        def update_leverage(self, lev, coin, is_cross=True):
            calls.append(("lev", lev, coin, is_cross))
            return {"status": "ok"}

        def cancel(self, coin, oid):
            calls.append(("cancel", coin, oid))

    b = hl.LiveBroker.__new__(hl.LiveBroker)
    b.ex, b.account, b._leverage_set = Ex(), "0xabc", set()
    b.info = SimpleNamespace(user_state=lambda a, d: {"assetPositions": [{"position": {"coin": "xyz:BB", "szi": "1.5"}}]})
    monkeypatch.setattr(b, "mid", lambda c: 8.08123)
    inst = hl.Instrument("xyz:BB", "xyz", "BB", "BB", 3, 10, True, 1e7, 8.08)
    f = b.open(inst, True, 1.5004)
    coin, is_buy, sz, px, ot, ro = calls[1]
    assert calls[0] == ("lev", 1, "xyz:BB", False)  # isolated, 1x
    assert (coin, is_buy, sz, ot, ro) == ("xyz:BB", True, 1.5, {"limit": {"tif": "Ioc"}}, False)
    assert px == hl.round_px(8.08123 * 1.01, 3) and len(str(px).split(".")[1]) <= 3  # 6 - szDecimals
    assert f.size == 1.5
    b.close(inst, True, 9.0)  # never more than what's held
    assert calls[-1][1] is False and calls[-1][2] == 1.5 and calls[-1][5] is True
    ids = b.protect(inst, True, 1.5, 7.5, None)
    assert ids == {"sl": "77"} and calls[-1][4]["trigger"]["tpsl"] == "sl" and calls[-1][5] is True
    monkeypatch.setattr(hl, "_info", lambda body: "unifiedAccount" if body["type"] == "userAbstraction" else
                        {"balances": [{"coin": "USDC", "total": "250.5"}, {"coin": "HYPE", "total": "3"}]})
    assert b.equity(inst) == 250.5


def test_env_file_last_setting_wins(tmp_path, monkeypatch):
    from trader.config import _load_dotenv

    env = tmp_path / ".env"
    env.write_text("TRADER_TEST_X=old\n# comment\nTRADER_TEST_X=new\n")
    monkeypatch.delenv("TRADER_TEST_X", raising=False)
    _load_dotenv(env)
    import os

    assert os.environ["TRADER_TEST_X"] == "new"
    monkeypatch.delenv("TRADER_TEST_X")
