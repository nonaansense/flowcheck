import time
import json, os, re
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler

from vision_parser import extract_trade_from_tweet
from fetcher import fetch_trade_data
from scorer import score_trade
from sms import send_sms, send_telegram
from economic_calendar import get_today_warnings
from premarket_summary import send_premarket_summary, send_eod_summary, verify_eod_positions
from technical import add_to_watchlist, run_technical_scan
from outcomes import track_outcomes

app = FastAPI()

# ── Persistence ───────────────────────────────────────────────────────
ANALYSES_FILE = "/tmp/flowcheck_analyses.json"

def save_analyses():
    try:
        today     = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        today_data = [a for a in analyses if a.get("date") == today]
        serializable = []
        for a in today_data:
            try:
                serializable.append({
                    "id":       a.get("id"),
                    "tweet":    a.get("tweet",""),
                    "tweet_url":a.get("tweet_url",""),
                    "date":     a.get("date",""),
                    "time":     a.get("time",""),
                    "trade":    {k:a["trade"].get(k) for k in ["ticker","strike","option_type","expiry","expiry_short","expiry_raw","premium"]},
                    "result":   {k:a["result"].get(k) for k in ["verdict","final_score","raw_score","one_liner","improvements","market_adjustment"]},
                    "data":     {k:a["data"].get(k) for k in ["stock_price","open_interest","days_to_expiry","earnings_date",
                                 "fill_type","fill_label","fill_emoji","expiry_timing_label","expiry_timing_emoji",
                                 "is_breakout_bet","breakout_label","breakout_emoji",
                                 "earnings_is_past","days_since_earnings","earnings_context",
                                 "market","sector","time_of_day"]},
                    "pattern":  a.get("pattern",{}),
                    "macro":    a.get("macro",{}),
                })
            except:
                continue
        with open(ANALYSES_FILE,"w") as f:
            json.dump({"date":today,"analyses":serializable},f)
    except Exception as e:
        print(f"[PERSIST] Save error: {e}")

def load_analyses():
    try:
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        with open(ANALYSES_FILE) as f:
            data = json.load(f)
        if data.get("date") == today:
            loaded = data.get("analyses",[])
            print(f"[PERSIST] Loaded {len(loaded)} analyses from disk")
            return loaded
    except FileNotFoundError:
        print("[PERSIST] No saved analyses — starting fresh")
    except Exception as e:
        print(f"[PERSIST] Load error: {e}")
    return []

# ── Stores ────────────────────────────────────────────────────────────
analyses      = load_analyses()
seen_tickers  = defaultdict(list)
ticker_alerts = defaultdict(list)
scheduler     = BackgroundScheduler(timezone="America/New_York")

# ── Scheduler ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    try:
        scheduler.add_job(lambda: send_premarket_summary(analyses),
                          "cron", day_of_week="mon-fri", hour=8, minute=0, id="premarket")
        scheduler.add_job(lambda: verify_eod_positions(analyses),
                          "cron", day_of_week="mon-fri", hour=16, minute=15, id="eod_oi")
        scheduler.add_job(lambda: send_eod_summary(analyses),
                          "cron", day_of_week="mon-fri", hour=16, minute=30, id="eod_summary")
        scheduler.add_job(lambda: run_technical_scan(send_sms),
                          "interval", minutes=5, id="technical_scan")
        scheduler.add_job(lambda: track_outcomes(analyses),
                          "cron", day_of_week="mon-fri", hour=16, minute=0, id="outcome_track")
        scheduler.start()
        print("[SCHEDULER] Started: 8:00AM pre-market | 4:15PM EOD | 4:30PM EOD SMS | every 5min scan | 4:00PM outcomes")
    except Exception as e:
        print(f"[SCHEDULER] Warning: {e}")

