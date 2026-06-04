"""
spx_flow.py — Dedicated SPX/SPY 0DTE flow handler.
Filters for highest-conviction SPX sweeps only ($5M+, 0-1 DTE, ask side).
Sends to TELEGRAM_SPX_CHAT_ID with GEX context baked in.
"""
import os, requests, time
from datetime import datetime
from zoneinfo import ZoneInfo


def create_spx_custom_alert():
    """
    Register a custom Bullflow alert for SPX/SPY 0DTE flow.
    Called once on startup. Returns alert id or None.
    """
    key = os.environ.get("BULLFLOW_API_KEY","")
    if not key:
        return None
    try:
        payload = {
            "name":            "FlowCheck SPX 0DTE",
            "tickerAllowlist": ["SPY", "SPX", "SPXW"],
            "premiumMin":      5_000_000,   # $5M+ only
            "dteMin":          0,
            "dteMax":          1,            # 0DTE and 1DTE
            "includeCalls":    True,
            "includePuts":     True,
            "includeAskSide":  True,
            "includeBidSide":  False,        # Ask side only — aggression
            "includeSweeps":   True,
            "quickFilters":    ["Sweeps", "Ask", "Unusual", "Whales"],
        }
        r = requests.post(
            "https://api.bullflow.io/v1/alerts/create-alert",
            params={"key": key},
            json=payload,
            timeout=15
        )
        if r.status_code == 200:
            alert_id = r.json().get("id","")
            print(f"[SPX] Custom alert created: {alert_id}")
            return alert_id
        else:
            print(f"[SPX] Alert creation failed: {r.status_code} {r.text[:100]}")
            return None
    except Exception as e:
        print(f"[SPX] Alert creation error: {e}")
        return None


def format_spx_alert(alert: dict, gex_data: dict = None) -> str:
    """
    Format a high-conviction SPX flow alert with GEX context.
    """
    now_et   = datetime.now(ZoneInfo("America/New_York"))
    ticker   = alert.get("symbol","SPY").split(":")[1][:3] if ":" in alert.get("symbol","") else "SPY"
    premium  = float(alert.get("alertPremium", 0) or 0)
    fill_px  = float(alert.get("averageFillPrice", 0) or 0)
    name     = alert.get("alertName","")
    ts       = alert.get("timestamp", 0)

    # Parse option details from OCC symbol e.g. O:SPY260605C00755000
    sym      = alert.get("symbol","")
    opt_type = ""
    strike   = ""
    expiry   = ""
    try:
        # OCC format: O:SPY260605C00755000
        clean = sym.replace("O:","")
        # Find C or P
        for i, ch in enumerate(clean):
            if ch in ("C","P") and i > 3:
                opt_type = "Call" if ch == "C" else "Put"
                expiry_raw = clean[len(ticker):i]
                strike_raw = clean[i+1:]
                strike = str(int(strike_raw) / 1000)
                # Parse expiry YYMMDD
                if len(expiry_raw) == 6:
                    expiry = f"06/{expiry_raw[2:4]}/20{expiry_raw[:2]}"
                break
    except: pass

    prem_str = f"${premium/1_000_000:.1f}M" if premium >= 1_000_000 else f"${premium/1_000:.0f}K"
    emoji    = "📞" if "call" in opt_type.lower() else "📉" if "put" in opt_type.lower() else "⚡"
    time_str = now_et.strftime("%I:%M%p")

    lines = [
        f"━━━ SPX FLOW {time_str} ━━━",
        f"{emoji} {ticker} {strike}{opt_type[0] if opt_type else ''} {expiry} — {prem_str} @ ${fill_px:.2f}",
        f"🚨 {name}",
    ]

    # GEX context
    if gex_data:
        regime   = gex_data.get("regime","")
        flip     = gex_data.get("gamma_flip")
        spot     = gex_data.get("spot_price",0)
        cwall    = gex_data.get("call_wall")
        pwall    = gex_data.get("put_wall")

        if regime == "negative":
            lines.append(f"⚡ GEX: NEGATIVE — dealers amplify moves")
        else:
            lines.append(f"🧲 GEX: POSITIVE — dealers fade moves")

        if flip and spot:
            above = spot > flip
            dist  = round(abs(((flip - spot)/spot)*100), 1)
            if dist < 0.3:
                lines.append(f"🎯 Flip: ${flip:.0f} ⚠️ AT THE FLIP")
            else:
                lines.append(f"🎯 Flip: ${flip:.0f} ({'above' if above else 'below'} spot by {dist}%)")

        if "call" in opt_type.lower() and cwall:
            dist_wall = round(((cwall - spot)/spot)*100, 1) if spot else 0
            lines.append(f"🎯 Call wall: ${cwall:.0f} (+{dist_wall:.1f}%) — target/resistance")

        if "put" in opt_type.lower() and pwall:
            dist_wall = round(((spot - pwall)/spot)*100, 1) if spot else 0
            lines.append(f"🎯 Put wall: ${pwall:.0f} (-{dist_wall:.1f}%) — target/support")

    # Direction bias summary
    if opt_type:
        if "call" in opt_type.lower():
            lines.append("📈 Bullish 0DTE sweep — institutional buy pressure")
        else:
            lines.append("📉 Bearish 0DTE sweep — institutional sell pressure")

    lines.append(f"📈 https://www.tradingview.com/chart/?symbol=SPY")
    return chr(10).join(lines)


def send_spx_alert(alert: dict, gex_data: dict = None):
    """Send formatted SPX alert to dedicated SPX channel."""
    spx_chat = os.environ.get("TELEGRAM_SPX_CHAT_ID","")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN","")
    if not spx_chat or not bot_token:
        print("[SPX] TELEGRAM_SPX_CHAT_ID not set — skipping SPX alert")
        return
    from sms import send_telegram
    msg = format_spx_alert(alert, gex_data)
    send_telegram(msg, bot_token, spx_chat)
    print(f"[SPX] Alert sent to SPX channel")
