"""
Pre-Market Summary Module
=========================

Correct market timing:
  8:30 AM ET  — Economic data drops (CPI, NFP, PPI etc.)
  9:30 AM ET  — Options market opens
  9:30-10:00  — Noisy open, wide spreads, avoid entries
  10:00 AM    — First clean entry window

Schedule:
  7:30 AM ET  — economic_calendar.fetch_and_cache_today() (separate job)
  8:00 AM ET  — send_premarket_summary() — reads cached calendar
  4:15 PM ET  — verify_eod_positions() — OI now updated post-close
  4:30 PM ET  — send_eod_summary()

Pre-market SMS (8:00 AM) contains:
  1. Macro warnings with actionable "do not enter before X" guidance
  2. Week-ahead high-impact events
  3. Carryover positions — stock price change overnight, EOD OI from yesterday
  4. Market snapshot (pre-market VIX + futures if available)

EOD Summary (4:30 PM) contains:
  1. Verified OI post-close for all open positions
  2. Day's alert recap (TRADE/WATCH/SKIP counts)
  3. Top trades of the day
  4. Tomorrow's macro events
"""

import os
import yfinance as yf
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sms import send_sms
from economic_calendar import (
    get_today_warnings, get_week_ahead_summary,
    get_economic_events, classify_event
)


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def is_trading_day(dt=None) -> bool:
    if dt is None:
        dt = datetime.now(ZoneInfo("America/New_York"))
    return dt.weekday() < 5


def get_pre_market_price(ticker: str) -> float | None:
    """Get latest available price (pre-market or prior close)."""
    try:
        hist = yf.Ticker(ticker).history(period="2d", interval="1m")
        if not hist.empty:
            return round(hist["Close"].iloc[-1], 2)
    except Exception as e:
        print(f"[PREMARKET] Price error for {ticker}: {e}")
    return None


def verify_eod_oi(trade: dict, original_oi: int) -> dict:
    """
    Verify OI post-close (4:15 PM+). OI updates EOD after market close.
    Returns current OI, change, and interpretation.
    """
    result = {
        "ticker":      trade.get("ticker"),
        "current_oi":  None,
        "original_oi": original_oi,
        "oi_change":   None,
        "oi_change_pct": None,
        "status":      "UNKNOWN",
        "emoji":       "❓",
        "note":        ""
    }

    try:
        ticker      = trade.get("ticker")
        strike      = trade.get("strike")
        option_type = trade.get("option_type", "call")
        expiry_raw  = trade.get("expiry_raw")

        if not all([ticker, strike, expiry_raw]):
            result["note"] = "Insufficient data for OI check"
            return result

        stock  = yf.Ticker(ticker)
        parts  = expiry_raw.split("/")
        m, d, y = parts
        y = "20" + y if len(y) == 2 else y
        expiry_yf = f"{y}-{m.zfill(2)}-{d.zfill(2)}"

        available = stock.options
        if not available:
            result["note"] = "No options data"
            return result

        target  = datetime.strptime(expiry_yf, "%Y-%m-%d")
        closest = min(available, key=lambda d: abs(
            (datetime.strptime(d, "%Y-%m-%d") - target).days
        ))

        chain   = stock.option_chain(closest)
        options = chain.calls if option_type == "call" else chain.puts
        options = options.copy()
        options["diff"] = abs(options["strike"] - float(strike))
        row = options.nsmallest(1, "diff").iloc[0]

        current_oi = int(row.get("openInterest", 0))
        result["current_oi"] = current_oi

        if original_oi and original_oi > 0:
            change     = current_oi - original_oi
            change_pct = round((change / original_oi) * 100, 1)
            result["oi_change"]     = change
            result["oi_change_pct"] = change_pct

            if change_pct < -30:
                result["status"] = "LIKELY_CLOSED"
                result["emoji"]  = "❌"
                result["note"]   = f"OI dropped {abs(change_pct):.0f}% — position likely closed/reduced"
            elif change_pct < -10:
                result["status"] = "REDUCED"
                result["emoji"]  = "⚠️"
                result["note"]   = f"OI down {abs(change_pct):.0f}% — partial close possible"
            elif change_pct > 15:
                result["status"] = "GROWING"
                result["emoji"]  = "🟢"
                result["note"]   = f"OI grew +{change_pct:.0f}% — fresh buying added"
            else:
                result["status"] = "STABLE"
                result["emoji"]  = "✅"
                result["note"]   = f"OI stable ({change_pct:+.0f}%) — position still open"
        else:
            result["status"] = "UNKNOWN"
            result["emoji"]  = "❓"
            result["note"]   = f"Current OI: {current_oi:,}"

    except Exception as e:
        print(f"[PREMARKET] OI verify error: {e}")
        result["note"] = "OI check failed"

    return result