# ── SMS builder ───────────────────────────────────────────────────────
def build_sms(trade: dict, data: dict, result: dict,
              tweet_url: str, analysis_id: int, pattern: dict) -> str:

    ticker    = trade.get("ticker","?")
    strike    = trade.get("strike","?")
    otype     = trade.get("option_type","call")[0].upper()
    expiry    = trade.get("expiry","?")
    dte       = data.get("days_to_expiry")
    dte_str   = f" [{dte}d]" if dte is not None else ""

    raw_score   = result.get("raw_score",0)
    final_score = result.get("final_score",0)
    verdict     = result.get("verdict","SKIP")
    mkt_adj     = result.get("market_adjustment",0)
    adj_str     = f" ({mkt_adj:+.1f}mkt)" if mkt_adj else ""

    verdict_emoji = {"TRADE":"✅","WATCH":"👀","SKIP":"❌"}.get(verdict,"❓")

    mkt = data.get("market",{})
    vix_str = f"{mkt.get('vix','?')} {mkt.get('vix_label','')}" if mkt.get("vix") else "N/A"
    spy_str = mkt.get("spy_trend","N/A") or "N/A"

    one_liner = result.get("one_liner","")
    imps      = result.get("improvements") or []
    top_imp   = imps[0] if imps else ""

    def cap(text, limit=120):
        if not text or len(text)<=limit: return text or ""
        t  = text[:limit]
        sp = t.rfind(" ")
        return (t[:sp]+"…") if sp>0 else (t+"…")

    one_liner = cap(one_liner)
    top_imp   = cap(top_imp)

    base_url = os.environ.get("BASE_URL","https://flowcheck-production.up.railway.app")

    # Stock price context for title
    stock_px = data.get("stock_price")
    if stock_px and float(stock_px) < 40:
        px_tag = f" 🔴${stock_px:.2f}"  # Below $40 — immediate entry
    elif stock_px:
        px_tag = f" 🟢${stock_px:.2f}"  # Above $40 — monitor for pullback
    else:
        px_tag = ""

    lines = [
        f"{verdict_emoji} {ticker} {strike}{otype} {expiry}{dte_str}{px_tag}",
        f"{raw_score}/7{adj_str}→ {final_score}/7 {verdict}",
        f"VIX {vix_str} · SPY {spy_str}",
    ]

    # Fill aggression
    fill_type  = data.get("fill_type")
    fill_emoji = data.get("fill_emoji","")
    fill_label = data.get("fill_label","")
    if fill_type and fill_type not in ("UNKNOWN","") and fill_label:
        lines.append(f"{fill_emoji} {fill_label}")

    # Premium size signal
    if data.get("premium_label") and data.get("premium_emoji"):
        lines.append(f"{data['premium_emoji']} {data['premium_label']}")

    # Vol/OI ratio signal
    if data.get("vol_oi_label") and data.get("vol_oi_ratio",0) >= 3:
        lines.append(f"{data.get('vol_oi_emoji','')} {data['vol_oi_label']}")

    # Breakout warning
    if data.get("is_breakout_bet") and data.get("breakout_label"):
        lines.append(f"⚠️ BREAKOUT BET: {cap(data['breakout_label'], 80)}")

    # Earnings timing
    earn = data.get("expiry_timing_label","")
    if earn:
        lines.append(f"{data.get('expiry_timing_emoji','')} {earn}")

    # Pattern alert
    if pattern.get("alert"):
        lines.append(f"⚠️ {ticker} alerted {pattern['count']}x today")

    # Analysis lines
    if one_liner:
        lines.append(f"→ {one_liner}")
    if top_imp:
        lines.append(f"→ {top_imp}")

    # Links
    if tweet_url:
        lines.append(f"🐦 {tweet_url}")
    lines.append(f"📊 {base_url.rstrip('/')}/analysis/{analysis_id}")

    body   = "\n".join([str(l) for l in lines if l])
    footer = ""
    max_body = 3800 - len(footer)

    if len(body) > max_body:
        truncated = []
        running   = 0
        for line in lines:
            if running + len(line) + 1 > max_body:
                break
            truncated.append(line)
            running += len(line) + 1
        body = "\n".join(truncated)

    return body + footer

