import time
import json, os, re
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler

from vision_parser import extract_trade_from_tweet
from fetcher import fetch_trade_data
from scorer import score_trade
from sms import send_sms, send_telegram
from economic_calendar import get_today_warnings
from premarket_summary import send_premarket_summary, send_eod_summary, verify_eod_positions
from technical import add_to_watchlist, run_technical_scan, get_watchlist
from outcomes import track_outcomes
from exit_signals import add_position, check_exit_signals, get_open_positions
from premarket_gap import send_premarket_gap_alerts
from telegram_commands import poll_commands
from weekly_report import send_weekly_report
from market_calendar import is_market_open, market_status, get_holiday_name
from price_alerts import check_price_alerts
from daily_pnl import send_daily_pnl

def send_position_check():
    """
    Send daily position check at 4:05 PM ET.
    Shows open journal positions and prompts to sync any missing.
    """
    try:
        from trade_journal import load_journal
        journal  = load_journal()
        open_t   = journal.get("trades",[])
        accounts = journal.get("accounts",{})

        if not open_t:
            return

        lines = ["📋 Daily Position Check — verify your open positions"]
        lines.append("")
        lines.append("Journal shows " + str(len(open_t)) + " open position(s):")
        for t in open_t:
            otype  = t.get("option_type","call")[0].upper()
            aid    = t.get("account_id","default")
            aname  = accounts.get(aid,{}).get("name",aid)
            remaining = t.get("contracts_remaining") or t.get("contracts","?")
            lines.append(
                "  " + t.get("ticker","") + " " + str(t.get("strike","")) + otype +
                " " + str(t.get("expiry","")) +
                " x" + str(remaining) + " [@" + aname + "]"
            )
        lines.append("")
        lines.append("Missing a position? Add it with:")
        lines.append("/sync TICKER STRIKE C/P CONTRACTS PRICE @ACCOUNT")
        lines.append("Example: /sync FLNC 23 C 3 2.85 @rh_trad")
        send_sms(chr(10).join(lines))
        print("[POSITION CHECK] Sent daily check")
    except Exception as e:
        print(f"[POSITION CHECK] Error: {e}")
from eod_pricer import update_eod_prices
from storage import init_db, storage_status
from position_sizing import calc_position_size, format_sizing_for_sms
from news_check import format_news_for_sms
from flow_intelligence import run_flow_intelligence, track_sector_flow
from risk_manager import run_risk_checks, send_theta_calendar
from paper_trading import add_paper_trade, update_paper_outcomes, get_paper_stats

app = FastAPI()

# ── Persistence ───────────────────────────────────────────────────────
ANALYSES_FILE = "/tmp/flowcheck_analyses.json"

ANALYSES_KEY = "analyses_today"

def save_analyses():
    from storage import save_data, db_set
    try:
        today      = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        today_data = [a for a in analyses if a.get("date") == today]

        # Also save yesterday's analyses separately for pre-market OI check
        yesterday  = (datetime.now(ZoneInfo("America/New_York")).date() - __import__("datetime").timedelta(days=1)).isoformat()
        yest_data  = [a for a in analyses if a.get("date") == yesterday]
        if yest_data:
            import json as _j
            db_set("analyses_yesterday", _j.dumps({"date": yesterday, "analyses": yest_data}))
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
        payload = {"date": today, "analyses": serializable}
        save_data(ANALYSES_KEY, ANALYSES_FILE, payload)
        print(f"[PERSIST] Saved {len(serializable)} analyses to Supabase")
    except Exception as e:
        print(f"[PERSIST] Save error: {e}")

def load_analyses():
    from storage import load_data
    try:
        today  = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        data   = load_data(ANALYSES_KEY, ANALYSES_FILE, {"date":"","analyses":[]})
        if data.get("date") == today:
            loaded = data.get("analyses",[])
            print(f"[PERSIST] Loaded {len(loaded)} analyses from Supabase")
            return loaded
        print("[PERSIST] No analyses for today — starting fresh")
    except Exception as e:
        print(f"[PERSIST] Load error: {e}")
    return []

# ── Stores ────────────────────────────────────────────────────────────
analyses        = load_analyses()
seen_tickers    = defaultdict(list)
ticker_alerts   = defaultdict(list)
seen_tweet_ids  = set()  # Duplicate tweet filter
last_webhook_ts = 0      # IFTTT watchdog
scheduler       = BackgroundScheduler(timezone="America/New_York")

def check_polygon_health():
    """Check Polygon API is working before market open."""
    import requests as _req
    key = os.environ.get("POLYGON_API_KEY","")
    if not key:
        send_sms("⚠️ POLYGON_API_KEY not set — technical scanner disabled")
        return
    try:
        # Use free-tier endpoint — aggregates
        r = _req.get(
            "https://api.polygon.io/v2/aggs/ticker/AAPL/range/1/day/2025-01-02/2025-01-03",
            params={"apiKey": key}, timeout=8
        )
        if r.status_code == 200:
            print("[HEALTH] Polygon OK")
        elif r.status_code == 403:
            send_sms("⚠️ Polygon API key invalid — technical scanner disabled")
        elif r.status_code == 429:
            print("[HEALTH] Polygon rate limited — key valid")
        else:
            print(f"[HEALTH] Polygon status {r.status_code} — monitoring")
    except Exception as e:
        print(f"[HEALTH] Polygon unreachable: {str(e)[:60]}")

def remind_open_journal_trades():
    """4:10 PM — remind about open journal trades that may need logging."""
    try:
        from trade_journal import load_journal
        journal   = load_journal()
        open_t    = journal.get("trades", [])
        if not open_t:
            return
        now_et = datetime.now(ZoneInfo("America/New_York"))
        today  = now_et.strftime("%Y-%m-%d")
        # Only remind for trades entered today or still open from before
        remind = []
        for t in open_t:
            # Skip if entered today and held < 5 hours (still in play)
            try:
                entry_dt = datetime.fromisoformat(t["entry_datetime"])
                hrs_held = (now_et - entry_dt).total_seconds() / 3600
                if t.get("entry_date") == today and hrs_held < 5:
                    continue
            except:
                pass
            remind.append(t)
        if not remind:
            return
        lines = ["OPEN JOURNAL TRADES AT EOD"]
        lines.append("")
        for t in remind:
            otype = t.get("option_type","call")[0].upper()
            remaining = t.get("contracts_remaining", t.get("contracts","?"))
            lines.append(
                t["ticker"] + " " + str(t.get("strike","")) + otype +
                " x" + str(remaining) + " @ $" + str(t.get("entry_price",""))
            )
            lines.append(
                "  In: " + str(t.get("entry_date","")) + " " + str(t.get("entry_time",""))
            )
        lines.append("")
        lines.append("Use /exit TICKER PRICE DATE TIME to log exits")
        lines.append("Use /journal to review open trades")
        send_sms(chr(10).join(lines))
        print(f"[REMINDER] Sent EOD reminder for {len(remind)} open journal trades")
    except Exception as e:
        print(f"[REMINDER] Error: {e}")

def cleanup_expired_positions():
    """4:02 PM — close positions where option has expired."""
    from exit_signals import load_positions, save_positions, close_position
    from datetime import datetime as _dt
    positions = load_positions()
    closed    = 0
    for p in positions:
        if p.get("status") != "OPEN":
            continue
        expiry_raw = p.get("expiry_raw","")
        if not expiry_raw:
            continue
        try:
            parts = expiry_raw.split("/")
            m, d, y = parts
            y = "20"+y if len(y)==2 else y
            exp = _dt(int(y),int(m),int(d))
            if (_dt.now() - exp).days >= 0:
                p["status"]     = "CLOSED"
                p["exit_reason"]= "EXPIRED"
                p["closed_at"]  = _dt.now().isoformat()
                closed += 1
                print(f"[CLEANUP] Auto-closed expired: {p.get('ticker')} {expiry_raw}")
        except Exception as e:
            print(f"[CLEANUP] Error: {e}")
    if closed:
        save_positions(positions)
        print(f"[CLEANUP] Closed {closed} expired positions")

def keep_alive_ping():
    """Self-ping to prevent Railway cold starts."""
    base_url = os.environ.get("BASE_URL","")
    if base_url:
        try:
            import requests as _req
            _req.get(f"{base_url}/health", timeout=5)
            print("[KEEPALIVE] Ping sent")
        except:
            pass

def check_ifttt_watchdog():
    """Alert if no webhook received during market hours for 2+ hours."""
    global last_webhook_ts
    now_et = datetime.now(ZoneInfo("America/New_York"))
    total  = now_et.hour * 60 + now_et.minute
    # Only check during prime market hours 10 AM - 3 PM
    if total < 10*60 or total > 15*60:
        return
    if last_webhook_ts == 0:
        return
    mins_since = int((time.time() - last_webhook_ts) / 60)
    if mins_since >= 120:
        from sms import send_sms as _sms
        _sms(f"⚠️ IFTTT WATCHDOG: No alerts in {mins_since} minutes\n"
             f"Check IFTTT connection at ifttt.com")
        print(f"[WATCHDOG] No webhook in {mins_since}min — alert sent")

