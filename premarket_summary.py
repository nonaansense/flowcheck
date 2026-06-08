"""Pre-market (8 AM) and EOD (4:30 PM) summary messages."""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sms import send_sms
from economic_calendar import get_today_warnings, get_week_ahead

ANALYSES_FILE = "/tmp/flowcheck_analyses.json"

def get_tomorrow_str() -> str:
    now = datetime.now(ZoneInfo("America/New_York"))
    for i in range(1,8):
        d = now + timedelta(days=i)
        if d.weekday() < 5:
            return d.strftime("%A %b %d")
    return "Next trading day"

def load_all_today(in_memory: list) -> list:
    """Merge in-memory + disk analyses for accurate daily count."""
    today_str  = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    all_items  = list(in_memory)
    try:
        with open(ANALYSES_FILE) as f:
            saved = json.load(f)
        if saved.get("date") == today_str:
            existing_ids = {a.get("id") for a in all_items}
            for a in saved.get("analyses",[]):
                if a.get("id") not in existing_ids:
                    all_items.append(a)
    except:
        pass
    return [a for a in all_items if a.get("date") == today_str]


def load_yesterday_analyses() -> list:
    """Load yesterday's analyses from Supabase for carryover check."""
    from storage import load_data, db_get
    import json as _j
    yesterday = (datetime.now(ZoneInfo("America/New_York")).date() -
                 __import__("datetime").timedelta(days=1)).isoformat()
    # Try analyses_yesterday key first (saved by save_analyses)
    try:
        raw = db_get("analyses_yesterday")
        if raw:
            data = _j.loads(raw) if isinstance(raw, str) else raw
            items = data.get("analyses", [])
            if items:
                print(f"[PERSIST] Loaded {len(items)} yesterday analyses from Supabase")
                return [a for a in items if isinstance(a, dict)]
    except Exception as e:
        print(f"[PERSIST] Yesterday load error: {e}")

    # Fallback: try analyses_today key with yesterday's date
    try:
        data = load_data("analyses_today", ANALYSES_FILE, {"date":"","analyses":[]})
        if data.get("date") == yesterday:
            items = data.get("analyses", [])
            return [a for a in items if isinstance(a, dict)]
    except:
        pass
    return []

