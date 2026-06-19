"""
Pre-market gap alert — runs at 9:00 AM ET.
Checks if any watchlist tickers are gapping up/down in pre-market.
Especially useful for after-hours and late day flows.
"""
import os, requests, time
from datetime import datetime
from zoneinfo import ZoneInfo
from sms import send_sms

def get_premarket_price(ticker: str) -> float | None:
    """Get current pre-market price from Polygon."""
    key = os.environ.get("POLYGON_API_KEY","")
    if not key:
        return None
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/last/trade/{ticker.upper()}",
            params={"apiKey": key},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            price = data.get("results", {}).get("p")
            if price:
                return round(float(price), 2)
    except Exception as e:
        print(f"[PREMARKET] Price error {ticker}: {e}")

    # Fallback: Polygon snapshot
    try:
        r2 = requests.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker.upper()}",
            params={"apiKey": key},
            timeout=8
        )
        if r2.status_code == 200:
            snap = r2.json().get("ticker", {})
            price = (snap.get("day", {}).get("o") or
                     snap.get("prevDay", {}).get("c") or
                     snap.get("lastTrade", {}).get("p"))
            if price:
                return round(float(price), 2)
    except Exception as e:
        print(f"[PREMARKET] Snapshot error {ticker}: {e}")
    return None

def get_prev_close(ticker: str) -> float | None:
    """Get previous closing price from Polygon."""
    key = os.environ.get("POLYGON_API_KEY","")
    if not key:
        return None
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker.upper()}",
            params={"apiKey": key},
            timeout=8
        )
        if r.status_code == 200:
            prev = r.json().get("ticker", {}).get("prevDay", {})
            c = prev.get("c")
            if c:
                return round(float(c), 2)
    except Exception as e:
        print(f"[PREMARKET] Prev close error {ticker}: {e}")
    return None

def send_premarket_gap_alerts(watchlist: dict):
    """
    Called at 9:00 AM ET.
    Checks all watchlist tickers for pre-market gaps.
    Sends alert if gap > 1.5% in either direction.
    """
    if not watchlist:
        print("[PREMARKET] No watchlist tickers to check")
        return

    now_et    = datetime.now(ZoneInfo("America/New_York"))
    day_label = now_et.strftime("%a %b %d")

    print(f"[PREMARKET] Checking {len(watchlist)} tickers for pre-market gaps...")
    alerts    = []
    no_gap    = []

    for ticker, entry in list(watchlist.items()):
        try:
            flow_price = entry.get("flow_stock_price")
            prev_close = get_prev_close(ticker)
            pre_price  = get_premarket_price(ticker)
            time.sleep(13)  # Polygon rate limit

            if not pre_price or not prev_close:
                print(f"[PREMARKET] {ticker}: no price data")
                continue

            # Gap vs previous close
            gap_pct = round(((pre_price - prev_close) / prev_close) * 100, 2)

            # Gap vs flow entry
            flow_gap = None
            if flow_price:
                flow_gap = round(((pre_price - float(flow_price)) / float(flow_price)) * 100, 2)

            strike   = entry.get("strike","?")
            opt_type = entry.get("option_type","call")[0].upper()
            expiry   = entry.get("expiry","?")
            verdict  = entry.get("verdict","WATCH")
            score    = entry.get("flow_score","?")
            dte      = entry.get("dte_remaining","?")
            v_emoji  = "✅" if verdict == "TRADE" else "👀"

            print(f"[PREMARKET] {ticker}: prev_close=${prev_close} pre=${pre_price} gap={gap_pct:+.1f}%")

            if abs(gap_pct) >= 1.5:
                gap_emoji = "🚀" if gap_pct > 0 else "📉"
                direction = "UP" if gap_pct > 0 else "DOWN"
                confirm   = "✅ Confirms flow direction" if gap_pct > 0 else "❌ Against flow direction"

                alert = {
                    "ticker":    ticker,
                    "gap_pct":   gap_pct,
                    "pre_price": pre_price,
                    "prev_close":prev_close,
                    "flow_gap":  flow_gap,
                    "direction": direction,
                    "emoji":     gap_emoji,
                    "confirm":   confirm,
                    "entry":     entry,
                }
                alerts.append(alert)
            else:
                no_gap.append(f"{ticker}: {gap_pct:+.1f}% (flat)")

        except Exception as e:
            print(f"[PREMARKET] Error for {ticker}: {e}")

    if not alerts and not no_gap:
        return

    # Build message
    lines = [f"🌅 PRE-MARKET GAP CHECK — {day_label}", ""]

    if alerts:
        # Sort by absolute gap size
        alerts.sort(key=lambda x: abs(x["gap_pct"]), reverse=True)
        for a in alerts:
            e       = a["entry"]
            strike  = e.get("strike","?")
            otype   = e.get("option_type","call")[0].upper()
            expiry  = e.get("expiry","?")
            score   = e.get("flow_score","?")
            verdict = e.get("verdict","WATCH")
            v_emoji = "✅" if verdict == "TRADE" else "👀"

            lines.append(f"{a['emoji']} <b>{a['ticker']} {a['direction']} {a['gap_pct']:+.1f}%</b>")
            lines.append(f"  {v_emoji} {a['ticker']} {strike}{otype} {expiry} [{score}/7]")
            lines.append(f"  Pre-market: ${a['pre_price']} (prev close: ${a['prev_close']})")
            if a["flow_gap"] is not None:
                lines.append(f"  vs Flow entry: {a['flow_gap']:+.1f}%")
            lines.append(f"  {a['confirm']}")
            if a["gap_pct"] > 0:
                lines.append(f"  <b>→ Consider entry at 9:30 open if gap holds</b>")
            else:
                lines.append(f"  <b>→ Wait — gap against flow, reassess thesis</b>")
            lines.append("")
    else:
        lines.append("No significant gaps (>1.5%) in watchlist tickers")
        lines.append("")

    if no_gap:
        lines.append("Flat pre-market: " + " · ".join(no_gap[:5]))

    send_sms("\n".join(lines))
    print(f"[PREMARKET] Gap alerts sent: {len(alerts)} gaps, {len(no_gap)} flat")
