"""
Feature 2: Real-time price alerts for open positions.
Polls Finnhub every 60 seconds during market hours.
Fires immediately when stop or target is hit — no 15-min wait.
"""
import os, requests, time
from datetime import datetime
from zoneinfo import ZoneInfo

_last_alerts = {}  # ticker -> last alert type (stop/target) to avoid spam

def fh_get_price(ticker: str) -> float | None:
    key = os.environ.get("FINNHUB_API_KEY","")
    if not key:
        return None
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": ticker.upper(), "token": key},
            timeout=6
        )
        if r.status_code == 200:
            return float(r.json().get("c",0) or 0) or None
    except:
        pass
    return None

def check_price_alerts(send_sms_fn):
    """
    Check open positions against stop/target prices.
    Called every 60 seconds during market hours.
    """
    from market_calendar import is_market_open
    if not is_market_open():
        return

    now_et = datetime.now(ZoneInfo("America/New_York"))
    total  = now_et.hour * 60 + now_et.minute
    # Only during market hours 9:30 AM - 4:00 PM
    if total < 9*60+30 or total > 16*60:
        return

    try:
        from exit_signals import get_open_positions
        positions = get_open_positions()
        if not positions:
            return

        for p in positions:
            ticker = p.get("ticker","")
            stop   = p.get("stop_price")
            target = p.get("target_price")
            if not ticker or not stop:
                continue

            price = fh_get_price(ticker)
            if not price:
                continue

            last_alert = _last_alerts.get(ticker,"")

            # Stop hit
            if float(price) <= float(stop) and last_alert != "stop":
                otype = p.get("option_type","call")[0].upper()
                strike= p.get("strike","")
                expiry= p.get("expiry","")
                msg   = (
                    "🛑 STOP HIT: " + ticker + " " + strike + otype + " " + expiry + chr(10) +
                    "Stock: $" + str(price) + " ≤ Stop: $" + str(stop) + chr(10) +
                    "Consider closing position"
                )
                send_sms_fn(msg, verdict="TRADE")
                _last_alerts[ticker] = "stop"
                print(f"[PRICE ALERT] Stop hit: {ticker} @ ${price}")

            # Target hit
            elif target and float(price) >= float(target) and last_alert != "target":
                otype = p.get("option_type","call")[0].upper()
                strike= p.get("strike","")
                expiry= p.get("expiry","")
                msg   = (
                    "🎯 TARGET HIT: " + ticker + " " + strike + otype + " " + expiry + chr(10) +
                    "Stock: $" + str(price) + " ≥ Target: $" + str(target) + chr(10) +
                    "Consider taking profits"
                )
                send_sms_fn(msg, verdict="TRADE")
                _last_alerts[ticker] = "target"
                print(f"[PRICE ALERT] Target hit: {ticker} @ ${price}")

            # Reset alert if price recovers
            elif last_alert in ("stop","target"):
                mid = (float(stop) + float(target or stop)) / 2
                if float(stop) < price < float(target or price+1):
                    _last_alerts[ticker] = ""

    except Exception as e:
        print(f"[PRICE ALERT] Error: {e}")