# ── Process alert ─────────────────────────────────────────────────────
async def process_alert(tweet: str, tweet_url: str):
    try:
        import asyncio, concurrent.futures
        loop = asyncio.get_event_loop()

        # Run blocking IO in thread pool with timeout
        def _process():
            return extract_trade_from_tweet(tweet, tweet_url)

        try:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                trade = await asyncio.wait_for(
                    loop.run_in_executor(pool, _process),
                    timeout=30.0  # 30s max for vision/parse
                )
        except asyncio.TimeoutError:
            print(f"[PROCESS] Vision parse timeout for {tweet[:50]}")
            trade = None
        if not trade or not trade.get("ticker"):
            print("[WEBHOOK] Could not extract trade from text or image — skipping")
            return

        ticker = trade.get("ticker")
        print(f"[PROCESS] Starting analysis for {ticker}...")

        # Duplicate detection (2 min window)
        now = datetime.now().timestamp()
        key = f"{ticker}_{trade.get('strike','')}_{trade.get('expiry_raw','')}"
        prev = [t for t in seen_tickers.get(key,[]) if now-t < 120]
        if prev:
            print(f"[WEBHOOK] Duplicate — skipping {ticker}")
            return
        seen_tickers[key] = prev + [now]

        # Pattern detection
        today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        ticker_alerts[f"{ticker}_{today_str}"].append(now)
        count   = len(ticker_alerts[f"{ticker}_{today_str}"])
        pattern = {"count": count, "alert": count >= 2}

        # Macro warnings
        macro = get_today_warnings()

        # Fetch data
        flow_premium = trade.get("option_price") or (
            str(trade["premium"]/1000)+"K" if trade.get("premium") else None)
        def _fetch():
            return fetch_trade_data(trade, flow_premium=flow_premium)

        try:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                data = await asyncio.wait_for(
                    loop.run_in_executor(pool, _fetch),
                    timeout=45.0  # 45s max for data fetching
                )
        except asyncio.TimeoutError:
            print(f"[PROCESS] Data fetch timeout for {ticker}")
            data = {"market":{},"sector":{},"time_of_day":{},"stock_price":None,
                    "open_interest":None,"otm_pct":None,"days_to_expiry":None,
                    "earnings_date":None,"earnings_context":None,"earnings_is_past":False,
                    "days_since_earnings":None,"expiry_timing_label":None,"expiry_timing_emoji":None,
                    "fill_type":"UNKNOWN","fill_emoji":"❓","fill_label":"timeout",
                    "vol_oi_ratio":None,"vol_oi_label":None,"vol_oi_emoji":"",
                    "premium_label":None,"premium_emoji":None,"premium_raw":0,
                    "is_breakout_bet":False,"breakout_emoji":"","breakout_label":"",
                    "flow_fill_price":None,"spread_pct":None,"bid":None,"ask":None}

        # Score
        result = score_trade(trade, data, pattern)

        # Save
        analysis_id = len(analyses)
        now_et      = datetime.now(ZoneInfo("America/New_York"))
        entry = {
            "id":        analysis_id,
            "tweet":     tweet,
            "tweet_url": tweet_url,
            "date":      now_et.strftime("%Y-%m-%d"),
            "time":      now_et.strftime("%H:%M"),
            "trade":     trade,
            "result":    result,
            "data":      data,
            "pattern":   pattern,
            "macro":     macro,
        }
        analyses.append(entry)
        save_analyses()

        # Add to technical watchlist if WATCH or TRADE
        if result.get("verdict") in ("WATCH", "TRADE"):
            add_to_watchlist(ticker, trade, result, data, send_sms_fn=send_sms)

        # Build and send SMS
        msg     = build_sms(trade, data, result, tweet_url, analysis_id, pattern)
        success = send_sms(msg)

        print(f"[PROCESS] Done: {ticker} {result.get('final_score')}/7 {result.get('verdict')} — SMS {'sent' if success else 'failed'}")

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[PROCESS] Error for {tweet[:50]}: {e}")
        print(f"[PROCESS] Traceback:\n{tb}")
        try:
            token   = os.environ.get("TELEGRAM_BOT_TOKEN")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID")
            ticker  = "?"
            try: ticker = re.search(r'\$([A-Z]{1,5})',tweet).group(1)
            except: pass
            if token and chat_id:
                send_telegram(f"⚠️ FlowCheck error for {ticker}\n{str(e)[:100]}", token, chat_id)
        except:
            pass

# ── Routes ────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/ping")
async def ping():
    return {"status": "ok"}

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
        return {"status": "error", "message": str(e)}

    tweet     = body.get("tweet","")
    tweet_url = body.get("tweet_url","")
    print(f"[WEBHOOK] Received: {tweet[:80]}")
    if tweet_url:
        print(f"[WEBHOOK] Tweet URL: {tweet_url}")

    import asyncio
    asyncio.create_task(process_alert(tweet, tweet_url))
    ticker = "?"
    try: ticker = re.search(r'\$([A-Z]{1,5})',tweet).group(1)
    except: pass
    print(f"[WEBHOOK] Queued background processing for {ticker}")
    return {"status":"queued","ticker":ticker,"message":"Processing in background — SMS incoming"}

@app.get("/check-env")
async def check_env():
    vars_ = ["ANTHROPIC_API_KEY","TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID",
             "FINNHUB_API_KEY","TIINGO_API_KEY","BASE_URL"]
    return {v: ("SET ✅" if os.environ.get(v) else "MISSING ❌") for v in vars_}

