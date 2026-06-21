# =========================================
# Polymarket Weather Execution Bot
# v1 — signal-driven temperature-bucket trader
#
# Role split:
#   • YOUR forecast bot is the EDGE engine. It decides which
#     temperature bucket to buy and when the peak has confirmed,
#     then writes a signal to a shared JSON file (SIGNALS_FILE)
#     with `buy_now: true`.
#   • THIS bot is the EXECUTION engine. It watches that file,
#     resolves the Polymarket event → the matching temperature
#     sub-market → the correct YES/NO token, places a marketable
#     buy (capped at max_price), then manages the position with a
#     take-profit resting sell and a stop-loss, holding to
#     resolution if neither triggers.
#
# Order placement, auth, price polling and balance reconciliation
# are modeled on the BTC 5m bot (polymarket_98_bot.py) but adapted
# for multi-outcome "Highest temperature in <city> on <date>"
# events, which are a group of binary YES/NO sub-markets — one per
# temperature bucket (groupItemTitle = "36°C", "29°C or below", …).
#
# Deploy target: Railway (see README.md).
# =========================================

import os
import re
import sys
import json
import time
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from eth_account import Account

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    OrderArgs,
    OrderType,
    BalanceAllowanceParams,
    AssetType,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL

# ===================== CONFIG (env-overridable) =====================

def _env(name, default, cast=str):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except Exception:
        return default

def _envbool(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

# Where your forecast bot writes signals. On Railway, point this at a
# shared volume path (e.g. /data/signals.json) mounted into both
# services, or run both bots in the same service.
SIGNALS_FILE = _env("SIGNALS_FILE", "signals.json")

# Persists which signals we've already acted on so a restart doesn't
# re-buy. Put it on the same persistent volume as the signal file.
STATE_FILE = _env("STATE_FILE", "weather_bot_state.json")

# Default trade size; a signal may override with its own "shares".
SHARES_PER_TRADE = _env("SHARES_PER_TRADE", 5, int)

# Hard ceiling on price we'll ever pay, regardless of signal max_price.
ENTRY_MAX = _env("ENTRY_MAX", 0.95, float)

# Marketable-entry buffer: bid this far above the ask so the order
# crosses and fills (capped at the signal's max_price and ENTRY_MAX).
ENTRY_BUFFER = _env("ENTRY_BUFFER", 0.01, float)

# Exit defaults (a signal may override tp_price / sl_price per trade).
DEFAULT_TP_PRICE = _env("TP_PRICE", 0.90, float)   # take-profit target
DEFAULT_SL_PRICE = _env("SL_PRICE", 0.20, float)   # stop-loss trigger
SL_CROSS_BUFFER  = _env("SL_CROSS_BUFFER", 0.02, float)  # cross book on SL
SL_FLOOR         = _env("SL_FLOOR", 0.02, float)   # never sell below this

# How long to keep watching a position if it never hits TP/SL, before
# falling back to "hold to resolution" reconciliation. Bounded by the
# market's endDate when we can read it.
MAX_HOLD_HOURS = _env("MAX_HOLD_HOURS", 30, float)

SIGNAL_POLL_S    = _env("SIGNAL_POLL_S", 5, float)     # check file
PRICE_LOG_S      = _env("PRICE_LOG_S", 15, float)      # log cadence
FILL_TIMEOUT_S   = _env("FILL_TIMEOUT_S", 45, int)
HTTP_TIMEOUT_S   = _env("HTTP_TIMEOUT_S", 5, int)

# Test without sending real orders — buys/sells are logged, not placed.
DRY_RUN = _envbool("DRY_RUN", False)

HOST       = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"

# ===================== LOGGING =====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("weather_bot_log.txt", mode="a", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("weather_bot")

# ===================== ENV / AUTH =====================

load_dotenv()

private_key    = os.getenv("PRIVATE_KEY")
funder_address = os.getenv("FUNDER_ADDRESS")

if not private_key or not funder_address:
    logger.error("❌ Missing PRIVATE_KEY or FUNDER_ADDRESS in environment")
    sys.exit(1)

private_key    = private_key.strip()
funder_address = funder_address.strip()

account = Account.from_key(private_key)
logger.info("🔍 WALLET")
logger.info(f"   Signer Wallet  → {account.address.lower()}")
logger.info(f"   Funder Address → {funder_address}")

client = ClobClient(
    host=HOST,
    chain_id=137,
    key=private_key,
    signature_type=3,
    funder=funder_address,
)

try:
    creds = client.create_or_derive_api_key()
    client.set_api_creds(creds)
    logger.info("✅ API credentials initialized")
except Exception as e:
    logger.error(f"❌ API credential setup failed: {e}")
    sys.exit(1)

# ===================== STATE =====================

wins = losses = unresolved = total_trades = 0

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("processed", []))
    except Exception:
        return set()

