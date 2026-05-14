from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
import uvicorn, json, os
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
from apscheduler.schedulers.background import BackgroundScheduler
from fetcher import fetch_trade_data
from scorer import score_trade, format_premium
from sms import send_sms
from parser import parse_tweet
from vision_parser import extract_trade_from_tweet
from economic_calendar import get_today_warnings, get_week_ahead_summary, fetch_and_cache_today
from premarket_summary import (
    send_premarket_summary, send_eod_summary, verify_eod_positions
)

app = FastAPI()

# ─────────────────────────────────────────
# IN-MEMORY STORES
# ─────────────────────────────────────────
analyses      = []
seen_tickers  = defaultdict(list)   # duplicate detection
ticker_alerts = defaultdict(list)   # multi-strike pattern detection


# ─────────────────────────────────────────
# SCHEDULER
# Schedule (all times America/New_York):
#   7:30 AM Mon-Fri  — Pre-fetch economic calendar
#   8:00 AM Mon-Fri  — Pre-market SMS
#   4:15 PM Mon-Fri  — Verify EOD OI (OI updates post-close)
#   4:30 PM Mon-Fri  — EOD summary SMS
# ─────────────────────────────────────────
scheduler = BackgroundScheduler(timezone="America/New_York")

@app.on_event("startup")
async def startup():
    # 7:30 AM — fetch and cache today's economic calendar
    scheduler.add_job(
        fetch_and_cache_today,
        "cron", day_of_week="mon-fri", hour=7, minute=30,
        id="fetch_calendar"
    )
    # 8:00 AM — pre-market SMS (reads cached calendar)
    scheduler.add_job(
        lambda: send_premarket_summary(analyses),
        "cron", day_of_week="mon-fri", hour=8, minute=0,
        id="premarket_summary"
    )
    # 4:15 PM — verify EOD OI now that market is closed
    scheduler.add_job(
        lambda: verify_eod_positions(analyses),
        "cron", day_of_week="mon-fri", hour=16, minute=15,
        id="verify_eod_oi"
    )
    # 4:30 PM — EOD summary SMS (15 min after OI verification)
    scheduler.add_job(
        lambda: send_eod_summary(analyses),
        "cron", day_of_week="mon-fri", hour=16, minute=30,
        id="eod_summary"
    )
    scheduler.start()
    print("[SCHEDULER] Started:")
    print("  7:30 AM — fetch economic calendar")
    print("  8:00 AM — pre-market SMS")
    print("  4:15 PM — verify EOD OI")
    print("  4:30 PM — EOD summary SMS")

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
    print("[SCHEDULER] Stopped")


# ─────────────────────────────────────────
# PATTERN DETECTION
# ─────────────────────────────────────────
def check_multi_strike_pattern(ticker: str, analysis_id: int) -> dict:
    """Detect same ticker alerted multiple times today."""
    now_et = datetime.now(ZoneInfo("America/New_York"))
    today  = now_et.date().isoformat()
    ticker_alerts[ticker].append({
        "id": analysis_id, "date": today,
        "time": now_et.strftime("%H:%M")
    })
    today_alerts = [a for a in ticker_alerts[ticker] if a["date"] == today]
    if len(today_alerts) >= 2:
        return {
            "detected": True,
            "count": len(today_alerts),
            "message": (
                f"⚠️ PATTERN: {ticker} alerted {len(today_alerts)}x today — "
                f"coordinated multi-strike positioning signal"
            )
        }
    return {"detected": False}


def is_duplicate(ticker: str, expiry: str, strike: str) -> bool:
    """True if same ticker+expiry+strike seen in last 5 min.
    5 min window prevents IFTTT double-firing but allows retesting.
    """
    import time
    key  = f"{ticker}_{expiry}_{strike}"
    now  = time.time()
    prev = [t for t in seen_tickers.get(key, []) if now - t < 300]  # 5 min
    seen_tickers[key] = prev + [now]
    return len(prev) > 0


