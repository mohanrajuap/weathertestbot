# =========================================
# place_test_order.py — Polymarket order smoke test
#
# Goal: prove that order placement works with YOUR wallet on Railway (or
# locally) BEFORE building the rest of the bot. It resolves a team in the
# World Cup Winner event, shows your USDC balance and the live book, then
# places ONE small GTC limit BUY *below* the market so it RESTS UNFILLED —
# exercising auth → sign → post → status → cancel with ~$1 at risk and no
# real fill. You then cancel it with the printed one-liner.
#
# Usage (from this folder, with .env filled in):
#   python place_test_order.py                 # place the safe resting test order
#   python place_test_order.py status <id>     # check an order's status
#   python place_test_order.py cancel <id>     # cancel an order
#
# Env knobs:
#   TEAM=France           which sub-market (groupItemTitle) to use
#   TEST_PRICE=           explicit limit price (else best_bid - TEST_UNDER)
#   TEST_UNDER=0.03       how far below best bid to rest (safe = won't fill)
#   TEST_NOTIONAL=1.10    target $ notional (size = notional / price)
#   TEST_SIZE=            explicit share size (overrides notional)
#   MARKETABLE=false      true = cross the book and actually FILL (spends $!)
#   DRY_RUN=true          true = log only, don't send (set false to really place)
# =========================================

import os
import sys
import json
import math
import time

import requests
from dotenv import load_dotenv
from eth_account import Account

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    OrderArgs, OrderType, BalanceAllowanceParams, AssetType,
)
from py_clob_client_v2.order_builder.constants import BUY

load_dotenv()

HOST  = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
EVENT_SLUG = os.getenv("EVENT_SLUG", "world-cup-winner")

TEAM         = os.getenv("TEAM", "France")
TEST_UNDER   = float(os.getenv("TEST_UNDER", "0.03"))
TEST_NOTIONAL= float(os.getenv("TEST_NOTIONAL", "1.10"))
TEST_PRICE   = os.getenv("TEST_PRICE")            # optional explicit price
TEST_SIZE    = os.getenv("TEST_SIZE")             # optional explicit size
MARKETABLE   = os.getenv("MARKETABLE", "false").lower() in ("1", "true", "yes")
DRY_RUN      = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")


def die(msg):
    print(f"❌ {msg}")
    sys.exit(1)


def make_client():
    pk = os.getenv("PRIVATE_KEY")
    fa = os.getenv("FUNDER_ADDRESS")
    if not pk or not fa:
        die("Missing PRIVATE_KEY or FUNDER_ADDRESS in .env")
    # strip whitespace + any surrounding quotes (common Railway paste error
    # that triggers 'Non-hexadecimal digit found')
    pk = pk.strip().strip('"').strip("'").strip()
    fa = fa.strip().strip('"').strip("'").strip()
    if not pk.lower().startswith("0x"):
        pk = "0x" + pk
    acct = Account.from_key(pk)
    print(f"🔑 Signer  : {acct.address.lower()}")
    print(f"🏦 Funder  : {fa}")
    c = ClobClient(host=HOST, chain_id=137, key=pk, signature_type=3, funder=fa)
    creds = c.create_or_derive_api_key()
    c.set_api_creds(creds)
    print("✅ API credentials initialized")
    return c


def usdc_balance(client):
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                                        signature_type=3)
        try:
            client.update_balance_allowance(params)
        except Exception:
            pass
        resp = client.get_balance_allowance(params)
        raw = resp.get("balance") if isinstance(resp, dict) else None
        return (float(raw) / 1_000_000.0) if raw is not None else None
    except Exception as e:
        print(f"⚠️ USDC balance check failed: {e}")
        return None


def get_price(token_id, side):
    """side 'BUY' → best ask, 'SELL' → best bid. Retries the occasional
    transient DNS/connection blip on clob.polymarket.com."""
    for _ in range(3):
        try:
            r = requests.get(f"{HOST}/price",
                             params={"token_id": token_id, "side": side}, timeout=6)
            d = r.json()
            return float(d["price"]) if isinstance(d, dict) and "price" in d else None
        except Exception:
            time.sleep(1)
    return None


