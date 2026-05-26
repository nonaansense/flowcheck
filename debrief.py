"""
Feature 1: /debrief — Claude analyzes your trade journal and gives honest feedback.
Uses actual journal data to identify patterns and suggest improvements.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

def generate_debrief() -> str:
    """
    Use Claude to analyze trade journal data and produce honest feedback.
    Returns formatted debrief message.
    """
    from trade_journal import load_journal
    import anthropic, json

    journal  = load_journal()
    closed_t = journal.get("closed", [])
    open_t   = journal.get("trades", [])
    missed_t = journal.get("missed", [])

    if len(closed_t) < 3:
        return (
            "Debrief needs at least 3 closed trades — you have " +
            str(len(closed_t)) + ".\n\n"
            "Keep logging entries and exits and check back after more trades."
        )

    # Build stats for Claude
    wins         = [t for t in closed_t if (t.get("pnl_total",0) or 0) > 0]
    losses       = [t for t in closed_t if (t.get("pnl_total",0) or 0) <= 0]
    win_rate     = round(len(wins)/len(closed_t)*100,1)
    avg_win_pct  = round(sum(t.get("pnl_pct",0) or 0 for t in wins)/len(wins),1) if wins else 0
    avg_loss_pct = round(sum(t.get("pnl_pct",0) or 0 for t in losses)/len(losses),1) if losses else 0
    total_pnl    = round(sum(t.get("pnl_total",0) or 0 for t in closed_t),2)

    # Holding time analysis
    held_hrs     = [t.get("holding_hours",0) or 0 for t in closed_t]
    win_holds    = [t.get("holding_hours",0) or 0 for t in wins]
    loss_holds   = [t.get("holding_hours",0) or 0 for t in losses]
    avg_hold     = round(sum(held_hrs)/len(held_hrs),1) if held_hrs else 0
    avg_win_hold = round(sum(win_holds)/len(win_holds),1) if win_holds else 0
    avg_loss_hold= round(sum(loss_holds)/len(loss_holds),1) if loss_holds else 0

    # Peak analysis
    peaks        = [t.get("peak_pct",0) or 0 for t in closed_t if t.get("peak_pct")]
    lots         = [t.get("left_on_table",0) or 0 for t in closed_t if t.get("left_on_table")]
    dds          = [t.get("max_drawdown",0) or 0 for t in closed_t if t.get("max_drawdown")]
    avg_peak     = round(sum(peaks)/len(peaks),1) if peaks else None
    avg_lot      = round(sum(lots)/len(lots),1) if lots else None
    avg_dd       = round(sum(dds)/len(dds),1) if dds else None

    # Time of day breakdown
    morning  = [t for t in closed_t if t.get("entry_time","") < "11:30"]
    midday   = [t for t in closed_t if "11:30" <= t.get("entry_time","") < "14:00"]
    late     = [t for t in closed_t if t.get("entry_time","") >= "14:00"]
    def wr(lst): return round(sum(1 for t in lst if (t.get("pnl_total",0) or 0)>0)/len(lst)*100,1) if lst else None

    # FlowCheck verdict breakdown
    trade_v = [t for t in closed_t if t.get("fc_verdict") == "TRADE"]
    watch_v = [t for t in closed_t if t.get("fc_verdict") == "WATCH"]
    skip_v  = [t for t in closed_t if t.get("fc_verdict") == "SKIP"]

    # Tags
    tag_stats = {}
    for t in closed_t:
        for tag in t.get("tags",[]):
            tag_stats.setdefault(tag, {"wins":0,"total":0})
            tag_stats[tag]["total"] += 1
            if (t.get("pnl_total",0) or 0) > 0:
                tag_stats[tag]["wins"] += 1

    # Recent trades summary
    recent = []
    for t in closed_t[-10:]:
        otype = t.get("option_type","call")[0].upper()
        recent.append({
            "ticker":  t.get("ticker"),
            "contract": t.get("strike","") + otype + " " + t.get("expiry",""),
            "pnl_pct": t.get("pnl_pct"),
            "hold_h":  t.get("holding_hours"),
            "peak":    t.get("peak_pct"),
            "lot":     t.get("left_on_table"),
            "max_dd":  t.get("max_drawdown"),
            "verdict": t.get("fc_verdict"),
            "tags":    t.get("tags",[]),
        })

    data_summary = {
        "total_trades":     len(closed_t),
        "win_rate":         win_rate,
        "total_pnl":        total_pnl,
        "avg_win_pct":      avg_win_pct,
        "avg_loss_pct":     avg_loss_pct,
        "avg_hold_hours":   avg_hold,
        "avg_win_hold":     avg_win_hold,
        "avg_loss_hold":    avg_loss_hold,
        "avg_peak_gain":    avg_peak,
        "avg_left_on_table": avg_lot,
        "avg_max_drawdown": avg_dd,
        "morning_wr":       wr(morning),
        "morning_count":    len(morning),
        "midday_wr":        wr(midday),
        "midday_count":     len(midday),
        "late_wr":          wr(late),
        "late_count":       len(late),
        "trade_verdict_wr": wr(trade_v),
        "trade_verdict_count": len(trade_v),
        "watch_verdict_wr": wr(watch_v),
        "watch_verdict_count": len(watch_v),
        "missed_count":     len(missed_t),
        "tag_stats":        tag_stats,
        "recent_trades":    recent,
    }

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))

    prompt = (
        "You are analyzing a trader's options trading journal. "
        "Be direct, honest, and specific. No generic advice. "
        "Focus on what the data actually shows.\n\n"
        "TRADE DATA:\n" + json.dumps(data_summary, indent=2) + "\n\n"
        "Write a concise debrief (max 300 words) covering:\n"
        "1. ONE key strength (what they're doing well)\n"
        "2. ONE critical weakness (most impactful thing to fix)\n"
        "3. ONE specific actionable change for next week\n\n"
        "Use the actual numbers. Be direct. No fluff."
    )

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        analysis = resp.content[0].text.strip()
    except Exception as e:
        analysis = "AI analysis unavailable: " + str(e)

    now_et = datetime.now(ZoneInfo("America/New_York"))
    lines  = [
        "📊 TRADE DEBRIEF — " + now_et.strftime("%b %d, %Y"),
        str(len(closed_t)) + " trades | " + str(win_rate) + "% win rate | $" + str(total_pnl),
        "",
        analysis,
    ]

    if avg_lot is not None:
        lines += ["", "Key stats:"]
        lines.append("  Avg peak gain: +" + str(avg_peak) + "%")
        lines.append("  Avg exit gain: +" + str(round(avg_win_pct,1)) + "%")
        lines.append("  Avg left on table: " + str(avg_lot) + "%")
        if avg_dd:
            lines.append("  Avg max drawdown held: -" + str(avg_dd) + "%")

    return chr(10).join(lines)
