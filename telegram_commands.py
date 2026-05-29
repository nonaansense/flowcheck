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
    elif cmd == "sectors":
        handle_sectors(from_chat_id)
    elif cmd == "oi" and args:
        # /oi TICKER STRIKE C/P EXPIRY  — single option OI check
        # /oi all                        — OI check for all yesterday's alerts
        if args[0].lower() == "all":
            handle_oi_all(from_chat_id)
        elif len(args) >= 4:
            handle_oi_single(args[0].upper(), args[1], args[2], args[3], from_chat_id)
        else:
            send_reply(
                "Usage:" + chr(10) +
                "  /oi TICKER STRIKE C/P EXPIRY" + chr(10) +
                "  /oi NVDA 140 C 06/20/26" + chr(10) +
                "  /oi all  (checks all yesterday alerts)",
                from_chat_id
            )
    elif cmd == "oi" and not args:
        handle_oi_all(from_chat_id)
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
        acc = args[0].lstrip("@") if args else None
        handle_journal(from_chat_id, acc)
    elif cmd == "j":
        # /j — just send the web table link
        import os as _os
        base = _os.environ.get("BASE_URL","").rstrip("/")
        acc  = args[0].lstrip("@") if args else None
        url  = base + "/journal-view" + ("?account=" + acc if acc else "")
        send_reply(url, from_chat_id)
    elif cmd == "journal_help":
        handle_journal_help(from_chat_id)
    elif cmd == "delete" and args:
        # /delete TICKER [@ACCOUNT]
        ticker     = args[0].upper()
        account_id = None
        for a in args[1:]:
            if a.startswith("@"):
                account_id = a[1:].lower()
        handle_delete(ticker, account_id, from_chat_id)
    elif cmd == "edit" and len(args) >= 3:
        # /edit TICKER [@ACCOUNT] FIELD VALUE
        # Without account: /edit BE entry_time 10:34AM
        # With account:    /edit BE @RH_Trad entry_time 10:34AM
        edit_args  = args[1:]  # skip ticker
        account_id = None
        if edit_args and edit_args[0].startswith("@"):
            account_id = edit_args[0][1:].lower()
            edit_args  = edit_args[1:]
        if len(edit_args) >= 2:
            field = edit_args[0]
            value = " ".join(edit_args[1:])
            handle_edit(args[0].upper(), field, value, from_chat_id, account_id)
        else:
            send_reply(
                "Usage: /edit TICKER [@ACCOUNT] FIELD VALUE" + chr(10) +
                "Example: /edit BE @RH_Trad entry_time 10:34AM",
                from_chat_id
            )
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
    elif cmd == "refresh":
        handle_refresh(from_chat_id)
    elif cmd == "sync" and args:
        # /sync TICKER STRIKE C/P CONTRACTS PRICE [@ACCOUNT]
        # /sync FLNC 23 C 3 2.85 @rh_trad
        # Can send multiple lines — each /sync adds one position
        handle_sync_position(args, from_chat_id)
    elif cmd == "sync" and not args:
        send_reply(
            "Sync your open positions against the journal." + chr(10) + chr(10) +
            "Format: /sync TICKER STRIKE C/P CONTRACTS PRICE [@ACCOUNT]" + chr(10) +
            "Example: /sync FLNC 23 C 3 2.85 @rh_trad" + chr(10) + chr(10) +
            "Send one line per position. Bot adds any missing to journal.",
            from_chat_id
        )
    elif cmd == "debrief":
        handle_debrief(from_chat_id)
    elif cmd == "account" and args:
        sub = args[0].lower()
        if sub == "add" and len(args) >= 4:
            handle_account_add(args[1], " ".join(args[2:-1]) or args[2], args[-1], from_chat_id)
        elif sub == "delete" and len(args) >= 2:
            handle_account_delete(args[1].lstrip("@").lower(), from_chat_id)
        elif sub == "remove" and len(args) >= 2:
            handle_account_delete(args[1].lstrip("@").lower(), from_chat_id)
        else:
            send_reply(
                "Account commands:" + chr(10) +
                "  /account add ID NAME SIZE" + chr(10) +
                "  /account delete ID" + chr(10) + chr(10) +
                "Examples:" + chr(10) +
                "  /account add rh_brok Margin 100000" + chr(10) +
                "  /account delete rh_brok",
                from_chat_id
            )
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

def handle_edit(ticker, field, value, reply_chat_id, account_id=None):
    try:
        from trade_journal import edit_trade
        success, msg, trade = edit_trade(ticker, field, value, account_id=account_id)
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