def resolve_team(team):
    r = requests.get(f"{GAMMA}/events", params={"slug": EVENT_SLUG}, timeout=8)
    events = r.json()
    if not events:
        die(f"Event '{EVENT_SLUG}' not found")
    markets = events[0].get("markets", []) or []
    want = team.strip().lower()
    # exact, then substring
    cand = None
    for m in markets:
        if (m.get("groupItemTitle") or "").strip().lower() == want:
            cand = m; break
    if cand is None:
        for m in markets:
            if want in (m.get("groupItemTitle") or "").strip().lower():
                cand = m; break
    if cand is None:
        names = [m.get("groupItemTitle") for m in markets][:20]
        die(f"Team '{team}' not found. Some options: {names}")
    if cand.get("closed"):
        die(f"'{team}' market is CLOSED (eliminated) — pick an active team")
    toks = json.loads(cand.get("clobTokenIds") or "[]")
    if len(toks) < 2:
        die("Could not parse token ids")
    return cand.get("groupItemTitle"), toks[0], toks[1]   # name, YES, NO


def cmd_status(client, order_id):
    try:
        print(json.dumps(client.get_order(order_id), indent=2, default=str))
    except Exception as e:
        die(f"status failed: {e}")


def cmd_cancel(client, order_id):
    for name, call in (("cancel", lambda f: f(order_id)),
                       ("cancel_order", lambda f: f(order_id)),
                       ("cancel_orders", lambda f: f([order_id]))):
        fn = getattr(client, name, None)
        if fn is None:
            continue
        try:
            call(fn)
            print(f"🗑️ Cancelled {order_id} via .{name}()")
            return
        except Exception as e:
            print(f"⚠️ .{name}() failed: {e}")
    die("Could not cancel — try the Polymarket UI")


def cmd_place(client):
    name, yes_tok, no_tok = resolve_team(TEAM)
    token = yes_tok  # buying YES on the team

    bid = get_price(token, "SELL")
    ask = get_price(token, "BUY")
    print(f"\n📊 {name} YES  | bid {bid} | ask {ask}")

    bal = usdc_balance(client)
    print(f"💰 USDC balance: {bal if bal is not None else 'unknown'}\n")

    # Decide price
    if TEST_PRICE:
        price = round(float(TEST_PRICE), 3)
    elif MARKETABLE:
        if ask is None:
            die("No ask to cross — set TEST_PRICE")
        price = round(min(ask + 0.01, 0.99), 3)          # crosses → may FILL
    else:
        base = bid if bid is not None else (ask or 0.10)
        price = max(round(base - TEST_UNDER, 3), 0.01)    # rests BELOW bid
    price = min(max(price, 0.01), 0.99)

    # Decide size (meet ~$1 min notional unless overridden)
    if TEST_SIZE:
        size = int(float(TEST_SIZE))
    else:
        size = max(5, math.ceil(TEST_NOTIONAL / price))
    notional = round(price * size, 2)

    kind = "MARKETABLE (will likely FILL)" if MARKETABLE else "RESTING (should NOT fill)"
    print(f"📝 Test order: BUY {size} {name} YES @ {price}  (~${notional}) — {kind}")

    if bal is not None and bal < notional and not DRY_RUN:
        die(f"USDC balance ${bal} < order notional ${notional} — fund the wallet first")

    if DRY_RUN:
        print("🧪 DRY_RUN=true — order NOT sent. Set DRY_RUN=false to place for real.")
        return

    try:
        args = OrderArgs(token_id=token, price=price, size=size, side=BUY)
        resp = client.create_and_post_order(args, order_type=OrderType.GTC)
        print(f"\n✅ ORDER RESPONSE:\n{json.dumps(resp, indent=2, default=str)}")
        oid = resp.get("orderID") or resp.get("id")
        print(f"\n🆔 Order ID: {oid}")
        if oid:
            time.sleep(2)
            try:
                st = client.get_order(oid)
                print(f"📄 Status: {st.get('status')} | matched {st.get('size_matched')}")
            except Exception:
                pass
            print(f"\n👉 To cancel:  python place_test_order.py cancel {oid}")
    except Exception as e:
        die(f"Order placement FAILED: {e}")


def main():
    args = sys.argv[1:]
    client = make_client()
    if not args or args[0] == "place":
        cmd_place(client)
    elif args[0] == "status" and len(args) > 1:
        cmd_status(client, args[1])
    elif args[0] == "cancel" and len(args) > 1:
        cmd_cancel(client, args[1])
    else:
        print("usage: python place_test_order.py [place | status <id> | cancel <id>]")


if __name__ == "__main__":
    main()