def send_premarket_summary(analyses: list):
    now_et    = datetime.now(ZoneInfo("America/New_York"))
    day_label = now_et.strftime("%a %b %d")
    base_url  = __import__("os").environ.get("BASE_URL","https://flowcheck-production.up.railway.app")

    warnings = get_today_warnings()
    week     = get_week_ahead()

    lines = [f"⚡ FlowCheck Pre-Market — {day_label}",
             f"Options open 9:30 AM · First clean window 10:00 AM",
             "─" * 20, ""]

    # Macro
    lines.append("📅 TODAY'S MACRO")
    if warnings["events_today"]:
        for s in warnings["events_summary"]:
            lines.append(s)
        if warnings["avoid_buying"]:
            lines.append(f"⚠️ Avoid new entries before {warnings['avoid_until']}")
    else:
        lines.append("✅ No major macro events today — clean trading day.")

    # Carryover — use live watchlist (persists across days, always current)
    try:
        from technical import get_watchlist as _gwl_pm
        _wl_pm = _gwl_pm()
        watches_wl = list(_wl_pm.values()) if _wl_pm else []
    except:
        watches_wl = []
    lines.append("")
    lines.append(f"🔄 ACTIVE WATCHLIST ({len(watches_wl)} positions)")

    watches = watches_wl  # alias for display section below
    if watches:
        # Check OI for each carryover via Tradier
        oi_lines = []
        for a in watches[:5]:
            t          = a.get("trade",{})
            ticker     = t.get("ticker","")
            strike     = str(t.get("strike",""))
            opt_type   = t.get("option_type","call")
            expiry     = t.get("expiry_raw","") or t.get("expiry","")
            orig_oi    = int(a.get("data",{}).get("open_interest",0) or 0)
            verdict    = a.get("result",{}).get("verdict","")
            emoji      = "✅" if verdict == "TRADE" else "👀"

            oi_str = ""
            print(f"[PREMARKET OI] {ticker} {strike} expiry={expiry} orig_oi={orig_oi}")
            if ticker and strike and expiry:  # Fetch OI even without baseline
                try:
                    from fetcher import get_option_chain_oi
                    current_oi = get_option_chain_oi(ticker, strike, opt_type, expiry)
                    if current_oi is not None:
                        if orig_oi > 0:
                            oi_change = current_oi - orig_oi
                            oi_pct    = round((oi_change / orig_oi) * 100, 1)
                            if oi_change < -orig_oi * 0.20:
                                oi_str = f" ⚠️ OI -{abs(oi_pct)}% ({orig_oi}→{current_oi}) likely day trade"
                            elif oi_change > 0:
                                oi_str = f" ✅ OI +{oi_pct}% ({orig_oi}→{current_oi}) held overnight"
                            else:
                                oi_str = f" OI unchanged ({current_oi})"
                        else:
                            oi_str = f" OI: {current_oi} (no baseline)"
                except Exception as e:
                    print(f"[PREMARKET] OI check error for {ticker}: {e}")

            exp_disp = (t.get("expiry_short") or t.get("expiry","?") or "?")
            if exp_disp in ("None","none",""): exp_disp = t.get("expiry","?")
            lines.append(
                f"  {emoji} {ticker} {strike}{opt_type[0].upper()} "
                f"{exp_disp}{oi_str}"
            )
    else:
        lines.append("  No carryover from yesterday")

    # Week ahead
    if week:
        lines.append("")
        lines.append("📆 THIS WEEK:")
        lines.extend(week[:3])

    lines.append("")
    lines.append(f"History: {base_url}/history")

    msg = "\n".join(lines)
    # Haiku market brief
    try:
        import os as _os2, anthropic as _ant2
        _cli2 = _ant2.Anthropic(api_key=_os2.environ.get("ANTHROPIC_API_KEY",""))
        _vx2  = ""
        try:
            from fetcher import fetch_vix as _fv2
            _v2 = _fv2()
            _vx2 = f"VIX {round(_v2,1)}" if _v2 else ""
        except: pass
        _bp  = (f"Today is {now_et.strftime('%A %B %d, %Y')}. {_vx2}. "
                f"In 2 sentences, give a concise setup brief for an options flow trader today. No disclaimers.")
        _r2  = _ant2.Anthropic(api_key=_os2.environ.get("ANTHROPIC_API_KEY","")).messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=80,
            messages=[{"role":"user","content":_bp}])
        _b2  = _r2.content[0].text.strip()
        # Strip markdown headers/formatting Haiku sometimes adds
        import re as _re2
        _b2 = _re2.sub(r'^[#]+\s+', '', _b2, flags=_re2.MULTILINE)
        _b2 = _re2.sub(r'\*\*(.+?)\*\*', r'\1', _b2)              # remove **bold**
        _b2 = _re2.sub(r'\*(.+?)\*', r'\1', _b2)                  # remove *italic*
        _b2 = _b2.strip()
        if _b2:
            msg += chr(10) + chr(10) + "🤖 " + _b2
    except Exception as _be2:
        print(f"[PREMARKET] Haiku brief error: {_be2}")

    print(f"[PREMARKET] Sending pre-market summary ({len(msg)} chars)")
    send_sms(msg)

def send_eod_summary(analyses: list):
    now_et    = datetime.now(ZoneInfo("America/New_York"))
    day_label = now_et.strftime("%a %b %d")
    base_url  = __import__("os").environ.get("BASE_URL","https://flowcheck-production.up.railway.app")

    today_all = load_all_today(analyses)
    trades    = [a for a in today_all if a.get("result",{}).get("verdict")=="TRADE"]
    watches   = [a for a in today_all if a.get("result",{}).get("verdict")=="WATCH"]
    skips     = [a for a in today_all if a.get("result",{}).get("verdict")=="SKIP"]

    # Tomorrow macro
    warnings  = get_today_warnings()
    week      = get_week_ahead()
    tmw_label = get_tomorrow_str()

    lines = [f"⚡ FlowCheck EOD — {day_label}", "─"*20, ""]
    lines.append(f"📊 TODAY: {len(today_all)} alerts · ✅{len(trades)} TRADE · 👀{len(watches)} WATCH · ❌{len(skips)} SKIP")

    if trades:
        lines.append("\n✅ TRADES:")
        for a in trades[:3]:
            t = a["trade"]
            exp2 = t.get('expiry_short') or t.get('expiry','?')
            lines.append(f"  {t.get('ticker')} {t.get('strike')}{t.get('option_type','C')[0].upper()} {exp2} — {a['result'].get('one_liner','')[:50]}")

    if watches:
        lines.append("\n👀 WATCHING:")
        for a in watches[:3]:
            t = a["trade"]
            exp3 = t.get('expiry_short') or t.get('expiry','?')
            lines.append(f"  {t.get('ticker')} {t.get('strike')}{t.get('option_type','C')[0].upper()} {exp3}")

    lines.append("")
    lines.append(f"📅 TOMORROW: {tmw_label}")
    if week:
        lines.extend(week[:2])
    else:
        lines.append("  No major macro events")

    lines.append("")
    lines.append(f"Full history: {base_url}/history")

    msg = "\n".join(lines)
    print(f"[EOD] Sending EOD summary ({len(msg)} chars) — {len(today_all)} total alerts today")
    send_sms(msg)

