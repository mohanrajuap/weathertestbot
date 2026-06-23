# Snipe bot — Railway order-placement test

This deploys **`polymarket_5m_snipe_bot_v3_3.py` unchanged** (byte-for-byte
copy of your `profit taking/` script — no code modifications) so you can
run it on Railway and confirm real order placement works.

> ⚠️ **This places REAL trades with REAL USDC.** It's the actual BTC 5m
> momentum bot: when its conditions hit (a momentum leader crosses up into
> the 0.93–0.935 band), it buys `SHARES_PER_TRADE` (5) shares and manages
> a TP/SL bracket. Fund the wallet only with what you're willing to risk
> for the test. There is no dry-run switch in this script.

## Files
| File | Purpose |
|---|---|
| `polymarket_5m_snipe_bot_v3_3.py` | The bot, **verbatim**. |
| `requirements.txt` | `py-clob-client-v2` + deps. |
| `Procfile` / `railway.json` | Railway worker entrypoint. |
| `runtime.txt` | Pins Python 3.11 (eth/CLOB deps build cleanly). |

## Deploy on Railway
1. **New Project → Deploy from GitHub repo** → `mohanrajuap/weathertestbot`.
2. In the service **Settings → Root Directory**, set: `snipe_bot`
   (so Railway builds/deploys this folder, not the repo root).
3. **Variables** (Service → Variables):
   ```
   PRIVATE_KEY=0x...            # signer key
   FUNDER_ADDRESS=0x...         # your Polymarket funder/proxy wallet
   ```
   The script reads these from the environment (its `load_dotenv()` is a
   no-op on Railway — no `.env` needed).
4. Deploy. Watch **Deploy Logs**: you should see
   `✅ API credentials initialized successfully`, then
   `🚀 Processing Market …` and live ASK prints each 5m window. When the
   entry conditions trigger, `✅ ORDER RESPONSE` confirms placement.

## Before it can fill
The funder wallet must hold **USDC on Polygon** and have Polymarket
**allowances set** (place one manual trade in the Polymarket UI first if
you never have). Otherwise the buy will reject on allowance.

## Notes
- Logs go to both stdout (Railway captures them) and
  `btc_5m_breakout_bot_log.txt` (git-ignored; Railway's disk is ephemeral).
- Restart policy is ALWAYS, so a transient network drop self-recovers.
- The bot only trades the current BTC 5m window — it can sit through
  several windows logging prices before conditions trigger an order.
