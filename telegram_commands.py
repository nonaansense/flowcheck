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
    elif cmd == "entry" and len(args) >= 6:
        # /entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE [DATE] [TIME]
        # Log now:   /entry FLNC 23 C 06/18/26 3 2.85
        # Log later: /entry FLNC 23 C 06/18/26 3 2.85 2026-05-27 10:34AM
        handle_entry(args, from_chat_id)
    elif cmd == "exit" and len(args) >= 2:
        # /exit TICKER PRICE [CONTRACTS] [DATE] [TIME]
        # Close all:    /exit FLNC 4.20
        # Partial:      /exit FLNC 4.20 2          (close 2 contracts)
        # With date:    /exit FLNC 4.20 2 2026-05-27 2:30PM
        # All + date:   /exit FLNC 4.20 all 2026-05-27 2:30PM
        contracts_arg = None
        date_arg      = None
        time_arg      = None
        remaining_args = args[2:]
        if remaining_args:
            first = remaining_args[0]
            if first.lower() == "all":
                remaining_args = remaining_args[1:]
            elif first.isdigit():
                contracts_arg  = int(first)
                remaining_args = remaining_args[1:]
        if remaining_args: date_arg = remaining_args[0]
        if len(remaining_args) > 1: time_arg = remaining_args[1]
        handle_exit(args[0].upper(), args[1], from_chat_id,
                    date_arg, time_arg, contracts_arg)
    elif cmd in ("journal", "trades"):
        handle_journal(from_chat_id)
    elif cmd == "journal_help":
        handle_journal_help(from_chat_id)
    elif cmd == "edit" and len(args) >= 3:
        # /edit TICKER FIELD VALUE
        # /edit FLNC entry_date 2026-05-27
        # /edit FLNC entry_time 10:34AM
        # /edit FLNC entry_price 2.95
        # /edit FLNC contracts 3
        # /edit FLNC expiry 06/18/26
        # /edit FLNC note "Entered on VWAP bounce"
        handle_edit(args[0].upper(), args[1], " ".join(args[2:]), from_chat_id)
    elif cmd == "pnl":
        handle_pnl(from_chat_id)
    elif cmd == "add" and len(args) >= 3:
        # /add TICKER CONTRACTS PRICE [DATE] [TIME ET — 10:34AM or 2:30PM]
        date_arg = args[3] if len(args) > 3 else None
        time_arg = args[4] if len(args) > 4 else None
        handle_add(args[0].upper(), args[1], args[2], from_chat_id, date_arg, time_arg)
    elif cmd == "export":
        handle_export(from_chat_id)
    elif cmd == "missed" and args:
        reason = " ".join(args[1:]) if len(args) > 1 else ""
        handle_missed(args[0].upper(), reason, from_chat_id)
    elif cmd == "tag" and len(args) >= 2:
        # /tag TICKER #earnings_play #momentum
        handle_tag(args[0].upper(), args[1:], from_chat_id)
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

def handle_entry(args: list, reply_chat_id: str):
    """
    Record actual trade entry.
    /entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE [DATE] [TIME]

    Examples:
      Log immediately:  /entry FLNC 23 C 06/18/26 3 2.85
      Retroactive:     /entry FLNC 23 C 06/18/26 3 2.85 2026-05-27 10:34AM
      Date only:       /entry FLNC 23 C 06/18/26 3 2.85 2026-05-27
    """
    try:
        from trade_journal import add_entry
        if len(args) < 6:
            raise ValueError("Need: TICKER STRIKE C/P EXPIRY CONTRACTS PRICE [DATE] [TIME]")
        ticker     = args[0].upper()
        strike     = args[1]
        opt_type   = args[2]
        expiry     = args[3]
        contracts  = int(args[4])
        price      = float(args[5])
        entry_date = args[6] if len(args) > 6 else None
        entry_time = args[7] if len(args) > 7 else None
        trade      = add_entry(ticker, strike, opt_type, expiry, contracts, price,
                               entry_date, entry_time)
        otype     = "C" if trade["option_type"] == "call" else "P"
        fc        = ""
        if trade.get("fc_score"):
            fc = "[" + str(trade["fc_score"]) + "/7 " + str(trade["fc_verdict"]) + "]"
        auto = trade.get("entry_auto_filled", True)
        time_label = (
            "Entered (auto): " + str(trade["entry_date"]) + " " + str(trade["entry_time"]) + " ET"
            + " — add DATE TIME to /entry for accuracy"
            if auto else
            "Entered: " + str(trade["entry_date"]) + " " + str(trade["entry_time"]) + " ET"
        )
        parts = [
            "Entry recorded:",
            ticker + " " + strike + otype + " " + expiry +
            " | " + str(contracts) + " contracts @ $" + str(price),
            "Total cost: $" + str(round(price*contracts*100,2)),
            time_label,
            "FlowCheck: " + (fc if fc else "No matching alert today"),
            "Partial: /exit " + ticker + " PRICE CONTRACTS 2026-05-27 1:15PM",
            "Full:    /exit " + ticker + " PRICE 2026-05-27 1:15PM",
        ]
        send_reply("\n".join(parts), reply_chat_id)
    except Exception as e:
        send_reply(
            "Error: " + str(e) + "\nFormat: /entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE [DATE] [TIME]\nExample: /entry FLNC 23 C 06/18/26 3 2.85 2026-05-27 10:34AM",
            reply_chat_id
        )