def handle_spread(args: list, reply_chat_id: str):
    """
    Log a credit/debit spread entry.
    /spread TICKER TYPE EXPIRY CONTRACTS SHORT_STRIKE LONG_STRIKE CREDIT [DATE] [TIME] [@ACCOUNT]

    Types:
      cc = credit call spread (bear call)
      cp = credit put spread  (bull put)
      dc = debit call spread  (bull call)
      dp = debit put spread   (bear put)
      ic = iron condor

    Example:
      /spread BE cc 07/17/26 3 465 460 1.25 2026-05-27 10:34AM @RH_Brok
      /spread NVDA cp 06/20/26 1 130 125 0.85
    """
    try:
        from trade_journal import add_entry
        if len(args) < 6:
            raise ValueError("Need: TICKER TYPE EXPIRY CONTRACTS SHORT_STRIKE LONG_STRIKE CREDIT")

        ticker       = args[0].upper()
        raw_type     = args[1].lower()
        expiry       = args[2]
        contracts    = int(args[3])
        short_strike = args[4]
        long_strike  = args[5]
        credit       = float(args[6]) if len(args) > 6 else 0.0

        # Parse optional date/time/@account from remaining args
        remaining  = args[7:] if len(args) > 7 else []
        account_id = "default"
        entry_date = None
        entry_time = None
        for a in remaining:
            if a.startswith("@"):
                account_id = a[1:].lower()
            elif not entry_date:
                entry_date = a
            elif not entry_time:
                entry_time = a

        # Map type codes
        type_map = {
            "cc": "credit_call", "credit_call": "credit_call",
            "cp": "credit_put",  "credit_put":  "credit_put",
            "dc": "debit_call",  "debit_call":  "debit_call",
            "dp": "debit_put",   "debit_put":   "debit_put",
            "ic": "iron_condor", "iron_condor": "iron_condor",
            "ib": "iron_butterfly",
        }
        spread_type = type_map.get(raw_type)
        if not spread_type:
            raise ValueError("Unknown type: " + raw_type +
                           ". Use: cc cp dc dp ic")

        # Determine option type from spread
        opt_type = "call" if "call" in spread_type else "put"

        # Spread width
        try:
            width = abs(float(short_strike) - float(long_strike))
        except:
            width = None

        trade = add_entry(
            ticker, short_strike, opt_type, expiry,
            contracts, 0.0,
            entry_date, entry_time, account_id,
            spread_type=spread_type,
            short_strike=short_strike,
            long_strike=long_strike,
            spread_width=width,
            credit=credit,
        )

        auto      = trade.get("entry_auto_filled", True)
        time_note = (" (time auto-filled)" if auto
                    else " " + str(trade.get("entry_date","")) + " " + str(trade.get("entry_time","")))
        stype     = spread_type.replace("_"," ").upper()
        acc_label = " [@" + account_id + "]" if account_id != "default" else ""
        mp        = trade.get("max_profit","?")
        ml        = trade.get("max_loss","?")

        parts = [
            "Spread logged" + acc_label + ":",
            ticker + " " + stype + " " + expiry,
            "Short: $" + short_strike + " | Long: $" + long_strike +
            " | Width: $" + str(width),
            "Credit: $" + str(credit) + " x" + str(contracts) +
            " = $" + str(round(credit * contracts * 100, 2)) + " received",
            "Max profit: $" + str(mp) + " | Max loss: $" + str(ml),
            time_note.strip(),
            "",
            "Exit: /exit " + ticker + " PRICE DATE TIME",
        ]
        send_reply(chr(10).join(parts), reply_chat_id)

    except Exception as e:
        send_reply(
            "Error: " + str(e) + chr(10) + chr(10) +
            "Format: /spread TICKER TYPE EXPIRY CONTRACTS SHORT LONG CREDIT [DATE] [TIME] [@ACCOUNT]" + chr(10) +
            "Types: cc cp dc dp ic" + chr(10) +
            "Example: /spread BE cc 07/17/26 3 465 460 1.25 2026-05-27 10:34AM @RH_Brok",
            reply_chat_id
        )

