"""
morning_summary.py — Daily 9:45 AM top setups briefing.

Re-ranks all active watchlist positions by:
- Trend alignment (stock vs 20-day SMA)
- IV rank
- Conviction score
- GEX entry status

Sends top 3 actionable setups to priority channel.
"""
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _get_price_history(ticker: str) -> list:
    """Fetch 30-day closing prices for SMA calculation."""
    try:
        key = os.environ.get("TIINGO_API_KEY","")
        if not key:
            return []
        import requests
        from datetime import timedelta
        end   = datetime.now(ET)
        start = end - timedelta(days=45)
        r = requests.get(
            f"https://api.tiingo.com/tiingo/daily/{ticker}/prices",
            params={"startDate": start.strftime("%Y-%m-%d"),
                    "endDate":   end.strftime("%Y-%m-%d"),
                    "token":     key},
            timeout=8
        )
        if r.status_code == 200:
            return [float(d.get("adjClose") or d.get("close",0)) for d in r.json()]
    except: pass
    return []


def _score_setup(ticker: str, entry: dict) -> dict | None:
    """Score a watchlist position for morning ranking."""
    try:
        is_call   = "put" not in (entry.get("option_type","call") or "call").lower()
        strike    = entry.get("strike","?")
        expiry    = entry.get("expiry","?")
        flow_score = float(entry.get("flow_score",0) or 0)
        dte       = int(entry.get("dte",30) or 30)
        verdict   = entry.get("verdict","WATCH")
        added_at  = float(entry.get("added_at",0) or 0)
        days_ago  = int((time.time() - added_at) / 86400) if added_at else 0

        # Skip expired or deeply ITM
        if dte < 1:
            return None

        # Get current price
        from fetcher import fetch_price as _fp
        current_px = float(_fp(ticker) or 0)
        if not current_px:
            return None

        # Staleness: skip if >8% ITM
        strike_f = float(str(strike).replace("C","").replace("P","") or 0)
        if strike_f > 0:
            itm_pct = (current_px-strike_f)/strike_f*100 if is_call else (strike_f-current_px)/strike_f*100
            if itm_pct > 8:
                return None

        # Trend alignment
        prices = _get_price_history(ticker)
        from signal_quality import check_trend_alignment, check_iv_rank
        trend = check_trend_alignment(ticker, is_call, prices)

        # GEX entry
        gex_tag = entry.get("gex_entry_score","")
        gex_ok  = gex_tag == "GOOD"
        gex_str = "✅ ENTER NOW" if gex_ok else "⏳ WAIT"

        # IV rank from stored data
        iv_info = check_iv_rank(ticker, float(entry.get("iv",0) or 0))

        # Conviction from storage
        conv_total = 0
        try:
            from storage import db_get as _dg
            import json
            _conv_raw = _dg(f"conviction_{ticker.lower()}") or "{}"
            conv_total = json.loads(_conv_raw).get("total", 0)
        except: pass

        # Composite rank score (higher = better)
        rank_score = flow_score
        if trend.get("aligned"):    rank_score += 1.0
        if gex_ok:                  rank_score += 0.5
        if conv_total >= 3:         rank_score += 0.5
        if iv_info.get("flag") == "HIGH": rank_score -= 1.0
        if not trend.get("aligned"): rank_score -= 1.5

        otype = "C" if is_call else "P"
        return {
            "ticker":      ticker,
            "label":       f"${ticker} {strike}{otype} {expiry}",
            "flow_score":  flow_score,
            "verdict":     verdict,
            "rank_score":  round(rank_score, 2),
            "trend":       trend,
            "gex_str":     gex_str,
            "gex_ok":      gex_ok,
            "iv_info":     iv_info,
            "conv_total":  conv_total,
            "dte":         dte,
            "days_ago":    days_ago,
            "current_px":  current_px,
        }
    except Exception as e:
        print(f"[MORNING] Score error {ticker}: {e}")
        return None


def send_morning_summary(watchlist: dict, send_fn=None):
    """
    Score all watchlist positions, rank, send top setups.
    Called at 9:45 AM ET on weekdays.
    """
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:
        return

    bot   = os.environ.get("TELEGRAM_BOT_TOKEN","")
    chat  = os.environ.get("TELEGRAM_TRADE_CHAT_ID","") or os.environ.get("TELEGRAM_CHAT_ID","")
    if not bot or not chat or not send_fn:
        return

    print(f"[MORNING] Scoring {len(watchlist)} watchlist positions...")

    setups     = []
    suppressed = []

    for ticker, entry in watchlist.items():
        time.sleep(0.3)  # rate limit
        scored = _score_setup(ticker, entry)
        if not scored:
            continue
        if float(scored.get("rank_score",0)) >= 6.0 and scored.get("trend",{}).get("aligned",True):
            setups.append(scored)
        else:
            reason = []
            if not scored["trend"]["aligned"]:
                reason.append("trend mismatch")
            if scored["iv_info"].get("flag") == "HIGH":
                reason.append("high IV")
            if scored["rank_score"] < 5.5:
                reason.append("low score")
            suppressed.append(f"{ticker} ({', '.join(reason)})")

    # Sort by rank score
    setups.sort(key=lambda x: -x["rank_score"])
    top = setups[:3]

    if not top:
        msg = (f"🌅 TOP SETUPS — {now_et.strftime('%A %b %d')}\n\n"
               f"No high-conviction setups this morning.\n"
               f"{'Suppressed: '+', '.join(suppressed[:5]) if suppressed else ''}")
        send_fn(msg, bot, chat)
        return

    medals = ["🥇","🥈","🥉"]
    lines  = [f"🌅 TOP SETUPS — {now_et.strftime('%A %b %d')}\n"]

    for i, s in enumerate(top):
        medal     = medals[i] if i < 3 else "▪️"
        iv_note   = s["iv_info"].get("note","")
        trend_note = s["trend"].get("note","")
        conv_str  = f"{s['conv_total']}/6 conviction" if s["conv_total"] else ""
        day_str   = f"Day {s['days_ago']+1} since flow" if s["days_ago"] > 0 else "Today's flow"

        lines.append(
            f"{medal} {s['label']} [{s['flow_score']:.1f}/7 {s['verdict']}]\n"
            f"   {trend_note}\n"
            f"   {s['gex_str']} | {iv_note}\n"
            f"   {conv_str} | {day_str} | {s['dte']}d DTE\n"
        )

    if suppressed:
        lines.append(f"\n⚠️ {len(suppressed)} position(s) suppressed — "
                     f"{', '.join(suppressed[:4])}"
                     f"{'...' if len(suppressed) > 4 else ''}")

    send_fn("\n".join(lines), bot, chat)
    print(f"[MORNING] Sent top {len(top)} setups, suppressed {len(suppressed)}")
