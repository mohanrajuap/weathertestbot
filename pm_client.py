# =========================================
# pm_client.py — shared Polymarket CLOB helpers
#
# Order placement, price polling, balance and market resolution,
# factored out so the interactive Telegram trader and any autonomous
# variant share one battle-tested implementation (modeled on the BTC
# 5m bot). Auth is lazy: nothing connects until get_client() is first
# called, so importing this module is cheap and side-effect free.
# =========================================

import os
import re
import json
import time
import logging
from datetime import datetime

import requests
from dotenv import load_dotenv
from eth_account import Account

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    OrderArgs,
    MarketOrderArgs,
    OrderType,
    BalanceAllowanceParams,
    AssetType,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL

load_dotenv()
logger = logging.getLogger("pm_client")

HOST       = "https://clob.polymarket.com"
GAMMA      = "https://gamma-api.polymarket.com"
DATA       = "https://data-api.polymarket.com"

HTTP_TIMEOUT_S  = int(os.getenv("HTTP_TIMEOUT_S", "5"))
ENTRY_BUFFER    = float(os.getenv("ENTRY_BUFFER", "0.01"))
ENTRY_MAX       = float(os.getenv("ENTRY_MAX", "0.97"))
SL_CROSS_BUFFER = float(os.getenv("SL_CROSS_BUFFER", "0.02"))
SL_FLOOR        = float(os.getenv("SL_FLOOR", "0.02"))

# Log orders without sending them. Flip to false only when you trust it.
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in ("1", "true", "yes", "on")

# ── lazy client ──
_client = None

_HEX = set("0123456789abcdefABCDEF")

def _clean_secret(s):
    """Strip whitespace and any surrounding quotes — Railway/.env values
    are frequently pasted as \"0xabc...\" (quotes included) or with a stray
    newline, which makes eth-account raise 'Non-hexadecimal digit found'."""
    return (s or "").strip().strip('"').strip("'").strip()

def _normalize_private_key(pk):
    pk = _clean_secret(pk)
    body = pk[2:] if pk.lower().startswith("0x") else pk
    if len(body) != 64 or any(c not in _HEX for c in body):
        bad = next((c for c in body if c not in _HEX), None)
        hint = (f"contains a non-hex character {bad!r}" if bad
                else f"is {len(body)} hex chars, expected 64")
        raise RuntimeError(
            "PRIVATE_KEY is invalid: it " + hint + ". Set it to your 64-char "
            "hex key (0x-prefix optional) with NO surrounding quotes or spaces."
        )
    return "0x" + body

def get_client():
    global _client
    if _client is not None:
        return _client
    pk = os.getenv("PRIVATE_KEY")
    fa = os.getenv("FUNDER_ADDRESS")
    if not pk or not fa:
        raise RuntimeError("Missing PRIVATE_KEY or FUNDER_ADDRESS in environment")
    pk = _normalize_private_key(pk)
    fa = _clean_secret(fa)
    acct = Account.from_key(pk)
    logger.info(f"🔑 Signer {acct.address.lower()} | funder {fa}")
    c = ClobClient(host=HOST, chain_id=137, key=pk, signature_type=3, funder=fa)
    creds = c.create_or_derive_api_key()
    c.set_api_creds(creds)
    logger.info("✅ Polymarket API credentials initialized")
    _client = c
    return c

# ── price polling (public /price on a keep-alive session) ──
PRICE_SESSION = requests.Session()
PRICE_TIMEOUT = (2, 3)

def _parse_price(d):
    if isinstance(d, dict) and "price" in d:
        try:
            return float(d["price"])
        except Exception:
            return None
    if isinstance(d, (int, float)):
        return float(d)
    return None

def _price_http(token_id, side, retries=3):
    """best price via the public /price endpoint, retried — the CLOB host
    throws intermittent connection errors, so one attempt loses the quote."""
    for _ in range(retries):
        try:
            r = PRICE_SESSION.get(
                f"{HOST}/price",
                params={"token_id": token_id, "side": side},
                timeout=PRICE_TIMEOUT,
            )
            p = _parse_price(r.json())
            if p is not None:
                return p
        except Exception:
            time.sleep(0.4)
    return None

def best_ask(token_id):
    p = _price_http(token_id, "BUY")
    if p is not None:
        return p
    try:
        return _parse_price(get_client().get_price(token_id, "BUY"))
    except Exception as e:
        logger.warning(f"⚠️ ask fetch failed: {e}")
        return None

def best_bid(token_id):
    p = _price_http(token_id, "SELL")
    if p is not None:
        return p
    try:
        return _parse_price(get_client().get_price(token_id, "SELL"))
    except Exception as e:
        logger.warning(f"⚠️ bid fetch failed: {e}")
        return None

