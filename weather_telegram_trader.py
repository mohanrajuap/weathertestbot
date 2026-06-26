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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

# Hardcoded so NO Railway variable is needed (and stale ones can't break it).
USD_PRESETS    = [1.0, 5.0, 10.0, 25.0, 50.0]
SHARE_PRESETS  = [1, 5, 10, 25, 50]
DEFAULT_UNIT   = "SHARES"
# Polymarket rejects orders below BOTH a ~$1 notional AND a 5-share minimum.
MIN_ORDER_USD  = 1.0
MIN_SHARES     = 5

# ── Webhook receiver (your signal bot POSTs here) ──
WEBHOOK_PATH     = os.getenv("WEBHOOK_PATH", "/api/signal")
WEBHOOK_TOKEN    = os.getenv("WEBHOOK_TOKEN", "").strip()   # optional Bearer secret
WEBHOOK_PORT     = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8080")))
QUICK_BUY_SHARES = int(os.getenv("QUICK_BUY_SHARES", "5"))  # one-tap default size

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
            desc = str(data.get("description") or "")
            if "not modified" not in desc:   # harmless: re-render with no change
                logger.warning(f"⚠️ Telegram {method} not ok: {desc}")
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

def _short_oid(oid):
    o = str(oid or "")
    return (o[:8] + "…" + o[-4:]) if len(o) > 14 else o

def _fmt_sh(x):
    """Show whole shares as an integer, fractional with 4 dp (no trailing 0s)."""
    try:
        x = float(x)
    except Exception:
        return "?"
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.4f}".rstrip("0").rstrip(".")

def _sellable_size(size):
    """Full position size, floored to 4 dp so we never try to sell more than
    the wallet holds (avoids over-balance rejects from rounding up)."""
    try:
        return math.floor(float(size) * 10000) / 10000
    except Exception:
        return 0.0

def _unit_word(unit):
    return "$" if unit == "USD" else "sh"

def _qty_min(unit):
    """Per-bucket minimum: limit orders need ≥5 shares; market needs ≥$1."""
    return MIN_ORDER_USD if unit == "USD" else MIN_SHARES

def render_card(sess):
    s = sess
    unit = s["unit"]
    qty = s["qty"]
    step_word = ("$ per bucket — market, fills now (min $1, can be &lt;5 sh)"
                 if unit == "USD" else
                 "shares per bucket — limit (min 5 sh; rests if &lt; $1)")
    head = (
        f"📈 <b>{s['city'].title()} ({s['target_date']})</b>\n"
        f"Dial a quantity per bucket with −/+, then Confirm.\n"
        f"Unit: <b>{step_word}</b>\n"
    )
    lines = []
    for i, c in enumerate(s["candidates"]):
        star = "⭐" if c.get("is_best") else ""
        price = c.get("price")
        win = f"win +{(1.0 - price) * 100:.0f}¢" if isinstance(price, (int, float)) else ""
        n = qty.get(i, 0)
        mark = f" → <b>{n}{_unit_word(unit)}</b>" if n else ""
        lines.append(
            f"<b>{c['bucket']}{s['unit_sym']}</b> {star} · mkt {_fmt_cents(price)} · {win}{mark}"
        )

    chosen = [(i, qty[i]) for i in sorted(qty) if qty.get(i, 0) > 0]

    # ── basket summary ──
    summary = ""
    if chosen:
        usym = s["unit_sym"]
        if unit == "USD":
            total = sum(n for _, n in chosen)
            items = ", ".join("${0}×{1}{2}".format(n, s["candidates"][i]["bucket"], usym)
                              for i, n in chosen)
            summary = "\n🧺 <b>Basket</b>: {0} = <b>~${1}</b>".format(items, total)
        else:
            cost = sum((s["candidates"][i].get("price") or 0) * n for i, n in chosen)
            items = ", ".join("{0}×{1}{2}".format(n, s["candidates"][i]["bucket"], usym)
                              for i, n in chosen)
            summary = ("\n🧺 <b>Basket</b>: {0}\ncost ~${1:.2f} · you get $1 per share "
                       "of whichever bucket wins".format(items, cost))
    return head + "\n".join(lines) + summary