def save_state(processed):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"processed": sorted(processed)}, f, indent=2)
    except Exception as e:
        logger.warning(f"⚠️ Could not persist state: {e}")

processed_signals = load_state()

# ===================== PRICE HELPERS =====================
# Public /price endpoint polled on a keep-alive session so a DNS/
# network blip can't block the loop for many seconds (see 98 bot).

PRICE_SESSION = requests.Session()
PRICE_TIMEOUT = (2, 3)  # (connect, read)

def _parse_price(data):
    if isinstance(data, dict) and "price" in data:
        try:
            return float(data["price"])
        except Exception:
            return None
    if isinstance(data, (int, float)):
        return float(data)
    return None

def _get_price_http(token_id, side):
    try:
        r = PRICE_SESSION.get(
            f"{HOST}/price",
            params={"token_id": token_id, "side": side},
            timeout=PRICE_TIMEOUT,
        )
        return _parse_price(r.json())
    except Exception:
        return None

def get_best_ask_price(token_id):
    """Best ASK — price you'd PAY to buy. Use for entries."""
    p = _get_price_http(token_id, "BUY")
    if p is not None:
        return p
    try:
        return _parse_price(client.get_price(token_id, "BUY"))
    except Exception as e:
        logger.warning(f"⚠️ Ask fetch failed: {e}")
        return None

def get_best_bid_price(token_id):
    """Best BID — price you'd RECEIVE selling. Use for exits."""
    p = _get_price_http(token_id, "SELL")
    if p is not None:
        return p
    try:
        return _parse_price(client.get_price(token_id, "SELL"))
    except Exception as e:
        logger.warning(f"⚠️ Bid fetch failed: {e}")
        return None

# ===================== MARKET RESOLUTION =====================

def _bucket_temp(s):
    """Extract the integer temperature from a bucket label. Matches the
    forecast bot's own parser: '36°C' → 36, '38°C or above' → 38,
    '29°C or below' → 29, bare '36' → 36. Returns None if no number."""
    if s is None:
        return None
    m = re.search(r"(-?\d{1,3})\s*°?\s*[CF]", str(s))
    if not m:
        m = re.search(r"(-?\d{1,3})", str(s))
    return int(m.group(1)) if m else None

def _normalize_bucket(s):
    """Canonical string form, used only as a fallback if the integer
    parse is ambiguous."""
    if s is None:
        return ""
    s = str(s).lower()
    s = s.replace("°", "").replace("º", "")
    s = s.replace("celsius", "").replace("c", "")
    s = s.replace("degrees", "").replace("deg", "")
    s = re.sub(r"\s+", "", s)
    return s

