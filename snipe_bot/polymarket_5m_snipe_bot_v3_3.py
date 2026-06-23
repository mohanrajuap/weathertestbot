# =========================================
# Polymarket BTC 5m Momentum Breakout Bot
# v3 — FIXED EXITS + BTC STRIKE FILTER
#
# Changes from v2:
#  1. STOP-LOSS now CROSSES the book (sells at live bid minus
#     buffer, floored at SL_FLOOR) instead of resting at 0.78
#     and never filling on a gap down.
#  2. Exits monitor the BID (what you can sell at), not the ask.
#  3. All timers are bounded by the actual 5m window end — no
#     more TP orders resting on already-resolved markets.
#  4. NEW ENTRY FILTER: BTC spot distance from the candle open
#     (Binance 5m kline open = the market's strike). Only buys
#     when BTC has moved far enough in the leader's direction
#     with little time left — a direct probability estimate
#     instead of trusting the quote.
#  5. requests timeouts everywhere; bounded seen_markets;
#     unresolved trades tracked separately (not hidden from W/L).
# =========================================

import os
import time
import json
import logging
import sys
import requests

from collections import deque
from datetime import datetime
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

# ===================== CONFIG =====================

SHARES_PER_TRADE = 5

MIN_LEADER_PCT = 0.75

# ── Entry band ──
TRIGGER_MIN = 0.93
TRIGGER_MAX = 0.935

SKIP_DECIDED_MARKET = 0.975

EXECUTION_LIMIT = 0.935   # used only for PnL estimates in logs

# Marketable entry
ENTRY_BUFFER = 0.01
ENTRY_MAX    = 0.945

# ── Bracket exits ──
TAKE_PROFIT_PRICE = 0.96
# Tightened 0.78 → 0.88: in logged losses, once the bid broke ~0.89
# it collapsed without recovering (0.89→0.78 in <25s), while winners
# never dipped below 0.93. Exiting at ~0.87 risks ~0.07/share vs
# 0.16+ — breakeven win rate drops from ~89% to ~78%. Revert to 0.78
# if you see too many stop-outs on dips that recover.
STOP_LOSS_TRIGGER = 0.88   # BID at/below this → bail out
SL_CROSS_BUFFER   = 0.05   # sell limit = live bid - buffer (crosses book)
SL_FLOOR          = 0.05   # never price the exit below this

# ── BTC strike filter (Binance) ──
# Only enter if BTC spot has moved at least this many USD past the
# 5m candle open, IN THE LEADER'S DIRECTION, and at most
# MAX_SECONDS_LEFT remain in the window.
BTC_FILTER_ENABLED   = False    # disabled — time gate cost valid entries
MIN_BTC_DISTANCE_USD = 25.0
MAX_SECONDS_LEFT     = 300      # if re-enabled: 300 = no time gate, distance only
BINANCE_KLINE_URL    = "https://api.binance.com/api/v3/klines"
BINANCE_PRICE_URL    = "https://api.binance.com/api/v3/ticker/price"

# Observation window before evaluating leader
DECISION_WINDOW_S = 150

SCAN_INTERVAL_S      = 2
PRICE_LOG_INTERVAL_S = 5

# Hard buffer before window end at which we stop waiting on exits
END_OF_WINDOW_BUFFER_S = 5

FAILURE_THRESHOLD = 0.50

HTTP_TIMEOUT_S = 5

HOST = "https://clob.polymarket.com"

# ==================================================

load_dotenv()

# ===================== ENV =====================

private_key    = os.getenv("PRIVATE_KEY")
funder_address = os.getenv("FUNDER_ADDRESS")

if not private_key or not funder_address:
    print("❌ Missing PRIVATE_KEY or FUNDER_ADDRESS in .env")
    sys.exit(1)

private_key    = private_key.strip()
funder_address = funder_address.strip()

# ===================== DEBUG =====================

print("\n🔍 MODERN SDK WALLET DEBUG")

account = Account.from_key(private_key)
derived_address = account.address.lower()