def get_yesterday_str() -> str:
    """Get previous trading day date string."""
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() == 0:       # Monday → Friday
        return (now - timedelta(days=3)).strftime("%Y-%m-%d")
    elif now.weekday() == 6:     # Sunday → Friday
        return (now - timedelta(days=2)).strftime("%Y-%m-%d")
    else:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")


def get_tomorrow_str() -> str:
    """Get next trading day date string."""
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() == 4:       # Friday → Monday
        return (now + timedelta(days=3)).strftime("%Y-%m-%d")
    elif now.weekday() == 5:     # Saturday → Monday
        return (now + timedelta(days=2)).strftime("%Y-%m-%d")
    else:
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")


# ─────────────────────────────────────────
# 8:00 AM PRE-MARKET SUMMARY
# ─────────────────────────────────────────
def build_premarket_sms(analyses: list) -> str:
    """
    8:00 AM summary. Options not open yet — focus on:
    - Macro warnings with clear entry time guidance
    - Carryover positions (stock price only, EOD OI from yesterday)
    - Week ahead
    """
    now_et    = datetime.now(ZoneInfo("America/New_York"))
    today_str = now_et.strftime("%Y-%m-%d")
    yest_str  = get_yesterday_str()
    day_label = now_et.strftime("%a %b %d")

    lines = [
        f"⚡ FlowCheck Pre-Market — {day_label}",
        f"Options open 9:30 AM · First clean window 10:00 AM",
        "─" * 34,
        ""
    ]

    # ── Macro warnings ──────────────────────────────
    macro = get_today_warnings()
    lines.append("📅 TODAY'S MACRO")
    lines.append(macro["advisory"])
    lines.append("")

    if macro["events_summary"]:
        for w in macro["events_summary"][:3]:
            lines.append(f"  {w}")
        lines.append("")

    # Week ahead
    week_lines = get_week_ahead_summary(macro)
    if week_lines:
        lines.append("THIS WEEK:")
        lines.extend(week_lines[:4])
        lines.append("")

    # ── Carryover positions ──────────────────────────
    # Yesterday's TRADE/WATCH alerts with days remaining
    carryover = [
        a for a in analyses
        if a.get("date") == yest_str
        and a["result"].get("verdict") in ["TRADE", "WATCH"]
        and (a["data"].get("days_to_expiry", 0) or 0) > 0
    ]

    lines.append(f"🔄 CARRYOVER FROM YESTERDAY ({len(carryover)})")

    if carryover:
        for a in carryover:
            trade  = a["trade"]
            data   = a["data"]
            result = a["result"]

            ticker  = trade.get("ticker", "?")
            strike  = trade.get("strike", "?")
            otype   = trade.get("option_type", "call")[0].upper()
            expiry  = trade.get("expiry", "?")
            score   = result.get("final_score", "?")
            verdict = result.get("verdict", "?")
            v_emoji = {"TRADE": "✅", "WATCH": "👀"}.get(verdict, "⚡")

            # Stock price change overnight
            prev_price    = data.get("stock_price")
            current_price = get_pre_market_price(ticker)
            price_line    = f"${current_price or '?'}"
            if current_price and prev_price:
                chg   = round(((current_price - prev_price) / prev_price) * 100, 1)
                arrow = "↑" if chg > 0 else "↓" if chg < 0 else "→"
                color = "+" if chg > 0 else ""
                price_line = f"${current_price} {arrow}{color}{abs(chg):.1f}% overnight"

            # EOD OI from yesterday (already verified at 4:15 PM)
            eod_oi = data.get("eod_oi_verified")
            if eod_oi:
                oi_line = f"OI: {eod_oi.get('current_oi', '?'):,} {eod_oi.get('emoji','')}"
                oi_note = eod_oi.get("note", "")
            else:
                oi_line = f"OI: {data.get('open_interest', '?'):,} (unverified)"
                oi_note = "Run EOD job to verify"

            # Earnings timing
            earn_label = data.get("expiry_timing_label", "")
            earn_emoji = data.get("expiry_timing_emoji", "")

            lines.append(f"{v_emoji} {ticker} {strike}{otype} {expiry} [{score}/7]")
            lines.append(f"  Stock: {price_line}")
            lines.append(f"  {oi_line} — {oi_note}")
            if earn_label:
                lines.append(f"  {earn_emoji} {earn_label}")

            # Entry guidance based on macro
            if macro["avoid_buying"]:
                lines.append(f"  ⛔ DO NOT ENTER TODAY — FOMC")
            elif macro.get("avoid_until") and macro["max_impact"] in ["HIGH", "EXTREME"]:
                lines.append(f"  ⏰ First entry window: {macro['avoid_until']} ET")
            else:
                lines.append(f"  ⏰ First entry window: 10:00 AM ET")
            lines.append("")
    else:
        lines.append("  No open positions from yesterday")
        lines.append("")

    # ── Pre-market VIX snapshot ──────────────────────
    try:
        vix_hist = yf.Ticker("^VIX").history(period="1d", interval="1m")
        if not vix_hist.empty:
            vix = round(vix_hist["Close"].iloc[-1], 1)
            vix_label = (
                "Calm ✅" if vix < 18 else
                "Elevated ⚠️" if vix < 25 else
                "High 🔴" if vix < 35 else
                "Extreme 🚨"
            )
            lines.append(f"📊 VIX: {vix} — {vix_label}")
    except Exception as e:
        print(f"[PREMARKET] VIX error: {e}")

    base_url = os.getenv("BASE_URL", "https://your-app.railway.app")
    lines.append(f"\nHistory: {base_url}/history")

    sms = "\n".join(str(l) for l in lines)
    if len(sms) > 1550:
        sms = sms[:1500] + f"...\n{os.getenv('BASE_URL','')}/history"
    print(f"[SMS] Pre-market length: {len(sms)} chars")
    return sms