def resolve_event_market(event_slug, bucket, side):
    """Fetch the event, find the sub-market whose groupItemTitle
    matches `bucket`, and return (token_id, market_dict).
    side is 'YES' or 'NO'. Returns (None, None) on failure."""
    try:
        r = requests.get(
            f"{GAMMA_HOST}/events",
            params={"slug": event_slug},
            timeout=HTTP_TIMEOUT_S,
        )
        events = r.json()
    except Exception as e:
        logger.error(f"❌ Event fetch failed for {event_slug}: {e}")
        return None, None

    if not events or not isinstance(events, list):
        logger.error(f"❌ No event found for slug {event_slug}")
        return None, None

    event = events[0]
    markets = event.get("markets", []) or []
    want_temp = _bucket_temp(bucket)
    want_str  = _normalize_bucket(bucket)

    match = None
    for m in markets:
        title = m.get("groupItemTitle")
        # primary: integer-temp match (handles 'N°C', 'N°C or above/below')
        if want_temp is not None and _bucket_temp(title) == want_temp:
            match = m
            break
        # fallback: exact normalized-string match
        if _normalize_bucket(title) == want_str:
            match = m
            break

    if match is None:
        available = [m.get("groupItemTitle") for m in markets]
        logger.error(
            f"❌ Bucket '{bucket}' not found in {event_slug}. "
            f"Available: {available}"
        )
        return None, None

    if match.get("closed") or not match.get("active"):
        logger.warning(
            f"⚠️ Sub-market '{bucket}' is not active "
            f"(active={match.get('active')} closed={match.get('closed')})"
        )

    yes_token, no_token = _safe_clob_tokens(match)
    if not yes_token or not no_token:
        logger.error(f"❌ Could not parse clobTokenIds for '{bucket}'")
        return None, None

    token = yes_token if str(side).upper() == "YES" else no_token
    return token, match

def _safe_clob_tokens(market):
    raw = market.get("clobTokenIds")
    if isinstance(raw, list) and len(raw) >= 2:
        return raw[0], raw[1]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw.replace("'", '"'))
            return parsed[0], parsed[1]
        except Exception as e:
            logger.error(f"❌ Token parse failed: {e}")
    return None, None

def market_end_ts(market):
    """Unix ts of the sub-market's endDate, or None."""
    for key in ("endDate", "endDateIso", "end_date_iso"):
        v = market.get(key)
        if not v:
            continue
        try:
            v = v.replace("Z", "+00:00")
            return datetime.fromisoformat(v).timestamp()
        except Exception:
            continue
    return None

# ===================== ORDER HELPERS =====================

def execute_buy(token, ask, max_price, shares):
    """Place a marketable GTC buy that crosses the book, capped at
    min(signal max_price, ENTRY_MAX)."""
    global total_trades

    cap = min(max_price, ENTRY_MAX)

    if ask > cap:
        logger.warning(f"⚠️ Ask {ask:.3f} above cap {cap:.3f} — skipping buy")
        return None

    buy_limit = min(round(ask + ENTRY_BUFFER, 3), cap)

    logger.info(
        f"🎯 BUY | ask {ask:.3f} | limit {buy_limit:.3f} | "
        f"shares {shares} | edge +${1 - buy_limit:.3f}/sh"
    )

    if DRY_RUN:
        logger.info("🧪 DRY_RUN — buy not sent")
        total_trades += 1
        return "DRYRUN-BUY"

    try:
        order_args = OrderArgs(
            token_id=token, price=buy_limit, size=shares, side=BUY,
        )
        resp = client.create_and_post_order(order_args, order_type=OrderType.GTC)
        logger.info(f"✅ BUY RESPONSE: {resp}")
        oid = resp.get("orderID") or resp.get("id")
        logger.info(f"🆔 Order ID: {oid}")
        total_trades += 1
        return oid
    except Exception as e:
        logger.error(f"❌ Buy placement FAILED: {e}")
        return None

def place_sell_order(token, price, size, label="SELL"):
    logger.info(f"💱 {label} → limit {price} | shares {size}")
    if DRY_RUN:
        logger.info(f"🧪 DRY_RUN — {label} not sent")
        return f"DRYRUN-{label}"
    try:
        order_args = OrderArgs(
            token_id=token, price=price, size=size, side=SELL,
        )
        resp = client.create_and_post_order(order_args, order_type=OrderType.GTC)
        logger.info(f"✅ {label} RESPONSE: {resp}")
        return resp.get("orderID") or resp.get("id")
    except Exception as e:
        logger.error(f"❌ {label} placement FAILED: {e}")
        return None