# ─────────────────────────────────────────
# WEBHOOK
# ─────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Returns immediately to avoid timeout.
    All processing happens in background task.
    """
    try:
        body       = await request.json()
        tweet_text = body.get("tweet", "") or body.get("text", "") or str(body)
        image_url  = body.get("image_url") or body.get("imageUrl") or None
        tweet_url  = body.get("tweet_url") or body.get("linkToTweet") or None
        print(f"[WEBHOOK] Received: {tweet_text[:120]}")
        if image_url:
            print(f"[WEBHOOK] Image URL: {image_url[:80]}")
        if tweet_url:
            print(f"[WEBHOOK] Tweet URL: {tweet_url[:80]}")

        # Try text first, fall back to vision using image or tweet URL
        trade = extract_trade_from_tweet(tweet_text, image_url, tweet_url)
        if not trade:
            print("[WEBHOOK] Could not extract trade from text or image — skipping")
            return {"status": "skipped", "reason": "no trade info found in text or image"}

        # Duplicate check — do this synchronously before returning
        if is_duplicate(trade.get("ticker",""),
                        trade.get("expiry",""),
                        trade.get("strike","")):
            print(f"[WEBHOOK] Duplicate — skipping {trade.get('ticker')}")
            return {"status": "skipped", "reason": "duplicate"}

        # Return immediately — process everything in background
        background_tasks.add_task(process_trade, tweet_text, trade, image_url, tweet_url)
        print(f"[WEBHOOK] Queued background processing for {trade.get('ticker')}")
        return {"status": "queued", "ticker": trade.get("ticker"), "message": "Processing in background — SMS incoming"}

    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
        return {"status": "error", "message": str(e)}


async def process_trade(tweet_text: str, trade: dict, image_url: str = None, tweet_url: str = None):
    """Background task — does all the heavy lifting after webhook returns."""
    import asyncio
    loop = asyncio.get_event_loop()

    try:
        print(f"[PROCESS] Starting analysis for {trade.get('ticker')}...")

        # Run blocking IO in thread pool so we don't block event loop
        macro = await loop.run_in_executor(None, get_today_warnings)

        data = await loop.run_in_executor(
            None, fetch_trade_data, trade, trade.get("option_price")
        )
        data["macro"] = macro

        result = await loop.run_in_executor(None, score_trade, trade, data)

        analysis_id = len(analyses)
        pattern     = check_multi_strike_pattern(trade.get("ticker",""), analysis_id)

        now_et = datetime.now(ZoneInfo("America/New_York"))
        entry = {
            "id":       analysis_id,
            "tweet":    tweet_text,
            "tweet_url": tweet_url,
            "trade":    trade,
            "data":     data,
            "result":   result,
            "pattern":  pattern,
            "macro":    macro,
            "date":     now_et.date().isoformat(),
            "time":     now_et.strftime("%H:%M"),
        }
        analyses.append(entry)

        sms_text = build_alert_sms(trade, result, data, macro, pattern, analysis_id, tweet_url)
        await loop.run_in_executor(None, send_sms, sms_text)

        print(f"[PROCESS] Done: {trade.get('ticker')} {result.get('final_score')}/7 {result.get('verdict')} — SMS sent")

    except Exception as e:
        print(f"[PROCESS ERROR] {trade.get('ticker')}: {e}")
        # Try to send error SMS so you know something failed
        try:
            send_sms(f"⚠️ FlowCheck error for {trade.get('ticker','?')}\n{str(e)[:100]}")
        except:
            pass


# ─────────────────────────────────────────
# ALERT SMS BUILDER
# ─────────────────────────────────────────
def build_alert_sms(trade, result, data, macro, pattern, analysis_id, tweet_url=None):
    """
    Concise SMS — keeps well under Twilio 1600 char limit.
    Full details available at the link.
    """
    verdict_emoji = {"TRADE": "✅", "WATCH": "👀", "SKIP": "❌"}.get(result.get("verdict",""), "⚡")
    ticker      = trade.get("ticker", "?")
    strike      = trade.get("strike", "?")
    otype       = trade.get("option_type", "call")[0].upper()
    expiry      = trade.get("expiry", "?")
    final_score = result.get("final_score", "?")
    raw_score   = result.get("raw_score", "?")
    adj         = result.get("market_adjustment", 0)
    verdict     = result.get("verdict", "?")
    one_liner   = result.get("one_liner", "")[:80]  # cap at 80 chars
    top_imp     = (result.get("improvements") or [""])[0][:80]  # cap at 80 chars
    mkt         = data.get("market", {})
    sector      = data.get("sector", {})
    chase       = data.get("chasing_flag")
    chase_move  = data.get("price_move_since_flow")
    adj_str     = f"({adj:+.0f}mkt)" if adj and adj != 0 else ""
    iv_emoji    = data.get("implied_vs_historical_emoji", "")
    iv_short    = ""
    if data.get("implied_move_pct") and data.get("avg_earnings_move"):
        iv_short = f"{iv_emoji} IV {data['implied_move_pct']}% vs avg {data['avg_earnings_move']}%"

    base_url = os.getenv("BASE_URL", "https://your-app.railway.app")

    lines = [
        f"{verdict_emoji} {ticker} {strike}{otype} {expiry}",
        f"{raw_score}/7 {adj_str}→ {final_score}/7 {verdict}",
        f"VIX {mkt.get('vix','?')} {mkt.get('vix_label','')} · SPY {mkt.get('spy_trend','?')}",
    ]

    # Earnings timing — one line
    earn = data.get("expiry_timing_label","")
    if earn:
        lines.append(f"{data.get('expiry_timing_emoji','')} {earn}")

    # IV cheapness
    if iv_short:
        lines.append(iv_short)

    # Chasing risk — only if notable
    if chase == "HIGH" and chase_move:
        lines.append(f"🚨 Already +{chase_move}% from flow — chasing")

    # Macro — only if HIGH or EXTREME
    if macro.get("avoid_buying"):
        lines.append(f"🚨 FOMC — avoid all day")
    elif macro.get("max_impact") in ["HIGH", "EXTREME"]:
        avoid = macro.get("avoid_until", "10 AM")
        lines.append(f"🔴 Macro: wait until {avoid}")

    # Pattern
    if pattern.get("detected"):
        lines.append(f"⚠️ {ticker} alerted {pattern.get('count','?')}x today")

    # One liner + top improvement
    if one_liner:
        lines.append(f"→ {one_liner}")
    if top_imp:
        lines.append(top_imp)

    # Build footer links — always included regardless of length
    footer_lines = []
    if tweet_url:
        footer_lines.append(f"🐦 {tweet_url}")
    footer_lines.append(f"📊 {base_url}/analysis/{analysis_id}")
    footer = "\n".join(footer_lines)

    # Build body — truncate this if needed, never the footer
    body = "\n".join(str(l) for l in lines if l)
    max_body = 1500 - len(footer) - 2  # leave room for footer + newlines

    if len(body) > max_body:
        body = body[:max_body - 3] + "..."

    sms = body + "\n" + footer
    print(f"[SMS] Message length: {len(sms)} chars")
    return sms


# ─────────────────────────────────────────
# DETAIL PAGE
# ─────────────────────────────────────────
@app.get("/analysis/{analysis_id}", response_class=HTMLResponse)
async def analysis_detail(analysis_id: int):
    if analysis_id >= len(analyses):
        return "<h1>Analysis not found</h1>"

    a       = analyses[analysis_id]
    trade   = a["trade"]
    data    = a["data"]
    result  = a["result"]
    pattern = a.get("pattern", {})
    macro   = a.get("macro", {})
    market  = data.get("market", {})
    sector  = data.get("sector", {})
    tod     = data.get("time_of_day", {})

    vc = {
        "TRADE": "#00ff88", "WATCH": "#ffc93d", "SKIP": "#ff4d6d"
    }.get(result.get("verdict",""), "#fff")

    def row(label, val, color="rgba(255,255,255,0.85)"):
        return (
            f'<div class="data-item">'
            f'<div class="data-label">{label}</div>'
            f'<div class="data-value" style="color:{color}">{val}</div>'
            f'</div>'
        )

    # Checklist HTML
    checklist_html = ""
    for item in result.get("checklist", []):
        icon  = "✓" if item["pass"] else "✗"
        color = "#00ff88" if item["pass"] else "#ff4d6d"
        note  = (
            f' <span style="color:rgba(255,255,255,0.4);font-size:11px">'
            f'— {item["note"]}</span>'
            if item.get("note") else ""
        )
        checklist_html += (
            f'<div style="color:{color};padding:7px 0;font-size:13px;'
            f'border-bottom:1px solid rgba(255,255,255,0.05)">'
            f'{icon} {item["label"]}{note}</div>'
        )

    # Improvements HTML
    improvements_html = "".join(
        f'<div style="background:rgba(255,201,61,0.06);border:1px solid rgba(255,201,61,0.15);'
        f'border-radius:8px;padding:10px 14px;margin-bottom:8px;font-size:13px;'
        f'line-height:1.5;color:rgba(255,255,255,0.8)">{imp}</div>'
        for imp in result.get("improvements", [])
    )

    # Pattern alert HTML
    pattern_html = ""
    if pattern.get("detected"):
        pattern_html = (
            f'<div style="background:rgba(255,201,61,0.1);border:1px solid rgba(255,201,61,0.3);'
            f'border-radius:10px;padding:14px;margin-bottom:20px;font-size:13px;color:#ffc93d">'
            f'{pattern["message"]}</div>'
        )

    # Macro HTML
    mi = macro.get("max_impact", "NONE")
    if macro.get("avoid_buying"):
        macro_color = "#ff4d6d"
        macro_bg    = "rgba(255,77,109,0.1)"
        macro_border = "rgba(255,77,109,0.3)"
    elif mi in ["HIGH", "EXTREME"]:
        macro_color  = "#ff4d6d"
        macro_bg     = "rgba(255,77,109,0.07)"
        macro_border = "rgba(255,77,109,0.2)"
    elif mi == "MEDIUM":
        macro_color  = "#ffc93d"
        macro_bg     = "rgba(255,201,61,0.07)"
        macro_border = "rgba(255,201,61,0.2)"
    else:
        macro_color  = "#00ff88"
        macro_bg     = "rgba(0,255,136,0.05)"
        macro_border = "rgba(0,255,136,0.15)"

    macro_html = (
        f'<div style="background:{macro_bg};border:1px solid {macro_border};'
        f'border-radius:8px;padding:12px;margin-bottom:8px;'
        f'font-size:13px;color:{macro_color}">'
        f'{macro.get("advisory_emoji","")} {macro.get("advisory","")}</div>'
    )
    for w in (macro.get("events_summary") or [])[:3]:
        macro_html += (
            f'<div style="font-size:12px;color:rgba(255,255,255,0.5);'
            f'padding:4px 0;font-family:monospace">{w}</div>'
        )

    # Score breakdown
    score_breakdown = (
        f"Trade: {result.get('raw_score','?')}/7 · "
        f"Market adj: {result.get('market_adjustment',0):+.1f} · "
        f"Final: {result.get('final_score','?')}/7"
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FlowCheck — {trade.get('ticker','?')}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#080810;color:#fff;font-family:-apple-system,sans-serif;
     padding:20px;max-width:640px;margin:0 auto}}
.hdr{{display:flex;align-items:center;gap:10px;padding:20px 0;
      border-bottom:1px solid rgba(255,255,255,0.08);margin-bottom:20px}}
.logo{{width:32px;height:32px;border-radius:8px;
       background:linear-gradient(135deg,#ffc93d,#ff6b35);
       display:flex;align-items:center;justify-content:center;font-size:16px}}
.sec{{margin-bottom:24px}}
.sec-title{{font-size:10px;font-family:monospace;color:rgba(255,255,255,0.3);
            letter-spacing:.1em;text-transform:uppercase;margin-bottom:10px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.data-item{{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.07);
            border-radius:8px;padding:12px}}
.data-label{{font-size:10px;font-family:monospace;color:rgba(255,255,255,0.3);
             letter-spacing:.08em;text-transform:uppercase;margin-bottom:4px}}
.data-value{{font-size:14px;font-family:monospace;font-weight:600}}
.badge{{display:inline-flex;align-items:center;gap:8px;padding:8px 16px;
        border-radius:100px;border:1px solid {vc}44;background:{vc}11}}
.tweet{{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);
        border-radius:10px;padding:14px;font-size:13px;
        color:rgba(255,255,255,0.5);line-height:1.5}}
.note{{background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.07);
       border-radius:8px;padding:12px;font-size:13px;
       color:rgba(255,255,255,0.6);line-height:1.6;margin-top:8px}}
.foot{{padding:20px 0;border-top:1px solid rgba(255,255,255,0.06);
       font-size:11px;color:rgba(255,255,255,0.2);
       font-family:monospace;text-align:center}}
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">⚡</div>
  <div>
    <div style="font-size:16px;font-weight:700">FlowCheck</div>
    <div style="font-size:11px;color:rgba(255,255,255,0.3);font-family:monospace">
      {a.get('date','')} {a.get('time','')} ET
    </div>
  </div>
</div>

<div class="sec">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
    <div>
      <div style="font-size:26px;font-weight:700;letter-spacing:-.03em">
        {trade.get('ticker','?')}
      </div>
      <div style="font-size:14px;color:rgba(255,255,255,0.4);font-family:monospace">
        {trade.get('strike','?')} {trade.get('option_type','call').upper()} · {trade.get('expiry','?')}
      </div>
    </div>
    <div class="badge">
      <span style="font-family:monospace;font-size:22px;font-weight:700;color:{vc}">
        {result.get('final_score','?')}/7
      </span>
      <span style="width:1px;height:16px;background:rgba(255,255,255,0.15)"></span>
      <span style="font-family:monospace;font-size:12px;font-weight:600;
                   color:{vc};letter-spacing:.08em">
        {result.get('verdict','?')}
      </span>
    </div>
  </div>
  <div style="font-size:11px;color:rgba(255,255,255,0.25);font-family:monospace">
    {score_breakdown}
  </div>
</div>

{pattern_html}

<div class="sec">
  <div class="sec-title">Original Alert</div>
  <div class="tweet">{a['tweet']}</div>
  {f'<a href="{a["tweet_url"]}" target="_blank" style="display:inline-block;margin-top:10px;padding:6px 14px;border-radius:6px;background:rgba(96,165,250,0.1);border:1px solid rgba(96,165,250,0.3);color:#60A5FA;font-size:12px;font-family:monospace;text-decoration:none">🐦 View original tweet →</a>' if a.get("tweet_url") else ""}
</div>

<div class="sec">
  <div class="sec-title">📅 Economic Calendar</div>
  {macro_html}
  <div style="font-size:11px;color:rgba(255,255,255,0.25);font-family:monospace;margin-top:8px">
    Options open 9:30 AM ET · First clean window 10:00 AM ET
  </div>
</div>

<div class="sec">
  <div class="sec-title">Market Conditions (Real-Time)</div>
  <div class="grid">
    {row("VIX", f"{market.get('vix','N/A')} {market.get('vix_label','')} {market.get('vix_emoji','')}", '#00ff88' if (market.get('vix') or 99) < 18 else '#ffc93d' if (market.get('vix') or 99) < 25 else '#ff4d6d')}
    {row("SPY 5-Day", f"{market.get('spy_trend','N/A')} {market.get('spy_emoji','')}")}
    {row(f"Sector ({sector.get('etf','?')})", f"{sector.get('sector_trend','N/A')} {sector.get('sector_emoji','')}")}
    {row("Market Bias", market.get('market_bias','N/A'), '#00ff88' if market.get('market_bias')=='FAVORABLE' else '#ffc93d' if market.get('market_bias')=='CAUTION' else '#ff4d6d')}
  </div>
  <div class="note">{result.get('market_reasoning','')}</div>
  <div style="margin-top:8px;font-size:11px;color:rgba(255,255,255,0.3);font-family:monospace">
    Time of day: {tod.get('label','N/A')} {tod.get('emoji','')} — {tod.get('note','')}
  </div>
</div>

<div class="sec">
  <div class="sec-title">Implied vs Historical Move</div>
  <div class="grid">
    {row("ATM Straddle Implied", f"{data.get('implied_move_pct','N/A')}%")}
    {row("Avg Actual (8 qtrs)", f"{data.get('avg_earnings_move','N/A')}%")}
  </div>
  <div class="note" style="color:{'#0