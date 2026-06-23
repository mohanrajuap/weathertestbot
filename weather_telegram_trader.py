# =========================================
# Polymarket Weather Telegram Trader
# v2 — human-in-the-loop, NOT autonomous
#
# Flow:
#   1. Your forecast bot writes a signal (a menu of candidate
#      temperature buckets) to SIGNALS_FILE via signal_emitter.py.
#   2. This bot sends you a TELEGRAM CARD with inline buttons:
#        • tap to SELECT one or more positions (multi-select)
#        • choose UNIT  → 💵 USD or 📊 Shares
#        • choose AMOUNT → preset buttons or ✏️ Custom (type a number)
#        • ✅ Confirm Buy   /   ❌ Cancel
#   3. It buys ONLY what you approved (the amount applies to each
#      selected position).
#   4. At take-profit / stop-loss it sends ANOTHER prompt and waits for
#      your ✅ Sell now / ✋ Hold — nothing is sold without your tap.
#
# No trade ever happens without an explicit button press from you.
# Start with DRY_RUN=true to rehearse the whole flow safely.
# =========================================

import os
import sys
import json
import math
import time
import logging
import threading
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

import pm_client as pm

load_dotenv()

# ===================== CONFIG =====================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# Comma-separated chat ids allowed to control the bot. First = where new
# trade cards are sent.
_CHAT_RAW = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ALLOWED_CHATS = [c.strip() for c in _CHAT_RAW.split(",") if c.strip()]
PRIMARY_CHAT = ALLOWED_CHATS[0] if ALLOWED_CHATS else None

SIGNALS_FILE = os.getenv("SIGNALS_FILE", "signals.json")
STATE_FILE   = os.getenv("STATE_FILE", "trader_state.json")

DEFAULT_TP_PRICE = float(os.getenv("TP_PRICE", "0.90"))
DEFAULT_SL_PRICE = float(os.getenv("SL_PRICE", "0.20"))

USD_PRESETS    = [float(x) for x in os.getenv("USD_PRESETS", "1,5,10,25,50").split(",")]
SHARE_PRESETS  = [int(float(x)) for x in os.getenv("SHARE_PRESETS", "5,10,25,50").split(",")]
# Always keep the $1 / 5-share minimum as a one-tap option even if an old
# env var omits it.
if 1.0 not in USD_PRESETS:
    USD_PRESETS = [1.0] + USD_PRESETS
if 5 not in SHARE_PRESETS:
    SHARE_PRESETS = [5] + SHARE_PRESETS
# Polymarket rejects orders below BOTH a ~$1 notional AND a 5-share minimum.
MIN_ORDER_USD  = float(os.getenv("MIN_ORDER_USD", "1.0"))
MIN_SHARES     = int(os.getenv("MIN_SHARES", "5"))
DEFAULT_UNIT   = os.getenv("DEFAULT_UNIT", "SHARES").strip().upper()  # SHARES | USD

SIGNAL_POLL_S   = float(os.getenv("SIGNAL_POLL_S", "5"))
MONITOR_POLL_S  = float(os.getenv("MONITOR_POLL_S", "5"))
FILL_TIMEOUT_S  = int(os.getenv("FILL_TIMEOUT_S", "45"))
MAX_HOLD_HOURS  = float(os.getenv("MAX_HOLD_HOURS", "30"))

# /test with no slug uses this (override with TEST_EVENT_SLUG). Update the
# date to a currently-live event, or always pass a slug: /test <slug>.
DEFAULT_TEST_SLUG = os.getenv("TEST_EVENT_SLUG",
                             "highest-temperature-in-london-on-june-24-2026")

