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
    elif cmd == "sentiment" and args:
        handle_sentiment(args[0].upper(), from_chat_id)
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
        # /journal or /journal @roth
        acc = args[0].lstrip("@") if args else None
        handle_journal(from_chat_id, acc)
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
        # /pnl or /pnl @roth or /pnl all
        acc = args[0].lstrip("@") if args else None
        handle_pnl(from_chat_id, acc)
    elif cmd == "add" and len(args) >= 3:
        # /add TICKER CONTRACTS PRICE [DATE] [TIME ET — 10:34AM or 2:30PM]
        date_arg = args[3] if len(args) > 3 else None
        time_arg = args[4] if len(args) > 4 else None
        handle_add(args[0].upper(), args[1], args[2], from_chat_id, date_arg, time_arg)
    elif cmd == "export":
        # /export or /export @roth
        acc = args[0].lstrip("@") if args else None
        handle_export(from_chat_id, acc)
    elif cmd == "accounts":
        handle_accounts(from_chat_id)
    elif cmd == "debrief":
        handle_debrief(from_chat_id)
    elif cmd == "account" and len(args) >= 3:
        # /account add ID NAME SIZE
        # /account add roth "Roth IRA" 25000
        if args[0].lower() == "add":
            handle_account_add(args[1], " ".join(args[2:-1]) or args[2], args[-1], from_chat_id)
        else:
            send_reply("Usage: /account add ID NAME SIZE" + chr(10) + "Example: /account add roth Roth 25000", from_chat_id)
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
        entry_date  = args[6] if len(args) > 6 else None
        entry_time  = args[7] if len(args) > 7 else None
        # Account ID — optional last arg starting with @ e.g. @roth
        account_id  = "default"
        for a in args:
            if a.startswith("@"):
                account_id = a[1:].lower()
        trade       = add_entry(ticker, strike, opt_type, expiry, contracts, price,
                                entry_date, entry_time, account_id)
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

def handle_journal(reply_chat_id: str, account_id: str = None):
    """Show trade journal, optionally filtered by account."""
    try:
        import os as _os
        from trade_journal import get_journal_summary
        base_url = _os.environ.get("BASE_URL","").rstrip("/")
        summary  = get_journal_summary(account_id)
        if base_url:
            url = base_url + "/journal-view"
            if account_id:
                url += "?account=" + account_id
            summary += chr(10) + chr(10) + "Full table + CSV:" + chr(10) + url
        send_reply(summary, reply_chat_id)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_pnl(reply_chat_id: str, account_id: str = None):
    """Show P&L summary. Optionally filtered by account."""
    try:
        from trade_journal import get_pnl_summary
        send_reply(get_pnl_summary(account_id), reply_chat_id)
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

def handle_export(reply_chat_id: str, account_id: str = None):
    """Export closed trades as CSV. /export or /export @roth"""
    try:
        import os as _os
        from trade_journal import export_journal_csv
        csv_data = export_journal_csv(account_id)
        if "No closed trades" in csv_data:
            send_reply(csv_data, reply_chat_id)
            return

        # Count data rows (skip comment + header line)
        data_lines = [l for l in csv_data.split(chr(10))
                      if l and not l.startswith("#") and not l.startswith("id,")]
        n_trades   = len(data_lines)
        acc_label  = " [@" + account_id + "]" if account_id else ""
        header     = "Journal Export" + acc_label + " — " + str(n_trades) + " trades"

        # Send as code block (Telegram truncates at ~4096 chars)
        base_url = _os.environ.get("BASE_URL","").rstrip("/")
        if len(csv_data) > 3500:
            # Too long for Telegram — direct to web
            url = base_url + "/journal-view" + ("?account=" + account_id if account_id else "")
            send_reply(
                header + chr(10) + chr(10) +
                "CSV too large for Telegram — download from web:" + chr(10) + url,
                reply_chat_id
            )
        else:
            send_reply(
                header + chr(10) + chr(10) +
                "```" + chr(10) + csv_data + chr(10) + "```",
                reply_chat_id
            )
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