def render_keyboard(sess):
    s = sess
    sid = s["sid"]
    qty = s["qty"]
    kb = []
    # per-bucket stepper: [−] [24°C 40¢ ×N] [+]
    for i, c in enumerate(s["candidates"]):
        price = c.get("price")
        n = qty.get(i, 0)
        kb.append([
            {"text": "➖", "callback_data": f"b|{sid}|q-|{i}"},
            {"text": f"{c['bucket']}{s['unit_sym']} {_fmt_cents(price)} ×{n}",
             "callback_data": f"b|{sid}|qz|{i}"},
            {"text": "➕", "callback_data": f"b|{sid}|q+|{i}"},
        ])
    # unit row — interpret the per-bucket quantity as $ (market) or shares (limit)
    u = s["unit"]
    kb.append([
        {"text": ("● " if u == "SHARES" else "") + "📊 Shares", "callback_data": f"b|{sid}|u|SHARES"},
        {"text": ("● " if u == "USD" else "") + "💵 USD", "callback_data": f"b|{sid}|u|USD"},
        {"text": "🔄 Refresh", "callback_data": f"b|{sid}|r"},
    ])
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
        "qty": {},                                   # {candidate_index: quantity}
        "unit": DEFAULT_UNIT if DEFAULT_UNIT in ("USD", "SHARES") else "SHARES",
        "chat_id": PRIMARY_CHAT,
        "message_id": None,
        "status": "pending",
    }
    # Per-bucket quantities start at 0 — you set each with the −/+ steppers,
    # so a card never buys a bucket you didn't dial up.
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

_MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december"]
_MONTHS = {m: i for i, m in enumerate(_MONTH_NAMES, start=1)}

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

