"""
Weekly performance report for FlowCheck.
Fires every Friday at 4:45 PM ET.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from outcomes import load_outcomes
from sms import send_sms

def send_weekly_report():
    """Send weekly performance summary every Friday EOD."""
    now_et    = datetime.now(ZoneInfo("America/New_York"))
    week_ago  = (now_et - timedelta(days=7)).strftime("%Y-%m-%d")
    today_str = now_et.strftime("%Y-%m-%d")

    outcomes = load_outcomes()
    history  = outcomes.get("history", [])

    # Filter to this week
    week_hist = [r for r in history
                 if week_ago <= r.get("date","") <= today_str]

    if not week_hist:
        print("[WEEKLY] No data for this week")
        return

    # Stats
    wins       = sum(1 for r in week_hist if r["is_win"])
    win_rate   = round(wins/len(week_hist)*100, 1)
    avg_stock  = round(sum(r["stock_pct"] for r in week_hist)/len(week_hist), 2)

    opt_hist   = [r for r in week_hist if r.get("option_pct") is not None]
    avg_opt    = round(sum(r["option_pct"] for r in opt_hist)/len(opt_hist), 1) if opt_hist else None

    trade_h    = [r for r in week_hist if r["verdict"]=="TRADE"]
    watch_h    = [r for r in week_hist if r["verdict"]=="WATCH"]
    skip_h     = [r for r in week_hist if r["verdict"]=="SKIP"]

    trade_wr   = round(sum(1 for r in trade_h if r["is_win"])/len(trade_h)*100,1) if trade_h else 0
    watch_wr   = round(sum(1 for r in watch_h if r["is_win"])/len(watch_h)*100,1) if watch_h else 0

    # Best and worst
    sorted_by_stock = sorted(week_hist, key=lambda x: x["stock_pct"], reverse=True)
    best  = sorted_by_stock[:3]
    worst = sorted_by_stock[-3:]

    # By time of day
    morning  = [r for r in week_hist if r.get("alert_time","") < "11:30"]
    midday   = [r for r in week_hist if "11:30" <= r.get("alert_time","") < "14:00"]
    late     = [r for r in week_hist if r.get("alert_time","") >= "14:00"]

    def wr(lst):
        if not lst: return "N/A"
        return f"{round(sum(1 for r in lst if r['is_win'])/len(lst)*100,1)}%"

    lines = [
        f"📊 <b>FlowCheck Weekly Report</b>",
        f"Week of {now_et.strftime('%b %d, %Y')}",
        "",
        f"Total alerts: {len(week_hist)}",
        f"  ✅ TRADE: {len(trade_h)} | 👀 WATCH: {len(watch_h)} | ❌ SKIP: {len(skip_h)}",
        "",
        f"📈 Win Rates:",
        f"  Overall: {win_rate}% ({wins}/{len(week_hist)})",
        f"  TRADE: {trade_wr}% | WATCH: {watch_wr}%",
        f"  Avg stock move: {avg_stock:+.2f}%",
    ]

    if avg_opt is not None:
        lines.append(f"  Avg option move: {avg_opt:+.1f}% ({len(opt_hist)} tracked)")

    lines += [
        "",
        f"⏰ By Time of Day:",
        f"  9:30-11:30 AM: {wr(morning)} ({len(morning)} alerts)",
        f"  11:30-2:00 PM: {wr(midday)} ({len(midday)} alerts)",
        f"  2:00 PM+:      {wr(late)} ({len(late)} alerts)",
        "",
        f"🏆 Best Calls:",
    ]

    for r in best:
        opt_str = f" | Opt: {r['option_pct']:+.1f}%" if r.get("option_pct") else ""
        lines.append(f"  ✅ {r['ticker']} {r['verdict']}: {r['stock_pct']:+.1f}%{opt_str}")

    lines += ["", "💀 Worst Calls:"]
    for r in worst:
        opt_str = f" | Opt: {r['option_pct']:+.1f}%" if r.get("option_pct") else ""
        lines.append(f"  ❌ {r['ticker']} {r['verdict']}: {r['stock_pct']:+.1f}%{opt_str}")

    send_sms("\n".join(lines))
    print(f"[WEEKLY] Report sent — {win_rate}% win rate, {len(week_hist)} alerts")
