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

    # Carryover
    today_all = load_all_today(analyses)
    watches   = [a for a in today_all if a.get("result",{}).get("verdict")=="WATCH"]
    lines.append("")
    lines.append(f"🔄 CARRYOVER FROM YESTERDAY ({len(watches)})")
    if watches:
        for a in watches[:3]:
            t = a["trade"]
            lines.append(f"  👀 {t.get('ticker')} {t.get('strike')}{t.get('option_type','C')[0].upper()} {t.get('expiry_short','?')}")
    else:
        lines.append("  No open positions from yesterday")

    # Week ahead
    if week:
        lines.append("")
        lines.append("📆 THIS WEEK:")
        lines.extend(week[:3])

    lines.append("")
    lines.append(f"History: {base_url}/history")

    msg = "\n".join(lines)
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
            lines.append(f"  {t.get('ticker')} {t.get('strike')}{t.get('option_type','C')[0].upper()} {t.get('expiry_short','?')} — {a['result'].get('one_liner','')[:50]}")

    if watches:
        lines.append("\n👀 WATCHING:")
        for a in watches[:3]:
            t = a["trade"]
            lines.append(f"  {t.get('ticker')} {t.get('strike')}{t.get('option_type','C')[0].upper()} {t.get('expiry_short','?')}")

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
    """4:15 PM — log OI check reminder."""
    today_all = load_all_today(analyses)
    watches   = [a for a in today_all if a.get("result",{}).get("verdict")=="WATCH"]
    print(f"[EOD-OI] {len(watches)} WATCH positions to review at close")