def handle_delete(ticker: str, account_id: str, reply_chat_id: str):
    """Delete most recent trade for ticker. /delete TICKER [@ACCOUNT]"""
    try:
        from trade_journal import delete_trade
        success, msg = delete_trade(ticker, account_id)
        if success:
            send_reply("Deleted from journal:" + chr(10) + msg, reply_chat_id)
        else:
            send_reply(msg, reply_chat_id)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_account_delete(account_id: str, reply_chat_id: str):
    """Delete an account. /account delete ID"""
    try:
        from trade_journal import delete_account
        success, msg = delete_account(account_id)
        send_reply(msg, reply_chat_id)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_refresh(reply_chat_id: str):
    """Fetch current option prices for all open positions. /refresh"""
    try:
        send_reply("Fetching current prices...", reply_chat_id)
        from eod_pricer import update_eod_prices

        def send_result(msg):
            send_reply(msg, reply_chat_id)

        update_eod_prices(send_sms_fn=send_result)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_sync_position(args: list, reply_chat_id: str):
    """
    Log a position if not already in journal.
    /sync TICKER STRIKE C/P CONTRACTS PRICE [@ACCOUNT]
    /sync FLNC 23 C 3 2.85 @rh_trad
    """
    try:
        if len(args) < 5:
            send_reply(
                "Format: /sync TICKER STRIKE C/P CONTRACTS PRICE [@ACCOUNT]" + chr(10) +
                "Example: /sync FLNC 23 C 3 2.85 @rh_trad",
                reply_chat_id
            )
            return

        ticker     = args[0].upper()
        strike     = args[1]
        opt_type   = "call" if args[2].upper() in ("C","CALL") else "put"
        contracts  = int(args[3])
        price      = float(args[4])
        account_id = "default"
        expiry     = ""

        # Parse optional expiry and @account from remaining args
        for a in args[5:]:
            if a.startswith("@"):
                account_id = a[1:].lower()
            elif "/" in a or "-" in a:
                expiry = a

        # Normalize expiry format
        if expiry:
            from trade_journal import normalize_expiry
            expiry = normalize_expiry(expiry)

        from trade_journal import load_journal, add_entry
        journal = load_journal()
        open_t  = journal.get("trades",[])

        # Check if already in journal
        for t in open_t:
            if (t.get("ticker","").upper() == ticker and
                str(t.get("strike","")) == strike and
                t.get("option_type","call") == opt_type and
                t.get("account_id","default") == account_id):
                otype = opt_type[0].upper()
                send_reply(
                    "Already in journal: " + ticker + " " + strike + otype +
                    " [@" + account_id + "]" + chr(10) +
                    "No duplicate added.",
                    reply_chat_id
                )
                return

        # Not found — add it
        trade     = add_entry(
            ticker, strike, opt_type, expiry,
            contracts, price, None, None, account_id
        )
        otype     = opt_type[0].upper()
        acc_label = " [@" + account_id + "]" if account_id != "default" else ""
        total     = round(price * contracts * 100, 2)

        sizing_note = ""
        if trade.get("_deployed") is not None:
            sizing_note = (
                chr(10) + "Deployed: $" + str(trade["_deployed"]) +
                " of $" + str(int(trade["_acc_size"])) +
                " (" + str(trade["_deployed_pct"]) + "%)"
            )

        send_reply(
            "✅ Synced to journal" + acc_label + ":" + chr(10) +
            ticker + " " + strike + otype +
            (" " + expiry if expiry else "") +
            " x" + str(contracts) + " @ $" + str(price) + chr(10) +
            "Total: $" + str(total) +
            sizing_note,
            reply_chat_id
        )

    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_sectors(reply_chat_id: str):
    """Show sector ETF reference list. /sectors"""
    msg = chr(10).join([
        "│ Sector ETF Reference",
        "",
        "MAJOR SECTORS",
        "  XLK  — Technology",
        "  XLF  — Financials",
        "  XLV  — Healthcare",
        "  XLE  — Energy",
        "  XLI  — Industrials",
        "  XLC  — Communications",
        "  XLY  — Consumer Discretionary",
        "  XLP  — Consumer Staples",
        "  XLB  — Materials",
        "  XLRE — Real Estate",
        "  XLU  — Utilities",
        "",
        "SUBSECTORS",
        "  XBI  — Biotech",
        "  SMH  — Semiconductors",
        "  SOXX — Semiconductors (broad)",
        "  IBB  — Biotech (broad)",
        "  KRE  — Regional Banks",
        "  XOP  — Oil & Gas Exploration",
        "  XRT  — Retail",
        "  GDX  — Gold Miners",
        "  JETS — Airlines",
        "  XME  — Metals & Mining",
        "  ITB  — Homebuilders",
        "",
        "THEMATIC",
        "  ARKK — Innovation/Disruptive",
        "  HACK — Cybersecurity",
        "  FINX — Fintech",
        "  ROBO — Robotics & AI",
        "  MSOS — Cannabis",
        "",
        "Sector rotation alert fires when 3+ flows",
        "hit the same sector on the same day.",
    ])
    send_reply(msg, reply_chat_id)