API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ===================== LOGGING =====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("weather_trader_log.txt", mode="a", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("trader")

if not TELEGRAM_TOKEN or not PRIMARY_CHAT:
    logger.error("❌ Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    sys.exit(1)

# ===================== TELEGRAM API =====================

_send_lock = threading.Lock()
_order_lock = threading.Lock()

def tg(method, **params):
    try:
        with _send_lock:
            r = requests.post(f"{API}/{method}", json=params, timeout=15)
        data = r.json()
        if not data.get("ok"):
            logger.warning(f"⚠️ Telegram {method} not ok: {data.get('description')}")
        return data
    except Exception as e:
        logger.warning(f"⚠️ Telegram {method} failed: {e}")
        return {"ok": False}

def send_message(chat_id, text, keyboard=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if keyboard is not None:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    data = tg("sendMessage", **params)
    return (data.get("result") or {}).get("message_id") if data.get("ok") else None

def edit_message(chat_id, message_id, text, keyboard=None):
    params = {"chat_id": chat_id, "message_id": message_id, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard is not None:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    tg("editMessageText", **params)

def answer_callback(cb_id, text=None):
    p = {"callback_query_id": cb_id}
    if text:
        p["text"] = text
    tg("answerCallbackQuery", **p)

# ===================== STATE =====================
# sessions  : pending buy approvals (in memory; lost on restart = re-send)
# positions : open trades being watched for TP/SL (persisted)
# processed : signal_ids we've already turned into a card (persisted)
# awaiting  : chat_id -> session_id waiting for a typed custom amount

sessions = {}
positions = {}
processed = set()
awaiting = {}

_sid_counter = 0
_pid_counter = 0

def _next_sid():
    global _sid_counter
    _sid_counter += 1
    return format(_sid_counter, "x")

def _next_pid():
    global _pid_counter
    _pid_counter += 1
    return "p" + format(_pid_counter, "x")

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "processed": sorted(processed),
                "positions": positions,
                "pid_counter": _pid_counter,
            }, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"⚠️ save_state failed: {e}")

def load_state():
    global processed, positions, _pid_counter
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        processed = set(d.get("processed", []))
        positions = d.get("positions", {}) or {}
        _pid_counter = int(d.get("pid_counter", 0))
        logger.info(f"↩️ Restored {len(processed)} signals, {len(positions)} open positions")
    except Exception:
        pass

# ===================== BUY CARD RENDER =====================

def _fmt_pct(x):
    return f"{x*100:.0f}%" if isinstance(x, (int, float)) else "?"

def _fmt_cents(x):
    return f"{x*100:.0f}¢" if isinstance(x, (int, float)) else "?"

def render_card(sess):
    s = sess
    unit = s["unit"]
    amt = s["amount"]
    head = (
        f"📈 <b>Trade found — {s['city'].title()} "
        f"({s['target_date']})</b>\n"
        f"Select position(s) to buy, set unit &amp; amount, then Confirm.\n"
    )
    lines = []
    for i, c in enumerate(s["candidates"]):
        chk = "☑️" if i in s["selected"] else "▫️"
        star = "⭐" if c.get("is_best") else ""
        price = c.get("price")
        # per-share upside if THIS bucket wins: a YES share pays $1, so
        # profit = (1 - price). e.g. 45¢ → +55¢/sh.
        win = f"win +{(1.0 - price) * 100:.0f}¢" if isinstance(price, (int, float)) else ""
        lines.append(
            f"{chk} <b>{c['bucket']}{s['unit_sym']}</b> {star} · "
            f"mkt {_fmt_cents(price)} · {win}"
        )

    sel_idx = sorted(s["selected"])
    sel = ", ".join(f"{s['candidates'][i]['bucket']}{s['unit_sym']}" for i in sel_idx) or "—"
    amt_str = (f"${amt:g}" if unit == "USD" else f"{amt:g} sh") if amt else "—"

    # ── edge calculator for the SELECTED combination ──
    edge_line = ""
    if sel_idx:
        cost = sum((s["candidates"][i].get("price") or 0.0) for i in sel_idx)
        edge = 1.0 - cost
        ret = (edge / cost * 100) if cost > 0 else 0
        if len(sel_idx) == 1:
            edge_line = (f"\n🧮 <b>Edge</b>: pay {cost*100:.0f}¢ → $1 if it hits · "
                         f"+{edge*100:.0f}¢/sh ({ret:.0f}%)")
        elif edge > 0:
            edge_line = (f"\n🧮 <b>Edge</b>: {len(sel_idx)} buckets cost {cost*100:.0f}¢/set → "
                         f"$1 if it lands in any · +{edge*100:.0f}¢/set ({ret:.0f}%) — "
                         f"<i>no loss if the high is in your range</i>")
        else:
            edge_line = (f"\n🧮 <b>Edge</b>: {len(sel_idx)} buckets cost {cost*100:.0f}¢/set ≥ $1 → "
                         f"<i>no edge (overpriced together)</i>")

    foot = (
        f"\nSelected: <b>{sel}</b>{edge_line}\n"
        f"Unit: <b>{'💵 USD' if unit=='USD' else '📊 Shares'}</b> · "
        f"Amount: <b>{amt_str}</b>"
        + ("  (applied to each)" if len(sel_idx) > 1 else "")
    )
    return head + "\n".join(lines) + foot

def render_keyboard(sess):
    s = sess
    sid = s["sid"]
    kb = []
    # one toggle button per candidate (price + upside if it wins)
    for i, c in enumerate(s["candidates"]):
        chk = "☑️" if i in s["selected"] else "▫️"
        price = c.get("price")
        win = f" · win +{(1.0 - price) * 100:.0f}¢" if isinstance(price, (int, float)) else ""
        kb.append([{
            "text": f"{chk} {c['bucket']}{s['unit_sym']} · {_fmt_cents(price)}{win}",
            "callback_data": f"b|{sid}|t|{i}",
        }])
    # unit row
    u = s["unit"]
    kb.append([
        {"text": ("● " if u == "USD" else "") + "💵 USD",   "callback_data": f"b|{sid}|u|USD"},
        {"text": ("● " if u == "SHARES" else "") + "📊 Shares", "callback_data": f"b|{sid}|u|SHARES"},
    ])
    # amount presets
    presets = USD_PRESETS if u == "USD" else SHARE_PRESETS
    row = []
    for n in presets:
        label = (f"${n:g}" if u == "USD" else f"{n:g}")
        mark = "● " if (s["amount"] == n) else ""
        row.append({"text": mark + label, "callback_data": f"b|{sid}|a|{n:g}"})
    kb.append(row)
    kb.append([{"text": "✏️ Custom amount", "callback_data": f"b|{sid}|c"}])
    # confirm / cancel
    kb.append([
        {"text": "✅ Confirm Buy", "callback_data": f"b|{sid}|go"},
        {"text": "❌ Cancel",      "callback_data": f"b|{sid}|x"},
    ])
    return kb

def push_card(sess):
    """Send or update the card."""
    text = render_card(sess)
    kb = render_keyboard(sess)
    if sess.get("message_id"):
        edit_message(sess["chat_id"], sess["message_id"], text, kb)
    else:
        mid = send_message(sess["chat_id"], text, kb)
        sess["message_id"] = mid

# ===================== SIGNAL → CARD =====================

def handle_new_signal(sig):
    sid_key = str(sig.get("signal_id"))
    if not sig.get("buy_now"):
        return
    if sid_key in processed:
        return
    cands = sig.get("candidates") or []
    if not cands:
        return

    unit_sym = sig.get("temp_unit") or "°"
    sid = _next_sid()
    sess = {
        "sid": sid,
        "signal_id": sid_key,
        "event_slug": sig.get("event_slug"),
        "city": sig.get("city") or "?",
        "target_date": sig.get("target_date") or "?",
        "unit_sym": unit_sym,
        "candidates": cands,
        "tp_price": float(sig.get("tp_price", DEFAULT_TP_PRICE)),
        "sl_price": float(sig.get("sl_price", DEFAULT_SL_PRICE)),
        "selected": set(),
        "unit": DEFAULT_UNIT if DEFAULT_UNIT in ("USD", "SHARES") else "USD",
        "amount": None,
        "chat_id": PRIMARY_CHAT,
        "message_id": None,
        "status": "pending",
    }
    # NOTE: nothing is pre-selected — you only buy buckets you explicitly
    # tap, so a card can never place a position you didn't choose.
    sessions[sid] = sess
    push_card(sess)
    processed.add(sid_key)
    save_state()
    logger.info(f"📨 Sent trade card {sid} for {sid_key} ({len(cands)} candidates)")

# Only ever touch "Highest temperature in <city>" markets — never the
# city's lowest-temperature or precipitation/rain events.
HIGHEST_TEMP_PREFIX = "highest-temperature-in-"

def _is_highest_temp_slug(slug):
    return bool(slug) and str(slug).startswith(HIGHEST_TEMP_PREFIX)

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

def _parse_temp_slug(slug):
    """'highest-temperature-in-mexico-city-on-june-24-2026' →
    ('Mexico City', 'June 24 2026')."""
    try:
        rest = slug.split("-in-", 1)[1]
        city, date = rest.split("-on-", 1)
        return city.replace("-", " ").title(), date.replace("-", " ").title()
    except Exception:
        return slug, ""

def _slug_date(slug):
    """Date object for the event's day, or None. Used to prioritize TODAY's
    market and drop already-passed ones."""
    try:
        rest = slug.split("-on-", 1)[1]          # 'june-24-2026'
        mon, day, year = rest.split("-")
        from datetime import date as _date
        return _date(int(year), _MONTHS[mon.lower()], int(day))
    except Exception:
        return None

def build_test_signal(slug):
    """Build a signal from a LIVE temperature event so you can buy from any
    market without the forecast bot. Pulls the real buckets/prices/tokens off
    Gamma; model_prob is unknown (shown as '?') since there's no forecast."""
    # Guard: only "Highest temperature in …" markets — refuse lowest-temp,
    # precipitation, or any other weather event even if asked directly.
    if not _is_highest_temp_slug(slug):
        logger.warning(f"refusing non-highest-temperature slug: {slug}")
        return None
    ev = pm.fetch_event(slug)
    if not ev:
        return None
    cands = []
    for m in ev.get("markets", []) or []:
        if m.get("closed"):
            continue
        try:
            pr = json.loads(m.get("outcomePrices") or "[]")
            tk = json.loads(m.get("clobTokenIds") or "[]")
        except Exception:
            continue
        if not pr or not tk:
            continue
        temp = pm.bucket_temp(m.get("groupItemTitle"))
        cands.append({
            "bucket": str(temp) if temp is not None else (m.get("groupItemTitle") or "?"),
            "side": "YES", "model_prob": None,
            "price": float(pr[0]), "token_id": tk[0], "is_best": False,
        })
    # favorites first (highest YES price), keep it to a tidy menu
    cands.sort(key=lambda c: c["price"], reverse=True)
    cands = cands[:6]
    if not cands:
        return None
    cands[0]["is_best"] = True
    city, date = _parse_temp_slug(slug)
    return {
        "signal_id": f"{slug}|{int(time.time())}",   # unique each open
        "event_slug": slug, "city": city, "target_date": date or "—",
        "temp_unit": "°C", "tp_price": DEFAULT_TP_PRICE,
        "sl_price": DEFAULT_SL_PRICE, "buy_now": True, "candidates": cands,
    }

# ── live-markets browser ──
markets_cache = {}   # chat_id -> [(slug, city, date), …]

def _send_markets_menu(chat, city_filter=None):
    from datetime import date as _date
    send_message(chat, "🔎 Scanning live temperature markets …")
    events = pm.list_temperature_events()
    today = _date.today()

    parsed, seen = [], set()
    for e in events:
        slug = e.get("slug") or ""
        if not _is_highest_temp_slug(slug) or slug in seen:
            continue
        d = _slug_date(slug)
        if d is None or d < today:           # drop already-passed days
            continue
        city, date_label = _parse_temp_slug(slug)
        if city_filter and city_filter not in city.lower():
            continue
        seen.add(slug)
        parsed.append((d, city, slug, date_label))

    if not parsed:
        send_message(chat, "No live temperature markets found"
                     + (f" for '{city_filter}'." if city_filter else "."))
        return

    parsed.sort(key=lambda x: (x[0], x[1]))   # soonest date first, then city
    if city_filter:
        # show every upcoming date for that city (today first)
        rows = [(slug, city, dl) for (d, city, slug, dl) in parsed]
    else:
        # one button per city = its NEAREST upcoming market (today if live)
        by_city = {}
        for d, city, slug, dl in parsed:
            if city not in by_city:           # parsed is date-sorted → earliest wins
                by_city[city] = (slug, city, dl)
        rows = sorted(by_city.values(), key=lambda r: r[1])

    rows = rows[:60]
    markets_cache[str(chat)] = rows
    kb, row = [], []
    for i, (slug, city, date_label) in enumerate(rows):
        tag = " (today)" if _slug_date(slug) == today else ""
        row.append({"text": f"{city} · {date_label}{tag}", "callback_data": f"m|{i}"})
        if len(row) == 2:
            kb.append(row); row = []
    if row:
        kb.append(row)
    send_message(chat,
        f"🌡️ <b>{len(rows)} live temperature markets</b>"
        + (f" for '{city_filter}'" if city_filter else " (nearest day per city)")
        + "\nTap one to get its buy card:", kb)

# ===================== BUY EXECUTION =====================

def _shares_for(unit, amount, ask):
    """WHOLE share count for one position (no fractional fills). Buying is
    share-based; we place a marketable LIMIT order for this many shares.
    Honors your input but never below Polymarket's minimums — at least
    MIN_SHARES (5) AND enough shares that shares×price ≥ MIN_ORDER_USD ($1).
    Returns (shares, bumped_bool)."""
    price = ask if (ask and ask > 0) else 0.5
    if unit == "SHARES":
        want = int(round(float(amount)))
        shares = max(MIN_SHARES, want)
    else:  # USD → whole shares
        want = max(1, int(float(amount) // price))
        shares = max(MIN_SHARES, math.ceil(max(float(amount), MIN_ORDER_USD) / price))
    while shares * price < MIN_ORDER_USD - 1e-6:
        shares += 1
    return shares, (shares > want)

def execute_buys(sess, cb_chat):
    if not sess["selected"]:
        send_message(cb_chat, "⚠️ No position selected — tap a bucket first.")
        return
    if not sess["amount"]:
        send_message(cb_chat, "⚠️ No amount set — pick a preset or ✏️ Custom.")
        return

    sess["status"] = "executing"
    edit_message(sess["chat_id"], sess["message_id"],
                 render_card(sess) + "\n\n⏳ <i>Placing orders…</i>", [])

    results = []
    with _order_lock:
        for i in sorted(sess["selected"]):
            c = sess["candidates"][i]
            bucket = c["bucket"]
            side = c.get("side", "YES")
            # resolve token + market (for endDate); prefer the carried token
            rtoken, market = pm.resolve_token(sess["event_slug"], bucket, side)
            token = c.get("token_id") or rtoken
            if not token:
                results.append(f"❌ {bucket}{sess['unit_sym']}: could not resolve token")
                continue
            ask = pm.best_ask(token)
            if ask is None:
                results.append(f"❌ {bucket}{sess['unit_sym']}: no live price")
                continue
            shares, bumped = _shares_for(sess["unit"], sess["amount"], ask)
            max_price = round(ask + 0.05, 3)  # marketable limit: cross to fill

            oid, limit = pm.place_buy(token, ask, max_price, shares)
            if not oid:
                results.append(f"❌ {bucket}{sess['unit_sym']}: order rejected")
                continue

            # confirm fill
            filled = _await_fill(oid)
            if filled == 0:
                pm.cancel_order(oid)
                results.append(f"⚠️ {bucket}{sess['unit_sym']}: not filled, cancelled")
                continue
            held = shares if filled < 0 else int(filled)  # <0 = dry-run sentinel
            if held <= 0:
                results.append(f"⚠️ {bucket}{sess['unit_sym']}: 0 shares filled")
                continue

            pid = _next_pid()
            end_ts = pm.market_end_ts(market) or (time.time() + MAX_HOLD_HOURS * 3600)
            positions[pid] = {
                "pid": pid, "token_id": token, "bucket": bucket, "side": side,
                "city": sess["city"], "target_date": sess["target_date"],
                "event_slug": sess["event_slug"], "unit_sym": sess["unit_sym"],
                "shares": held, "entry_price": limit,
                "tp_price": sess["tp_price"], "sl_price": sess["sl_price"],
                "end_ts": end_ts, "tp_done": False, "sl_done": False,
                "status": "open", "chat_id": sess["chat_id"],
            }
            note = (f" — min {MIN_SHARES}sh/$1" if bumped else "")
            results.append(
                f"✅ {bucket}{sess['unit_sym']}: bought {held} sh @ ~{limit:.2f} "
                f"(~${held * limit:.2f}{note}, pid {pid})"
            )
            save_state()

    sess["status"] = "done"
    edit_message(sess["chat_id"], sess["message_id"],
                 render_card(sess) + "\n\n<b>Result</b>\n" + "\n".join(results), [])
    logger.info(f"🧾 Buys for {sess['sid']}: {results}")

def _await_fill(order_id):
    start = time.time()
    while time.time() - start < FILL_TIMEOUT_S:
        f = pm.get_filled_size(order_id)
        if f != 0:
            return f
        time.sleep(2)
    return 0

# ===================== EXIT (TP/SL) APPROVAL =====================

def send_exit_prompt(pos, kind, bid):
    pid = pos["pid"]
    pnl = (bid - pos["entry_price"]) * pos["shares"]
    emoji = "🏆" if kind == "TP" else "🛑"
    label = "TAKE-PROFIT" if kind == "TP" else "STOP-LOSS"
    text = (
        f"{emoji} <b>{pos['city'].title()} {pos['bucket']}{pos['unit_sym']} hit {label}</b>\n"
        f"bid <b>{_fmt_cents(bid)}</b> · entry {_fmt_cents(pos['entry_price'])} · "
        f"{pos['shares']} sh · est P&amp;L <b>${pnl:+.2f}</b>\n"
        f"Sell now?"
    )
    kb = [[
        {"text": "✅ Sell now", "callback_data": f"x|{pid}|s"},
        {"text": "✋ Hold",     "callback_data": f"x|{pid}|h"},
    ]]
    send_message(pos["chat_id"], text, kb)

def do_sell(pos, cb_chat=None):
    with _order_lock:
        oid, px = pm.sell_cross_book(pos["token_id"], pos["shares"], label="SELL")
    pos["status"] = "closed"
    save_state()
    msg = (f"💱 Sold {pos['shares']} {pos['bucket']}{pos['unit_sym']} @ ~{_fmt_cents(px)} "
           f"(entry {_fmt_cents(pos['entry_price'])})")
    send_message(cb_chat or pos["chat_id"], msg)
    logger.info(msg)

# ===================== CALLBACK HANDLING =====================

def handle_callback(cb):
    cb_id = cb["id"]
    data = cb.get("data") or ""
    chat = str((cb.get("message") or {}).get("chat", {}).get("id", ""))
    from_chat = str((cb.get("from") or {}).get("id", ""))

    if ALLOWED_CHATS and from_chat not in ALLOWED_CHATS and chat not in ALLOWED_CHATS:
        answer_callback(cb_id, "Not authorized")
        return

    parts = data.split("|")
    kind = parts[0]

    # ── buy session callbacks ──
    if kind == "b" and len(parts) >= 3:
        sid = parts[1]
        sess = sessions.get(sid)
        if not sess or sess["status"] not in ("pending",):
            answer_callback(cb_id, "This card has expired.")
            return
        act = parts[2]
        if act == "t":                       # toggle candidate
            i = int(parts[3])
            if i in sess["selected"]:
                sess["selected"].discard(i)
            else:
                sess["selected"].add(i)
            answer_callback(cb_id)
            push_card(sess)
        elif act == "u":                     # unit
            sess["unit"] = parts[3]
            sess["amount"] = None            # reset amount on unit change
            answer_callback(cb_id, f"Unit: {parts[3]}")
            push_card(sess)
        elif act == "a":                     # preset amount
            sess["amount"] = float(parts[3])
            answer_callback(cb_id, f"Amount set")
            push_card(sess)
        elif act == "c":                     # custom amount
            awaiting[str(sess["chat_id"])] = sid
            answer_callback(cb_id)
            unit_word = "USD ($)" if sess["unit"] == "USD" else "shares"
            send_message(sess["chat_id"],
                         f"✏️ Reply with the amount in <b>{unit_word}</b> "
                         f"(e.g. <code>15</code>).")
        elif act == "go":                    # confirm
            answer_callback(cb_id, "Placing…")
            threading.Thread(target=execute_buys, args=(sess, chat), daemon=True).start()
        elif act == "x":                     # cancel
            sess["status"] = "cancelled"
            answer_callback(cb_id, "Cancelled")
            edit_message(sess["chat_id"], sess["message_id"],
                         render_card(sess) + "\n\n❌ <i>Cancelled.</i>", [])
        return

    # ── exit (sell) callbacks ──
    if kind == "x" and len(parts) >= 3:
        pid = parts[1]
        pos = positions.get(pid)
        if not pos or pos["status"] != "open":
            answer_callback(cb_id, "Position no longer open.")
            return
        if parts[2] == "s":
            answer_callback(cb_id, "Selling…")
            threading.Thread(target=do_sell, args=(pos, chat), daemon=True).start()
        elif parts[2] == "h":
            answer_callback(cb_id, "Holding.")
            send_message(chat, f"✋ Holding {pos['bucket']}{pos['unit_sym']} — "
                               f"I'll prompt again if it hits the other level.")
        return

    # ── markets-browser pick → build that event's card ──
    if kind == "m" and len(parts) >= 2:
        rows = markets_cache.get(chat) or []
        try:
            idx = int(parts[1])
        except ValueError:
            idx = -1
        if not (0 <= idx < len(rows)):
            answer_callback(cb_id, "List expired — send /markets again")
            return
        slug = rows[idx][0]
        answer_callback(cb_id, "Loading market…")

        def _open():
            try:
                sig = build_test_signal(slug)
            except Exception as e:
                sig = None
                logger.warning(f"markets pick build failed: {e}")
            if not sig or not sig.get("candidates"):
                send_message(chat, f"⚠️ No tradeable buckets for <code>{slug}</code>")
            else:
                handle_new_signal(sig)
        threading.Thread(target=_open, daemon=True).start()
        return

    answer_callback(cb_id)

def handle_text(msg):
    chat = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or "").strip()

    if ALLOWED_CHATS and chat not in ALLOWED_CHATS:
        return

    if text.startswith("/"):
        cmd = text.split()[0].lower()
        if cmd in ("/start", "/help"):
            send_message(chat,
                "🌡️ <b>Weather Trader</b>\nI send a card when your forecast bot "
                "finds a trade. Tap the bucket(s) you want, set amount, Confirm. "
                "Nothing is pre-selected and I ask before every sell.\n\n"
                "/markets [city] — browse ALL live temperature markets and pick one\n"
                "/test [event-slug] — card from a specific live event\n"
                "/positions — list open trades\n/help — this message")
        elif cmd == "/markets":
            parts = text.split(maxsplit=1)
            city_filter = parts[1].strip().lower() if len(parts) > 1 else None
            threading.Thread(target=_send_markets_menu, args=(chat, city_filter),
                             daemon=True).start()
        elif cmd == "/test":
            parts = text.split()
            slug = parts[1] if len(parts) > 1 else os.getenv("TEST_EVENT_SLUG", DEFAULT_TEST_SLUG)
            send_message(chat, f"🧪 Building a test card from <code>{slug}</code> …")
            try:
                sig = build_test_signal(slug)
            except Exception as e:
                sig = None
                logger.warning(f"/test build failed: {e}")
            if not sig or not sig.get("candidates"):
                send_message(chat, f"⚠️ No tradeable buckets found for <code>{slug}</code>. "
                                   f"Pass a live event slug: <code>/test highest-temperature-in-london-on-june-24-2026</code>")
            else:
                handle_new_signal(sig)
        elif cmd == "/positions":
            opens = [p for p in positions.values() if p["status"] == "open"]
            if not opens:
                send_message(chat, "No open positions.")
            else:
                lines = [f"• {p['bucket']}{p['unit_sym']} {p['city'].title()} · "
                         f"{p['shares']} sh @ {_fmt_cents(p['entry_price'])} (pid {p['pid']})"
                         for p in opens]
                send_message(chat, "<b>Open positions</b>\n" + "\n".join(lines))
        return

    # custom amount reply?
    sid = awaiting.get(chat)
    if sid:
        sess = sessions.get(sid)
        if sess and sess["status"] == "pending":
            try:
                val = float(text.replace("$", "").replace(",", "").strip())
                if val <= 0:
                    raise ValueError
                sess["amount"] = val
                awaiting.pop(chat, None)
                send_message(chat, f"✅ Amount set to "
                                   f"{('$'+format(val,'g')) if sess['unit']=='USD' else format(val,'g')+' sh'}.")
                push_card(sess)
            except Exception:
                send_message(chat, "⚠️ Please reply with a positive number, e.g. 15")
        else:
            awaiting.pop(chat, None)

# ===================== THREADS =====================

def telegram_loop():
    offset = None
    logger.info("📡 Telegram listener started")
    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{API}/getUpdates", params=params, timeout=35)
            data = r.json()
            if not data.get("ok"):
                time.sleep(2)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    handle_callback(upd["callback_query"])
                elif "message" in upd:
                    handle_text(upd["message"])
        except Exception as e:
            logger.warning(f"⚠️ telegram_loop: {e}")
            time.sleep(3)

def signal_loop():
    logger.info("👀 Signal watcher started")
    while True:
        try:
            if os.path.exists(SIGNALS_FILE):
                with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                sigs = raw.get("signals") if isinstance(raw, dict) else raw
                if isinstance(raw, dict) and "signals" not in raw:
                    sigs = [raw]
                for sig in (sigs or []):
                    if isinstance(sig, dict):
                        handle_new_signal(sig)
        except Exception as e:
            logger.warning(f"⚠️ signal_loop: {e}")
        time.sleep(SIGNAL_POLL_S)

def monitor_loop():
    logger.info("📊 Position monitor started")
    while True:
        try:
            for pos in list(positions.values()):
                if pos["status"] != "open":
                    continue
                now = time.time()
                if now >= pos["end_ts"]:
                    _resolve_position(pos)
                    continue
                bid = pm.best_bid(pos["token_id"])
                if bid is None:
                    continue
                if (not pos["tp_done"]) and bid >= pos["tp_price"]:
                    pos["tp_done"] = True
                    save_state()
                    send_exit_prompt(pos, "TP", bid)
                elif (not pos["sl_done"]) and bid <= pos["sl_price"]:
                    pos["sl_done"] = True
                    save_state()
                    send_exit_prompt(pos, "SL", bid)
        except Exception as e:
            logger.warning(f"⚠️ monitor_loop: {e}")
        time.sleep(MONITOR_POLL_S)

def _resolve_position(pos):
    """Market closed — reconcile from settled balance, notify, close."""
    pos["status"] = "closed"
    save_state()
    try:
        time.sleep(5)
        bal = pm.token_balance(pos["token_id"])
    except Exception:
        bal = 0.0
    held = pos["shares"]
    if bal >= held * 0.5:
        pnl = (1.0 - pos["entry_price"]) * held
        msg = (f"🏆 <b>WIN</b> {pos['city'].title()} {pos['bucket']}{pos['unit_sym']} "
               f"resolved YES · +${pnl:.2f}")
    else:
        pnl = -pos["entry_price"] * held
        msg = (f"💀 <b>LOSS</b> {pos['city'].title()} {pos['bucket']}{pos['unit_sym']} "
               f"resolved NO · -${pos['entry_price']*held:.2f}")
    send_message(pos["chat_id"], msg)
    logger.info(msg)

# ===================== MAIN =====================

def main():
    logger.info("=" * 70)
    logger.info("🌡️ POLYMARKET WEATHER TELEGRAM TRADER (human-in-the-loop)")
    logger.info(f"   Signals file : {SIGNALS_FILE}")
    logger.info(f"   Chats        : {ALLOWED_CHATS}")
    logger.info(f"   Default unit : {DEFAULT_UNIT} | USD {USD_PRESETS} | Shares {SHARE_PRESETS}")
    logger.info(f"   DRY_RUN      : {pm.DRY_RUN}")
    logger.info("=" * 70)
    load_state()
    try:
        pm.get_client()
    except Exception as e:
        logger.error(f"❌ Polymarket client init failed: {e}")
        sys.exit(1)
    send_message(PRIMARY_CHAT, "🌡️ Weather Trader online. I'll send a card when a "
                               "trade is found." + ("  <i>(DRY_RUN)</i>" if pm.DRY_RUN else ""))

    threads = [
        threading.Thread(target=telegram_loop, daemon=True),
        threading.Thread(target=signal_loop, daemon=True),
        threading.Thread(target=monitor_loop, daemon=True),
    ]
    for t in threads:
        t.start()
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