def get_filled_size(order_id):
    if not order_id:
        return 0.0
    if isinstance(order_id, str) and order_id.startswith("DRYRUN"):
        return float(SHARES_PER_TRADE)
    try:
        status = client.get_order(order_id)
        if not status:
            return 0.0
        matched = status.get("size_matched")
        if matched is None:
            s = str(status.get("status", "")).lower()
            if s in ("filled", "matched"):
                return float(status.get("original_size", 0) or 0)
            return 0.0
        return float(matched)
    except Exception as e:
        logger.error(f"❌ Order status fetch failed: {e}")
        return 0.0

def cancel_order_safe(order_id):
    if not order_id:
        return False
    if isinstance(order_id, str) and order_id.startswith("DRYRUN"):
        return True
    for name, call in (
        ("cancel",        lambda f: f(order_id)),
        ("cancel_order",  lambda f: f(order_id)),
        ("cancel_orders", lambda f: f([order_id])),
    ):
        fn = getattr(client, name, None)
        if fn is None:
            continue
        try:
            call(fn)
            logger.info(f"🗑️ Order {order_id} cancelled via .{name}()")
            return True
        except Exception as e:
            logger.warning(f"⚠️ .{name}() failed: {e}")
    logger.error(
        f"❌ Could NOT cancel {order_id} — cancel manually in the UI."
    )
    return False

def get_token_balance(token_id):
    try:
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id,
            signature_type=3,
        )
        try:
            client.update_balance_allowance(params)
        except Exception as e:
            logger.warning(f"⚠️ balance refresh failed: {e}")
        resp = client.get_balance_allowance(params)
        if not resp:
            return 0.0
        raw = resp.get("balance") if isinstance(resp, dict) else None
        if raw is None:
            return 0.0
        return float(raw) / 1_000_000.0
    except Exception as e:
        logger.error(f"❌ Balance fetch failed: {e}")
        return 0.0

# ===================== POSITION MONITOR (TP + SL) =====================

def execute_stop_loss_sell(token, size):
    """Cross the book: price at live bid minus a buffer, floored at
    SL_FLOOR, so it fills immediately on a reversal."""
    bid = get_best_bid_price(token)
    px = max(round((bid - SL_CROSS_BUFFER), 3), SL_FLOOR) if bid is not None else SL_FLOOR
    oid = place_sell_order(token, px, size, label="STOP-LOSS")
    if oid is None:
        oid = place_sell_order(token, SL_FLOOR, size, label="STOP-LOSS-RETRY")
    return oid, px