def handle_oi_single(ticker: str, strike: str, opt_type_raw: str, expiry: str, reply_chat_id: str):
    """Check OI for a single option. /oi TICKER STRIKE C/P EXPIRY"""
    try:
        opt_type = "call" if opt_type_raw.upper() in ("C","CALL") else "put"
        send_reply("Fetching OI for " + ticker + " " + strike + opt_type[0].upper() + "...", reply_chat_id)
        from fetcher import get_option_chain_oi
        from trade_journal import normalize_expiry
        expiry = normalize_expiry(expiry)
        oi = get_option_chain_oi(ticker, strike, opt_type, expiry)
        if oi is not None:
            send_reply(
                "📊 Open Interest: " + ticker + " " + strike + opt_type[0].upper() + " " + expiry + chr(10) +
                "OI: " + str(oi) + " contracts",
                reply_chat_id
            )
        else:
            send_reply("No OI data available for " + ticker + " " + strike + " — Tradier may not have this chain.", reply_chat_id)
    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_oi_all(reply_chat_id: str):
    """Check OI for all flow alerts from yesterday's watchlist. /oi all"""
    try:
        from storage import db_get
        from fetcher import get_option_chain_oi
        from trade_journal import normalize_expiry
        import json, os
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        send_reply("Checking OI for all recent flow alerts...", reply_chat_id)

        # Load analyses from Supabase
        raw = db_get("analyses_today")
        if not raw:
            send_reply("No flow analyses found. Run during or after market hours.", reply_chat_id)
            return

        data      = json.loads(raw)
        analyses  = data.get("analyses", []) if isinstance(data, dict) else data
        now_et    = datetime.now(ZoneInfo("America/New_York"))

        # Filter TRADE and WATCH only
        candidates = [
            a for a in analyses
            if a.get("result",{}).get("verdict") in ("TRADE","WATCH")
        ]

        if not candidates:
            send_reply("No TRADE/WATCH alerts found in today' analyses.", reply_chat_id)
            return

        lines = ["📊 OI Confirmation — " + now_et.strftime("%b %d"), ""]
        total = 0

        for a in candidates:
            t        = a.get("trade",{})
            ticker   = t.get("ticker","")
            strike   = str(t.get("strike",""))
            opt_type = t.get("option_type","call")
            expiry   = normalize_expiry(t.get("expiry_raw","") or t.get("expiry",""))
            orig_oi  = int(a.get("data",{}).get("open_interest",0) or 0)
            verdict  = a.get("result",{}).get("verdict","")
            score    = a.get("result",{}).get("final_score","?")

            if not ticker or not strike or not expiry:
                continue

            try:
                current_oi = get_option_chain_oi(ticker, strike, opt_type, expiry)
            except:
                current_oi = None

            v_emoji = "✅" if verdict == "TRADE" else "👀"

            if current_oi is not None and orig_oi > 0:
                oi_change = current_oi - orig_oi
                oi_pct    = round((oi_change / orig_oi) * 100, 1)
                if oi_change < -orig_oi * 0.20:
                    status = "⚠️ OI -" + str(abs(oi_pct)) + "% — likely day trade/closed"
                elif oi_change > orig_oi * 0.05:
                    status = "✅ OI +" + str(oi_pct) + "% — held/accumulated"
                else:
                    status = "➡️ OI unchanged (" + str(current_oi) + ")"
            elif current_oi is not None:
                status = "OI: " + str(current_oi) + " (no baseline)"
            else:
                status = "No data"

            orig_str = " (was " + str(orig_oi) + ")" if orig_oi > 0 else ""
            lines.append(
                v_emoji + " " + ticker + " " + strike + opt_type[0].upper() +
                " " + expiry + " [" + str(score) + "/7]" + chr(10) +
                "  " + status + orig_str
            )
            total += 1

        if total == 0:
            lines.append("No options found with sufficient data.")
        else:
            lines.append("")
            lines.append(str(total) + " alerts checked")

        send_reply(chr(10).join(lines), reply_chat_id)

    except Exception as e:
        send_reply("Error: " + str(e), reply_chat_id)

def handle_help(reply_chat_id: str):
    msg = chr(10).join([
        'FlowCheck Commands',
        '',
        'FLOW MONITORING',
        '/watchlist — active technical watches',
        '/positions — open positions with P&L',
        '/portfolio — portfolio with sector breakdown',
        '/stats — win rate (option up 50%+ = win)',
        '/find TICKER — today' + chr(39) + 's flow alerts for ticker',
        '/history — today' + chr(39) + 's alerts link',
        '',
        'RESEARCH',
        '/sentiment TICKER — price, SMAs, RSI, news, flow, insiders',
        '/sectors — sector ETF reference list',
        '/oi all — OI confirmation for all yesterday' + chr(39) + 's TRADE/WATCH alerts',
        '/oi TICKER STRIKE C/P EXPIRY — OI for specific option',
        '/backtest URL TIME — backtest a historical tweet',
        '  /backtest https://x.com/i/status/123 2026-05-19T10:30:00',
        '',
        'ACTIONS',
        '/close TICKER — close a system-tracked position',
        '',
        'TRADE JOURNAL',
        '/journal [@ACCOUNT] — open trades with last price + unrealized P&L',
        '/sync TICKER STRIKE C/P CONTRACTS PRICE [@ACCOUNT] — add position',
        '/refresh — fetch current prices for all open positions',
        '/pnl [@ACCOUNT] — P&L + slippage + fees analysis',
        '/accounts — all accounts with P&L overview',
        '/export [@ACCOUNT] — CSV for Excel',
        '/debrief — AI analysis of your trades',
        '/journal_help — full journal command reference',
        '',
        '/help — this message',
    ])
    send_reply(msg, reply_chat_id)

