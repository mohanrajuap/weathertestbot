# =========================================
# signal_emitter.py  —  DROP-IN for the forecast bot (mohanrajuap/weather)
#
# The forecast bot (monitor.py) is alert-only: it pushes Telegram/ntfy
# cards but writes no machine-readable signal. This module bridges that
# gap. Call emit_signal(p) at the exact moment monitor.py decides a
# NEW clean+reliable TRADE — it appends/updates a signal in SIGNALS_FILE
# in the schema the weather_execution_bot.py consumes.
#
# It has NO heavy deps (stdlib only) and NEVER raises into the caller —
# a failure to write a signal must never break the alerting bot.
#
# ── How to wire it into monitor.py (2 additions, both additive) ──
#
#   # (1) near the top, with the other imports:
#   try:
#       from signal_emitter import emit_signal
#   except Exception:
#       emit_signal = None
#
#   # (2) inside the `if crossed_up:` block, right after
#   #     `alert_signal(fmt_new_signal(p))`:
#   if emit_signal:
#       emit_signal(p)
#
#   # (optional) also call it in the `bucket_shifted` branch so a
#   #            model bucket change emits a fresh signal_id.
#
# That's it. buy_now is set True here because monitor.py only reaches
# crossed_up when verdict==TRADE, prob>=threshold, has_edge and reliable.
# =========================================

import os
import json
import time
import tempfile
from datetime import datetime, timezone

# Same default path the execution bot reads. On Railway, set SIGNALS_FILE
# to the shared volume path (e.g. /data/signals.json) in BOTH services.
SIGNALS_FILE = os.environ.get("SIGNALS_FILE", "signals.json")

# Defaults the trader will suggest unless you override them in Telegram.
SIGNAL_TP_PRICE = float(os.environ.get("SIGNAL_TP_PRICE", "0.90"))
SIGNAL_SL_PRICE = float(os.environ.get("SIGNAL_SL_PRICE", "0.20"))
# How many candidate buckets to put on the Telegram menu (highest model
# probability first), so you can pick which position(s) to buy.
MAX_CANDIDATES  = int(os.environ.get("SIGNAL_MAX_CANDIDATES", "5"))


def _slug_from_pm(pm: dict) -> str:
    """The pm_data dict stores the slug only inside its url; recover it."""
    if not isinstance(pm, dict):
        return ""
    slug = pm.get("slug")
    if slug:
        return slug
    url = pm.get("url") or ""
    if "/event/" in url:
        return url.split("/event/")[-1].strip("/")
    return ""


def build_signal(p: dict) -> dict | None:
    """Translate a forecast prediction dict `p` into a trade signal with
    a MENU of candidate buckets (highest model probability first), so the
    Telegram trader can let you pick which position(s) to buy. Returns
    None if there's nothing actionable."""
    pm   = p.get("polymarket") or {}
    slug = _slug_from_pm(pm)
    if not slug:
        return None

    buckets = pm.get("buckets") or {}
    edges   = p.get("edges") or []
    best    = p.get("best_trade") or {}
    best_temp = best.get("temp")

    candidates = []
    for e in edges:
        temp = e.get("temp")
        price = e.get("yes_price")
        if price is None:
            continue
        b = buckets.get(temp) or {}
        candidates.append({
            "bucket":     str(temp),
            "side":       "YES",                  # buying the bucket to win
            "model_prob": e.get("model_prob"),
            "price":      price,                  # market YES price
            "edge":       e.get("edge_yes"),
            "ev":         e.get("ev"),
            "kelly_quarter": e.get("kelly_quarter"),
            "token_id":   b.get("token_yes"),     # pre-resolved YES token
            "is_best":    (temp == best_temp),
        })

    if not candidates:
        return None

    # Highest model probability first; keep the model's best trade pinned.
    candidates.sort(key=lambda c: (c.get("is_best", False),
                                   c.get("model_prob") or 0), reverse=True)
    candidates = candidates[:MAX_CANDIDATES]

    city  = p.get("city")
    tdate = p.get("target_date")

    return {
        "signal_id":  f"{city}|{tdate}",          # one menu per event
        "event_slug": slug,
        "city":       city,
        "target_date": tdate,
        "temp_unit":  p.get("temp_unit"),
        "candidates": candidates,
        "tp_price":   SIGNAL_TP_PRICE,
        "sl_price":   SIGNAL_SL_PRICE,
        "buy_now":    True,                       # set at crossed_up only
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _read(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, dict) and isinstance(data.get("signals"), list):
        return data["signals"]
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def _atomic_write(path: str, signals: list) -> None:
    """Write {"signals": [...]} atomically so the execution bot never
    reads a half-written file."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"signals": signals}, f, indent=2, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def emit_signal(p: dict, path: str | None = None) -> bool:
    """Append or update a signal for prediction `p`. Idempotent by
    signal_id: re-emitting refreshes price/edge/buy_now in place rather
    than duplicating. Never raises."""
    try:
        sig = build_signal(p)
        if sig is None:
            return False
        target = path or SIGNALS_FILE
        signals = _read(target)
        sid = sig["signal_id"]
        replaced = False
        for i, existing in enumerate(signals):
            if str(existing.get("signal_id")) == sid:
                signals[i] = sig
                replaced = True
                break
        if not replaced:
            signals.append(sig)
        # keep the file bounded
        if len(signals) > 200:
            signals = signals[-200:]
        _atomic_write(target, signals)
        return True
    except Exception as e:
        try:
            print(f"[signal_emitter] emit failed: {e}")
        except Exception:
            pass
        return False