# ── market resolution ──
def bucket_temp(s):
    """Integer temperature from a bucket label: '36°C'→36,
    '38°C or above'→38, bare '36'→36. None if no number."""
    if s is None:
        return None
    m = re.search(r"(-?\d{1,3})\s*°?\s*[CF]", str(s)) or re.search(r"(-?\d{1,3})", str(s))
    return int(m.group(1)) if m else None

def _safe_clob_tokens(market):
    raw = market.get("clobTokenIds")
    if isinstance(raw, list) and len(raw) >= 2:
        return raw[0], raw[1]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw.replace("'", '"'))
            return parsed[0], parsed[1]
        except Exception as e:
            logger.error(f"❌ token parse failed: {e}")
    return None, None

def _gamma_get(params, retries=3):
    """GET /events with retries — the Gamma API throws intermittent
    ConnectionErrors, so a single attempt loses real data."""
    for _ in range(retries):
        try:
            r = requests.get(f"{GAMMA}/events", params=params, timeout=HTTP_TIMEOUT_S + 5)
            d = r.json()
            if isinstance(d, list):
                return d
        except Exception:
            time.sleep(0.6)
    return []

def fetch_event(event_slug):
    d = _gamma_get({"slug": event_slug})
    return d[0] if d else None

def funder_address():
    return _clean_secret(os.getenv("FUNDER_ADDRESS") or "")

def get_wallet_positions(wallet=None, weather_only=True):
    """Live positions for the wallet from the public Data API. Each item has
    `asset` (the token id to sell), `size` (shares), avgPrice/curPrice, title.
    weather_only keeps only temperature/weather markets."""
    wallet = wallet or funder_address()
    if not wallet:
        return []
    for _ in range(3):
        try:
            r = requests.get(f"{DATA}/positions",
                             params={"user": wallet, "limit": 200, "sizeThreshold": 0.1},
                             timeout=HTTP_TIMEOUT_S + 5)
            d = r.json()
            if not isinstance(d, list):
                return []
            out = []
            for p in d:
                title = (p.get("title") or "")
                slug = (p.get("slug") or "") + (p.get("eventSlug") or "")
                is_w = any(k in (title + slug).lower()
                           for k in ("temperature", "temp", "weather"))
                if weather_only and not is_w:
                    continue
                out.append(p)
            return out
        except Exception:
            time.sleep(0.6)
    return []

def list_temperature_events(limit=500):
    """All LIVE 'Highest temperature in <city> on <date>' events across every
    city, via the 'highest-temperature' tag. Returns the raw event list."""
    return _gamma_get({"active": "true", "closed": "false",
                       "tag_slug": "highest-temperature", "limit": limit})

def resolve_token(event_slug, bucket, side):
    """(token_id, market) for the sub-market matching `bucket`, on the
    YES or NO side. (None, None) on failure."""
    event = fetch_event(event_slug)
    if not event:
        return None, None
    markets = event.get("markets", []) or []
    want = bucket_temp(bucket)
    match = None
    for m in markets:
        if want is not None and bucket_temp(m.get("groupItemTitle")) == want:
            match = m
            break
    if match is None:
        logger.error(
            f"❌ bucket '{bucket}' not in {event_slug}: "
            f"{[m.get('groupItemTitle') for m in markets]}"
        )
        return None, None
    yes_t, no_t = _safe_clob_tokens(match)
    if not yes_t or not no_t:
        return None, None
    return (yes_t if str(side).upper() == "YES" else no_t), match

def market_end_ts(market):
    if not market:
        return None
    for key in ("endDate", "endDateIso", "end_date_iso"):
        v = market.get(key)
        if not v:
            continue
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
    return None

# ── orders ──
def place_buy(token_id, ask, max_price, shares):
    """Marketable GTC buy capped at min(max_price, ENTRY_MAX).
    Returns (order_id, limit_price) or (None, None)."""
    cap = min(max_price, ENTRY_MAX)
    if ask is None:
        logger.warning("⚠️ no ask — skip buy")
        return None, None
    if ask > cap:
        logger.warning(f"⚠️ ask {ask:.3f} > cap {cap:.3f} — skip buy")
        return None, None
    # Cap the cross buffer at the ask itself so a sub-cent price isn't
    # inflated (ask 0.002 + 0.01 = 0.012 reserved 6× the real cost).
    buffer = min(ENTRY_BUFFER, max(ask, 0.001))
    limit = min(round(ask + buffer, 3), cap)
    logger.info(f"🎯 BUY limit {limit:.3f} x {shares}")
    if DRY_RUN:
        logger.info("🧪 DRY_RUN — buy not sent")
        return "DRYRUN-BUY", limit
    try:
        args = OrderArgs(token_id=token_id, price=limit, size=shares, side=BUY)
        resp = get_client().create_and_post_order(args, order_type=OrderType.GTC)
        logger.info(f"✅ BUY resp: {resp}")
        return (resp.get("orderID") or resp.get("id")), limit
    except Exception as e:
        logger.error(f"❌ buy failed: {e}")
        return None, None