def handle_journal_help(reply_chat_id: str):
    msg = chr(10).join([
        'Trade Journal — Full Command Reference',
        '',
        'POSITION SYNC (quickest way to log)',
        '/sync TICKER STRIKE C/P CONTRACTS PRICE [@ACCOUNT]',
        '  Adds position if not already in journal — no screenshot needed',
        '  /sync FLNC 23 C 3 2.85 @rh_trad',
        '  /sync BE 460 C 3 13.20 @rh_brok',
        '  Multiple fills on same ticker/strike = averaged into one position',
        '  4:05 PM ET: daily check-in shows open positions',
        '',
        'LOGGING ENTRIES (manual)',
        '/entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE [DATE] [TIME] [@ACCOUNT]',
        '  /entry BE 460 C 07/17/26 3 13.20',
        '  /entry BE 460 C 07/17/26 3 13.20 2026-05-27 10:34AM @rh_brok',
        '',
        '/spread TICKER TYPE EXPIRY CONTRACTS LEG1 LEG2 CREDIT [DATE] [TIME] [@ACCOUNT]',
        '  Types: cc=credit call  cp=credit put  dc=debit call  dp=debit put  ic=iron condor',
        '  Credit — LEG1=strike sold, LEG2=strike bought:',
        '    /spread BE cc 07/17/26 3 465 460 1.25 @rh_brok',
        '  Debit — LEG1=strike bought, LEG2=strike sold:',
        '    /spread MU dc 01/15/27 2 700 1100 101.00 @rh_brok',
        '  Max profit/loss calculated automatically',
        '',
        'SCREENSHOT LOGGING',
        '  Send any broker fill confirmation photo to the bot',
        '  Auto-reads: BTO/STO/STC/BTC, account type, both strikes,',
        '              fill date/time, trading fees, regulatory fees',
        '  Duplicate protection + expiry year auto-correction built in',
        '  Send multiple photos at once — each processed independently',
        '',
        'LOGGING EXITS',
        '/exit TICKER PRICE [CONTRACTS] [DATE] [TIME]',
        '  Full exit:        /exit BE 18.50',
        '  Partial (2 of 3): /exit BE 18.50 2 2026-05-29 2:15PM',
        '  Screenshot:       send STC/BTC confirmation photo',
        '',
        'ADDING TO POSITION',
        '/add TICKER CONTRACTS PRICE [DATE] [TIME]',
        '  /add BE 1 14.00 2026-05-28 11:00AM',
        '  Blended average entry calculated automatically',
        '',
        'SKIPPED ALERTS',
        '/missed TICKER [REASON]',
        '',
        'VIEWING',
        '/journal [@ACCOUNT] — newest first, shows last price + P&L',
        '/refresh — fetch current prices (Tradier) for all positions',
        '/pnl [@ACCOUNT] — P&L + slippage + fees (10+ trades)',
        '/accounts — all accounts with P&L and deployed capital',
        '/export [@ACCOUNT] — CSV for Excel',
        '/debrief — AI journal analysis (needs 3+ closed trades)',
        '',
        'WEB TABLE (/j for quick link)',
        '  Click any cell to edit inline — ✓ Save / ✗ Cancel',
        '  Red ✕ button to delete a row',
        '  Stats bar: Today P&L, unrealized, exposure, win rate per account',
        '  Account filter tabs + sort toggle + CSV download',
        '',
        'DAILY AUTOMATION',
        '  4:02 PM — EOD pricing via Tradier for all open positions',
        '  4:05 PM — position check-in (compare journal vs broker)',
        '  4:10 PM — daily P&L summary',
        '',
        'EDITING',
        '/edit TICKER [@ACCOUNT] FIELD VALUE',
        '  /edit BE entry_time 10:34AM',
        '  /edit MU @rh_brok long_strike 700',
        '  Fields: entry_date  entry_time  exit_date  exit_time',
        '          entry_price  contracts  expiry  strike',
        '          long_strike  short_strike  credit  spread_width',
        '          note  option_type  account_id  fc_score  fc_verdict',
        '',
        'DELETING',
        '/delete TICKER [@ACCOUNT]',
        '  /delete BE @rh_trad',
        '',
        'TAGGING',
        '/tag TICKER #tag1 #tag2',
        '',
        'ACCOUNTS',
        '/account add ID NAME SIZE',
        '  /account add rh_brok Margin 100000',
        '  /account add rh_trad IRA 125000',
        '/account delete ID',
        '',
        'TIME FORMAT — all times ET',
        '  10:34AM   2:30PM   10:34   14:30',
        '',
        '/journal_help — this message',
    ])
    send_reply(msg, reply_chat_id)