print(f"   Signer Wallet      → {derived_address}")
print(f"   Funder Address     → {funder_address}")
print("\n✅ MODERN SDK MODE ENABLED\n")

# ===================== CLIENT =====================

client = ClobClient(
    host=HOST,
    chain_id=137,
    key=private_key,
    signature_type=3,
    funder=funder_address,
)

# ===================== AUTH =====================

try:
    creds = client.create_or_derive_api_key()
    client.set_api_creds(creds)
    print("✅ API credentials initialized successfully\n")
except Exception as e:
    print(f"❌ API credential setup failed: {e}")
    sys.exit(1)

# ===================== LOGGING =====================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(
            "btc_5m_breakout_bot_log.txt",
            mode='a',
            encoding='utf-8',
        ),
        logging.StreamHandler(sys.stdout),
    ]
)

logger = logging.getLogger(__name__)

# ===================== STATS =====================

wins         = 0
losses       = 0
unresolved   = 0   # NEW: trades whose outcome we couldn't confirm
total_trades = 0

logger.info("=" * 80)
logger.info("🚀 BTC 5m BREAKOUT BOT v3 STARTED (fixed exits + strike filter)")
logger.info(f"🔥 Shares Per Trade: {SHARES_PER_TRADE}")
logger.info(f"🔥 Buy Zone: {TRIGGER_MIN} → {TRIGGER_MAX}")
logger.info(
    f"🧮 BTC filter: {'ON' if BTC_FILTER_ENABLED else 'OFF'} | "
    f"min dist ${MIN_BTC_DISTANCE_USD} | "
    f"enter only in last {MAX_SECONDS_LEFT}s"
)
logger.info(f"⏳ Decision Window: {DECISION_WINDOW_S}s")
logger.info("=" * 80)

# ===================== TIME HELPERS =====================

