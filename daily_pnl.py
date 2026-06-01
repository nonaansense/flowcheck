"""
Feature 3: Daily P&L summary sent at 4:10 PM ET Mon-Fri.
Shows closed P&L for today + unrealized P&L on open positions.
"""
import os
from datetime import datetime, date
from zoneinfo import ZoneInfo

def get_daily_pnl_summary() -> str:
    from trade_journal import load_journal
    from fetcher import fetch_price as get_current_price

    journal  = load_journal()
    closed_t = journal.get("closed", [])
    open_t   = journal.get("trades", [])
    accounts = journal.get("accounts", {})
    now_et   = datetime.now(ZoneInfo("America/New_York"))
    today    = now_et.strftime("%Y-%m-%d")

    # Closed trades today
    closed_today = [
        t for t in closed_t
        if t.get("exit_date","") == today
    ]

    # Open positions with unrealized P&L
    unrealized = []
    for t in open_t:
        ticker = t.get("ticker","")
        if not ticker:
            continue
        try:
            current = get_current_price(ticker)
            if current:
                entry  = float(t.get("entry_price",0) or 0)
                contr  = int(t.get("contracts_remaining") or t.get("contracts",1))
                if entry > 0:
                    pct    = round(((current - entry) / entry) * 100, 1)
                    dollar = round((current - entry) * contr * 100, 2)
                    unrealized.append({
                        "ticker":  ticker,
                        "pct":     pct,
                        "dollar":  dollar,
                        "current": current,
                        "account": t.get("account_id","default"),
                    })
        except:
            pass

    if not closed_today and not unrealized:
        return ""

    lines = ["📊 Daily P&L — " + now_et.strftime("%b %d")]
    lines.append("")

    # Closed today
    if closed_today:
        total_closed = sum(t.get("pnl_total",0) or 0 for t in closed_today)
        lines.append("CLOSED TODAY")
        for t in closed_today:
            pnl  = t.get("pnl_total",0) or 0
            pct  = t.get("pnl_pct",0) or 0
            sign = "+" if pnl >= 0 else ""
            emoji = "✅" if pnl > 0 else "❌"
            acc  = accounts.get(t.get("account_id","default"),{}).get("name","")
            lines.append(
                emoji + " " + t.get("ticker","") + ": " +
                sign + "$" + str(round(pnl,2)) +
                " (" + sign + str(round(pct,1)) + "%)" +
                (" [" + acc + "]" if acc else "")
            )
        sign = "+" if total_closed >= 0 else ""
        lines.append("Total closed: " + sign + "$" + str(round(total_closed,2)))
        lines.append("")

    # Open unrealized
    if unrealized:
        total_unreal = sum(u["dollar"] for u in unrealized)
        lines.append("OPEN (unrealized)")
        for u in unrealized:
            sign  = "+" if u["dollar"] >= 0 else ""
            emoji = "🟢" if u["dollar"] > 0 else "🔴"
            acc   = accounts.get(u["account"],"").get("name","") if isinstance(accounts.get(u["account"]), dict) else ""
            lines.append(
                emoji + " " + u["ticker"] + ": " +
                sign + "$" + str(u["dollar"]) +
                " (" + sign + str(u["pct"]) + "%)" +
                (" [" + acc + "]" if acc else "")
            )
        sign = "+" if total_unreal >= 0 else ""
        lines.append("Total unrealized: " + sign + "$" + str(round(total_unreal,2)))

    return chr(10).join(lines)

def send_daily_pnl(send_sms_fn):
    from market_calendar import is_market_open
    if not is_market_open():
        return
    try:
        msg = get_daily_pnl_summary()
        if msg:
            send_sms_fn(msg)
            print("[DAILY PNL] Sent")
    except Exception as e:
        print(f"[DAILY PNL] Error: {e}")