def handle_journal_help(reply_chat_id: str):
    lines = [
        'Trade Journal — Full Command Reference',
        '',
        'LOGGING ENTRIES',
        '/entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE [DATE] [TIME] [@ACCOUNT]',
        '  Single leg options only',
        '',
        '/spread TICKER TYPE EXPIRY CONTRACTS SHORT LONG CREDIT [DATE] [TIME] [@ACCOUNT]',
        '  Types: cc=credit call  cp=credit put  dc=debit call  dp=debit put  ic=iron condor',
        '  /spread BE cc 07/17/26 3 465 460 1.25 2026-05-27 10:34AM @RH_Brok',
        '  /spread NVDA cp 06/20/26 1 130 125 0.85',
        '  Max profit/loss calculated automatically from width and credit',
        '',
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
    client   = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))
    img_b64  = base64.standard_b64encode(image_bytes).decode()
    cap_note = (chr(10) + "User caption: " + caption) if caption else ""

    prompt = chr(10).join([
        "You are reading a brokerage app screenshot." + cap_note,
        "Extract trade details if this shows an order fill or confirmation.",
        "Return ONLY valid JSON. If not a trade screen return {" + chr(34) + "error" + chr(34) + ": " + chr(34) + "not_a_trade" + chr(34) + "}.",
        "",
        "JSON fields:",
        "  action: entry or exit",
        "  order_type: BTO or STO or STC or BTC",
        "  ticker: stock symbol",
        "  expiry: MM/DD/YY",
        "  contracts: integer",
        "  option_type: call or put",
        "  date: YYYY-MM-DD from Filled timestamp",
        "  time: 12-hour AM/PM ET from Filled timestamp",
        "  account_type: account label from screen",
        "  confidence: high or medium or low",
        "",
        "For single leg only:",
        "  strike: the strike price",
        "  price: fill price per contract",
        "",
        "For spreads (two strikes shown):",
        "  is_spread: true",
        "  spread_type: credit_call or credit_put or debit_call or debit_put or iron_condor",
        "  short_strike: strike sold",
        "  long_strike: strike bought",
        "  credit: net premium per share",
        "",
        "Optional fields (include if visible):",
        "  fees: trading commission/fee in dollars (e.g. 0.65)",
        "  reg_fees: regulatory fees shown separately (e.g. 0.02)",
        "",
        "Rules:",
        "  BTO or buy-to-open = entry. STO or sell-to-open = entry (put sell). STC or sell-to-close = exit. BTC or buy-to-close = exit.",
        "  IMPORTANT: Sell to Open (STO) means selling a put or call to open a new position — this is an ENTRY not an exit.",
        "  If you see 'Sell to Open', 'STO', or 'Sold to Open' set action=entry and order_type=STO.",
        "  ROBINHOOD SPECIFIC: Robinhood shows 'Sold 1 AAPL $150 Put' with status 'Open' for STO orders.",
        "  On Robinhood: if the order shows 'Sold' (not 'Bought') AND status is 'Open' = this is STO (sell to open).",
        "  On Robinhood: if the order shows 'Sold' AND status is 'Closed' = this is STC (sell to close).",
        "  On Robinhood: if the order shows 'Bought' AND status is 'Open' = this is BTO (buy to open).",
        "  On Robinhood: if the order shows 'Bought' AND status is 'Closed' = this is BTC (buy to close).",
        "  Look for the word 'Sold' or 'Bought' at the top of the fill confirmation — this is the key signal.",
        "  date and time must come from Filled timestamp on screen.",
        "  time format must be 10:34AM or 2:30PM with no space before AM/PM.",
        "  strike is critical — look carefully for the strike price number (e.g. 105, 460, 130).",
        "  strike appears near the option description e.g. DELL $105 Call or 105C.",
        "  fees appear near bottom of screen as Commission, Fee, or Regulatory Fee.",
        "  If two different strikes visible = spread.",
        "  expiry year: if year appears to be in the past (before 2026), correct it to 2026 or 2027.",
        "  expiry format must be MM/DD/YY e.g. 09/18/26 not 09/18/25 or 09/18/2025.",
    ])

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
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
        raw = resp.content[0].text.strip() if resp.content else ""
        print("[PHOTO] Raw: " + raw[:200])
        if not raw:
            return {"error": "empty_response"}
        raw  = raw.replace("", "").strip()
        start_i = raw.find("{")
        end_i   = raw.rfind("}") + 1
        if start_i >= 0 and end_i > start_i:
            raw = raw[start_i:end_i]
        data = json.loads(raw)
        print("[PHOTO] Parsed: " + str(data))
        return data
    except Exception as e:
        print("[PHOTO] Vision error: " + str(e))
        return None

