"""
Telegram command handler for FlowCheck.
Allows controlling the system directly from Telegram.

Commands:
/watchlist — show active watches
/positions — show open positions
/stats     — win rate summary
/close TICKER — close a position
/backtest URL TIME — trigger backtest
/help — show all commands
"""
import os, requests, threading, time
from datetime import datetime
from zoneinfo import ZoneInfo

def bot_token():
    return os.environ.get("TELEGRAM_BOT_TOKEN")

def chat_id():
    return os.environ.get("TELEGRAM_CHAT_ID")

def send_reply(text: str, reply_chat_id: str = None):
    token = bot_token()
    cid   = reply_chat_id or chat_id()
    if not token or not cid:
        return
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": cid, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=10
    )

_last_update_id = 0

def get_updates() -> list:
    global _last_update_id
    token = bot_token()
    if not token:
        return []
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": _last_update_id + 1, "timeout": 5, "limit": 10},
            timeout=10
        )
        if r.status_code == 200:
            updates = r.json().get("result", [])
            if updates:
                _last_update_id = updates[-1]["update_id"]
            return updates
    except Exception as e:
        print(f"[CMD] getUpdates error: {e}")
    return []

def handle_command(text: str, from_chat_id: str):
    text = text.strip()
    cmd  = text.split()[0].lower().lstrip("/")
    args = text.split()[1:] if len(text.split()) > 1 else []

    print(f"[CMD] Command: {cmd} {args}")

    if cmd in ("watchlist", "watch"):
        handle_watchlist(from_chat_id)
    elif cmd in ("positions", "pos"):
        handle_positions(from_chat_id)
    elif cmd == "portfolio":
        handle_portfolio(from_chat_id)
    elif cmd == "stats":
        handle_stats(from_chat_id)
    elif cmd == "close" and args:
        handle_close(args[0].upper(), from_chat_id)
    elif cmd == "backtest" and len(args) >= 2:
        handle_backtest(args[0], args[1], from_chat_id)
    elif cmd == "history":
        handle_history(from_chat_id)
    elif cmd == "find" and args:
        handle_find(args[0].upper(), from_chat_id)
    elif cmd in ("help", "start"):
        handle_help(from_chat_id)
    else:
        send_reply(
            "Unknown command. Send /help for list of commands.",
            from_chat_id
        )

def handle_watchlist(reply_chat_id: str):
    try:
        from technical import get_watchlist
        wl = get_watchlist()
        if not wl:
            send_reply("📋 No active watches right now.", reply_chat_id)
            return
        lines = [f"📋 Active Watchlist ({len(wl)} tickers)", ""]
        for ticker, e in wl.items():
            dte     = e.get("dte_remaining","?")
            verdict = e.get("verdict","?")
            score   = e.get("flow_score","?")
            strike  = e.get("strike","?")
            otype   = e.get("option_type","call")[0].upper()
            expiry  = e.get("expiry","?")
            age_h   = int((time.time()-e.get("added",time.time()))/3600)
            v_emoji = "✅" if verdict=="TRADE" else "👀"
            lines.append(f"{v_emoji} <b>{ticker}</b> {strike}{otype} {expiry}")
            lines.append(f"  [{score}/7 {verdict}] | {dte}d left | added {age_h}h ago")
        send_reply("\n".join(lines), reply_chat_id)
    except Exception as e:
        send_reply(f"Error: {e}", reply_chat_id)