def _apply_live_prices(cands):
    """Overwrite each candidate's price with the LIVE best ask (what you'd
    actually pay to buy YES) — Gamma's outcomePrices are a stale last/mid
    snapshot. Fetched in parallel; keeps the snapshot if an ask is missing."""
    threads = []
    def fetch(c):
        tok = c.get("token_id")
        if not tok:
            return
        a = pm.best_ask(tok)
        if a is not None:
            c["price"] = a
    for c in cands:
        th = threading.Thread(target=fetch, args=(c,), daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join(timeout=4)

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
    # favorites first (by gamma snapshot), keep a tidy menu, then overwrite
    # with LIVE order-book asks so the card matches Polymarket's Buy price.
    cands.sort(key=lambda c: c["price"], reverse=True)
    cands = cands[:6]
    if not cands:
        return None
    _apply_live_prices(cands)
    cands.sort(key=lambda c: (c.get("price") or 0), reverse=True)
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

_CITY_SLUG_MAP = {"new york": "nyc", "newyork": "nyc"}

def _city_to_slug(city_filter):
    c = city_filter.strip().lower()
    c = _CITY_SLUG_MAP.get(c, c)
    return c.replace(" ", "-")

def _city_today_slug(city):
    """Build today's highest-temperature slug for a city name."""
    from datetime import date as _date
    d = _date.today()
    return (f"highest-temperature-in-{_city_to_slug(city)}-on-"
            f"{_MONTH_NAMES[d.month-1]}-{d.day}-{d.year}")

def _do_basket(chat, slug, basket):
    """Buy a basket of DIFFERENT share counts across buckets in one go
    (e.g. 1×25°C, 4×26°C). Each bucket is placed as its own SHARES order so
    you get per-bucket success/error feedback; sub-$1 ones rest as limits."""
    if not _is_highest_temp_slug(slug):
        send_message(chat, f"❌ <code>{slug}</code> isn't a highest-temperature market.")
        return
    city, date = _parse_temp_slug(slug)
    total = sum(q for _, q in basket)
    send_message(chat, f"🧺 <b>Basket — {city.title()} ({date})</b>\n"
                       f"{', '.join(f'{q}×{b}°C' for b, q in basket)}  ({total} sh) …")
    results, pids = [], []
    with _order_lock:
        for bucket, qty in basket:
            if qty <= 0:
                continue
            pid, line = _buy_one(slug, bucket, "YES", city, date, "°C",
                                 qty, "SHARES", DEFAULT_TP_PRICE, DEFAULT_SL_PRICE, chat)
            results.append(line)
            if pid:
                pids.append(pid)
    send_message(chat, "<b>🧺 Basket result</b>\n" + "\n\n".join(results))
    for pid in pids:
        _send_sell_prompt(pid, chat)

def _parse_basket_args(parts):
    """['25:1','26:4'] → [('25',1),('26',4)]. Accepts ':' '=' 'x' '×'."""
    out = []
    for p in parts:
        for sep in (":", "=", "x", "×"):
            if sep in p:
                bk, qty = p.split(sep, 1)
                bk = bk.strip().rstrip("°cC")
                try:
                    n = int(float(qty))
                except Exception:
                    n = 0
                if bk and n > 0:
                    out.append((bk, n))
                break
    return out

def _send_markets_menu(chat, city_filter=None):
    from datetime import date as _date, timedelta
    send_message(chat, "🔎 Scanning live temperature markets …")
    today = _date.today()
    rows = []

    if city_filter:
        # RELIABLE per-city path: build this city's slugs for today..+5 days
        # and fetch each directly (the tag list is capped ~100 and can drop
        # today's market). fetch_event retries the flaky Gamma API.
        cslug = _city_to_slug(city_filter)
        for n in range(0, 6):
            d = today + timedelta(days=n)
            slug = f"highest-temperature-in-{cslug}-on-{_MONTH_NAMES[d.month-1]}-{d.day}-{d.year}"
            ev = pm.fetch_event(slug)
            if not ev or ev.get("closed") or not ev.get("active"):
                continue
            city, date_label = _parse_temp_slug(slug)
            rows.append((slug, city, date_label))
    else:
        # overview: one button per city = its nearest upcoming day
        parsed, seen = [], set()
        for e in pm.list_temperature_events():
            slug = e.get("slug") or ""
            if not _is_highest_temp_slug(slug) or slug in seen:
                continue
            d = _slug_date(slug)
            if d is None or d < today:
                continue
            city, date_label = _parse_temp_slug(slug)
            seen.add(slug)
            parsed.append((d, city, slug, date_label))
        parsed.sort(key=lambda x: (x[0], x[1]))
        by_city = {}
        for d, city, slug, dl in parsed:
            if city not in by_city:
                by_city[city] = (slug, city, dl)
        rows = sorted(by_city.values(), key=lambda r: r[1])

    if not rows:
        send_message(chat, "No live temperature markets found"
                     + (f" for '{city_filter}'." if city_filter else "."))
        return

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
    """WHOLE share count for one position (no fractional fills).

    SHARES mode honors EXACTLY what you typed (only floored at the 5-share
    exchange minimum) — it never silently inflates your count to hit the $1
    notional. If your shares are worth < $1 the caller rejects the order with
    a clear message instead of buying 100× more than you asked.

    USD mode converts $ → whole shares and DOES meet both minimums (that's
    the point of choosing USD).

    Returns (shares, bumped_bool)."""
    price = ask if (ask and ask > 0) else 0.5
    if unit == "SHARES":
        want = int(round(float(amount)))
        shares = max(MIN_SHARES, want)          # honor count; no $1 inflation
        return shares, (shares > want)
    # USD → whole shares, meeting both minimums
    want = max(1, int(float(amount) // price))
    shares = max(MIN_SHARES, math.ceil(max(float(amount), MIN_ORDER_USD) / price))
    while shares * price < MIN_ORDER_USD - 1e-6:
        shares += 1
    return shares, (shares > want)

def _buy_one(slug, bucket, side, city, target_date, unit_sym, amount, unit,
             tp_price, sl_price, chat, preset_token=None):
    """Place ONE buy and register it for TP/SL. The UNIT decides the order:

      💵 USD    → MARKET buy of that many dollars (FOK). Fills now, spends
                  ~$amount (≥ $1). This is the "$1 market" path.
      📊 Shares → LIMIT for exactly N shares at the price. If it's worth ≥ $1
                  it crosses and fills now; if it's a sub-$1 longshot it rests
                  on the book (bypasses Polymarket's $1 marketable minimum).

    Returns (pid_or_None, result_line)."""
    b = f"{bucket}{unit_sym}"
    rtoken, market = pm.resolve_token(slug, bucket, side)
    token = preset_token or rtoken
    if not token:
        return None, f"❌ <b>{b} failed</b>\n📡 could not resolve the market token"
    ask = pm.best_ask(token)
    if ask is None:
        return None, f"❌ <b>{b} failed</b>\n📡 no live price right now — try Refresh"

    resting = False
    shares = 0
    dollars = 0.0
    limit_px = ask
    if unit == "USD":
        dollars = round(max(float(amount), MIN_ORDER_USD), 2)   # $1 minimum
        oid, _px, info = pm.place_market_buy(token, dollars)
    else:  # SHARES → limit (Polymarket min is 5 shares for limit orders)
        shares = max(MIN_SHARES, int(round(float(amount))))
        if shares * ask >= MIN_ORDER_USD - 1e-6:
            oid, limit_px, info = pm.place_buy(token, ask, round(ask + 0.05, 3), shares)
        else:
            oid, limit_px, info = pm.place_limit_buy(token, ask, shares)   # tiny longshot
            resting = True

    if not oid:
        return None, (f"❌ <b>{b} buy rejected</b>\n"
                      f"📡 Polymarket: <i>{info.get('error', 'rejected')}</i>")

    oid_s = _short_oid(oid)
    status = str(info.get("status") or "").lower()

    filled = _await_fill(oid)
    if filled == 0:
        if resting:
            return None, (f"⏳ <b>{b} — limit placed, waiting to fill</b>\n"
                          f"{shares} sh @ {_fmt_cents(limit_px)}\n"
                          f"🆔 <code>{oid_s}</code> · 📡 Polymarket: <i>{status or 'live'}</i>\n"
                          f"<i>Rests on the book; no TP/SL until it fills.</i>")
        pm.cancel_order(oid)
        return None, (f"⚠️ <b>{b} — not filled, cancelled</b>\n"
                      f"🆔 <code>{oid_s}</code> · 📡 <i>{status or 'killed'}</i>")

    if filled < 0:                                   # DRY_RUN sentinel
        held = round(dollars / ask, 4) if unit == "USD" else float(shares)
    else:
        held = round(float(filled), 4)               # keep fractional (market fills)
    if held <= 0:
        return None, f"⚠️ <b>{b}</b>: 0 shares filled (🆔 <code>{oid_s}</code>)"

    entry = round(dollars / held, 3) if unit == "USD" else round(limit_px, 3)
    pid = _next_pid()
    end_ts = pm.market_end_ts(market) or (time.time() + MAX_HOLD_HOURS * 3600)
    positions[pid] = {
        "pid": pid, "token_id": token, "bucket": bucket, "side": side,
        "city": city, "target_date": target_date, "event_slug": slug,
        "unit_sym": unit_sym, "shares": held, "entry_price": entry,
        "tp_price": tp_price, "sl_price": sl_price, "end_ts": end_ts,
        "tp_done": False, "sl_done": False, "status": "open", "chat_id": chat,
    }
    save_state()
    how = "⚡ market" if unit == "USD" else "📊 limit"
    pm_status = "dry-run" if pm.DRY_RUN else (status or "matched")
    return pid, (f"✅ <b>BOUGHT {_fmt_sh(held)} × {b}</b> ({how})\n"
                 f"avg ~{_fmt_cents(entry)} · spent ~${held * entry:.2f}\n"
                 f"🆔 <code>{oid_s}</code> · 📡 Polymarket: <i>{pm_status} ✓</i>\n"
                 f"👁 watching TP {_fmt_cents(tp_price)} / SL {_fmt_cents(sl_price)} · pid {pid}")

def execute_buys(sess, cb_chat):
    chosen = [(i, sess["qty"][i]) for i in sorted(sess["qty"]) if sess["qty"].get(i, 0) > 0]
    if not chosen:
        send_message(cb_chat, "⚠️ Nothing dialed up — use ➕ on a bucket first.")
        return

    sess["status"] = "executing"
    edit_message(sess["chat_id"], sess["message_id"],
                 render_card(sess) + "\n\n⏳ <i>Placing orders…</i>", [])

    results, bought_pids = [], []
    with _order_lock:
        for i, n in chosen:
            c = sess["candidates"][i]
            pid, line = _buy_one(
                sess["event_slug"], c["bucket"], c.get("side", "YES"),
                sess["city"], sess["target_date"], sess["unit_sym"],
                n, sess["unit"], sess["tp_price"], sess["sl_price"],
                sess["chat_id"], preset_token=c.get("token_id"))
            results.append(line)
            if pid:
                bought_pids.append(pid)

    sess["status"] = "done"
    edit_message(sess["chat_id"], sess["message_id"],
                 render_card(sess) + "\n\n<b>Result</b>\n" + "\n\n".join(results), [])
    logger.info(f"🧾 Buys for {sess['sid']}: {results}")
    # offer an immediate Sell on each position just bought
    for pid in bought_pids:
        _send_sell_prompt(pid, cb_chat)

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
        f"{_fmt_sh(pos['shares'])} sh · est P&amp;L <b>${pnl:+.2f}</b>\n"
        f"Sell now?"
    )
    kb = [[
        {"text": "✅ Sell now", "callback_data": f"x|{pid}|s"},
        {"text": "✋ Hold",     "callback_data": f"x|{pid}|h"},
    ]]
    send_message(pos["chat_id"], text, kb)

def do_sell(pos, cb_chat=None):
    _sell_bot_position(pos["pid"], cb_chat or pos["chat_id"], mode="m")

def _send_sell_prompt(pid, chat):
    """A standalone message with Sell buttons for a bot-tracked position."""
    pos = positions.get(pid)
    if not pos or pos["status"] != "open":
        return
    txt = (f"📊 Holding <b>{_fmt_sh(pos['shares'])} × {pos['bucket']}{pos['unit_sym']}</b> "
           f"({pos['city'].title()}) @ {_fmt_cents(pos['entry_price'])} · pid {pid}\n"
           f"Sell whenever you like:")
    kb = [[{"text": f"💱 Sell all {_fmt_sh(pos['shares'])} sh", "callback_data": f"bs|{pid}|m"},
           {"text": "📌 Sell limit", "callback_data": f"bs|{pid}|l"}]]
    send_message(chat, txt, kb)

def _sell_bot_position(pid, chat, mode="m"):
    pos = positions.get(pid)
    if not pos or pos["status"] != "open":
        send_message(chat, "Position no longer open.")
        return
    token = pos["token_id"]
    size = _sellable_size(pos["shares"])        # sell the WHOLE holding (fractional)
    with _order_lock:
        if mode == "l":
            bid = pm.best_bid(token)
            px = round(bid if (bid and bid > 0) else 0.5, 3)
            oid = pm.place_sell(token, px, size, label="LIMIT-SELL")
        else:
            oid, px = pm.sell_cross_book(token, size, label="SELL")
    if not oid:
        send_message(chat, f"❌ <b>Sell failed</b> for {pos['bucket']}{pos['unit_sym']} "
                           f"— try the other mode or the Polymarket UI.")
        return
    pos["status"] = "closed"
    save_state()
    pnl = (px - pos["entry_price"]) * size
    send_message(chat,
        f"💱 <b>SOLD {_fmt_sh(size)} × {pos['bucket']}{pos['unit_sym']}</b> @ ~{_fmt_cents(px)}\n"
        f"entry {_fmt_cents(pos['entry_price'])} · est P&amp;L <b>${pnl:+.2f}</b>\n"
        f"🆔 <code>{_short_oid(oid)}</code> · 📡 Polymarket: <i>"
        + ("dry-run" if pm.DRY_RUN else "order placed") + " ✓</i>")
    logger.info(f"sold {pos['bucket']} {size} @ {px}")

# ── manual position selling (ANY wallet position, via the Data API) ──
sell_sessions = {}          # sxid -> {token, size, label}
_sxid_counter = 0

def _next_sxid():
    global _sxid_counter
    _sxid_counter += 1
    return "s" + format(_sxid_counter, "x")

def _send_positions(chat):
    send_message(chat, "🔎 Fetching your wallet positions …")
    poss = pm.get_wallet_positions()
    shown = 0
    for p in poss:
        size = _sellable_size(p.get("size"))      # full holding (fractional OK)
        token = p.get("asset")
        if size <= 0 or not token:
            continue
        title = p.get("title") or "?"
        outcome = p.get("outcome") or ""
        avg = p.get("avgPrice"); cur = p.get("curPrice")
        pnl = p.get("cashPnl"); pct = p.get("percentPnl")
        txt = (f"📊 <b>{title}</b>\n{outcome} · {_fmt_sh(size)} sh · "
               f"avg {_fmt_cents(avg)} → now {_fmt_cents(cur)}"
               + (f" · P&amp;L ${pnl:+.2f} ({pct:+.0f}%)" if isinstance(pnl, (int, float)) else ""))
        if p.get("redeemable"):
            send_message(chat, txt + "\n✅ <i>Resolved — claim/redeem in the Polymarket UI.</i>")
        else:
            sxid = _next_sxid()
            sell_sessions[sxid] = {"token": token, "size": size,    # full fractional size
                                   "label": f"{outcome} {title}"}
            kb = [[{"text": f"💱 Sell all {_fmt_sh(size)} sh", "callback_data": f"sx|{sxid}|m"},
                   {"text": "📌 Sell limit", "callback_data": f"sx|{sxid}|l"}]]
            send_message(chat, txt, kb)
        shown += 1
    if shown == 0:
        send_message(chat, "No open weather positions found for the funder wallet.")

def _do_wallet_sell(ss, chat, mode="m"):
    token, size, label = ss["token"], ss["size"], ss["label"]
    with _order_lock:
        if mode == "l":
            bid = pm.best_bid(token)
            px = bid if (bid and bid > 0) else 0.5
            oid = pm.place_sell(token, round(px, 3), size, label="LIMIT-SELL")
        else:
            oid, px = pm.sell_cross_book(token, size, label="SELL")
    if oid:
        send_message(chat,
            f"💱 <b>SOLD all {_fmt_sh(size)} sh</b> of {label} @ ~{_fmt_cents(px)}\n"
            f"🆔 <code>{_short_oid(oid)}</code> · 📡 Polymarket: <i>"
            + ("dry-run" if pm.DRY_RUN else "order placed") + " ✓</i>")
        logger.info(f"manual sell {label}: {size} @ {px}")
    else:
        send_message(chat, f"❌ Sell failed for {label} — try the other mode or the UI.")

# ===================== WEBHOOK SIGNALS (one-tap buy) =====================
# Your forecast/signal bot POSTs a JSON payload here when it finds a trade.
# We turn it into a Telegram card with ONE-TAP buy buttons for both the
# bias and no-bias buckets.

quick_sessions = {}          # qid -> {slug, bucket, side, city, date, unit_sym, price}
_qid_counter = 0

def _next_qid():
    global _qid_counter
    _qid_counter += 1
    return "q" + format(_qid_counter, "x")

def _signal_slug(payload):
    """Event slug from the payload's market.url, else built from city+date."""
    m = payload.get("market") or {}
    url = m.get("url") or ""
    if "/event/" in url:
        return url.split("/event/")[-1].strip("/")
    city = (payload.get("city") or "").lower().replace(" ", "-")
    td = payload.get("target_date") or ""        # "2026-06-22"
    try:
        y, mo, d = td.split("-")
        return f"highest-temperature-in-{city}-on-{_MONTH_NAMES[int(mo)-1]}-{int(d)}-{y}"
    except Exception:
        return None

def _bucket_from(val):
    try:
        return int(round(float(val)))
    except Exception:
        return None

def handle_webhook_signal(payload):
    """Build and send the one-tap buy card from a signal payload."""
    if not PRIMARY_CHAT:
        return
    slug = _signal_slug(payload)
    if not slug or not _is_highest_temp_slug(slug):
        logger.warning(f"webhook: non-highest-temperature or unresolved slug: {slug}")
        return

    city = payload.get("city") or "?"
    td   = payload.get("target_date") or "—"
    usym = payload.get("unit") or "°C"
    blend = payload.get("blend") or {}
    best  = payload.get("best_trade") or {}
    market = payload.get("market") or {}
    prices = market.get("prices") or {}

    with_bias = blend.get("with_bias")
    no_bias   = blend.get("no_bias")
    bias_b   = _bucket_from(with_bias)
    nobias_b = _bucket_from(no_bias)
    best_b   = best.get("bucket")

    def price_of(b):
        if b is None:
            return None
        v = prices.get(str(b))
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    # de-duplicate the one-tap options (bias / no-bias / best)
    options, seen = [], set()
    for label, b in (("bias", bias_b), ("no-bias", nobias_b), ("best", best_b)):
        if b is None or b in seen:
            if b is not None:
                # merge label into the existing option for that bucket
                for o in options:
                    if o["bucket"] == b and label not in o["labels"]:
                        o["labels"].append(label)
            continue
        seen.add(b)
        options.append({"bucket": b, "labels": [label], "price": price_of(b)})

    if not options:
        logger.warning("webhook: no buckets to offer")
        return

    # header
    edge = best.get("edge")
    edge_s = f" · edge +{edge*100:.0f}%" if isinstance(edge, (int, float)) else ""
    lines = [
        f"🚨 <b>Signal — {city.title()} ({td})</b>",
        f"{payload.get('verdict','?')} · {payload.get('timing','?')}{edge_s}",
    ]
    if isinstance(with_bias, (int, float)):
        lines.append(f"🧬 bias <b>{with_bias:.1f}{usym}</b> → bucket {bias_b}")
    if isinstance(no_bias, (int, float)):
        lines.append(f"📊 no-bias <b>{no_bias:.1f}{usym}</b> → bucket {nobias_b}")
    if best.get("action"):
        lines.append(f"⭐ {best.get('action')} {best_b}{usym} @ "
                     f"{_fmt_cents(best.get('yes_price'))}")

    # one-tap buttons (+ the multi-option card + dismiss)
    kb = []
    for o in options:
        qid = _next_qid()
        quick_sessions[qid] = {
            "slug": slug, "bucket": str(o["bucket"]), "side": "YES",
            "city": city, "target_date": td, "unit_sym": usym,
        }
        lab = "+".join(o["labels"])
        pc = f" @ {_fmt_cents(o['price'])}" if o["price"] is not None else ""
        kb.append([{
            "text": f"⚡ Buy {o['bucket']}{usym} ({lab}) · {QUICK_BUY_SHARES}sh{pc}",
            "callback_data": f"q|{qid}",
        }])
    # build a full card (all buckets) for amount selection
    sig = build_test_signal(slug)
    if sig and sig.get("candidates"):
        gid = _next_qid()
        quick_sessions[gid] = {"full_slug": slug}
        kb.append([{"text": "🎛 Choose amount / more buckets", "callback_data": f"q|{gid}"}])

    send_message(PRIMARY_CHAT, "\n".join(lines)
                 + f"\n\n<i>One-tap buys {QUICK_BUY_SHARES} shares (min $1/5sh enforced).</i>", kb)
    logger.info(f"📨 Webhook signal card sent for {slug}: buckets {[o['bucket'] for o in options]}")

def do_quick_buy(qid, chat):
    qs = quick_sessions.get(qid)
    if not qs:
        send_message(chat, "This signal expired — send the signal again.")
        return
    if qs.get("full_slug"):                       # "choose amount" → full card
        sig = build_test_signal(qs["full_slug"])
        if sig and sig.get("candidates"):
            handle_new_signal(sig)
        else:
            send_message(chat, "⚠️ Could not load the market.")
        return
    send_message(chat, f"⚡ Buying {QUICK_BUY_SHARES} sh {qs['bucket']}{qs['unit_sym']} "
                       f"{qs['city'].title()} …")
    with _order_lock:
        pid, line = _buy_one(
            qs["slug"], qs["bucket"], qs["side"], qs["city"], qs["target_date"],
            qs["unit_sym"], QUICK_BUY_SHARES, "SHARES",
            DEFAULT_TP_PRICE, DEFAULT_SL_PRICE, chat)
    send_message(chat, line)
    logger.info(f"⚡ one-tap buy: {line}")
    if pid:
        _send_sell_prompt(pid, chat)

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
        if act in ("q+", "q-", "qz"):        # per-bucket quantity steppers
            i = int(parts[3])
            cur = sess["qty"].get(i, 0)
            mn = _qty_min(sess["unit"])       # 5 sh (limit) or $1 (market)
            if act == "q+":
                sess["qty"][i] = mn if cur < mn else cur + 1
            elif act == "q-":
                sess["qty"][i] = 0 if cur <= mn else cur - 1
            else:                            # tap the label → reset that bucket to 0
                sess["qty"][i] = 0
            answer_callback(cb_id)
            push_card(sess)
        elif act == "u":                     # unit (shares=limit / usd=market)
            sess["unit"] = parts[3]
            mn = _qty_min(parts[3])           # clamp existing qtys to the new minimum
            for k, v in list(sess["qty"].items()):
                if 0 < v < mn:
                    sess["qty"][k] = mn
            answer_callback(cb_id, f"Unit: {parts[3]}")
            push_card(sess)
        elif act == "r":                     # refresh live prices
            answer_callback(cb_id, "Refreshing…")
            def _refresh():
                _apply_live_prices(sess["candidates"])
                push_card(sess)
            threading.Thread(target=_refresh, daemon=True).start()
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

    # ── sell a bot-tracked position (Sell button under a buy) ──
    if kind == "bs" and len(parts) >= 3:
        pid = parts[1]
        if pid not in positions or positions[pid]["status"] != "open":
            answer_callback(cb_id, "Position no longer open.")
            return
        answer_callback(cb_id, "Selling…")
        threading.Thread(target=_sell_bot_position, args=(pid, chat, parts[2]),
                         daemon=True).start()
        return

    # ── manual sell of a wallet position (/positions buttons) ──
    if kind == "sx" and len(parts) >= 3:
        ss = sell_sessions.get(parts[1])
        if not ss:
            answer_callback(cb_id, "List expired — send /positions again")
            return
        answer_callback(cb_id, "Selling…")
        threading.Thread(target=_do_wallet_sell, args=(ss, chat, parts[2]),
                         daemon=True).start()
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

    # ── one-tap webhook buy ──
    if kind == "q" and len(parts) >= 2:
        qid = parts[1]
        answer_callback(cb_id, "On it…")
        threading.Thread(target=do_quick_buy, args=(qid, chat), daemon=True).start()
        return

    # ── main-menu buttons ──
    if kind == "menu" and len(parts) >= 2:
        what = parts[1]
        answer_callback(cb_id)
        if what == "markets":
            threading.Thread(target=_send_markets_menu, args=(chat, None), daemon=True).start()
        elif what == "positions":
            threading.Thread(target=_send_positions, args=(chat,), daemon=True).start()
        elif what == "test":
            send_message(chat, "🧪 Send <code>/test &lt;event-slug&gt;</code>, or just use "
                               "🛒 Buy to pick a live market.")
        elif what == "help":
            handle_text({"chat": {"id": chat}, "text": "/help"})
        return

    answer_callback(cb_id)

def _send_main_menu(chat):
    txt = ("🌡️ <b>Weather Trader — menu</b>\n"
           "Pick an action (or use the ☰ menu / commands):")
    kb = [
        [{"text": "🛒 Buy — browse markets", "callback_data": "menu|markets"}],
        [{"text": "💼 Positions & Sell", "callback_data": "menu|positions"}],
        [{"text": "🧪 Test a market", "callback_data": "menu|test"},
         {"text": "❓ Help", "callback_data": "menu|help"}],
    ]
    send_message(chat, txt, kb)

def set_bot_commands():
    """Register the ☰ command menu shown in Telegram."""
    cmds = [
        {"command": "menu",      "description": "📋 Main menu"},
        {"command": "markets",   "description": "🛒 Browse markets & buy"},
        {"command": "basket",    "description": "🧺 Buy a basket (per-bucket qty)"},
        {"command": "positions", "description": "💼 Your positions & sell"},
        {"command": "sell",      "description": "💱 Sell a position"},
        {"command": "test",      "description": "🧪 Buy card for a market"},
        {"command": "help",      "description": "❓ How it works"},
    ]
    tg("setMyCommands", commands=cmds)

def handle_text(msg):
    chat = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or "").strip()

    if ALLOWED_CHATS and chat not in ALLOWED_CHATS:
        return

    if text.startswith("/"):
        cmd = text.split()[0].lower()
        if cmd in ("/start", "/menu"):
            _send_main_menu(chat)
        elif cmd == "/help":
            send_message(chat,
                "🌡️ <b>Weather Trader — how it works</b>\n"
                "I trade Polymarket 'highest temperature' markets. Use the menu "
                "or these commands:\n\n"
                "🛒 /markets [city] — browse live markets &amp; buy\n"
                "🧺 /basket &lt;city&gt; 25:1 26:4 — buy per-bucket share counts\n"
                "💼 /positions (/sell) — your positions with Sell buttons\n"
                "🧪 /test [slug] — buy card for a specific market\n"
                "📋 /menu — the main menu\n\n"
                "On a buy card: 💵 <b>USD</b> = market buy of that many $ (fills "
                "now, e.g. $1); 📊 <b>Shares</b> = limit for exactly N shares "
                "(rests if it's a sub-$1 longshot). I ask before every sell.")
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
        elif cmd in ("/basket", "/dca"):
            # /basket <slug-or-city> 25:1 26:4 27:2
            parts = text.split()
            if len(parts) < 3:
                send_message(chat,
                    "🧺 <b>Basket buy</b> — different share counts per bucket in one go:\n"
                    "<code>/basket &lt;city-or-slug&gt; 25:1 26:4 27:2</code>\n"
                    "e.g. <code>/basket tokyo 22:1 23:4</code> buys 1×22°C + 4×23°C.")
            else:
                target = parts[1]
                slug = target if _is_highest_temp_slug(target) else _city_today_slug(target)
                basket = _parse_basket_args(parts[2:])
                if not basket:
                    send_message(chat, "⚠️ No valid bucket:qty pairs. Example: "
                                       "<code>/basket tokyo 22:1 23:4</code>")
                else:
                    threading.Thread(target=_do_basket, args=(chat, slug, basket),
                                     daemon=True).start()
        elif cmd in ("/positions", "/sell"):
            threading.Thread(target=_send_positions, args=(chat,), daemon=True).start()
        return

# ===================== WEBHOOK HTTP SERVER =====================

class _WebhookHandler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                       # health check
        self._send(200, {"ok": True, "service": "weather-trader"})

    def do_POST(self):
        # accept the configured path (and tolerate a trailing slash / root)
        if WEBHOOK_PATH not in (self.path, self.path.rstrip("/")) and self.path != "/":
            self._send(404, {"ok": False, "error": "not found"})
            return
        # optional bearer auth
        if WEBHOOK_TOKEN:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {WEBHOOK_TOKEN}":
                self._send(401, {"ok": False, "error": "unauthorized"})
                return
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, {"ok": False, "error": f"bad json: {e}"})
            return
        # a test ping (no city) just confirms connectivity
        if not payload.get("city"):
            self._send(200, {"ok": True, "pong": True})
            return
        try:
            threading.Thread(target=handle_webhook_signal, args=(payload,),
                             daemon=True).start()
        except Exception as e:
            logger.warning(f"webhook dispatch failed: {e}")
        self._send(200, {"ok": True})

    def log_message(self, *a):              # quiet the default stderr logging
        return