@app.get("/test-telegram")
async def test_telegram():
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"status":"error","reason":"TELEGRAM credentials not set"}
    ok = send_telegram("⚡ FlowCheck test — Telegram working ✅", token, chat_id)
    return {"status":"sent" if ok else "failed"}

@app.get("/test-finnhub")
async def test_finnhub():
    from fetcher import fh_get, fh_key
    if not fh_key():
        return {"status":"error","reason":"FINNHUB_API_KEY not set"}
    data = fh_get("/quote",{"symbol":"AAPL"})
    if data and data.get("c",0) > 0:
        return {"status":"ok","AAPL_price":data["c"]}
    return {"status":"error","raw":str(data)[:200]}

@app.get("/test-tiingo")
async def test_tiingo():
    from fetcher import tiingo_history, tiingo_key
    if not tiingo_key():
        return {"status":"error","reason":"TIINGO_API_KEY not set"}
    spy = tiingo_history("SPY", days=5)
    return {"status":"ok" if spy else "error","SPY_history":spy[-3:] if spy else []}

@app.get("/test-polygon")
async def test_polygon():
    from technical import fetch_1min_candles
    import os
    if not os.environ.get("POLYGON_API_KEY"):
        return {"status":"error","reason":"POLYGON_API_KEY not set"}
    candles = fetch_1min_candles("AAPL", count=5)
    if candles:
        return {"status":"ok","candles":len(candles),"latest":candles[-1],"source":"Polygon"}
    return {"status":"error","reason":"No candles returned"}