# ── Scheduler ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    try:
        scheduler.add_job(lambda: send_premarket_summary(analyses),
                          "cron", day_of_week="mon-fri", hour=8, minute=0, id="premarket")
        scheduler.add_job(lambda: verify_eod_positions(analyses),
                          "cron", day_of_week="mon-fri", hour=16, minute=15, id="eod_oi")
        scheduler.add_job(lambda: send_eod_summary(analyses) if is_market_open() else None,
                          "cron", day_of_week="mon-fri", hour=16, minute=30, id="eod_summary")
        scheduler.add_job(cleanup_expired_positions,
                          "cron", day_of_week="mon-fri", hour=16, minute=2, id="expire_cleanup")
        scheduler.add_job(lambda: remind_open_journal_trades() if is_market_open() else None,
                          "cron", day_of_week="mon-fri", hour=16, minute=10, id="journal_reminder")
        scheduler.add_job(lambda: send_position_check() if is_market_open() else None,
                          "cron", day_of_week="mon-fri", hour=16, minute=5, id="position_check",
                          max_instances=1, coalesce=True)
        scheduler.add_job(lambda: send_daily_pnl(send_sms) if is_market_open() else None,
                          "cron", day_of_week="mon-fri", hour=16, minute=10, id="daily_pnl",
                          max_instances=1, coalesce=True)
        scheduler.add_job(lambda: run_technical_scan(send_sms),
                          "interval", minutes=5, id="technical_scan")
        scheduler.add_job(lambda: check_exit_signals(),
                          "interval", minutes=15, id="exit_signals")
        scheduler.add_job(lambda: track_outcomes(analyses),
                          "cron", day_of_week="mon-fri", hour=16, minute=0, id="outcome_track")
        scheduler.add_job(lambda: send_premarket_gap_alerts(get_watchlist()) if is_market_open() else None,
                          "cron", day_of_week="mon-fri", hour=9, minute=0, id="premarket_gap")
        scheduler.add_job(poll_commands,
                          "interval", seconds=10, id="telegram_commands",
                          max_instances=1, coalesce=True)
        scheduler.add_job(lambda: check_price_alerts(send_sms),
                          "interval", seconds=60, id="price_alerts",
                          max_instances=1, coalesce=True)
        scheduler.add_job(send_weekly_report,
                          "cron", day_of_week="fri", hour=16, minute=45, id="weekly_report")
        scheduler.add_job(lambda: send_theta_calendar(send_sms) if is_market_open() else None,
                          "cron", day_of_week="mon", hour=8, minute=5, id="theta_calendar")
        scheduler.add_job(lambda: check_polygon_health() if is_market_open() else None,
                          "cron", day_of_week="mon-fri", hour=9, minute=25, id="polygon_health")
        scheduler.add_job(update_paper_outcomes,
                          "cron", day_of_week="mon-fri", hour=16, minute=5, id="paper_outcomes")
        scheduler.add_job(keep_alive_ping,
                          "interval", minutes=5, id="keep_alive")
        scheduler.add_job(check_ifttt_watchdog,
                          "interval", minutes=30, id="ifttt_watchdog")
        scheduler.start()
        print("[SCHEDULER] Started: all jobs running")

    except Exception as _sch_e:
        print(f'[SCHEDULER] Warning: {_sch_e}')

    # Start Bullflow stream if API key present (runs alongside FlowGod if both configured)
    try:
        import threading
        bf_key       = os.environ.get("BULLFLOW_API_KEY","")
        flow_source  = os.environ.get("FLOW_SOURCE","flowgod").lower()
        dual_mode    = os.environ.get("DUAL_FLOW_MODE","").lower() == "true"
        bf_enabled   = bf_key and (flow_source == "bullflow" or dual_mode)
        already_running = any(t.name == "bullflow-stream" for t in threading.enumerate())
        if bf_enabled and not already_running:
            from bullflow_stream import start_stream_thread
            start_stream_thread(process_alert, send_sms)
            mode_label = "DUAL MODE (FlowGod + Bullflow)" if dual_mode else "Bullflow only"
            print(f"[STARTUP] {mode_label} — Bullflow SSE stream started")
        elif not bf_enabled:
            print(f"[STARTUP] FlowGod/IFTTT mode only")
    except Exception as _be:
        print(f"[STARTUP] Bullflow stream error: {_be}")

# ── SMS builder ───────────────────────────────────────────────────────
def calc_exit_target(final_score: int, data: dict) -> dict:
    """Calculate exit target and stop strategy based on flow conviction and DTE."""
    vol_oi    = float(data.get("vol_oi_ratio",0) or 0)
    premium   = float(data.get("premium",0) or 0)
    fill_type = (data.get("fill_type","") or "").upper()
    dte       = int(data.get("days_to_expiry",30) or 30)
    is_sweep  = bool(data.get("is_sweep"))

    # Base target from score
    if final_score >= 6:   target_pct = 100
    elif final_score >= 5: target_pct = 75
    else:                  target_pct = 51

    # Conviction boosts
    if vol_oi >= 10:                             target_pct = min(target_pct + 25, 150)
    elif vol_oi >= 5:                            target_pct = min(target_pct + 15, 125)
    if fill_type in ("FULL_ASK","ABOVE_ASK"):    target_pct = min(target_pct + 10, 150)
    if premium >= 1_000_000:                     target_pct = min(target_pct + 25, 200)
    elif premium >= 500_000:                     target_pct = min(target_pct + 15, 150)
    if is_sweep:                                 target_pct = min(target_pct + 10, 150)

    # DTE-based stop — short options stop on STOCK level, not option %
    if dte <= 3:
        target_pct = min(target_pct, 75)
        stop_str   = "No % stop — exit if stock breaks support or flat by 2PM ET"
    elif dte <= 7:
        target_pct = min(target_pct, 100)
        stop_str   = "No % stop — exit if stock breaks below entry-day low"
    elif dte <= 21:
        stop_str   = "-50% option loss"
    elif dte <= 45:
        stop_str   = "-60% option loss"
    else:
        stop_str   = "-70% option loss or thesis change"

    # Scale-out
    if target_pct > 75:
        scale = "Sell 50% at 51%, hold rest to +" + str(target_pct) + "%"
    elif target_pct > 51:
        scale = "Sell 50% at 51%, trail stop on rest"
    else:
        scale = "Full exit at +" + str(target_pct) + "%"

    return {"target": target_pct, "stop": stop_str, "scale": scale}

