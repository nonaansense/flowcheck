"""
Paper trading mode for FlowCheck.
Tracks hypothetical entries based on FlowCheck verdicts.
Compare what you actually traded vs what FlowCheck recommended.
"""
import json, os
from datetime import datetime
from zoneinfo import ZoneInfo

PAPER_FILE = "/tmp/flowcheck_paper.json"

def load_paper() -> dict:
    try:
        with open(PAPER_FILE) as f:
            return json.load(f)
    except:
        return {"trades": [], "stats": {}}

def save_paper(data: dict):
    try:
        with open(PAPER_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[PAPER] Save error: {e}")

def add_paper_trade(trade: dict, data: dict, result: dict):
    """Auto-track hypothetical entry for every TRADE verdict."""
    if result.get("verdict") != "TRADE":
        return
    paper = load_paper()
    entry = {
        "id":           len(paper["trades"]),
        "date":         datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"),
        "time":         datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M"),
        "ticker":       trade.get("ticker"),
        "strike":       trade.get("strike"),
        "option_type":  trade.get("option_type","call"),
        "expiry":       trade.get("expiry"),
        "expiry_raw":   trade.get("expiry_raw",""),
        "entry_stock":  data.get("stock_price"),
        "entry_option": trade.get("option_price"),
        "score":        result.get("final_score"),
        "status":       "OPEN",
        "close_stock":  None,
        "close_option": None,
        "stock_pnl":    None,
        "option_pnl":   None,
        "is_win":       None,
    }
    paper["trades"].append(entry)
    save_paper(paper)
    print(f"[PAPER] Tracking hypothetical: {trade.get('ticker')} {trade.get('strike')}")

def update_paper_outcomes():
    """Called at 4 PM — update paper trade outcomes."""
    from fetcher import fetch_price
    paper  = load_paper()
    today  = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    changed = False

    for t in paper["trades"]:
        if t.get("status") != "OPEN":
            continue
        if t.get("date") != today:
            continue
        ticker = t.get("ticker")
        close  = fetch_price(ticker)
        if close and t.get("entry_stock"):
            pnl = round(((close - float(t["entry_stock"])) / float(t["entry_stock"])) * 100, 2)
            t["close_stock"] = close
            t["stock_pnl"]   = pnl
            t["is_win"]      = pnl >= 5.0  # 5% stock move = approx 50% option gain
            changed = True

    if changed:
        save_paper(paper)
        # Recalculate stats
        closed    = [t for t in paper["trades"] if t.get("is_win") is not None]
        wins      = sum(1 for t in closed if t["is_win"])
        win_rate  = round(wins/len(closed)*100,1) if closed else 0
        avg_move  = round(sum(t["stock_pnl"] for t in closed if t.get("stock_pnl"))/len(closed),2) if closed else 0
        paper["stats"] = {
            "total":    len(closed),
            "wins":     wins,
            "win_rate": win_rate,
            "avg_move": avg_move,
        }
        save_paper(paper)
        print(f"[PAPER] Updated {len(closed)} paper trades — {win_rate}% win rate")

def get_paper_stats() -> dict:
    return load_paper().get("stats", {})