def handle_sentiment(ticker: str, reply_chat_id: str):
    """Full market sentiment for a ticker. /sentiment TICKER"""
    try:
        send_reply("Fetching sentiment for " + ticker + "...", reply_chat_id)
        from sentiment import get_sentiment
        msg = get_sentiment(ticker)
        send_reply(msg, reply_chat_id)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_accounts(reply_chat_id: str):
    """List all accounts with P&L summary."""
    try:
        from trade_journal import list_accounts_summary
        send_reply(list_accounts_summary(), reply_chat_id)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_account_add(account_id: str, name: str, size_str: str, reply_chat_id: str):
    """Add or update an account. /account add ID NAME SIZE"""
    try:
        from trade_journal import add_account
        size = float(size_str.replace(",","").replace("$","").replace("k","000").replace("K","000"))
        acc  = add_account(account_id.lower(), name, size)
        send_reply(
            "Account saved:" + chr(10) +
            "[" + account_id + "] " + name + " — $" + str(f"{size:,.0f}") + chr(10) + chr(10) +
            "Tag trades to this account with @" + account_id + " in /entry:" + chr(10) +
            "Example: /entry FLNC 23 C 06/18/26 3 2.85 @" + account_id,
            reply_chat_id
        )
    except Exception as e:
        send_reply(
            "Error: " + str(e) + chr(10) +
            "Usage: /account add ID NAME SIZE" + chr(10) +
            "Example: /account add roth Roth 25000",
            reply_chat_id
        )

def handle_debrief(reply_chat_id: str):
    """AI analysis of trade journal. /debrief"""
    try:
        send_reply("Analyzing your trades...", reply_chat_id)
        from debrief import generate_debrief
        msg = generate_debrief()
        send_reply(msg, reply_chat_id)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_help(reply_chat_id: str):
    msg = chr(10).join([
        "FlowCheck Commands",
        "",
        "FLOW MONITORING",
        "/watchlist â active technical watches",
        "/positions â open positions with P&L",
        "/portfolio â portfolio with sector breakdown",
        "/stats â win rate stats (option up 50%+ = win)",
        "/find TICKER â today" + chr(39) + "s flow alerts for ticker",
        "/history â today" + chr(39) + "s alerts link",
        "",
        "RESEARCH",
        "/sentiment TICKER â price, technicals, news, flow, insiders",
        "/backtest URL TIME â backtest a historical tweet",
        "  /backtest https://x.com/i/status/123 2026-05-19T10:30:00",
        "",
        "ACTIONS",
        "/close TICKER â close a system-tracked position",
        "",
        "TRADE JOURNAL",
        "/journal [@ACCOUNT] â open trades with live P&L + web table",
        "/pnl [@ACCOUNT] â full P&L + pattern analysis",
        "/accounts â all accounts overview",
        "/export [@ACCOUNT] â CSV download for Excel",
        "/journal_help â full journal command reference",
        "",
        "/help â this message",
    ])
    send_reply(msg, reply_chat_id)

def handle_journal_help(reply_chat_id: str):
    lines = [
        'Trade Journal — Full Command Reference',
        '',
        'LOGGING ENTRIES',
        '/entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE [DATE] [TIME] [@ACCOUNT]',
        '  Now:          /entry FLNC 23 C 06/18/26 3 2.85',
        '  With time:    /entry FLNC 23 C 06/18/26 3 2.85 2026-05-27 10:34AM',
        '  With account: /entry FLNC 23 C 06/18/26 3 2.85 2026-05-27 10:34AM @roth',
        '  Screenshot:   send broker confirmation photo to bot (auto-parsed)',
        '  Note: omitting date+time auto-fills current time (warned in reply)',
        '',
        'LOGGING EXITS',
        '/exit TICKER PRICE [CONTRACTS] [DATE] [TIME]',
        '  Full now:         /exit FLNC 4.20',
        '  Full + time:      /exit FLNC 4.20 2026-05-27 1:15PM',
        '  Partial (2 of 3): /exit FLNC 4.20 2 2026-05-27 1:15PM',
        '  Multi-day swing:  /exit FLNC 6.80 2026-05-29 11:30AM',
        '  Screenshot:       send exit confirmation photo to bot',
        '',
        'ADDING TO POSITION',
        '/add TICKER CONTRACTS PRICE [DATE] [TIME]',
        '  /add FLNC 1 3.10 2026-05-27 11:00AM',
        '  Blended avg entry price calculated automatically',
        '',
        'SKIPPED ALERTS',
        '/missed TICKER [REASON]',
        '  /missed FLNC Too risky near earnings',
        '',
        'VIEWING',
        '/journal — open trades: live P&L, stop/target + web table link',
        '/pnl [@ACCOUNT] — P&L + pattern analysis (unlocks at 10+ trades)',
        '  /pnl           all accounts',
        '  /pnl @roth     Roth IRA only',
        '/accounts — all accounts with P&L overview',
        '/export — CSV download for Excel/Google Sheets',
        '',
        'EDITING (analytics recalculate automatically)',
        '/edit TICKER FIELD VALUE',
        '  Fields: entry_date  entry_time  exit_date  exit_time',
        '          entry_price  contracts  expiry  strike  note  option_type',
        '  /edit FLNC entry_time 10:34AM',
        '  /edit FLNC note Entered on VWAP bounce',
        '',
        'TAGGING',
        '/tag TICKER #tag1 #tag2',
        '  /tag FLNC #earnings_play #insider #vwap',
        '  Win rate by tag in /pnl after 10+ trades',
        '',
        'ACCOUNTS',
        '/account add ID NAME SIZE',
        '  /account add roth Roth 25000',
        '  /account add margin Margin 100000',
        '  Tag trades: add @ID to /entry',
        '',
        'TIME FORMAT — all times ET',
        '  10:34AM   2:30PM   10:34   14:30   10AM   2PM',
        '',
        'WEB VIEW',
        '  Full table + CSV: link at bottom of /journal response',
        '',
        '/journal_help — this message',
    ]
    send_reply(chr(10).join(lines), reply_chat_id)

