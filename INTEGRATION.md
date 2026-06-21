# Connecting the forecast bot → the Telegram trader

Your forecast bot (`mohanrajuap/weather`, `monitor.py`) is **alert-only**:
it pushes Telegram/ntfy cards and deliberately holds no private key. To
let the trader act on its decisions, we bridge the two with a **shared
`signals.json` file**, written at the exact instant the forecast bot
decides a NEW clean+reliable TRADE. The trader then asks you, in Telegram,
which position(s) to buy — nothing trades automatically.

```
monitor.py ─(crossed_up: TRADE + prob≥thr + edge + reliable)─► emit_signal(p)
                                                                    │ writes
                                                                    ▼
                                                              signals.json
                                                                    │ polled
                                                                    ▼
                                            weather_telegram_trader.py
                                                                    │ Telegram card
                                                                    ▼
                                            YOU tap: positions + amount + Confirm
                                                                    │
                                                                    ▼
                                            buys, then asks before each TP/SL sell
```

Why this point? In `monitor.py` the `crossed_up` branch
(`alert_signal(fmt_new_signal(p))`) fires **only** when:
`verdict == "TRADE"` **and** `prob >= THRESHOLD` **and** `has_edge`
**and** `reliable_ok` (post-peak confirmed) **and** it wasn't already
alerted. That is exactly the "peak confirmed, buy now" condition you
described — so `emit_signal` sets `buy_now: true` there.

---

## Step 1 — copy the emitter into the forecast repo

Put `signal_emitter.py` (in this folder) next to `monitor.py` in your
`weather` repo. Stdlib-only, no new dependencies.

## Step 2 — two additive edits to `monitor.py`

**(a)** Near the top, with the other imports:

```python
try:
    from signal_emitter import emit_signal
except Exception:
    emit_signal = None
```

**(b)** Inside the `if crossed_up:` block, right after
`alert_signal(fmt_new_signal(p))`:

```python
            if emit_signal:
                emit_signal(p)
```

Optionally add the same two lines in the `bucket_shifted` branch so that a
model bucket change (e.g. 35°C → 36°C) emits a fresh `signal_id`.

That's the whole change — purely additive, no existing logic touched. If
`signal_emitter.py` is missing or errors, `emit_signal` is `None` / a
no-op and the forecast bot behaves exactly as before.

## Step 3 — point both bots at the same file

On Railway, mount **one Volume** at `/data` and set, in **both** services:

```
SIGNALS_FILE=/data/signals.json
```

(The execution bot also keeps `STATE_FILE=/data/weather_bot_state.json`.)
A Railway volume attaches to a single service, so the most reliable layout
is to run **both processes in one service** (a `Procfile` with two lines +
`honcho`, or a small `start.sh`). If your forecast bot already runs in its
own service, give that service the volume and have the execution bot run in
the same service.

---

## What a written signal looks like

`emit_signal(p)` writes a **menu of candidate buckets** per event — the
trader turns this into the Telegram card you pick from:

```json
{
  "signals": [
    {
      "signal_id": "shanghai|2026-06-22",
      "event_slug": "highest-temperature-in-shanghai-on-june-22-2026",
      "city": "shanghai",
      "target_date": "2026-06-22",
      "temp_unit": "°C",
      "tp_price": 0.90,
      "sl_price": 0.20,
      "buy_now": true,
      "candidates": [
        {"bucket":"33","side":"YES","model_prob":0.50,"price":0.48,"edge":0.02,"token_id":"...","is_best":true},
        {"bucket":"32","side":"YES","model_prob":0.40,"price":0.40,"edge":0.00,"token_id":"...","is_best":false},
        {"bucket":"34","side":"YES","model_prob":0.08,"price":0.06,"edge":0.02,"token_id":"...","is_best":false}
      ]
    }
  ]
}
```

Field mapping from the forecast `p` object:

| Signal field | Source in `p` |
|---|---|
| `event_slug` | `p["polymarket"]["url"]` → after `/event/` |
| `candidates[]` | built from `p["edges"]` (top `SIGNAL_MAX_CANDIDATES` by model prob) |
| `candidates[].bucket` / `model_prob` / `price` | edge `temp` / `model_prob` / `yes_price` |
| `candidates[].token_id` | `p["polymarket"]["buckets"][temp]["token_yes"]` |
| `candidates[].is_best` | matches `p["best_trade"]["temp"]` (pre-selected on the card) |

Tunable via env on the **forecast** side: `SIGNAL_MAX_CANDIDATES`
(default 5), `SIGNAL_TP_PRICE`, `SIGNAL_SL_PRICE`.

The trader prefers each candidate's `token_id` when present (skips a Gamma
lookup), falls back to resolving `event_slug` + `bucket` + `side`
otherwise, and fetches the event for the sub-market `endDate`.

---

## Two separate Telegram bots — don't confuse them

- The **forecast bot** uses Telegram to *alert you* (read-only cards).
- The **trader** uses Telegram for *interactive approval* (buttons that
  place orders). It needs its **own** `TELEGRAM_BOT_TOKEN`.

Use a **separate @BotFather bot** for the trader (recommended) so its
order buttons are isolated from the forecast alerts. `TELEGRAM_CHAT_ID`
can be the same chat — you'll just receive both the forecast alert and,
right after, the trader's actionable buy card.

## Alternative (no edit to the forecast bot): Telegram polling

If you'd rather not add the emitter, the trader could instead read the
forecast bot's Telegram channel (`getUpdates`) and parse the card — the
slug is in the `🔗 Open on Polymarket:` line, side/bucket in
`BUY YES 36°C @ 40¢`. No change to the forecast bot, but **more fragile**
(regex over a formatted card, and pre-peak "⏳ Potential" vs actionable
buy headers must be distinguished). The JSON emitter is recommended; ask
if you want the Telegram-reader variant.
