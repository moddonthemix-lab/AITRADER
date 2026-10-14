# Giving Trader his own wallet on Hyperliquid

Trader trades stocks on Hyperliquid: perpetuals on the "HIP-3" stock markets, named like `xyz:NVDA`,
`xyz:TSLA`, `xyz:SP500` (trade.xyz) or `mkts:US500`, `mkts:USTECH` (Markets by Kinetiq). He picks the
most liquid market for each stock automatically.

**Do this only after a few weeks of paper trading look good.** Until then `TRADER_MODE=paper` needs no
wallet at all: Trader uses real Hyperliquid prices with pretend money.

## How the money stays safe

| Who | Holds | Can withdraw? |
|---|---|---|
| **You** (your own wallet) | the money, and the only key that can move it | Yes — only you |
| **Trader** (an *API wallet*) | permission to place and cancel orders for one account | **No, never** |

An API wallet (Hyperliquid also calls it an "agent wallet") is Hyperliquid's own feature for trading bots:
it can trade an account, but it cannot transfer or withdraw anything. Trader refuses to trade live with any
key that could withdraw.

## Steps (one time)

1. **Make Trader's account.** Best: a **sub-account** just for Trader in the Hyperliquid app (Hyperliquid
   may require some trading volume before sub-accounts unlock). Or use a separate wallet that only holds
   Trader's money. Put in only what Trader may trade with.
   Copy that account's address (`0x…`) into Trader's `.env`:
   ```bash
   HL_ACCOUNT_ADDRESS=0x...
   HL_MASTER_ADDRESS=0x...   # only if it's a sub-account: your main address
   ```
2. **Install the trading tools** (in the Trader folder, with the virtual environment on):
   ```bash
   pip install -e '.[all]'
   ```
3. **Make Trader's API wallet:**
   ```bash
   trader wallet new
   ```
   It prints Trader's new address and saves his key in `.env` (only this Mac's user can read it).
   The key is never shown, posted or sent anywhere.
4. **Approve it in Hyperliquid:** app.hyperliquid.xyz → **More → API**. Name it `Trader`, paste the
   address from step 3, choose Trader's account (the sub-account, if you made one), and **Authorize**.
   Hyperliquid lets you pick how long the approval lasts (up to 180 days).
5. **Let it use the stock markets.** The stock markets keep their margin separately from the main
   account. In the app, move USDC to the stock market Trader uses (e.g. trade.xyz), or run once:
   ```bash
   trader wallet enable-stock-markets
   ```
6. **Check:**
   ```bash
   trader wallet status
   trader doctor
   ```
   `approved: true` and no problems = ready.
7. **Go live** only when you decide: in `.env` set `TRADER_MODE=live`, then restart Trader.
   Every live trade still waits for your 👍 in `#trades`.

## Changing Trader's wallet (do it periodically)

```bash
trader wallet new --yes
```
Approve the new address in **More → API**, then **remove the old one** there. Trader uses the new key after
a restart. When an approval expires, Trader's orders are refused until you approve a new wallet; `trader
doctor` shows the expiry date.

## Fees

The stock markets charge more than Hyperliquid's crypto markets. Trader plans with a safe
`TRADER_FEE_PCT=0.09` (% per side) and includes fees in every target, R:R and P&L number. `trader doctor`
shows your account's base fee for comparison.
