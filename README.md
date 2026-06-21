# Polymarket Weather Trader (Telegram, human-in-the-loop)

An **approval-based** trader for Polymarket **"Highest temperature in
&lt;city&gt; on &lt;date&gt;"** markets. It does **not** trade on its own.

Your existing **forecast bot** (`mohanrajuap/weather`) stays the brain —
it decides which temperature bucket has an edge and when the peak is
confirmed. This bot is the **execution layer with you in the loop**:

1. Forecast bot writes a signal (a menu of candidate buckets) to a shared
   `signals.json`.
2. This bot sends you a **Telegram card** with inline buttons:
   - tap to **select** one or more positions (multi-select)
   - choose **unit** → 💵 USD or 📊 Shares
   - choose **amount** → preset buttons **or** ✏️ Custom (type a number)
   - **✅ Confirm Buy** / **❌ Cancel**
3. It buys **only** what you approved (the amount applies to each selected
   position).
4. At **take-profit / stop-loss** it sends another prompt and waits for
   your **✅ Sell now / ✋ Hold** — nothing is sold without your tap.

> **No order is ever placed without an explicit button press from you.**

Order/auth/price code is shared (`pm_client.py`) and modeled on your
`polymarket_98_bot.py`.

---

## The Telegram card

```
📈 Trade found — Shanghai (2026-06-22)
Select position(s) to buy, set unit & amount, then Confirm.
☑️ 33°C ⭐ · model 50% · mkt 48¢
▫️ 32°C    · model 40% · mkt 40¢
▫️ 34°C    · model 8%  · mkt 6¢
Selected: 33°C
Unit: 💵 USD · Amount: —
[ ☑️ 33°C · 50% · 48¢ ]
[ ▫️ 32°C · 40% · 40¢ ]
[ ▫️ 34°C · 8% · 6¢ ]
[ ● 💵 USD ] [ 📊 Shares ]
[ $5 ] [ $10 ] [ $25 ] [ $50 ]
[ ✏️ Custom amount ]
[ ✅ Confirm Buy ] [ ❌ Cancel ]
```

- **Multi-select**: tap several buckets; the chosen amount is bought for
  **each** of them.
- **USD** → converted to whole shares at the live price per position.
  **Shares** → that many shares per position.
- ⭐ marks the model's best trade (pre-selected for convenience).

---

## Files

| File | Purpose |
|------|---------|
| `weather_telegram_trader.py` | **Main bot** — Telegram approval UI + execution. |
| `pm_client.py` | Shared Polymarket CLOB helpers (orders, prices, balance). |
| `signal_emitter.py` | **Drop into the forecast repo** — writes `signals.json`. See `INTEGRATION.md`. |
| `signals.example.json` | The signal/menu contract. |
| `weather_execution_bot.py` | Optional **autonomous** variant (no Telegram). Not used by default. |
| `requirements.txt` / `Procfile` / `railway.json` / `.env.example` | Deploy. |
| `INTEGRATION.md` | Exact hook to wire the forecast bot to this trader. |

---

## Setup

### 1. A Telegram bot for the trader
Create a **separate** bot via **@BotFather** (so its order buttons are
isolated from your forecast alerts). Note the token. Get your numeric
chat id (message the bot, then check `getUpdates`, or use @userinfobot).

### 2. Wire the forecast bot to emit signals
Follow **`INTEGRATION.md`**: copy `signal_emitter.py` next to `monitor.py`
in your `weather` repo and add the two-line hook. That writes
`signals.json` whenever a clean, peak-confirmed TRADE appears.

### 3. Local rehearsal (DRY_RUN)
```bash
cd weather_bot
python -m venv .venv && . .venv/Scripts/activate     # Windows
# or: python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env     # fill PRIVATE_KEY, FUNDER_ADDRESS, TELEGRAM_* ; keep DRY_RUN=true
cp signals.example.json signals.json   # set buy_now=true and real token_ids to test

python weather_telegram_trader.py
```
You'll get the card in Telegram. Tap through select → amount → Confirm and
watch the log "place" dry-run orders. When happy, set `DRY_RUN=false`.

---

## Deploy on Railway

The forecast bot and this trader **share `signals.json`**, so run them so
they see the same file (a **Volume** mounted at `/data`).

1. **Service**: deploy this folder (`railway up` or from a repo). Nixpacks
   auto-detects Python from `requirements.txt`; `railway.json` sets the
   start command + always-restart.
2. **Volume**: mount at `/data`; set in both bots:
   `SIGNALS_FILE=/data/signals.json`, and here `STATE_FILE=/data/trader_state.json`.
   (A Railway volume attaches to one service — the most reliable layout is
   both processes in one service via a two-line `Procfile` + `honcho`, or a
   `start.sh`.)
3. **Variables** (Service → Variables):
   ```
   PRIVATE_KEY=0x...
   FUNDER_ADDRESS=0x...
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   SIGNALS_FILE=/data/signals.json
   STATE_FILE=/data/trader_state.json
   DRY_RUN=true
   ```
   Tune amounts/exits from `.env.example` (`USD_PRESETS`, `SHARE_PRESETS`,
   `TP_PRICE`, `SL_PRICE`, `ENTRY_MAX`, …).
4. Watch logs → confirm wallet + API creds init and "Weather Trader online"
   lands in Telegram. Rehearse one card in DRY_RUN, then set
   `DRY_RUN=false` and redeploy.

---

## Commands & safety

- Telegram: `/positions` (list open trades), `/help`.
- Only `TELEGRAM_CHAT_ID`(s) can press buttons; others are ignored.
- `trader_state.json` persists open positions + handled signals across
  restarts — keep it on the Railway volume.
- `ENTRY_MAX` is a hard ceiling above any approved buy.
- Stop-loss sells **cross the book** (bid − buffer, floored at `SL_FLOOR`)
  so an approved exit actually fills.
- Keep `PRIVATE_KEY` only in Railway's encrypted variables (`.env` is
  git-ignored).
