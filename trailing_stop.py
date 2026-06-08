"""
trailing_stop.py — Trailing stop monitor for FlowCheck.

Tracks high-water mark (peak stock price) per watchlist position.
Fires alert when stock drops X% from peak.
"""
import os, time, json
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HWM_KEY = "high_water_marks"   # Supabase storage key


def load_hwm() -> dict:
    """Load high water marks from Supabase."""
    try:
        from storage import db_get as _dg
        return json.loads(_dg(HWM_KEY) or "{}")
    except:
        return {}


def save_hwm(hwm: dict):
    """Save high water marks to Supabase."""
    try:
        from storage import db_set as _ds
        _ds(HWM_KEY, json.dumps(hwm))
    except: pass


def update_hwm(ticker: str, current_price: float) -> float:
    """Update and return high water mark for ticker."""
    hwm = load_hwm()
    prev = float(hwm.get(ticker, 0) or 0)
    if current_price > prev:
        hwm[ticker] = current_price
        save_hwm(hwm)
        return current_price
    return prev


def check_trailing_stop(watchlist: dict, send_fn=None):
    """
    Check all watchlist positions for trailing stop triggers.
    Default trailing stop: 12% from peak stock price.
    Configurable via TRAILING_STOP_PCT Railway variable.
    """
    stop_pct = float(os.environ.get("TRAILING_STOP_PCT", "12")) / 100
    bot      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat     = os.environ.get("TELEGRAM_TRADE_CHAT_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot or not chat or not watchlist:
        return

    now_et = datetime.now(ET)
    if now_et.weekday() >= 5 or not (9 <= now_et.hour < 16):
        return

    hwm  = load_hwm()
    hwm_updated = False

    try:
        from fetcher import fetch_price as _fp
    except:
        return

    alerts_fired = 0
    for ticker, entry in watchlist.items():
        try:
            is_call = "put" not in (entry.get("option_type","call") or "call").lower()
            if not is_call:
                # For puts: alert when stock RISES X% from trough
                continue  # Handle calls only for now

            px = float(_fp(ticker) or 0)
            if not px:
                continue

            # Update HWM
            prev_hwm = float(hwm.get(ticker, 0) or 0)
            if px > prev_hwm:
                hwm[ticker] = px
                hwm_updated = True
                continue  # New high — no alert needed

            if prev_hwm <= 0:
                hwm[ticker] = px
                hwm_updated = True
                continue

            # Check trailing stop
            drop_pct  = (prev_hwm - px) / prev_hwm * 100
            threshold = stop_pct * 100

            if drop_pct >= threshold:
                strike   = entry.get("strike", "?")
                expiry   = entry.get("expiry", "?")
                score    = entry.get("flow_score", "?")
                verdict  = entry.get("verdict", "WATCH")

                msg = (
                    f"🛑 TRAILING STOP: {ticker}\n"
                    f"Peak: ${prev_hwm:.2f} → Now: ${px:.2f} "
                    f"({drop_pct:.1f}% from peak)\n"
                    f"Threshold: {threshold:.0f}% | "
                    f"Drop: ${prev_hwm-px:.2f}\n"
                    f"📋 {ticker} {strike}C {expiry} [{score}/7 {verdict}]\n"
                    f"→ Consider exiting or moving stop to ${px*0.97:.2f}"
                )

                if send_fn:
                    send_fn(msg, bot, chat)
                print(f"[TRAILING] 🛑 {ticker} down {drop_pct:.1f}% from peak ${prev_hwm:.2f}")
                alerts_fired += 1

                # Reset HWM after alert so we don't spam
                hwm[ticker] = px
                hwm_updated = True

        except Exception as e:
            print(f"[TRAILING] Error {ticker}: {e}")

    if hwm_updated:
        save_hwm(hwm)

    if alerts_fired:
        print(f"[TRAILING] {alerts_fired} trailing stop alert(s) sent")