def handle_trade_photo(photo_list: list, caption: str, reply_chat_id: str):
    """Handle a trade screenshot sent to the bot."""
    send_reply("Reading screenshot...", reply_chat_id)

    # Extract @account tag from caption BEFORE passing to vision
    import re as _re
    account_id = "default"
    caption_clean = caption or ""
    account_match = _re.search("@([A-Za-z0-9_]+)", caption_clean)
    if account_match:
        account_id = account_match.group(1).lower()
        caption_clean = _re.sub("@[A-Za-z0-9_]+", "", caption_clean).strip()
        print("[PHOTO] Account tag from caption: @" + account_id)

    image_bytes = download_telegram_photo(photo_list)
    if not image_bytes:
        send_reply(
            "Could not download photo. Try again or use:" + chr(10) +
            "/entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE DATE TIME",
            reply_chat_id
        )
        return

    data = parse_trade_screenshot(image_bytes, caption_clean)

    # If no @account in caption, try to map account_type from vision
    if account_id == "default" and data and not data.get("error"):
        account_type_raw = (data.get("account_type","") or "").lower()
        if account_type_raw:
            print("[PHOTO] Account type from screenshot: " + account_type_raw)
            # Load accounts and try to match
            try:
                from trade_journal import get_accounts
                accounts = get_accounts()
                # Try to match by account name keywords
                type_keywords = {
                    "traditional ira": ["ira","trad","traditional"],
                    "roth ira":        ["roth"],
                    "individual":      ["individual","margin","brokerage","taxable"],
                    "cash":            ["cash"],
                }
                for aid, acc in accounts.items():
                    aname = acc.get("name","").lower()
                    # Check if account name matches any keyword from screenshot type
                    for screen_type, keywords in type_keywords.items():
                        if any(kw in account_type_raw for kw in keywords):
                            if any(kw in aname for kw in keywords):
                                account_id = aid
                                print("[PHOTO] Matched account: @" + aid + " from " + account_type_raw)
                                break
                    if account_id != "default":
                        break
            except Exception as e:
                print("[PHOTO] Account match error: " + str(e))

    if not data:
        send_reply("Vision parse failed. Try again or log manually.", reply_chat_id)
        return

    if data.get("error") == "empty_response":
        # Vision failed completely — try to help with manual entry
        # Extract any hints from caption
        cap_upper = (caption or "").upper()
        is_spread_hint = any(w in cap_upper for w in ["SPREAD","CC","CP","DC","DP","IC","CREDIT","DEBIT"])
        if is_spread_hint:
            template = "/spread TICKER TYPE EXPIRY CONTRACTS LEG1 LEG2 CREDIT DATE TIME @ACCOUNT" + chr(10) + chr(10) + "Types: cc cp dc dp ic"
        else:
            template = "/entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE DATE TIME @ACCOUNT"
        send_reply(
            "Screenshot unreadable — Claude could not parse this image." + chr(10) + chr(10) +
            "Log manually:" + chr(10) + template,
            reply_chat_id
        )
        return

    if data.get("error") == "not_a_trade":
        send_reply(
            "Not recognized as a trade confirmation." + chr(10) + chr(10) +
            "For Robinhood spreads — send the screenshot showing both legs filled." + chr(10) +
            "Or log manually:" + chr(10) +
            "Single leg: /entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE DATE TIME" + chr(10) +
            "Spread:     /spread TICKER TYPE EXPIRY CONTRACTS LEG1 LEG2 CREDIT DATE TIME @ACCOUNT",
            reply_chat_id
        )
        return

    if data.get("error"):
        send_reply("Could not read: " + str(data.get("error")), reply_chat_id)
        return

    action     = data.get("action","entry")
    ticker     = (data.get("ticker","") or "").upper()
    strike     = str(data.get("strike","") or "")
    opt_type   = data.get("option_type","call")
    expiry_raw = data.get("expiry","")
    try:
        from trade_journal import normalize_expiry
        from datetime import date as _date, datetime as _dt
        expiry = normalize_expiry(expiry_raw)
        # Sanity check — if expiry is in the past, add 1 year
        if expiry:
            for fmt in ("%m/%d/%y", "%m/%d/%Y"):
                try:
                    exp_dt = _dt.strptime(expiry, fmt).date()
                    if exp_dt < _date.today():
                        # Add 1 year
                        corrected = exp_dt.replace(year=exp_dt.year + 1)
                        expiry    = corrected.strftime("%m/%d/%y")
                        print(f"[PHOTO] Expiry year corrected: {expiry_raw} → {expiry}")
                    break
                except:
                    continue
    except:
        expiry = expiry_raw
    contracts  = data.get("contracts")
    price      = data.get("price")
    date_str   = data.get("date","")
    time_str   = data.get("time","")
    confidence = data.get("confidence","medium")
    order_type = data.get("order_type","")  # BTO/STO/STC/BTC

    # Override action from order_type if present
    if order_type in ("BTO","STO"):
        action = "entry"
    elif order_type in ("STC","BTC"):
        action = "exit"

    is_spread   = data.get("is_spread", False)
    spread_type = data.get("spread_type")
    short_stk   = data.get("short_strike")
    long_stk    = data.get("long_strike")
    spread_cred = data.get("credit")

    missing = []
    if not ticker:    missing.append("ticker")
    if not expiry:    missing.append("expiry")
    if not contracts: missing.append("contracts")
    # For spreads: need short+long strike and credit instead of strike+price
    if is_spread:
        if not short_stk: missing.append("short_strike")
        if not long_stk:  missing.append("long_strike")
        if not spread_cred and not price: missing.append("credit")
    else:
        if not strike: missing.append("strike")
        if not price:  missing.append("price")

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
            fill_date = date_str if date_str else None
            fill_time = time_str if time_str else None
            acc_label = (" [@" + account_id + "]") if account_id != "default" else " [unassigned]"
            acct_type = data.get("account_type","")
            acct_note = (" — " + acct_type) if acct_type else ""
            ot_label  = (" " + order_type) if order_type else ""

            if is_spread and spread_type and (short_stk or long_stk):
                # SPREAD
                s_str = str(short_stk) if short_stk else str(long_stk)
                l_str = str(long_stk)  if long_stk  else str(short_stk)
                try:    width = abs(float(s_str) - float(l_str))
                except: width = None
                # Credit/debit is always positive — use abs()
                cred = abs(float(spread_cred or price or 0))
                sopt  = "call" if "call" in (spread_type or "") else "put"
                trade = add_entry(
                    ticker, s_str, sopt, expiry, int(contracts), 0.0,
                    fill_date, fill_time, account_id,
                    spread_type=spread_type,
                    short_strike=s_str, long_strike=l_str,
                    spread_width=width, credit=cred,
                )
                # Store order_type on trade
                if order_type:
                    from trade_journal import load_journal, save_journal
                    _j = load_journal()
                    for _t in _j.get("trades",[]):
                        if _t.get("id") == trade.get("id"):
                            _t["order_type"] = order_type
                            break
                    save_journal(_j)
                stype = (spread_type or "").replace("_"," ").upper()
                mp    = trade.get("max_profit","?")
                ml    = trade.get("max_loss","?")
                auto  = trade.get("entry_auto_filled", True)
                tnote = ("fill time auto-filled" if auto else
                         "filled " + (date_str or "") + " " + (time_str or "").replace(" ",""))
                parts = [
                    "Spread logged" + conf_note + ot_label + acc_label + acct_note + ":",
                    ticker + " " + stype + " " + expiry,
                    "Short: $" + s_str + " | Long: $" + l_str +
                    (" | Width: $" + str(round(width,2)) if width else ""),
                    "Premium: $" + str(cred) + " x" + str(contracts) +
                    " = $" + str(round(float(cred or 0)*int(contracts)*100,2)),
                    "Max profit: $" + str(mp) + " | Max loss: $" + str(ml),
                    tnote,
                    "",
                    "Exit: /exit " + ticker + " PRICE DATE TIME",
                ]
                send_reply(chr(10).join(parts), reply_chat_id)

            else:
                # SINGLE LEG
                _fees     = data.get("fees")
                _reg_fees = data.get("reg_fees")
                trade = add_entry(
                    ticker, strike, otype, expiry,
                    int(contracts), float(price),
                    fill_date, fill_time, account_id,
                    fees=_fees, reg_fees=_reg_fees
                )
                if order_type:
                    from trade_journal import load_journal, save_journal
                    _j = load_journal()
                    for _t in _j.get("trades",[]):
                        if _t.get("id") == trade.get("id"):
                            _t["order_type"] = order_type
                            break
                    save_journal(_j)
                auto  = trade.get("entry_auto_filled", True)
                tnote = (" (fill time auto-filled)" if auto else
                         " filled " + (date_str or "") + " " + (time_str or "").replace(" ",""))
                total_cost = round(float(price)*int(contracts)*100, 2)
                fee_note   = ""
                if data.get("fees"):
                    fee_note = " | Fees: $" + str(data["fees"])
                    if data.get("reg_fees"):
                        fee_note += " + $" + str(data["reg_fees"]) + " reg"
                sizing_note = ""
                if trade.get("_deployed") is not None:
                    sizing_note = (
                        chr(10) + "Deployed: $" + str(trade["_deployed"]) +
                        " of $" + str(int(trade["_acc_size"])) +
                        " (" + str(trade["_deployed_pct"]) + "%) — " +
                        "$" + str(trade["_remaining"]) + " remaining"
                    )
                parts = [
                    "Entry logged" + conf_note + ot_label + acc_label + acct_note + ":",
                    ticker + " " + strike + otype + " " + expiry +
                    " x" + str(contracts) + " @ $" + str(price),
                    "Total: $" + str(total_cost) + fee_note + tnote,
                    sizing_note,
                    "",
                    "Exit: /exit " + ticker + " PRICE DATE TIME",
                    "Edit: /edit " + ticker + " FIELD VALUE",
                ]
                parts = [p for p in parts if p != ""]
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
            media_group_id = msg.get("media_group_id")
            if media_group_id:
                # Part of a multi-photo send — process each independently
                # Caption only appears on first photo of group in Telegram
                print("[CMD] Photo in media group: " + str(media_group_id))
            try:
                handle_trade_photo(photo, caption, from_id)
            except Exception as e:
                print("[CMD] Photo error: " + str(e))
            continue

        if text.startswith("/"):
            try:
                handle_command(text, from_id)
            except Exception as e:
                print(f"[CMD] Command error: {e}")