@app.get("/analysis/{analysis_id}")
async def analysis_detail(analysis_id: int):
    if analysis_id >= len(analyses):
        return HTMLResponse("<h1>Analysis not found</h1>", status_code=404)
    a      = analyses[analysis_id]
    trade  = a.get("trade",{})
    result = a.get("result",{})
    data   = a.get("data",{})
    mkt    = data.get("market",{})
    ticker = trade.get("ticker","?")
    strike = trade.get("strike","?")
    otype  = trade.get("option_type","call").upper()
    expiry = trade.get("expiry","?")
    verdict= result.get("verdict","?")
    score  = result.get("final_score","?")
    verdict_color = {"TRADE":"#22c55e","WATCH":"#f59e0b","SKIP":"#ef4444"}.get(verdict,"#888")

    checklist_html = ""
    for k,v in (result.get("checklist") or {}).items():
        icon = "✅" if v.get("pass") else "❌"
        num  = k.replace("criterion_","")
        checklist_html += f'<div style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.05)">{icon} <b>#{num}</b> {v.get("note","")}</div>'

    # Tweet link
    if a.get("tweet_url"):
        tweet_link_html = f'<a href="{a["tweet_url"]}" target="_blank" style="display:inline-block;margin-top:10px;padding:6px 14px;border-radius:6px;background:rgba(96,165,250,0.1);border:1px solid rgba(96,165,250,0.3);color:#60A5FA;font-size:12px;text-decoration:none">🐦 View original tweet →</a>'
    else:
        tweet_link_html = ""

    improvements_html = "".join(
        f'<div style="padding:4px 0;color:#94a3b8">{imp}</div>'
        for imp in (result.get("improvements") or [])
    )

    base_url = os.environ.get("BASE_URL","https://flowcheck-production.up.railway.app")

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FlowCheck — {ticker}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#080810;color:#fff;font-family:-apple-system,sans-serif;padding:20px;max-width:640px;margin:0 auto}}
.hdr{{display:flex;align-items:center;gap:10px;padding:20px 0;border-bottom:1px solid rgba(255,255,255,0.08);margin-bottom:20px}}
.badge{{padding:8px 18px;border-radius:20px;font-weight:700;font-size:18px;color:#000;background:{verdict_color}}}
.sec{{background:rgba(255,255,255,0.04);border-radius:12px;padding:16px;margin-bottom:12px}}
.sec-title{{font-size:10px;letter-spacing:2px;color:#64748b;text-transform:uppercase;margin-bottom:10px}}
.tweet{{font-family:monospace;font-size:13px;color:#94a3b8;background:rgba(0,0,0,0.3);padding:10px;border-radius:8px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.cell{{background:rgba(255,255,255,0.04);border-radius:8px;padding:12px}}
.cell-label{{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px}}
.cell-value{{font-size:16px;font-weight:600}}
a{{color:#60a5fa}}
</style>
</head>
<body>
<div class="hdr">
  <div>
    <div style="font-size:28px;font-weight:800">{ticker}</div>
    <div style="color:#94a3b8;font-size:14px">{strike} {otype} · {expiry}</div>
  </div>
  <div class="badge">{score}/7 | {verdict}</div>
</div>

<div class="sec">
  <div class="sec-title">Original Alert</div>
  <div class="tweet">{a.get("tweet","")}</div>
  {tweet_link_html}
</div>

<div class="sec">
  <div class="sec-title">Market Conditions</div>
  <div class="grid">
    <div class="cell"><div class="cell-label">VIX</div><div class="cell-value">{mkt.get("vix","None")} {mkt.get("vix_label","")}</div></div>
    <div class="cell"><div class="cell-label">SPY 5-Day</div><div class="cell-value">{mkt.get("spy_trend","None")}</div></div>
    <div class="cell"><div class="cell-label">Sector ({data.get("sector",{}).get("etf","?")})</div><div class="cell-value">{data.get("sector",{}).get("sector_trend","None")}</div></div>
    <div class="cell"><div class="cell-label">Market Bias</div><div class="cell-value" style="color:{verdict_color}">{mkt.get("market_bias","?")}</div></div>
  </div>
  <div style="margin-top:10px;font-size:13px;color:#94a3b8">{mkt.get("market_summary","")}</div>
</div>

<div class="sec">
  <div class="sec-title">Trade Analysis</div>
  <div style="color:#94a3b8;font-size:14px;line-height:1.6">{result.get("reasoning","")}</div>
</div>

<div class="sec">
  <div class="sec-title">Checklist</div>
  {checklist_html}
</div>

<div class="sec">
  <div class="sec-title">Improvements</div>
  {improvements_html}
</div>

<div style="text-align:center;padding:20px 0">
  <a href="{base_url}/history">← Back to history</a>
</div>
</body>
</html>""")

@app.get("/history")
async def history():
    base_url = os.environ.get("BASE_URL","https://flowcheck-production.up.railway.app")
    rows = ""
    for a in reversed(analyses[-50:]):
        t       = a.get("trade",{})
        r       = a.get("result",{})
        verdict = r.get("verdict","?")
        color   = {"TRADE":"#22c55e","WATCH":"#f59e0b","SKIP":"#ef4444"}.get(verdict,"#888")
        rows   += f'<tr><td>{a.get("time","")}</td><td><b>{t.get("ticker","?")}</b></td><td>{t.get("strike","?")} {t.get("option_type","")[0].upper() if t.get("option_type") else "?"}</td><td>{t.get("expiry_short","?")}</td><td style="color:{color}">{r.get("final_score","?")}/7 {verdict}</td><td><a href="{base_url}/analysis/{a.get("id",0)}">View</a></td></tr>'

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FlowCheck History</title>
<style>
body{{background:#080810;color:#fff;font-family:-apple-system,sans-serif;padding:20px;max-width:800px;margin:0 auto}}
h1{{margin-bottom:20px}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:10px;text-align:left;border-bottom:1px solid rgba(255,255,255,0.08)}}
th{{color:#64748b;font-size:11px;text-transform:uppercase;letter-spacing:1px}}
a{{color:#60a5fa}}
</style>
</head>
<body>
<h1>📊 FlowCheck History</h1>
<table>
<tr><th>Time</th><th>Ticker</th><th>Strike</th><th>Expiry</th><th>Score</th><th>Detail</th></tr>
{rows if rows else '<tr><td colspan="6" style="text-align:center;color:#64748b">No alerts today</td></tr>'}
</table>
</body>
</html>""")

@app.get("/stats")
async def stats():
    """Win rate statistics across all tracked alerts."""
    from outcomes import get_stats
    return get_stats()

@app.get("/watchlist")
async def watchlist():
    from technical import get_watchlist
    wl = get_watchlist()
    return {
        "active_watches": len(wl),
        "tickers": {
            t: {
                "strike":             e.get("strike"),
                "expiry":             e.get("expiry"),
                "dte_remaining":      e.get("dte_remaining"),
                "verdict":            e.get("verdict"),
                "score":              e.get("flow_score"),
                "flow_stock_price":   e.get("flow_stock_price"),
                "flow_option_price":  e.get("flow_option_price"),
                "added_ago":          f"{int((time.time()-e['added'])/3600)}h ago",
                "alerted_timeframes": list(e.get("alerted",{}).keys()),
            } for t, e in wl.items()
        }
    }

@app.post("/backtest")
async def backtest_endpoint(request: Request):
    """
    Backtest a historical tweet with market conditions at time of posting.
    Runs in background and sends result to Telegram (takes ~60s due to Polygon delays).
    Body: {
      "tweet": "tweet text or leave empty to use image",
      "tweet_url": "https://twitter.com/...",
      "tweet_time": "2026-05-22T11:51:00"  (ET time)
    }
    """
    try:
        body       = await request.json()
        tweet      = body.get("tweet","")
        tweet_url  = body.get("tweet_url","")
        tweet_time = body.get("tweet_time","")

        if not tweet_time:
            return {"status":"error","reason":"tweet_time required (format: 2026-05-22T11:51:00 ET)"}

        import asyncio
        asyncio.create_task(_run_backtest(tweet, tweet_url, tweet_time))
        return {"status":"queued","message":"Backtest running — result will arrive on Telegram in ~60 seconds"}

    except Exception as e:
        return {"status":"error","reason":str(e)}

async def _run_backtest(tweet: str, tweet_url: str, tweet_time: str):
    """Background backtest task — sends result to Telegram."""
    import asyncio, concurrent.futures
    from backtest import build_historical_data
    from vision_parser import extract_trade_from_tweet
    from scorer import score_trade
    from zoneinfo import ZoneInfo
    from datetime import datetime

    try:
        send_sms(f"⏳ Backtest started for {tweet_time}\nFetching historical data — result in ~60s...")

        tweet_dt = datetime.strptime(tweet_time, "%Y-%m-%dT%H:%M:%S")
        tweet_dt = tweet_dt.replace(tzinfo=ZoneInfo("America/New_York"))

        loop = asyncio.get_event_loop()

        def _do_backtest():
            trade = extract_trade_from_tweet(tweet, tweet_url)
            if not trade or not trade.get("ticker"):
                return None, None, "Could not extract trade"
            data   = build_historical_data(trade, tweet_dt)
            result = score_trade(trade, data, {"count":1,"alert":False})
            return trade, data, result

        with concurrent.futures.ThreadPoolExecutor() as pool:
            trade, data, result = await asyncio.wait_for(
                loop.run_in_executor(pool, _do_backtest),
                timeout=120.0
            )

        if isinstance(result, str):
            send_sms(f"❌ Backtest error: {result}")
            return

        ticker  = trade.get("ticker","?")
        strike  = trade.get("strike","?")
        otype   = trade.get("option_type","call")[0].upper()
        expiry  = trade.get("expiry","?")
        mkt     = data.get("market",{})
        verdict = result.get("verdict","?")
        score   = result.get("final_score","?")
        v_emoji = {"TRADE":"✅","WATCH":"👀","SKIP":"❌"}.get(verdict,"❓")

        msg = (
            f"📊 BACKTEST RESULT — {tweet_time} ET\n"
            f"\n"
            f"{v_emoji} {ticker} {strike}{otype} {expiry}\n"
            f"{result.get('raw_score','?')}/7 → {score}/7 {verdict}\n"
            f"\n"
            f"Historical conditions:\n"
            f"  Stock: ${data.get('stock_price','?')} | OTM: {data.get('otm_pct','?')}%\n"
            f"  VIX: {mkt.get('vix','?')} {mkt.get('vix_label','')}\n"
            f"  SPY: {mkt.get('spy_trend','?')}\n"
            f"  DTE at tweet: {data.get('days_to_expiry','?')} days\n"
            f"  Fill: {data.get('fill_type','?')} {data.get('fill_emoji','')}\n"
            f"\n"
            f"→ {result.get('one_liner','')}\n"
            f"→ {(result.get('improvements') or [''])[0]}"
        )
        send_sms(msg)
        print(f"[BACKTEST] Done: {ticker} {score}/7 {verdict}")

    except asyncio.TimeoutError:
        send_sms("❌ Backtest timed out — Polygon API too slow")
    except Exception as e:
        import traceback
        print(f"[BACKTEST] Error: {e}\n{traceback.format_exc()}")
        send_sms(f"❌ Backtest error: {str(e)[:100]}")

@app.get("/")
async def root():
    return {"status":"ok","service":"FlowCheck","version":"4.0",
            "alerts_today": len([a for a in analyses if a.get("date")==datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")])}