def place_market_buy(token_id, dollars):
    """True MARKET buy (FOK) for `dollars` of USDC — the SDK fills as many
    shares as $dollars buys at the live book, or kills the order. Returns
    (order_id, dollars) or (None, None). For BUY, MarketOrderArgs.amount is
    the $ amount, not a share count."""
    dollars = round(float(dollars), 2)
    logger.info(f"🎯 MARKET BUY ${dollars} of {token_id[:14]}…")
    if DRY_RUN:
        logger.info("🧪 DRY_RUN — market buy not sent")
        return "DRYRUN-BUY", dollars
    try:
        args = MarketOrderArgs(token_id=token_id, amount=dollars, side=BUY)
        resp = get_client().create_and_post_market_order(args, order_type=OrderType.FOK)
        logger.info(f"✅ MARKET BUY resp: {resp}")
        return (resp.get("orderID") or resp.get("id")), dollars
    except Exception as e:
        logger.error(f"❌ market buy failed: {e}")
        return None, None

def place_limit_buy(token_id, price, shares):
    """GTC limit buy at EXACTLY `price` (no marketable buffer). Placed at the
    touch it behaves like Polymarket's 'Limit' mode — it accepts small orders
    (e.g. 5 shares @ 3¢) that a marketable buy would reject on the $1 min.
    Returns (order_id, price) or (None, None)."""
    px = round(min(max(float(price), 0.001), ENTRY_MAX), 3)
    logger.info(f"📌 LIMIT BUY {shares} @ {px}")
    if DRY_RUN:
        logger.info("🧪 DRY_RUN — limit buy not sent")
        return "DRYRUN-BUY", px
    try:
        args = OrderArgs(token_id=token_id, price=px, size=shares, side=BUY)
        resp = get_client().create_and_post_order(args, order_type=OrderType.GTC)
        logger.info(f"✅ LIMIT BUY resp: {resp}")
        return (resp.get("orderID") or resp.get("id")), px
    except Exception as e:
        logger.error(f"❌ limit buy failed: {e}")
        return None, None

def place_sell(token_id, price, shares, label="SELL"):
    logger.info(f"💱 {label} limit {price} x {shares}")
    if DRY_RUN:
        logger.info(f"🧪 DRY_RUN — {label} not sent")
        return f"DRYRUN-{label}"
    try:
        args = OrderArgs(token_id=token_id, price=price, size=shares, side=SELL)
        resp = get_client().create_and_post_order(args, order_type=OrderType.GTC)
        logger.info(f"✅ {label} resp: {resp}")
        return resp.get("orderID") or resp.get("id")
    except Exception as e:
        logger.error(f"❌ {label} failed: {e}")
        return None

def sell_cross_book(token_id, shares, label="SELL"):
    """Cross the book: sell at live bid − buffer, floored at SL_FLOOR."""
    bid = best_bid(token_id)
    px = max(round((bid - SL_CROSS_BUFFER), 3), SL_FLOOR) if bid is not None else SL_FLOOR
    oid = place_sell(token_id, px, shares, label=label)
    if oid is None:
        oid = place_sell(token_id, SL_FLOOR, shares, label=label + "-RETRY")
    return oid, px

def get_filled_size(order_id):
    if not order_id:
        return 0.0
    if isinstance(order_id, str) and order_id.startswith("DRYRUN"):
        return -1.0  # sentinel: treat dry-run as "filled" by caller
    try:
        status = get_client().get_order(order_id)
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
        logger.error(f"❌ order status failed: {e}")
        return 0.0

def cancel_order(order_id):
    if not order_id:
        return False
    if isinstance(order_id, str) and order_id.startswith("DRYRUN"):
        return True
    for name, call in (
        ("cancel",        lambda f: f(order_id)),
        ("cancel_order",  lambda f: f(order_id)),
        ("cancel_orders", lambda f: f([order_id])),
    ):
        fn = getattr(get_client(), name, None)
        if fn is None:
            continue
        try:
            call(fn)
            logger.info(f"🗑️ cancelled {order_id} via .{name}()")
            return True
        except Exception as e:
            logger.warning(f"⚠️ .{name}() failed: {e}")
    logger.error(f"❌ could not cancel {order_id} — cancel in UI")
    return False

def token_balance(token_id):
    try:
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=3,
        )
        try:
            get_client().update_balance_allowance(params)
        except Exception as e:
            logger.warning(f"⚠️ balance refresh failed: {e}")
        resp = get_client().get_balance_allowance(params)
        raw = resp.get("balance") if isinstance(resp, dict) else None
        return float(raw) / 1_000_000.0 if raw is not None else 0.0
    except Exception as e:
        logger.error(f"❌ balance failed: {e}")
        return 0.0