def handle_exit(ticker: str, price_str: str, reply_chat_id: str):
    """Record actual trade exit. /exit TICKER PRICE"""
    try:
        from trade_journal import add_exit
        price = float(price_str)
        trade = add_exit(ticker, price)
        if not trade:
            send_reply("No open trade found for " + ticker + ".", reply_chat_id)
            return
        pnl   = trade.get("pnl_total", 0) or 0
        pct   = trade.get("pnl_pct", 0) or 0
        emoji = "UP" if pnl > 0 else "DN"
        otype = trade["option_type"][0].upper()
        parts = [
            ("WIN" if pnl > 0 else "LOSS") + ": " + ticker + " " + str(trade["strike"]) + otype,
            "Entry $" + str(trade["entry_price"]) + " -> Exit $" + str(price),
            "PnL: $" + str(round(pnl,2)) + " (" + str(round(pct,1)) + "%) x" + str(trade["contracts"]),
            "Held: " + str(trade["entry_date"]) + " to " + str(trade["exit_date"]),
        ]
        send_reply("\n".join(parts), reply_chat_id)
    except Exception as e:
        send_reply(
            "Error: " + str(e) + "\nFormat: /exit TICKER PRICE [CONTRACTS] [DATE] [TIME]\nExamples:\n  /exit FLNC 4.20\n  /exit FLNC 4.20 2026-05-27 1:15PM\n  /exit FLNC 4.20 2 2026-05-27 1:15PM",
            reply_chat_id
        )

def handle_edit(ticker, field, value, reply_chat_id):
    try:
        from trade_journal import edit_trade
        success, msg, trade = edit_trade(ticker, field, value)
        if success and trade:
            otype = trade.get('option_type','call')[0].upper()
            parts = [
                'Updated: ' + ticker + ' ' + str(trade.get('strike','')) + otype,
                msg,
                'Entry: ' + str(trade.get('entry_date','')) + ' ' + str(trade.get('entry_time','')) + ' ET',
            ]
            if trade.get('exit_date'):
                parts.append('Exit: ' + str(trade.get('exit_date','')) + ' ' + str(trade.get('exit_time','')) + ' ET')
            send_reply(chr(10).join(parts), reply_chat_id)
        else:
            reply = ('Failed: ' + msg + chr(10) + chr(10) +
                     'Valid: entry_date, entry_time, exit_date, exit_time, ' +
                     'entry_price, contracts, expiry, strike, note, option_type' + chr(10) +
                     'Example: /edit FLNC entry_time 10:34AM')
            send_reply(reply, reply_chat_id)
    except Exception as e:
        send_reply('Error: ' + str(e), reply_chat_id)

def handle_journal(reply_chat_id: str):
    """Show trade journal."""
    try:
        import os as _os
        from trade_journal import get_journal_summary
        base_url  = _os.environ.get("BASE_URL","").rstrip("/")
        summary   = get_journal_summary()
        if base_url:
            summary += chr(10) + chr(10) + "Full table + CSV download:" + chr(10) + base_url + "/journal-view"
        send_reply(summary, reply_chat_id)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_pnl(reply_chat_id: str):
    """Show P&L summary."""
    try:
        from trade_journal import get_pnl_summary
        send_reply(get_pnl_summary(), reply_chat_id)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_recalc(ticker: str, reply_chat_id: str):
    """Re-fetch Polygon bars and recalculate analytics after an edit."""
    try:
        from trade_journal import recalc_analytics
        send_reply("Fetching Polygon data for " + ticker + "...", reply_chat_id)
        success, msg = recalc_analytics(ticker)
        send_reply(("OK: " if success else "Error: ") + msg, reply_chat_id)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_add(ticker: str, contracts_str: str, price_str: str,
               reply_chat_id: str, date_arg: str = None, time_arg: str = None):
    """Add contracts to existing open position. /add TICKER CONTRACTS PRICE [DATE] [TIME]"""
    try:
        from trade_journal import add_to_position
        contracts = int(contracts_str)
        price     = float(price_str)
        success, msg, trade = add_to_position(ticker, contracts, price, date_arg, time_arg)
        if success and trade:
            otype = trade.get("option_type","call")[0].upper()
            parts = [
                "Position updated: " + ticker + " " + str(trade.get("strike","")) + otype,
                msg,
                "Total: " + str(trade.get("contracts","")) + " contracts",
                "Avg entry: $" + str(trade.get("entry_price","")),
                "Total cost: $" + str(trade.get("total_cost","")),
            ]
            send_reply(chr(10).join(parts), reply_chat_id)
        else:
            send_reply(msg, reply_chat_id)
    except Exception as e:
        send_reply(
            "Error: " + str(e) + chr(10) +
            "Format: /add TICKER CONTRACTS PRICE [DATE] [TIME]" + chr(10) +
            "Example: /add FLNC 1 3.10 2026-05-27 11:00AM",
            reply_chat_id
        )