def webhook_loop():
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", WEBHOOK_PORT), _WebhookHandler)
    except Exception as e:
        logger.error(f"❌ webhook server failed to bind :{WEBHOOK_PORT}: {e}")
        return
    logger.info(f"🌐 Webhook server on :{WEBHOOK_PORT} path {WEBHOOK_PATH} "
                f"(auth {'on' if WEBHOOK_TOKEN else 'off'})")
    srv.serve_forever()

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
    logger.info(f"   Webhook      : :{WEBHOOK_PORT}{WEBHOOK_PATH} | one-tap {QUICK_BUY_SHARES} sh")
    logger.info(f"   DRY_RUN      : {pm.DRY_RUN}")
    logger.info("=" * 70)
    load_state()
    try:
        pm.get_client()
    except Exception as e:
        logger.error(f"❌ Polymarket client init failed: {e}")
        sys.exit(1)
    set_bot_commands()        # register the ☰ command menu
    send_message(PRIMARY_CHAT, "🌡️ Weather Trader online."
                 + ("  <i>(DRY_RUN)</i>" if pm.DRY_RUN else ""))
    _send_main_menu(PRIMARY_CHAT)

    threads = [
        threading.Thread(target=telegram_loop, daemon=True),
        threading.Thread(target=signal_loop, daemon=True),
        threading.Thread(target=monitor_loop, daemon=True),
        threading.Thread(target=webhook_loop, daemon=True),
    ]
    for t in threads:
        t.start()
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