def verify_eod_positions(analyses: list):
    """
    4:15 PM — verify OI on today's TRADE alerts via Tradier.
    Checks if open interest increased confirming flow was real positioning.
    Sends Telegram alert for any confirmed or suspicious signals.
    """
    import os, requests
    from sms import send_sms
    from zoneinfo import ZoneInfo

    today_all  = load_all_today(analyses)
    trades     = [a for a in today_all if a.get("result",{}).get("verdict") == "TRADE"]

    if not trades:
        print("[EOD-OI] No TRADE alerts today to verify")
        return

    tradier_token = os.environ.get("TRADIER_TOKEN","")
    if not tradier_token:
        print("[EOD-OI] No TRADIER_TOKEN — skipping OI verification")
        return

    print(f"[EOD-OI] Verifying OI for {len(trades)} TRADE alerts...")
    confirmations = []
    warnings      = []

    for a in trades[:10]:
        t      = a.get("trade",{})
        ticker = t.get("ticker","")
        strike = t.get("strike","")
        expiry = t.get("expiry","")
        otype  = (t.get("option_type","call") or "call").lower()
        orig_oi = float(t.get("open_interest",0) or 0)
        orig_vol = float(t.get("vol_oi_ratio",0) or 0)
        score  = a.get("result",{}).get("final_score","?")

        if not ticker or not strike or not expiry:
            continue

        try:
            # Convert expiry to YYYY-MM-DD
            from datetime import datetime as _dt
            exp_str = None
            for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
                try:
                    exp_str = _dt.strptime(expiry.strip(), fmt).strftime("%Y-%m-%d")
                    break
                except: pass
            if not exp_str:
                continue

            r = requests.get(
                "https://api.tradier.com/v1/markets/options/chains",
                params={"symbol": ticker, "expiration": exp_str, "greeks": "false"},
                headers={"Authorization": f"Bearer {tradier_token}", "Accept": "application/json"},
                timeout=10
            )
            if r.status_code != 200:
                print(f"[EOD-OI] Tradier {r.status_code} for {ticker}")
                continue

            options = (r.json().get("options") or {}).get("option") or []
            strike_f = float(strike)
            for opt in options:
                if (abs(float(opt.get("strike",0)) - strike_f) < 0.01 and
                    opt.get("option_type","").lower() == otype):
                    eod_oi  = int(opt.get("open_interest",0) or 0)
                    eod_vol = int(opt.get("volume",0) or 0)
                    oi_diff = eod_oi - orig_oi if orig_oi > 0 else 0
                    oi_pct  = round(oi_diff / orig_oi * 100, 1) if orig_oi > 0 else 0

                    line = (f"{ticker} {strike}{otype[0].upper()} {expiry} | "
                            f"OI: {int(orig_oi):,} → {eod_oi:,} ({'+' if oi_diff>=0 else ''}{oi_diff:,}) | "
                            f"Vol: {eod_vol:,} | Score: {score}/7")

                    if oi_diff > 100:
                        confirmations.append(f"✅ {line} — OI CONFIRMED new positioning")
                    elif oi_diff < -100:
                        warnings.append(f"⚠️ {line} — OI DECREASED (may be roll/close)")
                    else:
                        print(f"[EOD-OI] {line} — OI flat, inconclusive")
                    break

        except Exception as e:
            print(f"[EOD-OI] Error for {ticker}: {e}")

    # Send Telegram summary
    if confirmations or warnings:
        lines = [f"📊 EOD OI Verification — {len(trades)} trades checked"]
        lines.extend(confirmations)
        lines.extend(warnings)
        msg = chr(10).join(lines)
        send_sms(msg)
        print(f"[EOD-OI] Sent verification for {len(confirmations)} confirmed, {len(warnings)} warnings")
    else:
        print(f"[EOD-OI] OI verification complete — no significant changes")