# ─────────────────────────────────────────
# 4:15 PM EOD OI VERIFICATION JOB
# ─────────────────────────────────────────
def verify_eod_positions(analyses: list):
    """
    4:15 PM job. OI updates post-close.
    Re-fetches OI for all today's TRADE/WATCH alerts
    and stores verified OI back into the analysis data.
    """
    if not is_trading_day():
        return

    now_et    = datetime.now(ZoneInfo("America/New_York"))
    today_str = now_et.strftime("%Y-%m-%d")

    to_verify = [
        a for a in analyses
        if a.get("date") == today_str
        and a["result"].get("verdict") in ["TRADE", "WATCH"]
        and (a["data"].get("days_to_expiry", 0) or 0) > 0
    ]

    print(f"[EOD] Verifying OI for {len(to_verify)} positions...")

    for a in to_verify:
        oi_result = verify_eod_oi(a["trade"], a["data"].get("open_interest", 0))
        a["data"]["eod_oi_verified"] = oi_result
        print(f"[EOD] {a['trade'].get('ticker')} OI: {oi_result.get('note')}")

    print("[EOD] OI verification complete")


# ─────────────────────────────────────────
# 4:30 PM EOD SUMMARY
# ─────────────────────────────────────────
def build_eod_sms(analyses: list) -> str:
    """
    4:30 PM after-close summary.
    OI has been verified by the 4:15 PM job.
    """
    now_et      = datetime.now(ZoneInfo("America/New_York"))
    today_str   = now_et.strftime("%Y-%m-%d")
    tomorrow_str = get_tomorrow_str()
    day_label   = now_et.strftime("%a %b %d")

    today_all = [a for a in analyses if a.get("date") == today_str]
    trades    = [a for a in today_all if a["result"].get("verdict") == "TRADE"]
    watches   = [a for a in today_all if a["result"].get("verdict") == "WATCH"]
    skips     = [a for a in today_all if a["result"].get("verdict") == "SKIP"]

    lines = [
        f"⚡ FlowCheck EOD — {day_label}",
        f"─" * 32,
        ""
    ]

    # ── Day recap ────────────────────────────────────
    lines.append(
        f"📊 TODAY: {len(today_all)} alerts · "
        f"✅{len(trades)} TRADE · 👀{len(watches)} WATCH · ❌{len(skips)} SKIP"
    )
    lines.append("")

    # ── Verified OI for open positions ───────────────
    open_positions = [
        a for a in today_all
        if a["result"].get("verdict") in ["TRADE", "WATCH"]
        and (a["data"].get("days_to_expiry", 0) or 0) > 0
    ]

    if open_positions:
        lines.append(f"🔍 VERIFIED OI (post-close)")
        for a in open_positions:
            trade   = a["trade"]
            data    = a["data"]
            result  = a["result"]
            ticker  = trade.get("ticker", "?")
            strike  = trade.get("strike", "?")
            otype   = trade.get("option_type", "call")[0].upper()
            expiry  = trade.get("expiry", "?")
            score   = result.get("final_score", "?")
            v_emoji = {"TRADE": "✅", "WATCH": "👀"}.get(result.get("verdict",""), "⚡")

            eod_oi = data.get("eod_oi_verified")
            if eod_oi:
                oi_str  = f"OI {eod_oi.get('current_oi',0):,} {eod_oi.get('emoji','')}"
                oi_note = eod_oi.get("note", "")
            else:
                oi_str  = f"OI {data.get('open_interest','?')} (unverified)"
                oi_note = ""

            lines.append(f"{v_emoji} {ticker} {strike}{otype} {expiry} [{score}/7]")
            lines.append(f"  {oi_str}")
            if oi_note:
                lines.append(f"  {oi_note}")

        lines.append("")

    # ── Top trades ────────────────────────────────────
    if trades:
        top = sorted(trades, key=lambda a: a["result"].get("final_score", 0), reverse=True)
        lines.append("🎯 TOP TRADES TODAY:")
        for a in top[:3]:
            t = a["trade"]
            r = a["result"]
            lines.append(
                f"  • {t.get('ticker')} {t.get('strike')}{t.get('option_type','c')[0].upper()} "
                f"{t.get('expiry','?')} — {r.get('final_score','?')}/7"
            )
            liner = r.get("one_liner", "")
            if liner:
                lines.append(f"    {liner}")
        lines.append("")

    # ── Tomorrow's macro ──────────────────────────────
    tomorrow_events = get_economic_events(tomorrow_str)
    if tomorrow_events:
        lines.append("📅 TOMORROW:")
        for event in tomorrow_events[:3]:
            defn = classify_event(event.get("event", ""))
            lines.append(
                f"  {defn['emoji']} {event.get('time_et','')}: {event.get('event','')} "
                f"[{defn['impact']}] — avoid before {defn.get('avoid_until','10 AM ET')}"
            )
    else:
        lines.append("📅 TOMORROW: No major macro events")

    lines.append("")
    base_url = os.getenv("BASE_URL", "https://your-app.railway.app")
    lines.append(f"Full history: {base_url}/history")

    sms = "\n".join(str(l) for l in lines)
    if len(sms) > 1550:
        sms = sms[:1500] + f"...\n{os.getenv('BASE_URL','')}/history"
    print(f"[SMS] Pre-market length: {len(sms)} chars")
    return sms


# ─────────────────────────────────────────
# ENTRY POINTS (called by scheduler)
# ─────────────────────────────────────────
def send_premarket_summary(analyses: list):
    """8:00 AM ET — pre-market SMS."""
    if not is_trading_day():
        print("[PREMARKET] Weekend — skipping")
        return
    print("[PREMARKET] Building 8:00 AM summary...")
    try:
        msg = build_premarket_sms(analyses)
        send_sms(msg)
        print("[PREMARKET] Sent")
    except Exception as e:
        print(f"[PREMARKET] Error: {e}")
        send_sms(f"⚡ FlowCheck Pre-Market\n⚠️ Summary error: {str(e)[:80]}")


def send_eod_summary(analyses: list):
    """4:30 PM ET — EOD summary SMS."""
    if not is_trading_day():
        print("[EOD] Weekend — skipping")
        return
    print("[EOD] Building 4:30 PM summary...")
    try:
        msg = build_eod_sms(analyses)
        send_sms(msg)
        print("[EOD] Sent")
    except Exception as e:
        print(f"[EOD] Error: {e}")
        send_sms(f"⚡ FlowCheck EOD\n⚠️ Summary error: {str(e)[:80]}")