def monitor_position(order_id, signal, token, market, entry_price):
    """Confirm fill, then manage with a take-profit resting sell and a
    stop-loss trigger. If neither fires by the market's end, hold to
    resolution and reconcile from the settled balance."""
    global wins, losses, unresolved

    side    = str(signal.get("side", "YES")).upper()
    bucket  = signal.get("bucket")
    tp_px   = float(signal.get("tp_price", DEFAULT_TP_PRICE))
    sl_px   = float(signal.get("sl_price", DEFAULT_SL_PRICE))

    # ── Phase 1: confirm fill ──
    logger.info(f"⏳ Waiting for fill (timeout {FILL_TIMEOUT_S}s) …")
    start = time.time()
    filled = 0.0
    while time.time() - start < FILL_TIMEOUT_S:
        filled = get_filled_size(order_id)
        if filled > 0:
            logger.info(f"✅ Entry FILLED {filled} shares in {time.time()-start:.1f}s")
            break
        time.sleep(2)

    if filled <= 0:
        logger.warning("⚠️ Entry NOT filled — cancelling")
        cancel_order_safe(order_id)
        return

    held = int(filled)
    if held <= 0:
        logger.warning("⚠️ Filled size rounds to 0 — nothing to manage")
        return

    # ── Phase 2: place take-profit, watch for stop-loss ──
    end_ts = market_end_ts(market)
    hard_end = time.time() + MAX_HOLD_HOURS * 3600
    watch_end = min(end_ts, hard_end) if end_ts else hard_end

    tp_oid = None
    if tp_px and tp_px < 1.0:
        tp_oid = place_sell_order(token, round(tp_px, 3), held, label="TAKE-PROFIT")

    logger.info(
        f"🤝 MANAGING {held} {side} {bucket} @ ~{entry_price:.3f} | "
        f"TP {tp_px:.3f} | SL {sl_px:.3f} | "
        f"until {datetime.fromtimestamp(watch_end).strftime('%Y-%m-%d %H:%M')}"
    )

    last_log = 0
    high_seen = entry_price
    low_seen  = entry_price
    outcome = None  # 'tp' | 'sl'

    while time.time() < watch_end:
        bid = get_best_bid_price(token)
        now = time.time()

        if bid is not None:
            high_seen = max(high_seen, bid)
            low_seen  = min(low_seen, bid)

            # take-profit fill check
            if tp_oid and get_filled_size(tp_oid) >= held * 0.5:
                logger.info(f"🏁 TAKE-PROFIT filled @ ~{tp_px:.3f}")
                outcome = "tp"
                break

            # stop-loss trigger
            if bid <= sl_px:
                logger.warning(
                    f"🛑 STOP-LOSS triggered (bid {bid:.3f} ≤ {sl_px:.3f})"
                )
                if tp_oid:
                    cancel_order_safe(tp_oid)
                sl_oid, sl_fill_px = execute_stop_loss_sell(token, held)
                if sl_oid:
                    outcome = "sl"
                    break

            if now - last_log >= PRICE_LOG_S:
                ts = datetime.now().strftime("%H:%M:%S")
                logger.info(
                    f"   [{ts}] {side} {bucket} BID {bid:.3f} "
                    f"(hi {high_seen:.3f} lo {low_seen:.3f}) | "
                    f"{(watch_end - now)/3600:.1f}h left"
                )
                last_log = now

        time.sleep(3)

    # ── Phase 3: reconcile PnL ──
    if outcome == "tp":
        pnl = (tp_px - entry_price) * held
        wins += 1
        logger.info(
            f"🏆 WIN (take-profit) +${pnl:.3f} | "
            f"W:{wins} L:{losses} U:{unresolved} T:{total_trades}"
        )
        return
    if outcome == "sl":
        pnl = (low_seen - entry_price) * held  # approximate
        losses += 1
        logger.warning(
            f"💀 LOSS (stop-loss) ~${pnl:.3f} | "
            f"W:{wins} L:{losses} U:{unresolved} T:{total_trades}"
        )
        return

    # Held to resolution — read settled balance.
    logger.info("⏳ Window/hold ended — reconciling from settled balance …")
    if tp_oid:
        # TP may still be resting; leave it (it's a winning sell if YES
        # resolves), but try to read whether it filled.
        if get_filled_size(tp_oid) >= held * 0.5:
            pnl = (tp_px - entry_price) * held
            wins += 1
            logger.info(f"🏆 WIN (TP filled late) +${pnl:.3f}")
            return
    time.sleep(8)
    final_bal = get_token_balance(token)
    if final_bal >= held * 0.5:
        wins += 1
        pnl = (1.0 - entry_price) * held
        logger.info(
            f"🏆 WIN — {side} {bucket} resolved YES (+${pnl:.3f}) | "
            f"W:{wins} L:{losses} U:{unresolved} T:{total_trades}"
        )
    elif low_seen < 0.5:
        losses += 1
        pnl = -entry_price * held
        logger.warning(
            f"💀 LOSS — {side} {bucket} resolved NO (-${entry_price*held:.3f}) | "
            f"W:{wins} L:{losses} U:{unresolved} T:{total_trades}"
        )
    else:
        unresolved += 1
        logger.warning(
            f"❓ UNRESOLVED — balance {final_bal} unclear, verify in wallet | "
            f"W:{wins} L:{losses} U:{unresolved} T:{total_trades}"
        )

# ===================== SIGNAL HANDLING =====================