def download_telegram_photo(photo_list: list):
    """Download highest resolution photo from Telegram."""
    import os, requests as _req
    token = os.environ.get("TELEGRAM_BOT_TOKEN","")
    if not token or not photo_list:
        return None
    best = max(photo_list, key=lambda p: p.get("file_size", 0))
    fid  = best.get("file_id")
    if not fid:
        return None
    try:
        r = _req.get(
            "https://api.telegram.org/bot" + token + "/getFile",
            params={"file_id": fid}, timeout=10
        )
        if r.status_code != 200:
            return None
        file_path = r.json().get("result",{}).get("file_path")
        if not file_path:
            return None
        r2 = _req.get(
            "https://api.telegram.org/file/bot" + token + "/" + file_path,
            timeout=15
        )
        if r2.status_code == 200:
            return r2.content
    except Exception as e:
        print("[PHOTO] Download error: " + str(e))
    return None

def parse_trade_screenshot(image_bytes: bytes, caption: str = ""):
    """Use Claude Haiku vision to extract trade details from broker screenshot."""
    import anthropic, base64, json, os
    client  = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))
    img_b64 = base64.standard_b64encode(image_bytes).decode()
    cap_note = (chr(10) + "User caption: " + caption) if caption else ""

    prompt = chr(10).join([
        "This is a brokerage app screenshot." + cap_note,
        "Extract trade details if this is an order fill or confirmation.",
        'Return ONLY valid JSON. If NOT a trade, return {"error": "not_a_trade"}.',
        "",
        "Required JSON fields:",
        '  "action": "entry" or "exit"',
        '  "ticker": e.g. "FLNC"',
        '  "strike": e.g. "23"',
        '  "option_type": "call" or "put"',
        '  "expiry": MM/DD/YY format',
        '  "contracts": integer',
        '  "price": decimal e.g. 2.85',
        '  "date": YYYY-MM-DD if visible',
        '  "time": 12-hour AM/PM ET if visible',
        '  "confidence": "high" "medium" or "low"',
        "",
        "BUY TO OPEN = entry. SELL TO CLOSE = exit.",
        "Omit date/time if not clearly visible.",
    ])

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_b64,
                        }
                    },
                    {"type": "text", "text": prompt}
                ]
            }]
        )
        raw  = resp.content[0].text.strip()
        raw  = raw.replace("```json","").replace("```","").strip()
        data = json.loads(raw)
        print("[PHOTO] Parsed: " + str(data))
        return data
    except Exception as e:
        print("[PHOTO] Vision error: " + str(e))
        return None