def handle_export(reply_chat_id: str):
    """Export closed trades as CSV. /export"""
    try:
        from trade_journal import export_journal_csv
        csv = export_journal_csv()
        if csv.startswith("No closed"):
            send_reply(csv, reply_chat_id)
            return
        lines   = csv.split(chr(10))
        n_trades = len(lines) - 1
        header   = "Trade Journal Export — " + str(n_trades) + " closed trades"
        send_reply(header + chr(10) + chr(10) + "```" + chr(10) + csv[:3500] + chr(10) + "```", reply_chat_id)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_missed(ticker: str, reason: str, reply_chat_id: str):
    """Log a missed trade. /missed TICKER [REASON]"""
    try:
        from trade_journal import add_missed_trade, get_missed_summary
        missed = add_missed_trade(ticker, reason)
        fc     = str(missed.get("fc_score","?")) + "/7 " + str(missed.get("fc_verdict","?"))
        parts  = [
            "Missed trade logged: " + ticker,
            "FlowCheck: " + fc,
            "Reason: " + (reason or "None given"),
            "Use /missed to view all missed trades.",
        ]
        send_reply(chr(10).join(parts), reply_chat_id)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_tag(ticker: str, tags: list, reply_chat_id: str):
    """Add tags to a trade. /tag TICKER #tag1 #tag2"""
    try:
        from trade_journal import add_tags
        success, msg = add_tags(ticker, tags)
        send_reply(("Tagged " + ticker + ": " + msg) if success else msg, reply_chat_id)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_help(reply_chat_id: str):
    lines = [
        "FlowCheck Commands",
        "",
        "MONITORING",
        "/watchlist — active technical watches",
        "/positions — open positions with P&L",
        "/portfolio — portfolio with sector breakdown",
        "/stats — win rate statistics",
        "/find TICKER — today's alerts for ticker",
        "/history — today's alerts link",
        "",
        "ACTIONS",
        "/close TICKER — close a tracked position",
        "/backtest URL TIME — backtest a tweet\n"        "  Example: /backtest https://x.com/i/status/123 2026-05-19T10:30:00",
        "",
        "TRADE JOURNAL",
        "/journal — open trades with live P&L",
        "/pnl — full P&L and pattern analysis",
        "/journal_help — all journal commands",
        "",
        "/help — this message",
    ]
    send_reply(chr(10).join(lines), reply_chat_id)

def handle_journal_help(reply_chat_id: str):
    lines = [
        "Trade Journal Commands",
        "",
        "LOGGING TRADES",
        "/entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE [DATE] [TIME]",
        "  Log now:      /entry FLNC 23 C 06/18/26 3 2.85",
        "  Retroactive:  /entry FLNC 23 C 06/18/26 3 2.85 2026-05-27 10:34AM",
        "",
        "/exit TICKER PRICE [CONTRACTS] [DATE] [TIME]",
        "  Full exit:    /exit FLNC 4.20 2026-05-27 1:15PM",
        "  Partial exit: /exit FLNC 4.20 2 2026-05-27 1:15PM",
        "  Quick:        /exit FLNC 4.20  (uses current time ET)",
        "",
        "/add TICKER CONTRACTS PRICE [DATE] [TIME]",
        "  Example: /add FLNC 1 3.10 2026-05-27 11:00AM",
        "",
        "/missed TICKER [REASON]",
        "  Log skipped alert: /missed FLNC Too risky near earnings",
        "",
        "VIEWING",
        "/journal — open trades with live unrealized P&L + stop/target",
        "/pnl — full P&L summary + pattern analysis (10+ trades)",
        "/export — CSV of all closed trades",
        "",
        "EDITING",
        "/edit TICKER FIELD VALUE",
        "  Editable: entry_date entry_time exit_date exit_time",
        "            entry_price contracts expiry strike note option_type",
        "  Example: /edit FLNC entry_time 10:34AM",
        "  Analytics recalculate automatically after edits",
        "",
        "TAGGING",
        "/tag TICKER #tag1 #tag2",
        "  Example: /tag FLNC #earnings_play #insider",
        "  Tags appear in pattern analysis after 10+ trades",
        "",
        "/journal_help — this message",
    ]
    send_reply(chr(10).join(lines), reply_chat_id)


def poll_commands():
    """Poll Telegram for new commands. Runs every 10 seconds."""
    updates = get_updates()
    for update in updates:
        msg     = update.get("message", {})
        text    = msg.get("text", "")
        from_id = str(msg.get("chat", {}).get("id", ""))

        # Only accept commands from authorized chats
        import os as _os
        authorized = [
            str(_os.environ.get("TELEGRAM_CHAT_ID","")),
            str(_os.environ.get("TELEGRAM_TRADE_CHAT_ID",""))
        ]
        if from_id not in authorized:
            continue

        if text.startswith("/"):
            try:
                handle_command(text, from_id)
            except Exception as e:
                print(f"[CMD] Command error: {e}")
