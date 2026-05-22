"""
Outcome tracking for FlowCheck.
Runs at 4:00 PM ET daily to check if WATCH/TRADE calls were correct.
Compares stock price at time of flow vs closing price.
Builds win rate statistics over time.
"""
import json, os
from datetime import datetime
from zoneinfo import ZoneInfo
from fetcher import fetch_price
from sms import send_sms

OUTCOMES_FILE = "/tmp/flowcheck_outcomes.json"

def load_outcomes() -> dict:
    try:
        with open(OUTCOMES_FILE) as f:
            return json.load(f)
    except:
        return {"history": [], "stats": {}}

def save_outcomes(data: dict):
    try:
        with open(OUTCOMES_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[OUTCOMES] Save error: {e}")

def track_outcomes(analyses: list):
    """
    Called at 4:00 PM ET. Checks closing price vs flow entry price
    for all WATCH/TRADE alerts from today.
    """
    now_et    = datetime.now(ZoneInfo("America/New_York"))
    today_str = now_et.strftime("%Y-%m-%d")

    # Get today's WATCH/TRADE alerts
    tracked = [
        a for a in analyses
        if a.get("date") == today_str
        and a.get("result", {}).get("verdict") in ("WATCH", "TRADE")
        and a.get("data", {}).get("stock_price")  # Must have entry price
    ]

    if not tracked:
        print("[OUTCOMES] No WATCH/TRADE alerts to track today")
        return

    print(f"[OUTCOMES] Tracking {len(tracked)} alerts...")
    outcomes     = load_outcomes()
    today_results = []

    for a in tracked:
        ticker      = a["trade"].get("ticker")
        entry_price = a["data"].get("stock_price")
        verdict     = a["result"].get("verdict")
        score       = a["result"].get("final_score", 0)
        alert_time  = a.get("time", "?")

        if not ticker or not entry_price:
            continue

        # Get current closing price
        close_price = fetch_price(ticker)
        if not close_price:
            print(f"[OUTCOMES] Could not get closing price for {ticker}")
            continue

        pct_move    = round(((close_price - entry_price) / entry_price) * 100, 2)
        is_win      = pct_move > 0  # Stock closed higher than flow entry
        is_big_win  = pct_move > 3
        is_big_loss = pct_move < -3

        result = {
            "date":        today_str,
            "ticker":      ticker,
            "verdict":     verdict,
            "score":       score,
            "alert_time":  alert_time,
            "entry_price": entry_price,
            "close_price": close_price,
            "pct_move":    pct_move,
            "is_win":      is_win,
        }

        outcomes["history"].append(result)
        today_results.append(result)

        emoji = "✅" if is_big_win else "📈" if is_win else "❌" if is_big_loss else "📉"
        print(f"[OUTCOMES] {ticker} {verdict}: entry=${entry_price} close=${close_price} "
              f"{pct_move:+.1f}% {emoji}")

    save_outcomes(outcomes)

    # Calculate running stats
    history = outcomes["history"]
    if len(history) >= 3:
        all_wins   = sum(1 for r in history if r["is_win"])
        win_rate   = round((all_wins / len(history)) * 100, 1)

        trade_hist = [r for r in history if r["verdict"] == "TRADE"]
        watch_hist = [r for r in history if r["verdict"] == "WATCH"]

        trade_wr = round(sum(1 for r in trade_hist if r["is_win"]) / len(trade_hist) * 100, 1) if trade_hist else 0
        watch_wr = round(sum(1 for r in watch_hist if r["is_win"]) / len(watch_hist) * 100, 1) if watch_hist else 0

        avg_move = round(sum(r["pct_move"] for r in history) / len(history), 2)

        # Send daily outcome summary
        if today_results:
            lines = [f"📊 FlowCheck Outcomes — {now_et.strftime('%a %b %d')}"]
            lines.append("")
            lines.append(f"Today's results:")
            for r in today_results:
                emoji = "✅" if r["pct_move"] > 0 else "❌"
                lines.append(f"  {emoji} {r['ticker']} {r['verdict']}: {r['pct_move']:+.1f}%")
            lines.append("")
            lines.append(f"📈 Running stats ({len(history)} alerts):")
            lines.append(f"  Overall win rate: {win_rate}%")
            lines.append(f"  TRADE win rate: {trade_wr}% ({len(trade_hist)} alerts)")
            lines.append(f"  WATCH win rate: {watch_wr}% ({len(watch_hist)} alerts)")
            lines.append(f"  Avg stock move: {avg_move:+.2f}%")

            send_sms("\n".join(lines))
            print(f"[OUTCOMES] Summary sent — {win_rate}% overall win rate")

def get_stats() -> dict:
    """Return current win rate stats."""
    outcomes = load_outcomes()
    history  = outcomes.get("history", [])
    if not history:
        return {"total": 0, "win_rate": 0, "avg_move": 0}

    wins     = sum(1 for r in history if r["is_win"])
    avg_move = sum(r["pct_move"] for r in history) / len(history)

    trade_hist = [r for r in history if r["verdict"] == "TRADE"]
    watch_hist = [r for r in history if r["verdict"] == "WATCH"]

    return {
        "total":       len(history),
        "win_rate":    round(wins / len(history) * 100, 1),
        "avg_move":    round(avg_move, 2),
        "trade_count": len(trade_hist),
        "trade_wr":    round(sum(1 for r in trade_hist if r["is_win"]) / len(trade_hist) * 100, 1) if trade_hist else 0,
        "watch_count": len(watch_hist),
        "watch_wr":    round(sum(1 for r in watch_hist if r["is_win"]) / len(watch_hist) * 100, 1) if watch_hist else 0,
        "last_7_days": [r for r in history[-7:]]
    }