def get_current_window_ts():
    now = int(time.time())
    return (now // 300) * 300


def get_window_end_ts():
    return get_current_window_ts() + 300


def seconds_left_in_window():
    return get_window_end_ts() - time.time()

# ===================== BTC SPOT (BINANCE) =====================

def get_btc_candle_open():
    """Open price of the CURRENT 5m BTCUSDT candle on Binance.
    This is the strike the Polymarket Up/Down market resolves against
    (approximately — Polymarket uses its own oracle, but the 5m open
    tracks it closely)."""
    try:
        r = requests.get(
            BINANCE_KLINE_URL,
            params={"symbol": "BTCUSDT", "interval": "5m", "limit": 1},
            timeout=HTTP_TIMEOUT_S,
        )
        k = r.json()
        if k and isinstance(k, list):
            return float(k[0][1])  # [open_time, open, high, low, ...]
        return None
    except Exception as e:
        logger.warning(f"⚠️ Binance kline fetch failed: {e}")
        return None


def get_btc_spot():
    try:
        r = requests.get(
            BINANCE_PRICE_URL,
            params={"symbol": "BTCUSDT"},
            timeout=HTTP_TIMEOUT_S,
        )
        return float(r.json()["price"])
    except Exception as e:
        logger.warning(f"⚠️ Binance spot fetch failed: {e}")
        return None


def btc_filter_pass(leader):
    """True if BTC spot is at least MIN_BTC_DISTANCE_USD past the
    candle open IN THE LEADER'S DIRECTION and we're inside the
    final MAX_SECONDS_LEFT of the window."""

    if not BTC_FILTER_ENABLED:
        return True

    left = seconds_left_in_window()

    if left > MAX_SECONDS_LEFT:
        logger.info(
            f"🧮 BTC filter: too early ({left:.0f}s left "
            f"> {MAX_SECONDS_LEFT}s) — waiting"
        )
        return False

    strike = get_btc_candle_open()
    spot   = get_btc_spot()

    if strike is None or spot is None:
        # Fail open or closed? Closed — no data, no trade.
        logger.warning("🧮 BTC filter: no data — skipping entry")
        return False

    dist = spot - strike  # positive = Up winning

    logger.info(
        f"🧮 BTC filter: open {strike:.2f} | spot {spot:.2f} | "
        f"dist {dist:+.2f} | {left:.0f}s left | leader {leader}"
    )

    if leader == "YES" and dist >= MIN_BTC_DISTANCE_USD:
        return True
    if leader == "NO" and dist <= -MIN_BTC_DISTANCE_USD:
        return True

    return False

# ===================== MARKET HELPERS =====================

def get_open_fast_markets():
    try:
        ts   = get_current_window_ts()
        slug = f"btc-updown-5m-{ts}"

        r = requests.get(
            f"https://gamma-api.polymarket.com/markets?slug={slug}",
            timeout=HTTP_TIMEOUT_S,
        )
        markets = r.json()

        if markets and isinstance(markets, list) and len(markets) > 0:
            market = markets[0]
            if market.get("active"):
                # (no log here — already-seen markets were spamming
                # 'Market detected' every scan; 'Processing Market'
                # logs once when it's actually new)
                return market
        return None

    except Exception as e:
        logger.error(f"❌ Market scan failed: {e}")
        return None


def safe_get_clob_tokens(market):
    raw = market.get("clobTokenIds")

    if isinstance(raw, list):
        return raw[0], raw[1]

    if isinstance(raw, str):
        clean = raw.replace("'", '"')
        try:
            parsed = json.loads(clean)
            return parsed[0], parsed[1]
        except Exception as e:
            logger.error(f"❌ Token parse failed: {e}")

    return None, None

# ===================== PRICE HELPERS =====================

def _parse_price(data):
    if isinstance(data, dict) and "price" in data:
        return float(data["price"])
    if isinstance(data, (int, float)):
        return float(data)
    return None


# FIX (intermittent timeouts): the SDK opens fresh connections and
# uses long default timeouts, so a network/DNS blip blocks the loop
# for 6-21s per call ("read operation timed out" / "Temporary
# failure in name resolution" in logs). The /price endpoint is
# PUBLIC — no auth needed — so we poll it with a persistent
# keep-alive Session: the reused connection skips DNS entirely on
# most calls, and (connect=2s, read=3s) caps any stall at ~3s.
PRICE_SESSION = requests.Session()

PRICE_TIMEOUT = (2, 3)  # (connect, read) seconds


def _get_price_http(token_id, side):
    """Poll best price via raw HTTP on a keep-alive session.
    side='BUY' → best ask, side='SELL' → best bid."""
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
    """Best ASK — the price you'd PAY to buy. Use for entries."""
    price = _get_price_http(token_id, "BUY")
    if price is not None:
        return price

    # fallback: one SDK attempt
    try:
        return _parse_price(client.get_price(token_id, "BUY"))
    except Exception as e:
        logger.warning(f"⚠️ Ask fetch failed: {e}")
        return None


def get_best_bid_price(token_id):
    """Best BID — the price you'd RECEIVE selling. Use for exits."""
    price = _get_price_http(token_id, "SELL")
    if price is not None:
        return price

    try:
        return _parse_price(client.get_price(token_id, "SELL"))
    except Exception as e:
        logger.warning(f"⚠️ Bid fetch failed: {e}")
        return None

# ===================== ORDER HELPERS =====================

def execute_breakout_buy(token, live_price):
    global total_trades

    size = SHARES_PER_TRADE

    buy_limit = min(
        round(live_price + ENTRY_BUFFER, 3),
        ENTRY_MAX,
    )

    if live_price > ENTRY_MAX:
        logger.warning(
            f"⚠️ Ask {live_price} above ENTRY_MAX {ENTRY_MAX} — skipping buy"
        )
        return None

    logger.info(
        f"🔥 EXECUTING BUY | Trigger: {live_price} | "
        f"Limit: {buy_limit} | Shares: {size}"
    )

    try:
        order_args = OrderArgs(
            token_id=token,
            price=buy_limit,
            size=size,
            side=BUY,
        )

        response = client.create_and_post_order(
            order_args, order_type=OrderType.GTC
        )

        logger.info(f"✅ ORDER RESPONSE: {response}")

        order_id = response.get("orderID") or response.get("id")
        logger.info(f"🆔 Tracking Order ID: {order_id}")

        total_trades += 1
        return order_id

    except Exception as e:
        logger.error(f"❌ Order placement FAILED: {e}")
        return None


def get_filled_size(order_id):
    """Shares actually matched. A 'LIVE' order with size_matched 0
    is resting, NOT filled."""
    if not order_id:
        return 0.0

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


def is_order_truly_filled(order_id):
    return get_filled_size(order_id) > 0


def cancel_order_safe(order_id):
    """FIX: this SDK build has no .cancel() — both 13:37 and 13:42
    trades left a phantom TP order live on the book after the SL
    sold the shares. Try the method names used across
    py_clob_client versions until one works."""

    if not order_id:
        return False

    attempts = (
        ("cancel",        lambda f: f(order_id)),
        ("cancel_order",  lambda f: f(order_id)),
        ("cancel_orders", lambda f: f([order_id])),
    )

    for name, call in attempts:
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
        f"❌ Could NOT cancel {order_id} — phantom order may remain "
        f"on the book. Cancel it manually in the Polymarket UI."
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


def wait_for_token_balance(token_id, need, timeout_s=20, poll_s=2):
    logger.info(
        f"⏳ Waiting for {need} shares to settle (timeout {timeout_s}s) …"
    )

    start = time.time()
    bal   = 0.0

    while time.time() - start < timeout_s:
        # Don't wait past the window end either
        if seconds_left_in_window() < END_OF_WINDOW_BUFFER_S:
            logger.warning("⚠️ Window ending — abandoning settlement wait")
            break

        bal = get_token_balance(token_id)

        if bal >= need:
            logger.info(
                f"✅ Settled: {bal} shares in {time.time() - start:.1f}s"
            )
            return bal

        logger.info(f"   …settling: {bal}/{need} shares so far")
        time.sleep(poll_s)

    logger.warning(f"⚠️ Settlement timeout — only {bal}/{need} visible")
    return bal


def place_sell_order(token, price, size, label="SELL"):
    logger.info(f"💱 {label} → limit {price} | shares {size}")

    try:
        order_args = OrderArgs(
            token_id=token,
            price=price,
            size=size,
            side=SELL,
        )

        response = client.create_and_post_order(
            order_args, order_type=OrderType.GTC
        )

        logger.info(f"✅ {label} RESPONSE: {response}")
        return response.get("orderID") or response.get("id")

    except Exception as e:
        logger.error(f"❌ {label} placement FAILED: {e}")
        return None


def execute_stop_loss_sell(token, size):
    """FIX: cross the book. v2 sold with a limit AT 0.78 — on a gap
    down the bid is already below that and the order rests unfilled
    while the position rides to zero. Here we price at the LIVE BID
    minus a buffer (floored at SL_FLOOR) so it fills immediately,
    and retry once at the floor if needed."""

    bid = get_best_bid_price(token)

    if bid is not None:
        px = max(round(bid - SL_CROSS_BUFFER, 3), SL_FLOOR)
    else:
        px = SL_FLOOR  # no bid data — exit at any price

    oid = place_sell_order(token, px, size, label="STOP-LOSS")

    if oid is None:
        # one aggressive retry at the floor
        oid = place_sell_order(
            token, SL_FLOOR, size, label="STOP-LOSS-RETRY"
        )

    return oid, px

# ===================== POSITION MONITOR =====================

def monitor_position(order_id, leader_token, leader, entry_price):
    global wins, losses, unresolved

    # ── Phase 1: Confirm fill ─────────────────────

    FILL_TIMEOUT_S = 30
    FILL_POLL_S    = 2

    logger.info(f"⏳ Waiting for fill (timeout {FILL_TIMEOUT_S}s) …")

    fill_start  = time.time()
    filled_size = 0.0

    while time.time() - fill_start < FILL_TIMEOUT_S:
        if seconds_left_in_window() < END_OF_WINDOW_BUFFER_S:
            break

        filled_size = get_filled_size(order_id)

        if filled_size > 0:
            logger.info(
                f"✅ Entry FILLED {filled_size} shares in "
                f"{time.time() - fill_start:.1f}s"
            )
            break

        time.sleep(FILL_POLL_S)

    if filled_size <= 0:
        logger.warning(
            f"⚠️ Entry NOT filled (size_matched 0) — cancelling"
        )
        cancel_order_safe(order_id)
        return

    # ── Phase 2: Bracket ──────────────────────────

    POSITION_POLL_S = 1  # was 2 — faster SL reaction on collapses

    held = int(filled_size)
    if held <= 0:
        logger.warning("⚠️ Filled size rounds to 0 — aborting bracket")
        return

    settled  = wait_for_token_balance(leader_token, held)
    sellable = int(settled)

    if sellable <= 0:
        logger.error(
            "❌ No settled balance — holding, will retry SL at exit time"
        )
        sellable = held

    held = min(held, sellable)

    tp_order_id = place_sell_order(
        leader_token, TAKE_PROFIT_PRICE, held, label="TAKE-PROFIT"
    )

    tp_per_share = TAKE_PROFIT_PRICE - entry_price

    logger.info(
        f"🎯 TP @ {TAKE_PROFIT_PRICE} "
        f"(+${tp_per_share:.3f}/sh, +${tp_per_share * held:.3f}) | "
        f"SL trigger (BID) @ {STOP_LOSS_TRIGGER}"
    )

    # FIX: the watch window is bounded by the REAL market end, not a
    # fixed 170s that can overrun into the next window.
    watch_end = get_window_end_ts() - END_OF_WINDOW_BUFFER_S

    logger.info(
        f"👁️  MONITORING {leader} until window end "
        f"({watch_end - time.time():.0f}s away) …"
    )

    last_log = 0

    # FIX (reaction speed): the 13:42 loss collapsed 0.91 → 0.78
    # between two polls. Old loop did a TP-status HTTP call + bid
    # call + 2s sleep (~2.5s/cycle). Now: bid every ~1s, TP fill
    # check every 4th tick (TP fills are confirmed a couple seconds
    # later at worst; SL reaction is what's time-critical).
    BLIND_EXIT_S  = 15
    last_good_bid = time.time()
    tick          = 0

    while time.time() < watch_end:

        tick += 1

        if (
            tp_order_id
            and tick % 4 == 1
            and is_order_truly_filled(tp_order_id)
        ):
            wins += 1
            logger.info(
                f"🏆 TAKE-PROFIT FILLED @ {TAKE_PROFIT_PRICE} "
                f"(+${tp_per_share * held:.3f}) | "
                f"W:{wins} L:{losses} U:{unresolved} T:{total_trades}"
            )
            return

        # FIX: monitor the BID for exit decisions
        bid = get_best_bid_price(leader_token)
        now = time.time()

        if bid is None:

            blind_for = now - last_good_bid

            if blind_for >= BLIND_EXIT_S:
                logger.error(
                    f"🚨 BLIND for {blind_for:.0f}s (network down?) "
                    f"— protective exit"
                )

                if tp_order_id:
                    cancel_order_safe(tp_order_id)

                _, exit_px = execute_stop_loss_sell(leader_token, held)

                unresolved += 1
                logger.warning(
                    f"🚨 Protective exit attempted ~@{exit_px} "
                    f"(counted UNRESOLVED — verify in wallet) | "
                    f"W:{wins} L:{losses} U:{unresolved} T:{total_trades}"
                )
                return

            time.sleep(1)  # retry faster while blind
            continue

        last_good_bid = now

        if now - last_log >= PRICE_LOG_INTERVAL_S:
            ts = datetime.now().strftime("%H:%M:%S")
            logger.info(
                f"   [{ts}] Position Watch → {leader} "
                f"BID {bid:.3f} | "
                f"{watch_end - now:.0f}s to window end"
            )
            last_log = now

        if bid <= STOP_LOSS_TRIGGER:
            logger.warning(
                f"🛑 STOP-LOSS: bid {bid:.3f} ≤ {STOP_LOSS_TRIGGER}"
            )

            if tp_order_id:
                cancel_order_safe(tp_order_id)

            _, exit_px = execute_stop_loss_sell(leader_token, held)

            losses += 1
            sl_per_share = exit_px - entry_price

            logger.info(
                f"💀 STOP-LOSS exit ~@{exit_px} "
                f"(~${sl_per_share * held:.3f}) | "
                f"W:{wins} L:{losses} U:{unresolved} T:{total_trades}"
            )
            return

        time.sleep(POSITION_POLL_S)

    # ── Window ended without TP fill or SL ────────
    # Position rides to resolution. Cancel resting TP and count
    # the outcome honestly as unresolved (resolution will pay
    # $1 or $0 — check your wallet/logs to reconcile later).

    if tp_order_id:
        cancel_order_safe(tp_order_id)

    unresolved += 1

    logger.warning(
        f"⏰ Window ended — TP cancelled, position riding to "
        f"resolution (counted UNRESOLVED) | "
        f"W:{wins} L:{losses} U:{unresolved} T:{total_trades}"
    )

def wait_for_next_window():
    """After finishing/skipping a market, the next one doesn't exist
    until the current 5m window ends. Sleep until then with a visible
    heartbeat so the bot doesn't LOOK hung during the silent wait."""

    target = get_window_end_ts()

    while True:
        remaining = target - time.time()

        if remaining <= 0:
            logger.info("🔄 New window opening — scanning…")
            return

        logger.info(
            f"⏸️ Waiting for next window … {remaining:.0f}s "
            f"(opens {datetime.fromtimestamp(target).strftime('%H:%M:%S')})"
        )

        time.sleep(min(15, max(remaining, 0.5)))


# ===================== MAIN LOOP =====================

# FIX: bounded memory — old slugs evicted automatically
seen_markets = deque(maxlen=500)

while True:

    market = get_open_fast_markets()

    if not market:
        time.sleep(SCAN_INTERVAL_S)
        continue

    market_id = market.get("id") or market.get("slug")

    if market_id in seen_markets:
        # Already handled this window's market — instead of silently
        # polling every 2s (looks hung), wait visibly for the next
        # window to open.
        wait_for_next_window()
        continue

    seen_markets.append(market_id)

    logger.info(f"🚀 Processing Market → {market.get('question')}")

    yes_token, no_token = safe_get_clob_tokens(market)

    if not yes_token or not no_token:
        logger.error("❌ Failed to parse token IDs")
        continue

    # FIX: observation ends at min(DECISION_WINDOW_S, actual window
    # time minus what we need for entry+exit). If we caught the
    # market late, don't observe past the close.
    obs_end = min(
        time.time() + DECISION_WINDOW_S,
        get_window_end_ts() - 60,   # leave ≥60s for entry + exits
    )

    logger.info(
        f"⏳ Observing prices for {obs_end - time.time():.0f}s."
    )

    last_log = 0

    while time.time() < obs_end:

        yes_p = get_best_ask_price(yes_token)
        no_p  = get_best_ask_price(no_token)

        now = time.time()

        if now - last_log >= PRICE_LOG_INTERVAL_S:
            ts = datetime.now().strftime("%H:%M:%S")
            yes_str = f"{yes_p:.3f}" if yes_p is not None else "N/A"
            no_str  = f"{no_p:.3f}" if no_p is not None else "N/A"

            logger.info(
                f"   [{ts}] LIVE ASK → YES: {yes_str} | NO: {no_str}"
            )
            last_log = now

        time.sleep(1)

    yes_final = get_best_ask_price(yes_token)
    no_final  = get_best_ask_price(no_token)

    logger.info(f"📊 FINAL → YES: {yes_final} | NO: {no_final}")

    # ── Skip decided markets ──

    if (
        yes_final is not None and yes_final >= SKIP_DECIDED_MARKET
    ) or (
        no_final is not None and no_final >= SKIP_DECIDED_MARKET
    ):
        logger.warning(
            f"⚠️ SKIPPING | YES={yes_final} | NO={no_final} | decided"
        )
        continue

    # ── Pick leader ──

    leader = leader_token = leader_price = None

    if (
        yes_final is not None
        and yes_final >= MIN_LEADER_PCT
        and (no_final is None or yes_final > no_final)
    ):
        leader, leader_token, leader_price = "YES", yes_token, yes_final

    elif (
        no_final is not None
        and no_final >= MIN_LEADER_PCT
        and (yes_final is None or no_final > yes_final)
    ):
        leader, leader_token, leader_price = "NO", no_token, no_final

    if not leader:
        logger.info("⏭️ No momentum leader detected")
        continue

    logger.info(f"🔥 Momentum Leader → {leader} @ {leader_price}")

    # ── FIX: skip overshot markets ──
    # If the leader is ALREADY above the entry band at decision
    # time, the only way price re-enters 0.93–0.935 is by FALLING.
    # Buying that is a reversal entry, not a breakout. Skip.
    if leader_price > TRIGGER_MAX:
        logger.warning(
            f"⏭️ SKIPPING — leader {leader_price} already above "
            f"entry band ({TRIGGER_MAX}); would only enter on a "
            f"falling price"
        )
        continue

    # ── Breakout watch (bounded by window end) ──

    breakout_end = get_window_end_ts() - 30  # need ≥30s to manage exits

    # FIX: require an UPWARD cross into the band. Even when the
    # leader starts below the band, it can spike over it and fall
    # back through — that's also a falling entry. We only buy when
    # the PREVIOUS tick was below the band and the current tick is
    # inside it (price rising into the zone).
    prev_price = leader_price

    while time.time() < breakout_end:

        live_price = get_best_ask_price(leader_token)

        logger.info(f"📈 Breakout Watch → {live_price}")

        if live_price is None:
            time.sleep(1)
            continue

        # ── Entry logic ──
        # Prices tick in 0.01 increments but the band (0.93–0.935)
        # contains only ONE quotable price, so strong momentum can
        # GAP over it (0.92 → 0.94 in one tick). An upward cross of
        # TRIGGER_MIN counts as a breakout whether price lands IN
        # the band or jumps past it — as long as we'd still pay at
        # most ENTRY_MAX.
        crossed_up = (
            prev_price < TRIGGER_MIN
            and live_price >= TRIGGER_MIN
        )

        fell_into_band = (
            prev_price > TRIGGER_MAX
            and TRIGGER_MIN <= live_price <= TRIGGER_MAX
        )

        if fell_into_band:
            logger.info(
                f"⤵️ Price fell into band from {prev_price} — "
                f"NOT a breakout, ignoring"
            )

        if crossed_up and live_price <= ENTRY_MAX:

            # ── BTC strike filter ──
            if not btc_filter_pass(leader):
                prev_price = live_price
                time.sleep(1)
                continue

            logger.info(
                f"🔥 UPWARD BREAKOUT ({prev_price} → {live_price}, "
                f"cap {ENTRY_MAX})"
            )

            order_id = execute_breakout_buy(leader_token, live_price)

            logger.info(f"🎯 ORDER RESULT → {order_id}")

            if order_id:
                monitor_position(
                    order_id,
                    leader_token,
                    leader,
                    entry_price=min(
                        round(live_price + ENTRY_BUFFER, 3),
                        ENTRY_MAX,
                    ),
                )

            break

        # Price above what we're willing to pay — whether it gapped
        # there or climbed there, this market is done for us. Any
        # return to the band would be a falling entry.
        if live_price > ENTRY_MAX:
            logger.info(
                f"⏭️ Leader ran past max entry ({live_price} > "
                f"{ENTRY_MAX}) — done with this market"
            )
            break

        if live_price < FAILURE_THRESHOLD:
            logger.info("❌ Momentum failed.")
            break

        prev_price = live_price
        time.sleep(1)

    else:
        logger.info("⏰ Breakout watch ended — window too close to expiry")