def build_sms(trade: dict, data: dict, result: dict,
              tweet_url: str, analysis_id: int, pattern: dict,
              intel: dict = None, risk: dict = None) -> str:
    intel = intel or {}
    risk  = risk  or {}

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

    # Check time remaining in trading day
    from zoneinfo import ZoneInfo as _ZI
    from datetime import datetime as _dt2
    now_et       = _dt2.now(_ZI("America/New_York"))
    mins_to_close = (16*60) - (now_et.hour*60 + now_et.minute)
    dte_val  = data.get("days_to_expiry")
    is_leap  = dte_val is not None and int(dte_val) > 180
    is_expired = dte_val is not None and int(dte_val) < 0
    dte_display = "EXPIRED" if is_expired else (f"{dte_val}d DTE" if dte_val is not None else "")

    if mins_to_close <= 0:
        if is_leap:
            time_warning = "🌙 AFTER-HOURS LEAP — routine positioning, no urgency signal"
        elif is_expired:
            time_warning = f"⚠️ EXPIRED OPTION — historical alert only, cannot trade"
        else:
            is_ps = data.get("fill_type","") == "PUT_SELL_BID"
            ah_action = "stealth put sale" if is_ps else "stealth buy"
            time_warning = f"🌙 AFTER-HOURS FLOW ({dte_display}) — {ah_action}, expect overnight/pre-market move"
    elif mins_to_close <= 30:
        if is_leap:
            time_warning = f"⏰ Late day LEAP ({mins_to_close}min left) — long-dated, no urgency signal"
        elif is_expired:
            time_warning = f"⚠️ EXPIRED OPTION — historical alert only"
        else:
            is_ps2 = data.get("fill_type","") == "PUT_SELL_BID"
            late_action = "stealth put sale" if is_ps2 else "stealth buy"
            time_warning = f"🎯 LATE DAY FLOW ({mins_to_close}min left, {dte_display}) — {late_action}, avoiding copycats"
    else:
        if is_expired:
            time_warning = f"⚠️ EXPIRED OPTION — historical/backtest alert only"
        else:
            time_warning = None

    regime_str = ""
    if mkt.get("regime") and mkt.get("regime") not in ("NEUTRAL","UNKNOWN"):
        regime_str = f" · {mkt.get('regime_emoji','')} {mkt.get('regime','')}"

    # Source badge
    src = trade.get("source","")
    if src == "bullflow":
        src_badge = " 🅱"
    elif src == "flowgod":
        src_badge = " 🐦"
    else:
        src_badge = ""

    lines = [
        f"{verdict_emoji} {ticker} {strike}{otype} {expiry}{dte_str}{px_tag}{src_badge}",
        f"{raw_score}/7{adj_str}→ {final_score}/7 {verdict}",
        f"VIX {vix_str} · SPY {spy_str}{regime_str}",
    ]
    # Strategy note for non-neutral regimes
    if mkt.get("strategy_note") and mkt.get("regime") not in ("NEUTRAL","UNKNOWN","TRENDING_BULL"):
        lines.append(f"📋 {mkt['strategy_note']}")
    if time_warning:
        lines.append(time_warning)

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

    # Move analysis
    move = data.get("move_analysis")
    if move and data.get("days_to_expiry",99) <= 60:  # Only show for near-term
        lines.append(f"📐 {move['label']}")

    # Earnings timing
    earn = data.get("expiry_timing_label","")
    if earn:
        lines.append(f"{data.get('expiry_timing_emoji','')} {earn}")

    # Pattern alert
    if pattern.get("alert"):
        lines.append(f"⚠️ {ticker} alerted {pattern['count']}x today")

    # Analysis lines
    # Cross-source confirmation badge
    bf_conf = data.get("bullflow_confirmation")
    if bf_conf:
        bf_v = bf_conf.get("verdict","")
        bf_s = bf_conf.get("score","?")
        bf_t = bf_conf.get("time","")
        conf_emoji = "🔥" if bf_v == "TRADE" else "✅"
        time_note  = f" at {bf_t}" if bf_t else ""
        lines.append(f"{conf_emoji} CONFIRMED by Bullflow{time_note} — scored {bf_s}/7 {bf_v}")

    if one_liner:
        lines.append(f"→ {one_liner}")
    if top_imp:
        lines.append(f"→ {top_imp}")

    # Sweep detection
    if data.get("is_sweep") and data.get("sweep_label"):
        lines.append(f"{data.get('sweep_emoji','')} {data['sweep_label']}")

    # Current option price vs flow fill
    if data.get("option_entry_note") and data.get("option_entry_emoji"):
        lines.append(f"{data['option_entry_emoji']} {data['option_entry_note']}")

    # Confidence score from historical data
    if intel.get("confidence",{}).get("confidence") is not None:
        c = intel["confidence"]
        lines.append(f"📊 Confidence: {c['confidence_label']}")

    # Weekly ticker summary
    if intel.get("weekly_summary"):
        lines.append(f"📈 {intel['weekly_summary']['summary']}")

    # Intelligence signals
    if intel:
        if intel.get("roll",{}).get("is_roll"):
            lines.append(f"{intel['roll']['roll_emoji']} {intel['roll']['roll_label']}")
            lines.append(f"  → {intel['roll']['roll_note']}")
        if intel.get("repeat",{}).get("is_repeat"):
            lines.append(f"{intel['repeat']['repeat_emoji']} {intel['repeat']['repeat_label']}")
        if intel.get("divergence",{}).get("has_divergence") is not False and intel.get("divergence"):
            div = intel["divergence"]
            lines.append(f"{div.get('div_emoji','')} {div.get('div_label','')}")
        if intel.get("dark_pool",{}).get("unusual_volume"):
            dp = intel["dark_pool"]
            lines.append(f"{dp.get('dark_pool_emoji','')} {dp.get('dark_pool_label','')}")
        if intel.get("earnings_season",{}).get("in_earnings_season"):
            es = intel["earnings_season"]
            # Suppress earnings season note for put sells — they benefit from theta, not catalysts
            is_put_sell_es = data.get("fill_type","") == "PUT_SELL_BID"
            if not is_put_sell_es:
                lines.append(f"{es.get('season_emoji','')} {es.get('season_note','')}")

    # Risk warnings
    if risk and risk.get("warnings"):
        for w in risk["warnings"][:2]:
            lines.append(w)
    if risk and risk.get("smart_stop"):
        ss = risk["smart_stop"]
        lines.append(f"🛑 Smart stop: ${ss['stop_price']} ({ss['stop_reason']})")

    # Greeks
    greeks = data.get("greeks")
    if greeks:
        delta = greeks.get("delta","?")
        theta = greeks.get("theta","?")
        iv    = greeks.get("iv","?")
        lines.append(f"Δ {delta} | θ {theta}/day | IV {iv}%")

    # IV Rank
    if data.get("iv_rank") is not None:
        lines.append(f"IV Rank: {data['iv_rank']}% {data.get('iv_label','')} — {data.get('iv_advice','')}")

    # IV Crush risk
    if data.get("crush_risk","NONE") not in ("NONE","LOW",""):
        lines.append(f"{data.get('crush_emoji','')} IV Crush: {data.get('crush_label','')}")

    # News context
    news_lines = format_news_for_sms(data.get("news") or {})
    lines.extend(news_lines)

    # Insider buying
    if data.get("has_insider_buying") and data.get("insider_summary"):
        lines.append(f"👔 {data['insider_summary'][:80]}")

    # Short squeeze potential
    if data.get("short_squeeze_potential") and data.get("short_ratio"):
        lines.append(f"🔥 Short interest: {data['short_ratio']}% — squeeze potential")

    # Position sizing
    option_price = trade.get("option_price") or trade.get("avg_fill_price") or data.get("flow_fill_price")
    src_debug    = trade.get("source","")
    print(f"[BUILD_SMS] source={src_debug} option_price={option_price} flow_fill={data.get('flow_fill_price')}")
    if option_price:
        # Safe convert — option_price may be string like '2.85' or '462.0K'
        try:
            op_str = str(option_price).strip().upper()
            if op_str.endswith("K"):
                op_float = float(op_str[:-1]) * 1000
            elif op_str.endswith("M"):
                op_float = float(op_str[:-1]) * 1000000
            else:
                op_float = float(op_str)
            # Sanity check — option price should be under $500 per contract
            if op_float > 500:
                op_float = None  # Likely a premium value, not option price
        except:
            op_float = None

        if op_float and op_float > 0:
            # Show flow entry price and suggested entry limit
            opt_type_str = (trade.get("option_type","call") or "call").lower()
            source_str   = trade.get("source","")
            if source_str == "bullflow":
                # Entry limit: within 2-5% of flow fill price
                entry_limit = round(op_float * 1.03, 2)  # 3% above flow fill
                lines.append(f"💰 Flow filled @ ${op_float:.2f} | Entry limit: ${entry_limit:.2f}")

            from outcomes import get_stats
            stats      = get_stats()
            win_rate   = stats.get("win_rate") if stats.get("total",0) >= 5 else None
            sizing     = calc_position_size(op_float, verdict,
                                            win_rate=win_rate, score=final_score)
            sizing_str = format_sizing_for_sms(sizing, op_float)
            if sizing_str:
                lines.append(sizing_str)

    # Exit target + S/R
    try:
        tgt         = calc_exit_target(int(final_score), data)
        is_put_sell = data.get("fill_type","") == "PUT_SELL_BID"
        if is_put_sell:
            strike_val = trade.get("strike","?")
            lines.append(f"🎯 Target: Capture 50-80% of premium via time decay")
            lines.append(f"  Exit when premium decays to 20-50% | Close near ${strike_val}")
        else:
            lines.append(f"🎯 Target: +{tgt['target']}% | Stop: {tgt['stop']}")
            lines.append(f"  {tgt['scale']}")

        sr = data.get("support_resistance",{})
        opt_lower   = str(trade.get("option_type","call")).lower()
        is_put_sell = data.get("fill_type","") == "PUT_SELL_BID"
        is_bullish  = "call" in opt_lower or is_put_sell
        if sr and is_bullish and sr.get("support_levels"):
            levels = " → ".join(["$"+str(l) for l in sr["support_levels"]])
            lines.append(f"📊 Support: {levels}")
            lines.append(f"  Thesis broken below ${sr['primary_support']}")
        elif sr and not is_bullish and sr.get("resistance_levels"):
            levels = " → ".join(["$"+str(l) for l in sr["resistance_levels"]])
            lines.append(f"📊 Resistance: {levels}")
            lines.append(f"  Thesis broken above ${sr['primary_resistance']}")
    except Exception as _te:
        print(f"[TARGET] {_te}")

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
async def process_alert(tweet: str, tweet_url: str, pre_parsed_trade: dict = None):
    try:
        import asyncio, concurrent.futures
        loop = asyncio.get_event_loop()

        # Use pre-parsed trade if provided (test mode or Bullflow)
        if pre_parsed_trade:
            trade = pre_parsed_trade
        else:
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
        dedup_window = int(os.environ.get("DEDUP_WINDOW_SECS","120"))
        prev = [t for t in seen_tickers.get(key,[]) if now-t < dedup_window]
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

        # Score — with graceful degradation if API down
        try:
            result = score_trade(trade, data, pattern)
        except Exception as score_err:
            print(f"[PROCESS] Scorer failed: {score_err} — sending raw alert")
            prem = trade.get("premium",0)
            if isinstance(prem, str): prem = 0
            result = {
                "raw_score": 0, "final_score": 0, "verdict": "WATCH",
                "market_adjustment": 0, "checklist": {},
                "reasoning": "Scoring unavailable — raw flow data below.",
                "one_liner": "API unavailable — review flow manually.",
                "improvements": ["Check Anthropic API status"],
            }
            # Still enforce hard rules manually
            if data.get("fill_type") == "FULL_ASK" and int(prem) >= 500000:
                result["verdict"] = "WATCH"
                result["final_score"] = 4.0

        # Run intelligence checks
        intel = run_flow_intelligence(trade, data, result)
        risk  = run_risk_checks(trade, data, result)

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
            "intel":     intel,
            "risk":      risk,
        }
        if not trade.get("_test"):
            analyses.append(entry)
            save_analyses()

        # Add to technical watchlist if WATCH or TRADE (skip test trades)
        dte = data.get("days_to_expiry")
        if trade.get("_test"):
            print("[PROCESS] Test trade — skipping watchlist/position/analyses")
        elif dte is None or int(dte) >= 1:
            if result.get("verdict") in ("WATCH", "TRADE"):
                add_to_watchlist(ticker, trade, result, data, send_sms_fn=send_sms)
            if result.get("verdict") == "TRADE":
                add_position(trade, data, result)
        else:
            print(f"[PROCESS] Skipping watchlist/position — option expired (DTE={dte})")

        # Hard Vol/OI filter for Bullflow mode — skip low conviction before scoring
        bullflow_src = trade.get("source","") == "bullflow" or os.environ.get("FLOW_SOURCE","").lower() == "bullflow"
        if bullflow_src:
            vol_oi_ratio = float(data.get("vol_oi_ratio", 0) or 0)
            min_vol_oi   = float(os.environ.get("FILTER_MIN_VOL_OI", "3.0"))
            oi_val       = int(data.get("open_interest", 0) or 0)
            min_oi       = int(os.environ.get("FILTER_MIN_OI", "500"))
            if vol_oi_ratio > 0 and vol_oi_ratio < min_vol_oi:
                print(f"[FILTER] {ticker} Vol/OI {vol_oi_ratio}x < {min_vol_oi}x minimum — skipping")
                return
            if oi_val > 0 and oi_val < min_oi:
                print(f"[FILTER] {ticker} OI {oi_val} < {min_oi} minimum — skipping")
                return

        # Cross-source confirmation — check both directions
        if trade.get("source") in ("flowgod", "bullflow"):
            try:
                ticker_key  = trade.get("ticker","").upper()
                strike_key  = str(trade.get("strike",""))
                expiry_key  = trade.get("expiry","")
                opt_key     = (trade.get("option_type","") or "")[:1].upper()
                now_ts      = time.time()
                cutoff      = now_ts - 86400  # Last 24 hours

                # Search recent analyses for matching Bullflow alert
                bf_match = None
                for prev in analyses:
                    prev_trade = prev.get("trade",{})
                    opposite   = "bullflow" if trade.get("source") == "flowgod" else "flowgod"
                    if (prev_trade.get("source") == opposite and
                            prev_trade.get("ticker","").upper() == ticker_key and
                            str(prev_trade.get("strike","")) == strike_key and
                            float(prev.get("timestamp", 0) or 0) > cutoff):
                        bf_match = prev
                        break

                if bf_match:
                    bf_score   = bf_match.get("result",{}).get("final_score","?")
                    bf_verdict = bf_match.get("result",{}).get("verdict","?")
                    bf_time    = bf_match.get("data",{}).get("flow_time","")
                    data["bullflow_confirmation"] = {
                        "score":   bf_score,
                        "verdict": bf_verdict,
                        "time":    bf_time,
                    }
                    print(f"[CONFIRM] FlowGod {ticker_key} {strike_key} matches Bullflow alert (score={bf_score})")
            except Exception as _ce:
                print(f"[CONFIRM] Error: {_ce}")

        # Fetch support/resistance levels
        if data.get("stock_price") and trade.get("option_type"):
            try:
                from fetcher import get_support_resistance
                sr = get_support_resistance(
                    trade["ticker"],
                    float(data["stock_price"]),
                    trade.get("option_type","call")
                )
                if sr:
                    data["support_resistance"] = sr
                    print(f"[S/R] {trade['ticker']}: {sr.get('support_levels') or sr.get('resistance_levels')}")
                else:
                    print(f"[S/R] No levels for {trade['ticker']}")
            except Exception as _sre:
                print(f"[S/R] Error: {_sre}")
        else:
            print(f"[S/R] Skipped — stock_price={data.get('stock_price')}")

        # Boost put sell score BEFORE building SMS
        if data.get("fill_type","") == "PUT_SELL_BID":
            old_score  = float(result.get("final_score", 0) or 0)
            premium_v  = float(data.get("premium", 0) or 0)
            vol_oi     = float(data.get("vol_oi_ratio", 0) or 0)
            # Strong put sell: large premium + high Vol/OI = force TRADE
            if premium_v >= 500000 and vol_oi >= 5.0:
                new_score = max(old_score + 1.5, 6.0)  # Force to TRADE minimum
            elif premium_v >= 200000 and vol_oi >= 3.0:
                new_score = max(old_score + 1.0, 5.0)  # Force to strong WATCH
            else:
                new_score = old_score + 0.5
            new_score = min(new_score, 7.0)
            result["final_score"] = new_score
            result["market_adjustment"] = result.get("market_adjustment", 0) + (new_score - old_score)
            if new_score >= 6.0:   result["verdict"] = "TRADE"
            elif new_score >= 4.0: result["verdict"] = "WATCH"
            print(f"[PUT SELL BOOST] {ticker}: {old_score}→{new_score} {result['verdict']} (premium=${premium_v:,.0f} vol_oi={vol_oi}x)")

        # Build and send SMS
        msg         = build_sms(trade, data, result, tweet_url, analysis_id, pattern, intel, risk)

        verdict_val = (result.get("verdict") or "").strip()
        final_score = float(result.get("final_score", 0) or 0)
        source      = trade.get("source","")

        # For Bullflow: only send TRADE to Telegram — WATCH/SKIP stored silently
        # For FlowGod: send all verdicts (curated feed, low volume)
        force_send    = bool(trade.get("_force_send"))
        dual_mode     = os.environ.get("DUAL_FLOW_MODE","").lower() == "true"
        # Bullflow mode: only send TRADE to avoid Telegram flood
        # FlowGod mode: send all verdicts (curated feed, already filtered)
        is_bullflow   = source == "bullflow"
        bullflow_mode = is_bullflow and not force_send
        min_score_alert = float(os.environ.get("BULLFLOW_MIN_SCORE","6.0")) if bullflow_mode else 0
        should_send   = (force_send or
                        not is_bullflow or           # FlowGod always sends
                        verdict_val == "TRADE" or    # TRADE always sends
                        final_score >= min_score_alert)

        print(f"[SMS] Routing: verdict='{verdict_val}' score={final_score} source={source} send={should_send}")
        success = False
        if should_send:
            success = send_sms(msg, verdict=verdict_val)
        else:
            print(f"[SMS] Suppressed (Bullflow {verdict_val}) — stored in analyses only")

        # Send sector rotation alert if detected
        if intel.get("sector_rotation",{}).get("rotation_detected"):
            rot = intel["sector_rotation"]
            send_sms(rot["alert"])  # alert already contains emoji

        # Send max positions block warning to TRADE channel
        if risk.get("blocked"):
            send_sms(risk["block_msg"], verdict="TRADE")

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
    return {"status": "ok", "storage": storage_status()}