def handle_positions(reply_chat_id: str):
    try:
        from exit_signals import get_open_positions
        pos = get_open_positions()
        if not pos:
            send_reply("📊 No open positions being tracked.", reply_chat_id)
            return
        lines = [f"📊 Open Positions ({len(pos)})", ""]
        for p in pos:
            ticker   = p.get("ticker","?")
            strike   = p.get("strike","?")
            otype    = p.get("option_type","call")[0].upper()
            expiry   = p.get("expiry","?")
            entry_s  = p.get("entry_stock","?")
            curr_s   = p.get("current_stock","?")
            stock_pnl= p.get("stock_pnl_pct")
            opt_pnl  = p.get("option_pnl_pct")
            stop     = p.get("stop_price","?")
            target   = p.get("target_price","?")

            pnl_str = f"{stock_pnl:+.1f}%" if stock_pnl is not None else "?"
            opt_str = f" | Opt: {opt_pnl:+.1f}%" if opt_pnl is not None else ""
            emoji   = "📈" if (stock_pnl or 0) > 0 else "📉"

            lines.append(f"{emoji} <b>{ticker}</b> {strike}{otype} {expiry}")
            lines.append(f"  Entry: ${entry_s} → Now: ${curr_s} ({pnl_str}{opt_str})")
            lines.append(f"  Stop: ${stop} | Target: ${target}")
        send_reply("\n".join(lines), reply_chat_id)
    except Exception as e:
        send_reply(f"Error: {e}", reply_chat_id)

def handle_stats(reply_chat_id: str):
    try:
        from outcomes import get_stats
        s = get_stats()
        if s.get("total", 0) == 0:
            send_reply("📈 No outcome data yet — check back after market close.", reply_chat_id)
            return
        opt_str = f"\n  Avg option move: {s['avg_option_move']:+.1f}% ({s['options_tracked']} tracked)" if s.get("avg_option_move") is not None else ""
        msg = (
            f"📈 <b>FlowCheck Stats</b> ({s['total']} alerts)\n\n"
            f"Overall win rate: {s['win_rate']}%\n"
            f"TRADE win rate: {s['trade_wr']}% ({s['trade_count']} alerts)\n"
            f"WATCH win rate: {s['watch_wr']}% ({s['watch_count']} alerts)\n"
            f"Avg stock move: {s['avg_stock_move']:+.2f}%"
            f"{opt_str}"
        )
        send_reply(msg, reply_chat_id)
    except Exception as e:
        send_reply(f"Error: {e}", reply_chat_id)

def handle_close(ticker: str, reply_chat_id: str):
    try:
        from exit_signals import close_position
        close_position(ticker, exit_reason="MANUAL")
        send_reply(f"✅ Position closed: {ticker}", reply_chat_id)
    except Exception as e:
        send_reply(f"Error closing {ticker}: {e}", reply_chat_id)

def handle_backtest(tweet_url: str, tweet_time: str, reply_chat_id: str):
    try:
        send_reply(f"⏳ Backtest queued for {tweet_time} — result in ~60s...", reply_chat_id)
        import asyncio

        async def _run():
            from main import _run_backtest
            await _run_backtest("", tweet_url, tweet_time)

        def _thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_run())
            loop.close()

        threading.Thread(target=_thread, daemon=True).start()
    except Exception as e:
        send_reply(f"Backtest error: {e}", reply_chat_id)

def handle_history(reply_chat_id: str):
    base_url = os.environ.get("BASE_URL","")
    send_reply(f"📊 Today's alerts:\n{base_url}/history", reply_chat_id)

def handle_help(reply_chat_id: str):
    msg = """⚡ <b>FlowCheck Commands</b>

/watchlist — active technical watches
/positions — open positions with P&L
/portfolio — full portfolio view with sectors
/stats — win rate statistics
/find TICKER — search today's alerts
/close TICKER — close a position
/history — link to today's alerts
/backtest URL TIME — backtest a tweet
  Example: /backtest https://x.com/i/status/123 2026-05-22T10:30:00
/help — this message"""
    send_reply(msg, reply_chat_id)

def poll_commands():
    """Poll Telegram for new commands. Runs every 3 seconds."""
    updates = get_updates()
    for update in updates:
        msg = update.get("message", {})
        text    = msg.get("text", "")
        from_id = str(msg.get("chat", {}).get("id", ""))

        # Only accept commands from authorized chat
        authorized = [
            str(chat_id()),
            str(os.environ.get("TELEGRAM_TRADE_CHAT_ID",""))
        ]
        if from_id not in authorized:
            continue

        if text.startswith("/"):
            try:
                handle_command(text, from_id)
            except Exception as e:
                print(f"[CMD] Command error: {e}")