def handle_trade_photo(photo_list: list, caption: str, reply_chat_id: str):
    """Handle a trade screenshot sent to the bot."""
    send_reply("Reading screenshot...", reply_chat_id)

    image_bytes = download_telegram_photo(photo_list)
    if not image_bytes:
        send_reply(
            "Could not download photo. Try again or use:" + chr(10) +
            "/entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE DATE TIME",
            reply_chat_id
        )
        return

    data = parse_trade_screenshot(image_bytes, caption)

    if not data or data.get("error") == "not_a_trade":
        send_reply(
            "Not recognized as a trade confirmation." + chr(10) + chr(10) +
            "Send a screenshot of your order fill." + chr(10) +
            "Or use: /entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE DATE TIME",
            reply_chat_id
        )
        return

    if data.get("error"):
        send_reply("Could not read: " + str(data.get("error")), reply_chat_id)
        return

    action    = data.get("action","entry")
    ticker    = (data.get("ticker","") or "").upper()
    strike    = str(data.get("strike","") or "")
    opt_type  = data.get("option_type","call")
    expiry    = data.get("expiry","")
    contracts = data.get("contracts")
    price     = data.get("price")
    date_str  = data.get("date","")
    time_str  = data.get("time","")
    confidence= data.get("confidence","medium")

    missing = []
    if not ticker:    missing.append("ticker")
    if not strike:    missing.append("strike")
    if not expiry:    missing.append("expiry")
    if not contracts: missing.append("contracts")
    if not price:     missing.append("price")

    conf_note = (
        " (low confidence - verify)" if confidence == "low"
        else " (verify if needed)" if confidence == "medium"
        else ""
    )

    if missing:
        partial = []
        if ticker:    partial.append("Ticker: " + ticker)
        if strike:    partial.append("Strike: " + strike)
        if expiry:    partial.append("Expiry: " + expiry)
        if contracts: partial.append("Contracts: " + str(contracts))
        if price:     partial.append("Price: $" + str(price))
        partial.append("")
        partial.append("Missing: " + ", ".join(missing))
        partial.append("Complete manually: /entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE DATE TIME")
        send_reply(chr(10).join(partial), reply_chat_id)
        return

    otype = "C" if opt_type == "call" else "P"

    try:
        if action == "entry":
            from trade_journal import add_entry
            trade = add_entry(
                ticker, strike, otype, expiry,
                int(contracts), float(price),
                date_str if date_str else None,
                time_str if time_str else None
            )
            auto      = trade.get("entry_auto_filled", True)
            time_note = " (time auto-filled)" if auto else " " + date_str + " " + time_str
            parts = [
                "Entry logged" + conf_note + ":",
                ticker + " " + strike + otype + " " + expiry +
                " x" + str(contracts) + " @ $" + str(price),
                "Total: $" + str(round(float(price)*int(contracts)*100, 2)) + time_note,
                "",
                "Exit: /exit " + ticker + " PRICE DATE TIME",
                "Edit: /edit " + ticker + " FIELD VALUE",
            ]
            send_reply(chr(10).join(parts), reply_chat_id)

        elif action == "exit":
            from trade_journal import add_exit
            result = add_exit(
                ticker, float(price),
                date_str if date_str else None,
                time_str if time_str else None,
                int(contracts) if contracts else None
            )
            if not result:
                send_reply(
                    "No open trade for " + ticker + "." + chr(10) +
                    "Log entry first with /entry or a screenshot.",
                    reply_chat_id
                )
                return
            pnl       = result.get("pnl_total", 0) or 0
            pct       = result.get("pnl_pct", 0) or 0
            closing   = result.get("contracts", contracts)
            remaining = result.get("remaining", 0)
            label     = "WIN" if pnl > 0 else "LOSS"
            parts = [
                "Exit logged" + conf_note + ":",
                label + ": " + ticker + " " + strike + otype,
                "x" + str(closing) + " @ $" + str(price) +
                " P&L: $" + str(round(pnl,2)) +
                " (" + str(round(pct,1)) + "%)",
            ]
            if remaining > 0:
                parts.append(str(remaining) + " contracts still open")
            send_reply(chr(10).join(parts), reply_chat_id)

    except Exception as e:
        send_reply(
            "Read OK but logging failed: " + str(e) + chr(10) + chr(10) +
            "Manual: /" + action + " " + ticker + " " + strike + " " +
            otype + " " + expiry + " " + str(contracts) + " " + str(price),
            reply_chat_id
        )


def poll_commands():
    """Poll Telegram for new commands and photos. Runs every 10 seconds."""
    updates = get_updates()
    for update in updates:
        msg     = update.get("message", {})
        text    = msg.get("text", "")
        caption = msg.get("caption", "") or ""
        photo   = msg.get("photo")
        from_id = str(msg.get("chat", {}).get("id", ""))

        # Only accept from authorized chats
        import os as _os
        authorized = [
            str(_os.environ.get("TELEGRAM_CHAT_ID","")),
            str(_os.environ.get("TELEGRAM_TRADE_CHAT_ID",""))
        ]
        if from_id not in authorized:
            continue

        # Photo received — try to parse as trade screenshot
        if photo:
            try:
                handle_trade_photo(photo, caption, from_id)
            except Exception as e:
                print(f"[CMD] Photo error: {e}")
            continue

        if text.startswith("/"):
            try:
                handle_command(text, from_id)
            except Exception as e:
                print(f"[CMD] Command error: {e}")