@app.get("/migrate")
async def migrate_storage():
    """One-time migration of /tmp data to Supabase. Safe to run multiple times."""
    from storage import migrate_tmp_to_db
    result = migrate_tmp_to_db()
    return {"result": result}

@app.get("/attach-scores")
async def attach_scores():
    """Attach FlowCheck scores to open trades that are missing them."""
    from trade_journal import load_journal, save_journal
    from storage import db_get
    import json as _json
    from datetime import datetime
    from zoneinfo import ZoneInfo

    journal  = load_journal()
    open_t   = journal.get("trades", [])
    today    = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    # Load analyses from Supabase
    analyses_list = list(analyses)  # in-memory first
    if not analyses_list:
        raw = db_get("analyses_today")
        if raw:
            data = _json.loads(raw)
            if data.get("date") == today:
                analyses_list = data.get("analyses", [])

    if not analyses_list:
        return {"error": "No analyses found for today"}

    attached = 0
    for t in open_t:
        if t.get("fc_score") is not None:
            continue  # Already has score
        ticker  = t.get("ticker","").upper()
        matches = [
            a for a in analyses_list
            if a.get("trade",{}).get("ticker","").upper() == ticker
        ]
        if matches:
            latest = matches[-1]
            t["fc_score"]   = latest.get("result",{}).get("final_score")
            t["fc_verdict"] = latest.get("result",{}).get("verdict")
            attached += 1
            print(f"[ATTACH] {ticker}: {t['fc_score']}/7 {t['fc_verdict']}")

    if attached:
        save_journal(journal)

    return {"attached": attached, "message": f"Attached scores to {attached} trades"}

@app.get("/clear-test-trades")
async def clear_test_trades():
    """Remove test trades from watchlist and positions."""
    # Clear test analyses
    before = len(analyses)
    analyses[:] = [a for a in analyses if not a.get("trade",{}).get("_test")]
    removed_anal = before - len(analyses)
    save_analyses()
    return {"removed_analyses": removed_anal, "note": "Watchlist clears on next redeploy"}

@app.get("/close.js")
async def serve_close_js():
    import os
    from fastapi.responses import FileResponse, Response as _Resp
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "close.js")
    return FileResponse(p, media_type="application/javascript") if os.path.exists(p) else _Resp("", media_type="application/javascript")

@app.get("/journal.js")
async def serve_journal_js():
    import os
    from fastapi.responses import FileResponse, Response as _Resp
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journal.js")
    return FileResponse(p, media_type="application/javascript") if os.path.exists(p) else _Resp("", media_type="application/javascript")

@app.post("/journal-close")
async def journal_close_api(request: Request):
    """Close a trade from web table. POST: {trade_id, exit_price, contracts, exit_date, exit_time}"""
    try:
        body       = await request.json()
        trade_id   = str(body.get("trade_id",""))
        exit_price = float(body.get("exit_price",0))
        contracts  = body.get("contracts")
        exit_date  = body.get("exit_date","")
        exit_time  = body.get("exit_time","")
        if not trade_id or exit_price <= 0:
            return {"success": False, "error": "trade_id and exit_price required"}
        from trade_journal import load_journal, save_journal
        from datetime import datetime
        from zoneinfo import ZoneInfo
        journal  = load_journal()
        open_t   = journal.get("trades", [])
        closed_t = journal.get("closed", [])

        # Find trade by ID
        target = None
        for t in open_t:
            if str(t.get("id","")) == trade_id:
                target = t
                break
        if not target:
            return {"success": False, "error": "Trade not found"}

        ticker     = target.get("ticker","")
        remaining  = int(target.get("contracts_remaining") or target.get("contracts",1))
        close_qty  = int(contracts) if contracts else remaining
        entry_price= float(target.get("entry_price",0) or target.get("credit",0) or 0)

        # Calculate P&L
        pnl_per    = round(exit_price - entry_price, 2)
        pnl_total  = round(pnl_per * close_qty * 100, 2)
        pnl_pct    = round((pnl_per / entry_price * 100), 1) if entry_price > 0 else 0

        now_et     = datetime.now(ZoneInfo("America/New_York"))
        exit_d     = exit_date or now_et.strftime("%Y-%m-%d")
        exit_t     = exit_time or now_et.strftime("%I:%M%p")

        if close_qty >= remaining:
            # Full exit — move to closed
            target["exit_price"]        = exit_price
            target["exit_date"]         = exit_d
            target["exit_time"]         = exit_t
            target["pnl_total"]         = pnl_total
            target["pnl_pct"]           = pnl_pct
            target["contracts_remaining"] = 0
            closed_t.append(target)
            journal["trades"] = [t for t in open_t if str(t.get("id","")) != trade_id]
        else:
            # Partial exit — reduce contracts
            target["contracts_remaining"] = remaining - close_qty
            partial = dict(target)
            partial["contracts"]          = close_qty
            partial["exit_price"]         = exit_price
            partial["exit_date"]          = exit_d
            partial["exit_time"]          = exit_t
            partial["pnl_total"]          = pnl_total
            partial["pnl_pct"]            = pnl_pct
            closed_t.append(partial)

        journal["closed"] = closed_t
        save_journal(journal)
        sign = "+" if pnl_total >= 0 else ""
        return {"success": True, "ticker": ticker,
                "pnl": pnl_total, "pnl_str": sign + "$" + str(round(pnl_total,2))}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/journal-delete")