def load_signals():
    """Read SIGNALS_FILE. Accepts either a single signal object or a
    list of signal objects. Returns a list (possibly empty)."""
    if not os.path.exists(SIGNALS_FILE):
        return []
    try:
        with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"⚠️ Could not read {SIGNALS_FILE}: {e}")
        return []
    if isinstance(data, dict):
        # allow {"signals": [...]} or a bare single signal
        if "signals" in data and isinstance(data["signals"], list):
            return data["signals"]
        return [data]
    if isinstance(data, list):
        return data
    return []

def signal_key(sig):
    sid = sig.get("signal_id")
    if sid:
        return str(sid)
    return f"{sig.get('event_slug')}|{sig.get('bucket')}|{sig.get('side')}"

def validate_signal(sig):
    if not sig.get("event_slug"):
        return "missing event_slug"
    if not sig.get("bucket"):
        return "missing bucket"
    side = str(sig.get("side", "")).upper()
    if side not in ("YES", "NO"):
        return "side must be YES or NO"
    return None

def handle_signal(sig):
    key = signal_key(sig)

    if not sig.get("buy_now"):
        return  # forecast bot hasn't confirmed the peak yet

    if key in processed_signals:
        return  # already acted on this signal

    err = validate_signal(sig)
    if err:
        logger.error(f"❌ Invalid signal {key}: {err}")
        processed_signals.add(key)   # don't spam — mark bad signal seen
        save_state(processed_signals)
        return

    slug   = sig["event_slug"]
    bucket = sig["bucket"]
    side   = str(sig["side"]).upper()
    shares = int(sig.get("shares", SHARES_PER_TRADE))
    max_px = float(sig.get("max_price", ENTRY_MAX))
    edge   = sig.get("edge")

    logger.info("=" * 70)
    logger.info(
        f"📥 SIGNAL {key} → BUY {side} {bucket} on {slug} "
        f"| max {max_px:.2f}" + (f" | edge {edge}" if edge is not None else "")
    )

    # The forecast bot may pass the already-resolved YES token id. Trust it
    # when present (skips a Gamma call), but still fetch the event so we have
    # the sub-market's endDate for monitoring — best-effort, non-fatal.
    pre_token = sig.get("token_id")
    rtoken, market = resolve_event_market(slug, bucket, side)
    token = pre_token or rtoken

    if not token:
        logger.error(f"❌ Could not resolve token for {key} — will retry next poll")
        return  # NOT marked processed; transient resolution failure can retry

    ask = get_best_ask_price(token)
    if ask is None:
        logger.error(f"❌ No ask price for {key} — will retry next poll")
        return

    order_id = execute_buy(token, ask, max_px, shares)

    # Mark processed regardless of fill so we don't double-buy on the
    # next poll; monitor_position handles an unfilled order.
    processed_signals.add(key)
    save_state(processed_signals)

    if order_id:
        entry_price = min(round(ask + ENTRY_BUFFER, 3), min(max_px, ENTRY_MAX))
        monitor_position(order_id, sig, token, market, entry_price)
    else:
        logger.warning(f"⚠️ No order placed for {key}")

# ===================== MAIN LOOP =====================

def main():
    logger.info("=" * 70)
    logger.info("🌡️ POLYMARKET WEATHER EXECUTION BOT STARTED")
    logger.info(f"   Signals file : {SIGNALS_FILE}")
    logger.info(f"   State file   : {STATE_FILE}")
    logger.info(f"   Shares/trade : {SHARES_PER_TRADE} (signal can override)")
    logger.info(f"   Entry cap    : {ENTRY_MAX} | buffer {ENTRY_BUFFER}")
    logger.info(f"   TP / SL def  : {DEFAULT_TP_PRICE} / {DEFAULT_SL_PRICE}")
    logger.info(f"   DRY_RUN      : {DRY_RUN}")
    logger.info(f"   Already seen : {len(processed_signals)} signals")
    logger.info("=" * 70)

    while True:
        try:
            signals = load_signals()
            for sig in signals:
                if isinstance(sig, dict):
                    handle_signal(sig)
        except Exception as e:
            logger.error(f"❌ Loop error: {e}")
        time.sleep(SIGNAL_POLL_S)

if __name__ == "__main__":
    main()