def handle_find(ticker: str, reply_chat_id: str):
    """Search today's alerts for a specific ticker."""
    try:
        from main import analyses
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        found = [
            a for a in analyses
            if a.get("trade",{}).get("ticker","").upper() == ticker.upper()
            and a.get("date") == today
        ]
        if not found:
            send_reply(f"No alerts for {ticker.upper()} today.", reply_chat_id)
            return
        lines = [f"🔍 {ticker.upper()} alerts today ({len(found)})", ""]
        base  = __import__("os").environ.get("BASE_URL","")
        for a in found:
            r      = a.get("result",{})
            t      = a.get("trade",{})
            verdict= r.get("verdict","?")
            score  = r.get("final_score","?")
            v_emoji= {"TRADE":"✅","WATCH":"👀","SKIP":"❌"}.get(verdict,"❓")
            lines.append(f"{v_emoji} {t.get('strike','?')}{t.get('option_type','call')[0].upper()} "
                         f"{t.get('expiry_short','?')} [{score}/7 {verdict}]")
            lines.append(f"  {a.get('time','?')} — {r.get('one_liner','')[:50]}")
            lines.append(f"  {base}/analysis/{a.get('id',0)}")
        send_reply("\n".join(lines), reply_chat_id)
    except Exception as e:
        send_reply(f"Error: {e}", reply_chat_id)

def handle_portfolio(reply_chat_id: str):
    """Show all open positions with P&L and sector breakdown."""
    try:
        from exit_signals import get_open_positions
        from flow_intelligence import get_sector
        pos = get_open_positions()
        if not pos:
            send_reply("📊 No open positions.", reply_chat_id)
            return

        total_cost   = sum((p.get("entry_option",0) or 0) * 100 for p in pos)
        total_pnl_pct= None
        opt_pnls     = [p.get("option_pnl_pct") for p in pos if p.get("option_pnl_pct") is not None]
        if opt_pnls:
            total_pnl_pct = round(sum(opt_pnls)/len(opt_pnls), 1)

        # Group by sector
        by_sector = {}
        for p in pos:
            sector = get_sector(p.get("ticker",""))
            by_sector.setdefault(sector, []).append(p)

        lines = [f"📊 <b>Portfolio</b> ({len(pos)} positions)", ""]
        for p in pos:
            ticker   = p.get("ticker","?")
            strike   = p.get("strike","?")
            otype    = p.get("option_type","call")[0].upper()
            expiry   = p.get("expiry","?")
            entry_s  = p.get("entry_stock","?")
            stock_pnl= p.get("stock_pnl_pct")
            opt_pnl  = p.get("option_pnl_pct")
            stop     = p.get("stop_price","?")
            target   = p.get("target_price","?")
            pnl_str  = f"{stock_pnl:+.1f}%" if stock_pnl is not None else "?"
            opt_str  = f" | Opt: {opt_pnl:+.1f}%" if opt_pnl is not None else ""
            emoji    = "📈" if (stock_pnl or 0) > 0 else "📉"
            lines.append(f"{emoji} <b>{ticker}</b> {strike}{otype} {expiry}")
            lines.append(f"  Stock: {pnl_str}{opt_str}")
            lines.append(f"  Stop: ${stop} | Target: ${target}")

        lines.append("")
        if by_sector:
            lines.append("Sectors: " + " | ".join(
                f"{s}×{len(v)}" for s,v in by_sector.items()
            ))
        if total_pnl_pct is not None:
            lines.append(f"Avg option P&L: {total_pnl_pct:+.1f}%")

        send_reply("\n".join(lines), reply_chat_id)
    except Exception as e:
        send_reply(f"Error: {e}", reply_chat_id)