async def journal_delete_api(request: Request):
    """Delete a trade by ID from web table. POST body: {"trade_id": "abc", "bucket": "trades"}"""
    try:
        body     = await request.json()
        trade_id = body.get("trade_id","")
        bucket   = body.get("bucket","trades")  # "trades" or "closed"
        if not trade_id:
            return {"success": False, "error": "trade_id required"}
        from trade_journal import load_journal, save_journal
        journal = load_journal()
        before  = len(journal.get(bucket,[]))
        journal[bucket] = [t for t in journal.get(bucket,[]) if str(t.get("id","")) != str(trade_id)]
        after   = len(journal.get(bucket,[]))
        if before != after:
            save_journal(journal)
            return {"success": True, "deleted": before - after}
        return {"success": False, "error": "Trade not found"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/journal-edit")
async def journal_edit_api(request: Request):
    """
    API endpoint for inline journal editing from web table.
    POST body: {"trade_id": "abc123", "field": "account_id", "value": "rh_brok"}
    """
    try:
        body     = await request.json()
        trade_id = body.get("trade_id","")
        field    = body.get("field","")
        value    = body.get("value","")
        if not trade_id or not field:
            return {"success": False, "error": "trade_id and field required"}
        from trade_journal import load_journal, save_journal
        journal  = load_journal()
        updated = False
        for bucket in ("trades","closed"):
            for t in journal.get(bucket,[]):
                if str(t.get("id","")) == str(trade_id):
                    # Allowed fields for web editing
                    allowed = {
                        "account_id", "note", "ticker", "order_type",
                        "entry_price", "exit_price", "credit", "spread_width",
                        "contracts", "contracts_remaining", "expiry", "strike",
                        "option_type", "entry_date", "entry_time",
                        "exit_date", "exit_time", "exit_price", "pnl_total", "pnl_pct", "fc_score", "fc_verdict",
                        "last_price", "long_strike", "short_strike", "spread_type",
                    }
                    if field not in allowed:
                        return {"success": False, "error": f"Field '{field}' not editable. Allowed: {sorted(allowed)}"}
                    # Type coercion — strip any "X/Y" format from contracts display
                    if field == "contracts":
                        try:
                            value = int(float(str(value).split("/")[0].strip()))
                        except:
                            return {"success": False, "error": "Must be a number (e.g. 3)"}
                    elif field == "contracts_remaining":
                        try:
                            value = int(float(str(value).split("/")[0].strip()))
                        except:
                            return {"success": False, "error": "Must be a number (e.g. 3)"}
                    elif field in ("entry_price","exit_price","fc_score","last_price","credit","spread_width"):
                        try:
                            value = float(str(value).replace("$","").replace(",","").strip())
                        except:
                            return {"success": False, "error": "Must be a number (e.g. 13.20)"}
                    if field == "fc_verdict" and value.upper() not in ("TRADE","WATCH","SKIP"):
                        return {"success": False, "error": "fc_verdict must be TRADE, WATCH, or SKIP"}
                    if field == "account_id":
                        # Verify account exists
                        accounts = journal.get("accounts",{})
                        if value not in accounts and value != "default":
                            acct_list = ", ".join(accounts.keys())
                            return {"success": False, "error": f"Unknown account. Valid: {acct_list}"}
                    t[field] = value
                    updated  = True
                    break
            if updated:
                break
        if updated:
            # Save directly to Supabase via storage module
            save_journal(journal)
            # Verify by reloading
            verify = load_journal()
            found  = False
            for bkt in ("trades","closed"):
                for tr in verify.get(bkt,[]):
                    if str(tr.get("id","")) == str(trade_id):
                        actual = tr.get(field)
                        print(f"[EDIT] Verified: trade {trade_id} {field}={actual} (wanted {value})")
                        found = True
                        if str(actual) != str(value):
                            return {"success": False, "error": f"Save verified but value mismatch: {actual} != {value}"}
                        break
            if not found:
                print(f"[EDIT] Warning: trade {trade_id} not found on verify")
            return {"success": True, "trade_id": trade_id, "field": field, "value": value}
        print(f"[EDIT] Trade not found: id={trade_id}")
        return {"success": False, "error": "Trade not found"}
    except Exception as e:
        print(f"[EDIT] Error: {e}")
        return {"success": False, "error": str(e)}

@app.get("/fix-spreads")
async def fix_spreads_endpoint():
    """Set is_spread=True on any trade that has spread_type or long_strike/short_strike."""
    from trade_journal import load_journal, save_journal
    journal = load_journal()
    fixed   = 0
    for bucket in ("trades", "closed"):
        for t in journal.get(bucket, []):
            if t.get("is_spread"):
                continue
            if t.get("spread_type") or (t.get("long_strike") and t.get("short_strike")):
                t["is_spread"] = True
                fixed += 1
    if fixed:
        save_journal(journal)
    return {"fixed": fixed, "message": f"Set is_spread=True on {fixed} trades"}

@app.get("/merge-positions")
async def merge_positions():
    """Merge duplicate open positions into averaged single entries."""
    from trade_journal import load_journal, save_journal
    journal  = load_journal()
    open_t   = journal.get("trades", [])
    merged   = 0
    seen     = {}
    keep     = []

    for t in open_t:
        key = (
            t.get("ticker","").upper(),
            str(t.get("strike","")),
            t.get("account_id","default"),
            t.get("expiry",""),
            str(t.get("is_spread",False)),
        )
        if key in seen:
            # Merge into existing
            existing   = seen[key]
            e_contr    = int(existing.get("contracts_remaining") or existing.get("contracts",0))
            n_contr    = int(t.get("contracts_remaining") or t.get("contracts",0))
            total      = e_contr + n_contr
            e_price    = float(existing.get("entry_price",0) or existing.get("credit",0) or 0)
            n_price    = float(t.get("entry_price",0) or t.get("credit",0) or 0)
            if total > 0 and e_price > 0 and n_price > 0:
                avg = round((e_price*e_contr + n_price*n_contr) / total, 2)
                existing["entry_price"]         = avg
                existing["credit"]              = avg if existing.get("is_spread") else existing.get("credit")
                existing["contracts"]           = total
                existing["contracts_remaining"] = total
                existing["total_cost"]          = round(avg * total * 100, 2)
            else:
                existing["contracts"]           = total
                existing["contracts_remaining"] = total
            merged += 1
        else:
            seen[key] = t
            keep.append(t)

    if merged:
        journal["trades"] = keep
        save_journal(journal)

    return {"merged": merged, "remaining_positions": len(keep),
            "message": f"Merged {merged} duplicate positions into averaged entries"}

@app.get("/test-edit-spread")
async def test_edit_spread():
    """Directly edit spread trade 42 credit to 99.99 to test saving."""
    from trade_journal import load_journal, save_journal
    journal = load_journal()
    for t in journal.get("trades", []):
        if str(t.get("id","")) == "42":
            old_val = t.get("credit")
            t["credit"] = 99.99
            save_journal(journal)
            # Verify
            j2 = load_journal()
            for t2 in j2.get("trades",[]):
                if str(t2.get("id","")) == "42":
                    return {"old": old_val, "new": t2.get("credit"), "saved": t2.get("credit") == 99.99}
    return {"error": "trade 42 not found"}

@app.get("/debug-spreads")
async def debug_spreads():
    """Show all spread trades in journal for debugging."""
    from trade_journal import load_journal
    journal  = load_journal()
    spreads  = []
    all_open = journal.get("trades", [])
    for t in all_open:
        if t.get("is_spread") or t.get("spread_type"):
            spreads.append({
                "id":           t.get("id"),
                "ticker":       t.get("ticker"),
                "is_spread":    t.get("is_spread"),
                "spread_type":  t.get("spread_type"),
                "long_strike":  t.get("long_strike"),
                "short_strike": t.get("short_strike"),
                "expiry":       t.get("expiry"),
                "credit":       t.get("credit"),
                "strike":       t.get("strike"),
            })
    return {"spread_count": len(spreads), "spreads": spreads}

@app.get("/backfill-price-history")
async def backfill_price_history():
    """Backfill price_history from existing last_price data on all trades."""
    from trade_journal import load_journal, save_journal
    from datetime import datetime
    from zoneinfo import ZoneInfo
    journal = load_journal()
    today   = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    updated = 0
    for bucket in ("trades", "closed"):
        for t in journal.get(bucket, []):
            last_price = t.get("last_price")
            entry      = float(t.get("entry_price", 0) or 0)
            if not last_price or not entry:
                continue
            # Only backfill if no history yet
            if t.get("price_history"):
                continue
            pct = round(((float(last_price) - entry) / entry) * 100, 1)
            is_closed = bucket == "closed" or bool(t.get("exit_price"))
            t["price_history"] = [{"date": today, "price": float(last_price), "pct": pct, "post_exit": is_closed}]
            # Set peak from last_price
            t["peak_price"] = float(last_price)
            t["peak_pct"]   = pct
            updated += 1
    if updated:
        save_journal(journal)
    return {"backfilled": updated, "note": "Full analytics will populate after 4:02 PM ET daily"}

@app.get("/normalize-expiry")
async def normalize_expiry_endpoint():
    """Normalize all expiry dates in journal to MM/DD/YY format."""
    from trade_journal import load_journal, save_journal, normalize_expiry
    journal = load_journal()
    fixed   = 0
    for bucket in ("trades", "closed"):
        for t in journal.get(bucket, []):
            raw = t.get("expiry","")
            if raw:
                normalized = normalize_expiry(raw)
                if normalized != raw:
                    t["expiry"] = normalized
                    fixed += 1
    if fixed:
        save_journal(journal)
    return {"normalized": fixed, "message": f"Fixed {fixed} expiry dates to MM/DD/YY format"}

@app.get("/setup-storage")
async def setup_storage():
    """Create flowcheck_store table in Supabase. Run once after connecting."""
    from storage import ensure_table, storage_status
    result = ensure_table()
    return {"result": result, "status": storage_status()}

@app.post("/test-alert")
async def test_alert(request: Request, background_tasks: BackgroundTasks):
    """
    Test endpoint — processes a flow alert without needing a tweet URL.
    POST body: {"ticker": "NVDA", "strike": "140", "opt_type": "call", "expiry": "06/20/26", "premium": 500000}
    """
    try:
        body    = await request.json()
    except:
        body    = {}

    ticker   = body.get("ticker","NVDA").upper()
    strike   = body.get("strike","140")
    opt_type = body.get("opt_type","call")
    expiry   = body.get("expiry","06/20/26")
    premium  = body.get("premium",500000)

    # Build a fake tweet text
    prem_str  = f"${float(premium)/1000:.0f}K" if float(premium) < 1_000_000 else f"${float(premium)/1_000_000:.1f}M"
    fake_tweet = f"${ticker} - {prem_str} {opt_type.title()} sweep expiring {expiry} strike ${strike} [TEST]"

    # Build pre-parsed trade data using field names fetcher expects
    vol_oi_val = float(body.get("vol_oi", 5.0))
    oi_val     = int(body.get("oi", 500))
    fill_val   = body.get("fill_type","FULL_ASK")
    # Simulate ask_size so calc_fill_aggression sees FULL_ASK
    fake_trade = {
        "ticker":        ticker,
        "strike":        strike,
        "option_type":   opt_type,
        "expiry":        expiry,
        "expiry_raw":    expiry,
        "expiry_short":  expiry,
        "premium":       float(premium),
        "fill_type":     fill_val,       # Pre-set fill — respected by calc_fill_aggression
        "open_interest": oi_val,
        "volume":        int(vol_oi_val * oi_val),
        "vol_oi_ratio":  vol_oi_val,
        "ask_size":      100 if "ASK" in fill_val.upper() else 0,
        "bid_size":      0   if "ASK" in fill_val.upper() else 100,
    }

    fake_trade["_test"]   = True   # Flag to skip watchlist/journal
    fake_trade["_force_send"] = True  # Always send Telegram regardless of mode
    background_tasks.add_task(process_alert, fake_tweet, None, fake_trade)
    return {"status": "queued", "ticker": ticker, "tweet": fake_tweet}

@app.get("/test-storage")
async def test_storage():
    """Write and read a test value to verify Supabase is working."""
    from storage import db_set, db_get
    import time
    test_key = "__test__"
    test_val = str(time.time())
    write_ok = db_set(test_key, test_val)
    read_val = db_get(test_key)
    return {
        "write": "OK" if write_ok else "FAILED",
        "read":  "OK" if read_val == test_val else "FAILED",
        "match": read_val == test_val,
    }

@app.get("/test-tasty")
async def test_tasty():
    """Test Tastytrade API connection."""
    from tasty_pricer import test_connection
    return {"status": test_connection()}

@app.get("/sync-bullflow-filters")
async def sync_bullflow_filters():
    """Recreate Bullflow custom alert with current Railway filter settings."""
    try:
        import os as _os
        _os.environ["BULLFLOW_FORCE_RECREATE"] = "true"
        from bullflow_stream import setup_flowcheck_filters
        setup_flowcheck_filters()
        _os.environ.pop("BULLFLOW_FORCE_RECREATE", None)
        key = _os.environ.get("BULLFLOW_API_KEY","")
        import requests as _req
        r = _req.get(f"https://api.bullflow.io/v1/alerts/custom-alerts?key={key}", timeout=8)
        alerts = r.json().get("alerts",[]) if r.status_code == 200 else []
        return {
            "status": "✅ Done",
            "custom_alerts": len(alerts),
            "alerts": [a.get("alertName") for a in alerts],
            "filters": {
                "min_premium": _os.environ.get("FILTER_MIN_PREMIUM","500000"),
                "min_dte":     _os.environ.get("FILTER_MIN_DTE","7"),
                "max_dte":     _os.environ.get("FILTER_MAX_DTE","90"),
            }
        }
    except Exception as e:
        return {"status": f"❌ Error: {str(e)}"}

@app.get("/test-bullflow")
async def test_bullflow():
    """Test Bullflow API connection."""
    key = os.environ.get("BULLFLOW_API_KEY","")
    if not key:
        return {"status": "❌ BULLFLOW_API_KEY not set"}
    try:
        import requests as _req
        r = _req.get(
            f"https://api.bullflow.io/v1/alerts/custom-alerts?key={key}",
            timeout=8
        )
        if r.status_code == 200:
            data  = r.json()
            count = data.get("count",0)
            names = [a.get("alertName","") for a in data.get("alerts",[])]
            return {"status": "✅ Connected", "custom_alerts": count, "alerts": names}
        return {"status": f"❌ HTTP {r.status_code}", "detail": r.text[:200]}
    except Exception as e:
        return {"status": f"❌ Error: {str(e)}"}

@app.get("/test-tradier")
async def test_tradier():
    """Test Tradier API connection and option pricing."""
    import os, requests
    token = os.environ.get("TRADIER_TOKEN","")
    if not token:
        return {"status": "❌ TRADIER_TOKEN not set"}
    try:
        r = requests.get(
            "https://api.tradier.com/v1/user/profile",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=8
        )
        if r.status_code == 200:
            profile = r.json().get("profile",{})
            name    = profile.get("name","unknown")
            return {"status": "✅ Connected", "account": name}
        return {"status": f"❌ HTTP {r.status_code}: {r.text[:100]}"}
    except Exception as e:
        return {"status": f"❌ Error: {str(e)}"}

@app.get("/setup-robinhood")
async def setup_robinhood():
    """
    Step 1: Initiate Robinhood login.
    If MFA required, visit /robinhood-mfa?code=XXXXXX with the code from your phone.
    """
    from robinhood_sync import login, get_status
    if not __import__("robinhood_sync").has_credentials():
        return {"error": "Add ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD to Railway first"}
    success, msg = login()
    if success:
        return {"status": "✅ Logged in — auto-sync will start at next market open"}
    if msg == "MFA_REQUIRED":
        return {
            "status": "MFA required",
            "next":   "Check your phone for Robinhood verification code",
            "action": "Visit /robinhood-mfa?code=XXXXXX with your code",
        }
    return {"status": "❌ " + msg}

@app.get("/robinhood-mfa")
async def robinhood_mfa(code: str = ""):
    """Step 2: Submit MFA code after /setup-robinhood."""
    if not code:
        return {"error": "Provide code parameter: /robinhood-mfa?code=123456"}
    from robinhood_sync import login
    success, msg = login(mfa_code=code)
    if success:
        return {"status": "✅ Authenticated — Robinhood sync active"}
    return {"status": "❌ " + msg}

@app.get("/robinhood-status")
async def robinhood_status():
    """Check Robinhood sync status."""
    from robinhood_sync import get_status
    return get_status()

@app.get("/robinhood-sync")
async def robinhood_sync_now():
    """Manually trigger Robinhood order sync."""
    from robinhood_sync import sync_orders, has_credentials
    if not has_credentials():
        return {"error": "No credentials configured"}
    new_logs = sync_orders()
    return {"synced": len(new_logs), "trades": new_logs}

@app.get("/robinhood-orders")
async def robinhood_orders_raw():
    """Show raw Robinhood option orders for debugging."""
    from robinhood_sync import get_option_orders, ensure_logged_in
    if not ensure_logged_in():
        return {"error": "Login failed — check ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD"}
    orders = get_option_orders(20)
    summary = []
    for o in orders[:20]:
        summary.append({
            "id":      o.get("id","")[:8],
            "state":   o.get("state",""),
            "type":    o.get("opening_strategy") or o.get("closing_strategy",""),
            "legs":    len(o.get("legs",[])),
            "created": o.get("created_at","")[:10],
        })
    return {"total": len(orders), "sample": summary}

@app.get("/reset-journal")
async def reset_journal():
    """Delete journal and accounts from Supabase. Irreversible."""
    from storage import db_set
    default_journal  = {"trades": [], "closed": [], "missed": []}
    default_accounts = {"default": {"name": "Main", "size": 10000}}
    import json
    db_set("journal",  json.dumps(default_journal))
    db_set("accounts", json.dumps(default_accounts))
    return {"status": "cleared", "journal": "empty", "accounts": "reset to default"}

@app.get("/reset-accounts")
async def reset_accounts():
    """Delete only accounts from Supabase."""
    from storage import db_set
    import json
    default_accounts = {"default": {"name": "Main", "size": 10000}}
    db_set("accounts", json.dumps(default_accounts))
    return {"status": "accounts reset to default only"}

@app.get("/storage-check")
async def storage_check():
    """Check what keys exist in Supabase."""
    from storage import db_get, has_db
    if not has_db():
        return {"error": "No database configured"}
    keys = ["journal","accounts","outcomes","flow_history","sector_flows"]
    result = {}
    for k in keys:
        val = db_get(k)
        if val:
            try:
                import json as _json
                data = _json.loads(val)
                if isinstance(data, dict):
                    result[k] = {sub: len(v) for sub,v in data.items() if isinstance(v,(list,dict))}
                elif isinstance(data, list):
                    result[k] = len(data)
            except:
                result[k] = len(val)
        else:
            result[k] = "NOT FOUND"
    return result

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

    # Update watchdog timestamp
    global last_webhook_ts
    last_webhook_ts = time.time()

    # Reject webhooks on market holidays
    if not is_market_open():
        status = market_status()
        print(f"[WEBHOOK] Market closed ({status['reason']}) — processing anyway for backtest use")
        # Still process but note it's a holiday

    # Duplicate tweet filter — extract tweet ID from URL
    tweet_id = None
    if tweet_url:
        import re as _re
        m = _re.search(r"/status/(\d+)", tweet_url)
        if m:
            tweet_id = m.group(1)
            if tweet_id in seen_tweet_ids:
                print(f"[WEBHOOK] Duplicate tweet {tweet_id} — skipping")
                return {"status":"duplicate","message":"Already processed"}
            seen_tweet_ids.add(tweet_id)
            # Keep set from growing unbounded
            if len(seen_tweet_ids) > 500:
                seen_tweet_ids.clear()
        print(f"[WEBHOOK] Tweet URL: {tweet_url}")

    import asyncio
    asyncio.create_task(process_alert(tweet, tweet_url, {"source": "flowgod"}))
    ticker = "?"
    try: ticker = re.search(r'\$([A-Z]{1,5})',tweet).group(1)
    except: pass
    print(f"[WEBHOOK] Queued background processing for {ticker}")
    return {"status":"queued","ticker":ticker,"message":"Processing in background — SMS incoming"}

@app.post("/webhook-bullflow")
async def webhook_bullflow(request: Request, background_tasks: BackgroundTasks):
    """
    Bullflow.io webhook endpoint.
    Configure in Bullflow: Alerts -> Webhook -> URL = https://your-app.railway.app/webhook-bullflow
    """
    global last_webhook_ts
    try:
        payload = await request.json()
    except:
        return {"status": "error", "reason": "invalid JSON"}

    last_webhook_ts = time.time()
    print(f"[BULLFLOW] Received payload: {str(payload)[:200]}")

    from prefilter import parse_bullflow_webhook, prefilter

    # Parse Bullflow payload
    flow_data = parse_bullflow_webhook(payload)
    ticker    = flow_data.get("ticker","").upper()

    if not ticker:
        return {"status": "skipped", "reason": "no ticker"}

    # Pre-filter before scoring
    pf_result = prefilter(flow_data)
    if not pf_result["pass"]:
        print(f"[BULLFLOW] {ticker} pre-filtered: {pf_result['reason']}")
        return {"status": "filtered", "ticker": ticker, "reason": pf_result["reason"]}

    # Build tweet-like text for existing pipeline
    premium  = flow_data.get("premium",0)
    prem_str = f"${float(premium)/1000:.0f}K" if premium < 1_000_000 else f"${float(premium)/1_000_000:.1f}M"
    fake_text = (
        f"${ticker} - {prem_str} {flow_data.get('option_type','call').title()} "
        f"{flow_data.get('fill_type','sweep')} [Bullflow]"
    )

    # Queue through existing analysis pipeline
    background_tasks.add_task(
        process_alert,
        fake_text,
        None,  # no tweet URL
        flow_data,  # pre-parsed data
    )

    return {
        "status":      "queued",
        "ticker":      ticker,
        "conviction":  pf_result["conviction"],
        "reasons":     pf_result["conviction_reasons"],
    }

@app.get("/flow-source")
async def flow_source_status():
    """Show current flow source and filter settings."""
    from prefilter import MIN_PREMIUM, MIN_OI, MIN_DTE, MAX_DTE, MAX_OTM_PCT
    return {
        "source":      os.environ.get("FLOW_SOURCE","flowgod"),
        "filters": {
            "min_premium": f"${MIN_PREMIUM:,.0f}",
            "min_oi":      MIN_OI,
            "dte_range":   f"{MIN_DTE}-{MAX_DTE} days",
            "max_otm":     f"{MAX_OTM_PCT}%",
        },
        "switch": "Set FLOW_SOURCE=bullflow or FLOW_SOURCE=flowgod in Railway variables",
    }

@app.get("/check-env")
async def check_env():
    vars_ = [
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_TRADE_CHAT_ID",
        "FINNHUB_API_KEY",
        "TIINGO_API_KEY",
        "POLYGON_API_KEY",
        "SUPABASE_URL",
        "SUPABASE_KEY",
        "TASTY_USERNAME",
        "TASTY_PASSWORD",
        "BASE_URL",
        "ACCOUNT_SIZE",
        "OPTION_WIN_PCT",
        "MAX_POSITIONS",
        "DEDUP_WINDOW_SECS",
        "FLOW_SOURCE",
        "FILTER_MIN_PREMIUM",
        "FILTER_MIN_OI",
        "FILTER_MIN_DTE",
        "FILTER_MAX_DTE",
        "FILTER_MAX_OTM",
        "ROBINHOOD_USERNAME",
        "ROBINHOOD_PASSWORD",
    ]
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

@app.get("/journal-view")
async def journal_page(account: str = None, sort: str = "desc"):
    """Full trade journal as HTML table — filterable by account, downloadable as CSV."""
    try:
        from trade_journal import load_journal, export_journal_csv
        journal  = load_journal()
        accounts = journal.get("accounts", {"default": {"name": "Main", "size": 10000}})
        all_open   = journal.get("trades", [])
        all_closed = journal.get("closed", [])
        missed_t   = journal.get("missed", [])

        # Filter by account if specified
        if account and account != "all":
            open_t   = [t for t in all_open   if t.get("account_id","default") == account]
            closed_t = [t for t in all_closed if t.get("account_id","default") == account]
        else:
            open_t   = all_open
            closed_t = all_closed
    except:
        accounts = {}
        open_t = closed_t = missed_t = []
        all_open = all_closed = []

    def fmt(v, suffix=""):
        if v is None: return "—"
        return str(v) + suffix

    def pnl_color(v):
        if v is None: return ""
        try:
            return "color:#22c55e" if float(v) > 0 else "color:#ef4444"
        except:
            return ""

    def acc_name(aid):
        return accounts.get(aid or "default", {}).get("name", aid or "Main")

    multi_account = len(accounts) > 1

    def _sort_key(t):
        return t.get("entry_datetime") or t.get("entry_date","") + " " + t.get("entry_time","")

    sort_desc = sort != "asc"
    open_t   = sorted(open_t,   key=_sort_key, reverse=sort_desc)
    closed_t = sorted(closed_t, key=_sort_key, reverse=sort_desc)

    # ── Stats dashboard — use filtered closed_t/open_t for account-specific stats ──
    from datetime import datetime as _dt, date as _date
    now_et    = _dt.now(__import__("zoneinfo").ZoneInfo("America/New_York"))
    today_str = now_et.strftime("%Y-%m-%d")

    # Today's closed P&L (filtered)
    closed_today = [t for t in closed_t if t.get("exit_date","") == today_str]
    today_pnl    = sum(float(t.get("pnl_total",0) or 0) for t in closed_today)
    today_wins   = sum(1 for t in closed_today if float(t.get("pnl_total",0) or 0) > 0)
    today_losses = len(closed_today) - today_wins

    # Total open exposure (filtered)
    acct_exposure = {}
    for t in open_t:
        aid  = t.get("account_id","default")
        cost = float(t.get("total_cost",0) or 0)
        acct_exposure[aid] = acct_exposure.get(aid, 0) + cost

    # Total unrealized P&L (filtered)
    total_unreal = sum(float(t.get("unrealized_pnl",0) or 0) for t in open_t)

    # All-time P&L (filtered)
    all_time_pnl  = sum(float(t.get("pnl_total",0) or 0) for t in closed_t)
    all_time_wins = sum(1 for t in closed_t if float(t.get("pnl_total",0) or 0) > 0)
    win_rate      = round(all_time_wins / len(closed_t) * 100) if closed_t else 0

    # Account label for title
    acct_label = (" — " + acc_name(account)) if account and account != "all" else ""

    def stat_card(label, value, color=""):
        c = f"color:{color}" if color else ""
        return (f"<div style='background:#1e293b;border-radius:10px;padding:14px 18px;"
                f"min-width:130px;flex:1'>"
                f"<div style='font-size:11px;color:#94a3b8;margin-bottom:4px'>{label}</div>"
                f"<div style='font-size:18px;font-weight:700;{c}'>{value}</div>"
                f"</div>")

    def money(v, plus=True):
        sign = "+" if v >= 0 and plus else ""
        col  = "#22c55e" if v >= 0 else "#ef4444"
        return f"<span style='color:{col}'>{sign}${round(abs(v),2):,}</span>" if v != 0 else "—"

    # Build account exposure cards
    exposure_cards = ""
    for aid, exp in acct_exposure.items():
        aname = acc_name(aid)
        acc_size = float(accounts.get(aid,{}).get("size",0) or 0)
        pct_str  = f" ({round(exp/acc_size*100,1)}%)" if acc_size > 0 else ""
        exposure_cards += stat_card(aname + " Exposure", f"${exp:,.0f}{pct_str}")

    # Today P&L color
    today_color = "#22c55e" if today_pnl >= 0 else "#ef4444"
    today_sign  = "+" if today_pnl >= 0 else ""

    # Per-account today P&L
    acct_today = {}
    for t in closed_today:
        aid = t.get("account_id","default")
        acct_today[aid] = acct_today.get(aid,0) + float(t.get("pnl_total",0) or 0)

    acct_today_html = ""
    if len(acct_today) > 1:
        for aid, pnl in acct_today.items():
            aname = acc_name(aid)
            sign  = "+" if pnl >= 0 else ""
            col   = "#22c55e" if pnl >= 0 else "#ef4444"
            acct_today_html += (f"<span style='color:{col};font-size:12px;margin-right:10px'>"
                               f"{aname}: {sign}${round(pnl,2):,}</span>")

    unreal_color = "#22c55e" if total_unreal >= 0 else "#ef4444"
    unreal_sign  = "+" if total_unreal >= 0 else ""
    alltime_color = "#22c55e" if all_time_pnl >= 0 else "#ef4444"
    alltime_sign  = "+" if all_time_pnl >= 0 else ""

    stats_dashboard = f"""
<div style='margin:16px 0;display:flex;flex-wrap:wrap;gap:10px;align-items:stretch'>
  {stat_card("Today P&L" + acct_label + " (" + str(len(closed_today)) + ")",
             f"<span style='color:{today_color}'>{today_sign}${abs(round(today_pnl,2)):,}</span>")}
  {stat_card("Today W/L", f"{today_wins}W / {today_losses}L") if closed_today else ""}
  {stat_card("Open Unrealized",
             f"<span style='color:{unreal_color}'>{unreal_sign}${abs(round(total_unreal,2)):,}</span>")}
  {exposure_cards}
  {stat_card("All-Time P&L",
             f"<span style='color:{alltime_color}'>{alltime_sign}${abs(round(all_time_pnl,2)):,}</span>")}
  {stat_card("Win Rate", f"{win_rate}% ({all_time_wins}/{len(all_closed)})")}
</div>
{('<div style="margin-bottom:12px;font-size:13px">' + acct_today_html + '</div>') if acct_today_html else ""}
"""

    rows_open = ""
    for t in open_t:
        otype     = t.get("option_type","call")[0].upper()
        remaining = t.get("contracts_remaining", t.get("contracts","?"))
        ot        = t.get("order_type","BTO") or "BTO"
        if t.get("is_spread") and t.get("spread_type"):
            ss    = t.get("short_strike","?")
            ls    = t.get("long_strike","?")
            is_db = "debit" in t.get("spread_type","")
            stype = t.get("spread_type","").replace("_"," ").upper()
            contract_str = f"{ls}{otype}/{ss}{otype}"
            type_str     = stype
        else:
            contract_str = f"{t.get('strike','')}{otype}"
            type_str     = ot
        tid = str(t.get("id",""))
        acc_disp = acc_name(t.get("account_id","default"))
        lp   = t.get("last_price")
        pct  = t.get("unrealized_pct")
        pnl  = t.get("unrealized_pnl")
        if lp:
            lp_cell  = f"<td data-edit='last_price' data-trade-id='{tid}'>${lp}</td>"
            pnl_sign = "+" if float(pnl or 0) >= 0 else ""
            pnl_col  = "color:#22c55e" if float(pnl or 0) >= 0 else "color:#ef4444"
            pnl_cell = f"<td style='{pnl_col}'>{pnl_sign}{pct}% / ${pnl_sign}{pnl}</td>"
        else:
            lp_cell  = f"<td data-edit='last_price' data-trade-id='{tid}'>—</td>"
            pnl_cell = "<td>—</td>"
        rows_open += (
            "<tr>"
            + (f"<td data-edit='account_id' data-trade-id='{tid}'>{acc_disp}</td>" if multi_account else "") +
            f"<td data-edit='ticker' data-trade-id='{tid}'>{t.get('ticker','')}</td>"
            f"<td data-edit='order_type' data-trade-id='{tid}'>{type_str}</td>"
            f"<td data-edit='strike' data-trade-id='{tid}'>{contract_str}</td>"
            f"<td data-edit='expiry' data-trade-id='{tid}'>{t.get('expiry','')}</td>"
            f"<td data-edit='contracts' data-trade-id='{tid}'>{remaining}/{t.get('contracts','?')}</td>"
            f"<td data-edit='entry_price' data-trade-id='{tid}'>${t.get('entry_price') or t.get('credit','')}</td>"
            + lp_cell + pnl_cell +
            f"<td>${t.get('total_cost','')}</td>"
            f"<td data-edit='entry_date' data-trade-id='{tid}'>{t.get('entry_date','')} {t.get('entry_time','')}</td>"
            f"<td data-edit='fc_score' data-trade-id='{tid}'>{fmt(t.get('fc_score'))}/7 {fmt(t.get('fc_verdict'))}</td>"
            f"<td data-edit='note' data-trade-id='{tid}'>{t.get('note','')}</td>"
            "<td style='white-space:nowrap'><button onclick='closeTrade(\"" + tid + "\")' style='background:#6366f1;color:white;border:none;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:11px;margin-right:4px'>Close</button><button onclick='deleteTrade(\"" + tid + "\",\"open\")' style='background:#ef4444;color:white;border:none;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:11px'>✕</button></td>"
            "</tr>"
        )

    rows_closed = ""
    for t in closed_t:
        otype    = t.get("option_type","call")[0].upper()
        pnl      = t.get("pnl_total")
        pct      = t.get("pnl_pct")
        color    = pnl_color(pnl)
        tags     = " ".join(["#"+tg for tg in t.get("tags",[])])
        ot_c     = t.get("order_type","BTO") or "BTO"
        if t.get("is_spread") and t.get("spread_type"):
            ss_c    = t.get("short_strike","?")
            ls_c    = t.get("long_strike","?")
            stype_c = t.get("spread_type","").replace("_"," ").upper()
            contract_c = f"{ls_c}{otype}/{ss_c}{otype}"
            type_c     = stype_c
        else:
            contract_c = f"{t.get('strike','')}{otype}"
            type_c     = ot_c
        tid_c    = str(t.get("id",""))
        acc_disp_c = acc_name(t.get("account_id","default"))
        rows_closed += (
            "<tr>"
            + (f"<td data-edit='account_id' data-trade-id='{tid_c}'>{acc_disp_c}</td>" if multi_account else "") +
            f"<td>{t.get('ticker','')}</td>"
            f"<td>{type_c}</td>"
            f"<td>{contract_c}</td>"
            f"<td data-edit='expiry' data-trade-id='{tid_c}'>{t.get('expiry','')}</td>"
            f"<td data-edit='contracts' data-trade-id='{tid_c}'>{t.get('contracts','?')}</td>"
            f"<td data-edit='entry_price' data-trade-id='{tid_c}'>${t.get('entry_price','')}</td>"
            f"<td data-edit='exit_price' data-trade-id='{tid_c}'>${t.get('exit_price','')}</td>"
            f"<td data-edit='pnl_total' data-trade-id='{tid_c}' style='{color}'>{fmt(pct,'%')} / ${fmt(pnl)}</td>"
            f"<td>{t.get('entry_date','')} {t.get('entry_time','')}</td>"
            f"<td>{t.get('exit_date','')} {t.get('exit_time','')}</td>"
            f"<td>{fmt(t.get('holding_hours'),'h')}</td>"
            f"<td>{fmt(t.get('peak_pct'),'%')}</td>"
            f"<td>{fmt(t.get('max_drawdown'),'%')}</td>"
            f"<td>{fmt(t.get('left_on_table'),'%')}</td>"
            f"<td data-edit='fc_score' data-trade-id='{tid_c}'>{fmt(t.get('fc_score'))}/7 {fmt(t.get('fc_verdict'))}</td>"
            f"<td>{tags}</td>"
            f"<td data-edit='note' data-trade-id='{tid_c}'>{t.get('note','')}</td>"
            "<td><button onclick='deleteTrade(\"" + tid_c + "\",\"closed\")' style='background:#ef4444;color:white;border:none;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:11px'>✕</button></td>"
            "</tr>"
        )

    try:
        csv_data = export_journal_csv(account)
    except:
        csv_data = ""

    # Filename for download
    csv_filename = "flowcheck_journal_" + (account or "all") + ".csv"

    # Define variables BEFORE using them in sections
    acc_th       = "<th>Account</th>" if multi_account else ""
    open_label   = f"Open Trades ({len(open_t)})"
    closed_label = f"Closed Trades ({len(closed_t)})"
    title_suffix = f" — {acc_name(account)}" if account else ""

    # Account filter tabs
    tabs_html = ""
    if multi_account:
        base_url_j = ""
        try:
            base_url_j = os.environ.get("BASE_URL","").rstrip("/") + "/journal-view"
        except:
            base_url_j = "/journal-view"
        tabs_html = f'<div style="margin:8px 0 16px;display:flex;gap:8px;flex-wrap:wrap">'
        tabs_html += f'<a href="{base_url_j}" style="padding:6px 14px;border-radius:6px;background:{"#3b82f6" if not account else "#1e3a5f"};color:#fff;text-decoration:none;font-size:13px">All</a>'
        for aid, acc_info in accounts.items():
            active = "#3b82f6" if account == aid else "#1e3a5f"
            tabs_html += f'<a href="{base_url_j}?account={aid}" style="padding:6px 14px;border-radius:6px;background:{active};color:#fff;text-decoration:none;font-size:13px">{acc_info.get("name",aid)}</a>'
        tabs_html += "</div>"

    open_section = ""
    if open_t:
        open_section = (
            f"<h2>{open_label}</h2>"
            "<div class='scroll'><table>"
            "<tr>" + acc_th +
            "<th>Ticker</th><th>Type</th><th>Contract</th><th>Expiry</th><th>Qty</th>"
            "<th>Entry $</th><th>Last $</th><th>Open P&amp;L</th>"
            "<th>Cost</th><th>Entry Time</th><th>FlowCheck</th><th>Note</th><th>Actions</th></tr>"
            + rows_open +
            "</table></div>"
        )

    closed_section = ""
    if closed_t:
        closed_section = (
            f"<h2>{closed_label}</h2>"
            "<div class='scroll'><table>"
            "<tr>" + acc_th +
            "<th>Ticker</th><th>Type</th><th>Contract</th><th>Expiry</th><th>Qty</th>"
            "<th>Entry $</th><th>Exit $</th><th>P&amp;L</th>"
            "<th>Entry Time</th><th>Exit Time</th><th>Held</th>"
            "<th>Peak</th><th>Max DD</th><th>Left on Table</th>"
            "<th>FlowCheck</th><th>Tags</th><th>Note</th><th></th></tr>"
            + rows_closed +
            "</table></div>"
        )

    html = f"""<!DOCTYPE html>
<html>
<head><script src='/close.js'></script><script src='/journal.js'></script>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FlowCheck Journal</title>
<style>
body{{font-family:system-ui;background:#0f172a;color:#e2e8f0;margin:0;padding:16px}}
h1{{color:#38bdf8;margin-bottom:4px}}
h2{{color:#38bdf8;margin:24px 0 8px}}
p{{color:#94a3b8;font-size:13px;margin-top:0}}
.scroll{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;min-width:700px;font-size:13px}}
th{{background:#1e3a5f;color:#7dd3fc;padding:8px 10px;text-align:left;white-space:nowrap;position:sticky;top:0}}
td{{padding:7px 10px;border-bottom:1px solid #1e293b;white-space:nowrap}}
tr:hover td{{background:#1e293b}}
[data-edit]{{cursor:pointer}}
[data-edit]:hover{{outline:1px dashed #6366f1;background:rgba(99,102,241,0.1);border-radius:2px}}
.edit-input{{border:1.5px solid #6366f1;border-radius:4px;padding:2px 5px;font-size:12px;background:#1e293b;color:#f1f5f9;width:100%}}
.btn{{display:inline-block;background:#3b82f6;color:#fff;padding:10px 20px;
      border-radius:8px;text-decoration:none;margin:8px 0 16px;
      font-size:14px;cursor:pointer;border:none}}
.btn:hover{{background:#2563eb}}
</style>
</head>
<body>
<h1>FlowCheck Journal{title_suffix}</h1>
<p>{len(closed_t)} closed &nbsp;·&nbsp; {len(open_t)} open &nbsp;·&nbsp; {len(missed_t)} missed</p>
{stats_dashboard}
{tabs_html}
<button class="btn" onclick="downloadCSV()">⬇ Download CSV for Excel</button>
<div style="display:inline-block;margin-left:12px">
  <a href="?account={account or ''}&sort=desc" style="padding:6px 12px;background:#1e3a5f;color:#7dd3fc;border-radius:6px;text-decoration:none;font-size:13px;margin-right:4px">Newest first</a>
  <a href="?account={account or ''}&sort=asc"  style="padding:6px 12px;background:#1e3a5f;color:#7dd3fc;border-radius:6px;text-decoration:none;font-size:13px">Oldest first</a>
</div>
{open_section}
{closed_section}
</body></html>"""

    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)

@app.get("/stats")
async def stats():
    from outcomes import get_stats
    from paper_trading import get_paper_stats
    return {
        "real_outcomes": get_stats(),
        "paper_trading": get_paper_stats(),
    }

@app.get("/positions")
async def positions():
    from exit_signals import get_open_positions
    pos = get_open_positions()
    return {
        "open_positions": len(pos),
        "positions": [
            {
                "ticker":       p.get("ticker"),
                "strike":       p.get("strike"),
                "option_type":  p.get("option_type"),
                "expiry":       p.get("expiry"),
                "entry_stock":  p.get("entry_stock"),
                "entry_option": p.get("entry_option"),
                "stop":         p.get("stop_price"),
                "target":       p.get("target_price"),
                "current_stock":p.get("current_stock"),
                "stock_pnl":    p.get("stock_pnl_pct"),
                "option_pnl":   p.get("option_pnl_pct"),
                "score":        p.get("score"),
                "added":        p.get("added"),
            } for p in pos
        ]
    }

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
            import traceback as _tb
            try:
                print("[BACKTEST] Step 1: extracting trade...")
                trade = extract_trade_from_tweet(tweet, tweet_url)
                if not trade or not trade.get("ticker"):
                    return None, None, "Could not extract trade"
                print(f"[BACKTEST] Step 2: building historical data for {trade.get('ticker')}...")
                data = build_historical_data(trade, tweet_dt)
                print("[BACKTEST] Step 3: scoring...")
                result = score_trade(trade, data, {"count":1,"alert":False})
                print(f"[BACKTEST] Step 4: done — {result.get('verdict')}")
                return trade, data, result
            except Exception as _e:
                print(f"[BACKTEST] Error: {_e}")
                print(_tb.format_exc())
                return None, None, str(_e)

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
