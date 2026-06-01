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


def handle_evaluate_command(from_chat_id, ticker_filter=None, account_filter=None,
                             range_start=1, range_end=20):
    """Evaluate open positions: HOLD, TRIM, or CLOSE. Filters by ticker/account/range."""
    import os
    import anthropic as _ant
    from storage import load_data
    from fetcher import fetch_price, fetch_vix
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # Build status message
    parts = []
    if ticker_filter:  parts.append(ticker_filter.upper())
    if account_filter: parts.append("@" + account_filter)
    rng_str = str(range_start) + "-" + str(range_end)
    if range_start != 1 or range_end != 20:
        parts.append("pos " + rng_str)
    label = " + ".join(parts) if parts else "all positions"
    send_reply("Evaluating " + label + "...", from_chat_id)

    try:
        journal  = load_data("journal", "/tmp/journal.json", {"trades": []})
        all_open = [t for t in journal.get("trades", [])
                    if t.get("status", "").upper() != "CLOSED"]

        # Apply ticker filter
        if ticker_filter:
            all_open = [t for t in all_open
                        if (t.get("ticker","") or "").upper() == ticker_filter.upper()]
            if not all_open:
                send_reply("No open positions for " + ticker_filter.upper() + ".", from_chat_id)
                return

        # Apply account filter or dedup by ticker+strike
        if account_filter:
            open_trades = [t for t in all_open
                           if (t.get("account_id","") or "").lower().lstrip("@") == account_filter]
            if not open_trades:
                send_reply("No open positions for @" + account_filter + ".", from_chat_id)
                return
        else:
            seen    = {}
            deduped = []
            for t in all_open:
                key = (t.get("ticker","") + "_" + str(t.get("strike","")) +
                       "_" + (t.get("option_type","") or "") + "_" + (t.get("expiry","") or ""))
                if key not in seen:
                    seen[key] = 0
                    t2 = dict(t)
                    deduped.append(t2)
                seen[key] += 1
            for t2 in deduped:
                key = (t2.get("ticker","") + "_" + str(t2.get("strike","")) +
                       "_" + (t2.get("option_type","") or "") + "_" + (t2.get("expiry","") or ""))
                t2["_acct_count"] = seen.get(key, 1)
            open_trades = deduped

        if not open_trades:
            send_reply("No open positions to evaluate.", from_chat_id)
            return

        try:
            vix     = fetch_vix()
            vix_str = str(round(vix, 1)) if vix else "?"
        except Exception:
            vix_str = "?"

        now_et    = datetime.now(ZoneInfo("America/New_York"))
        client    = _ant.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        results   = []
        total_pnl = 0.0

        raw_count   = len(all_open)   # total raw positions before dedup
        total_count = len(open_trades) # after dedup/filter
        # Apply range (1-indexed)
        range_end_actual = min(range_end, total_count)
        slice_start      = max(0, range_start - 1)
        slice_end        = range_end_actual
        page_trades      = open_trades[slice_start:slice_end]

        # Hint about remaining positions
        dedup_note = (" (" + str(raw_count) + " raw, " + str(total_count) + " unique)" 
                      if raw_count != total_count and not account_filter and not ticker_filter else "")
        if total_count > range_end:
            remaining = total_count - range_end
            send_reply(
                str(total_count) + " unique positions" + dedup_note + " | showing " +
                str(range_start) + "-" + str(range_end_actual) +
                " | /eval " + str(range_end + 1) + "-" + str(min(range_end + 20, total_count)) +
                " for next " + str(min(remaining, 20)) +
                " | /eval @acct for per-account view",
                from_chat_id
            )
        elif total_count > 20 and range_start == 1:
            send_reply(
                str(total_count) + " unique positions" + dedup_note +
                " — showing 1-20 | /eval 21-" + str(total_count) + " for rest",
                from_chat_id
            )
        elif dedup_note:
            send_reply(
                str(total_count) + " unique positions" + dedup_note +
                " | /eval @acct for per-account breakdown",
                from_chat_id
            )

        print(f"[EVAL] Evaluating {len(page_trades)} positions ({range_start}-{range_end_actual} of {total_count})")
        for t in page_trades:
            ticker     = t.get("ticker", "?")
            strike     = str(t.get("strike", "?"))
            opt_type   = ((t.get("option_type", "call") or "call").upper() + " ")[0]
            expiry     = t.get("expiry", "?")
            entry_px   = float(t.get("entry_price", 0) or 0)
            curr_px    = float(t.get("last_price", 0) or entry_px)
            contracts  = int(t.get("contracts", 1) or 1)
            order_type = (t.get("order_type", "BTO") or "BTO").upper()
            is_sto     = order_type == "STO" or t.get("fill_type", "") == "PUT_SELL_BID"
            account    = t.get("account_id", "")
            score      = t.get("score", "?")
            verdict_orig = t.get("verdict", "?")
            acct_count   = t.get("_acct_count", 1)

            try:
                stock_px  = fetch_price(ticker)
                stock_str = "$" + str(round(stock_px, 2)) if stock_px else "?"
            except Exception:
                stock_str = "?"

            dte = None
            try:
                parts2 = expiry.split("/")
                m2, d2, y2 = parts2
                y2     = "20" + y2 if len(y2) == 2 else y2
                exp_dt = datetime(int(y2), int(m2), int(d2), tzinfo=ZoneInfo("America/New_York"))
                dte    = (exp_dt - now_et).days
            except Exception:
                pass
            dte_str = str(dte) + "d" if dte is not None else "?"

            # Spread-aware P&L
            is_spread = (t.get("is_spread") or bool(t.get("spread_type"))
                          or t.get("legs","single") == "spread"
                          or "/" in str(t.get("strike","")))

            # Auto-parse long/short from strike field if format is "1100/1200"
            long_s  = t.get("long_strike")
            short_s = t.get("short_strike")
            if not long_s or not short_s:
                raw_strike = str(t.get("strike",""))
                if "/" in raw_strike:
                    parts_s = raw_strike.split("/")
                    # Lower strike = long for calls (bought), higher = short (sold)
                    try:
                        s1 = float(parts_s[0].replace("C","").replace("P","").strip())
                        s2 = float(parts_s[1].replace("C","").replace("P","").strip())
                        opt_lo = (t.get("option_type","call") or "call").lower()
                        if "call" in opt_lo:
                            long_s  = str(int(min(s1,s2)) if min(s1,s2)==int(min(s1,s2)) else min(s1,s2))
                            short_s = str(int(max(s1,s2)) if max(s1,s2)==int(max(s1,s2)) else max(s1,s2))
                        else:
                            long_s  = str(int(max(s1,s2)) if max(s1,s2)==int(max(s1,s2)) else max(s1,s2))
                            short_s = str(int(min(s1,s2)) if min(s1,s2)==int(min(s1,s2)) else min(s1,s2))
                    except: pass

            # Infer debit/credit from order_type if spread_type not set
            if not t.get("spread_type"):
                ot_lower = (t.get("order_type","BTO") or "BTO").upper()
                is_debit_default = ot_lower == "BTO"  # BTO = debit spread, STO = credit spread
            else:
                is_debit_default = "debit" in (t.get("spread_type","") or "")

            if is_spread and long_s and short_s:
                print(f"[EVAL] Spread detected: {ticker} long={long_s} short={short_s} debit={is_debit_default} expiry={expiry}")
                try:
                    from fetcher import fetch_spread_value as _fsv
                    is_debit2 = is_debit_default
                    sv        = _fsv(ticker, t.get("option_type","call"), expiry,
                                     long_s, short_s, is_debit2)
                    net_val   = sv.get("net_value")
                    entry_net = float(t.get("credit",0) or t.get("entry_price",0) or 0)
                    lp        = sv.get("long_price","?")
                    sp2       = sv.get("short_price","?")
                    print(f"[EVAL] Spread result: long={lp} short={sp2} net={net_val} entry_net={entry_net}")
                    stock_str = "L$" + str(lp) + "/S$" + str(sp2)
                    if net_val is not None and entry_net > 0:
                        if is_debit2:
                            pnl_usd = round((net_val - entry_net) * contracts * 100, 0)
                            pnl_pct = round((net_val - entry_net) / entry_net * 100, 1)
                        else:
                            pnl_usd = round((entry_net - net_val) * contracts * 100, 0)
                            pnl_pct = round((entry_net - net_val) / entry_net * 100, 1)
                        total_pnl += pnl_usd
                        sign     = "+" if pnl_pct >= 0 else ""
                        sign_usd = "+" if pnl_usd >= 0 else ""
                        pnl_str  = sign + str(pnl_pct) + "% (" + sign_usd + "$" + str(int(pnl_usd)) + ")"
                    elif net_val is not None:
                        pnl_str = "net $" + str(net_val) + " (no entry cost)"
                    else:
                        pnl_str = "spread legs unavailable"
                except Exception as _se:
                    pnl_str = "spread error: " + str(_se)[:30]
            elif entry_px > 0 and curr_px > 0:
                if is_sto:
                    pnl_pct = round((entry_px - curr_px) / entry_px * 100, 1)
                    pnl_usd = round((entry_px - curr_px) * contracts * 100, 0)
                else:
                    pnl_pct = round((curr_px - entry_px) / entry_px * 100, 1)
                    pnl_usd = round((curr_px - entry_px) * contracts * 100, 0)
                total_pnl += pnl_usd
                sign     = "+" if pnl_pct >= 0 else ""
                sign_usd = "+" if pnl_usd >= 0 else ""
                pnl_str  = sign + str(pnl_pct) + "% (" + sign_usd + "$" + str(int(pnl_usd)) + ")"
            else:
                pnl_str = "unknown (no price data)"
            # Calculate ITM/OTM explicitly so Haiku doesn't guess
            itm_otm_str = "unknown"
            try:
                if is_spread:
                    # For spreads use long strike vs stock price
                    ref_strike = float(long_s) if long_s else float(strike.split("/")[0].replace("C","").replace("P","").strip())
                else:
                    ref_strike = float(strike)
                # stock_str at this point may be "L$xxx/S$xxx" for spreads — get actual stock price
                raw_stock_str = stock_str
                if raw_stock_str.startswith("L$"):
                    # Get fresh stock price for ITM/OTM calc
                    try:
                        from fetcher import fetch_price as _fp2
                        _spx = _fp2(ticker)
                        raw_stock_str = "$" + str(_spx) if _spx else "?"
                    except: raw_stock_str = "?"
                stock_num = float(raw_stock_str.replace("$","").replace(",","")) if raw_stock_str not in ("?","") else None
                if stock_num and ref_strike:
                    if "C" in opt_type.upper():
                        diff_pct    = round((stock_num - ref_strike) / ref_strike * 100, 1)
                        itm_otm_str = ("ITM " if diff_pct > 0 else "OTM ") + str(abs(diff_pct)) + "%"
                    else:
                        diff_pct    = round((ref_strike - stock_num) / ref_strike * 100, 1)
                        itm_otm_str = ("ITM " if diff_pct > 0 else "OTM ") + str(abs(diff_pct)) + "%"
            except Exception:
                pass

            # Build clear prompt separating option price from stock price
            type_str2 = ("STO put sell" if is_sto else "BTO long call/put")
            if is_spread:
                type_str2 = ("DEBIT SPREAD" if is_debit_default else "CREDIT SPREAD") +                     " | Long $" + str(long_s) + " / Short $" + str(short_s or t.get("short_strike","?"))

            prompt = chr(10).join([
                "Evaluate this options position. Reply ONLY in this format:",
                "VERDICT: HOLD or TRIM or CLOSE",
                "REASON: one sentence, max 20 words",
                "",
                "POSITION DETAILS:",
                "Ticker: " + ticker,
                "Option: " + strike + opt_type + " expiring " + expiry + " (" + dte_str + " left)",
                "Type: " + type_str2,
                "UNDERLYING STOCK PRICE: " + (raw_stock_str if is_spread else stock_str) + " (this is the stock, NOT the option)",
                "Strike vs Stock: " + itm_otm_str + " — " + ("stock BELOW strike = OTM call" if "OTM" in itm_otm_str and "C" in opt_type.upper() else "stock ABOVE strike = ITM call" if "ITM" in itm_otm_str and "C" in opt_type.upper() else ""),
                "OPTION P&L: entry $" + str(entry_px) + " -> current $" + str(curr_px) + " | " + pnl_str,
                ("Spread leg prices: " + stock_str if is_spread else ""),
                "VIX: " + vix_str,
                "Original score: " + str(score) + "/7 " + str(verdict_orig),
            ])

            print(f"[EVAL] Scoring {ticker} {strike}{opt_type} [{dte_str}] PNL={pnl_str} {itm_otm_str}")
            try:
                resp     = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=80,
                    messages=[{"role": "user", "content": prompt}]
                )
                raw      = resp.content[0].text.strip()
                raw_lines= raw.splitlines()
                v_line   = next((l for l in raw_lines if "VERDICT:" in l), "VERDICT: HOLD")
                r_line   = next((l for l in raw_lines if "REASON:" in l), "REASON: Monitor position.")
                verdict_eval = v_line.replace("VERDICT:", "").strip()
                reason_eval  = r_line.replace("REASON:", "").strip()
            except Exception as he:
                verdict_eval = "HOLD"
                reason_eval  = "Haiku error: " + str(he)[:40]

            EMOJIS = {"CLOSE": "\U0001f534", "TRIM": "\U0001f7e1", "HOLD": "\U0001f7e2"}
            em = EMOJIS.get(verdict_eval, "\U0001f7e2")

            if account_filter:
                acct_str = " [" + account + "]" if account else ""
            elif acct_count > 1:
                acct_str = " (" + str(acct_count) + " accts)"
            else:
                acct_str = " [" + account + "]" if account else ""

            legs_badge = " [spread]" if is_spread else ""
            if is_spread and long_s and short_s:
                spread_title = "Long $" + str(long_s) + opt_type + " / Short $" + str(short_s) + opt_type
            else:
                spread_title = strike + opt_type
            line1 = em + " " + ticker + " " + spread_title + " [" + dte_str + "]" + legs_badge + acct_str + " -- " + verdict_eval
            line2 = "   " + pnl_str + " | " + stock_str
            line3 = "   -> " + reason_eval

            # For losing positions, fetch recent news to explain reversal
            news_lines = []
            try:
                is_losing = pnl_usd < -50 if isinstance(pnl_usd, (int, float)) else False
                if is_losing:
                    from news_check import fetch_recent_news
                    articles = fetch_recent_news(ticker, hours=72)[:2]
                    for art in articles:
                        headline = art.get("headline","")[:70]
                        url      = art.get("url","")
                        if headline and url:
                            news_lines.append('   📰 <a href="' + url + '">' + headline + '</a>')
                        elif headline:
                            news_lines.append("   📰 " + headline)
            except Exception: pass

            result_block = line1 + "\n" + line2 + "\n" + line3
            if news_lines:
                result_block += "\n" + "\n".join(news_lines)
            results.append(result_block)

        sep   = "-" * 20
        rng_label = " [" + str(range_start) + "-" + str(range_end_actual) + "/" + str(total_count) + "]" if total_count > 20 else ""
        hdr   = "=== Evaluate: " + label + rng_label + " | " + now_et.strftime("%b %d %I:%M%p ET") + " ==="
        sign  = "+" if total_pnl >= 0 else ""
        total = "Total P&L: " + sign + "$" + str(int(total_pnl))
        msg   = hdr + "\n" + "\n\n".join(results) + "\n" + sep + "\n" + total
        print(f"[EVAL] Sending result: {len(results)} positions, {len(msg)} chars")
        send_reply(msg, from_chat_id)

    except Exception as e:
        import traceback
        err = traceback.format_exc()[-200:]
        send_reply("Evaluate error: " + str(e) + "\n" + err, from_chat_id)
        print("[EVALUATE] Error: " + str(e))


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
    elif cmd in ("sent", "sentiment") and args:
        handle_sentiment(args[0].upper(), from_chat_id)

    elif cmd in ("eval", "evaluate", "review"):
        # /eval                  — all positions 1-20, deduped
        # /eval 1-20             — positions 1-20
        # /eval 21-40            — positions 21-40
        # /eval NVDA             — specific ticker only
        # /eval NVDA @rh_ira     — specific ticker + account
        # /eval @rh_ira          — specific account only
        tkr_f   = None
        act_f   = None
        rng_start = 1
        rng_end   = 20
        import re as _re
        for arg in args:
            if arg.startswith("@"):
                act_f = arg.lstrip("@").lower()
            elif _re.match(r"^[0-9]+-[0-9]+$", arg):
                parts3 = arg.split("-")
                rng_start = int(parts3[0])
                rng_end   = int(parts3[1])
            elif arg.upper() == arg and len(arg) <= 5 and arg.isalpha():
                tkr_f = arg.upper()
        handle_evaluate_command(from_chat_id, ticker_filter=tkr_f, account_filter=act_f,
                                 range_start=rng_start, range_end=rng_end)

    elif cmd in ("journal", "jv", "journal-view"):
        import os as _os
        base_url = _os.environ.get("BASE_URL","https://web-production-19e44.up.railway.app")
        send_reply("Trade Journal" + chr(10) + base_url + "/journal-view", from_chat_id)


    elif cmd in ("count", "cnt"):
        try:
            from storage import load_data as _ld3
            journal    = _ld3("journal", "/tmp/journal.json", {"trades":[]})
            all_open   = [t for t in journal.get("trades",[]) if t.get("status","").upper() != "CLOSED"]
            # Count by account
            from collections import Counter
            acct_counts = Counter(t.get("account_id","unknown") for t in all_open)
            lines = ["📊 Open positions: " + str(len(all_open))]
            for acct, cnt in sorted(acct_counts.items()):
                lines.append("  @" + str(acct) + ": " + str(cnt))
            # Unique tickers
            tickers = sorted(set(t.get("ticker","") for t in all_open))
            lines.append("🎯 Unique tickers (" + str(len(tickers)) + "): " + ", ".join(tickers))
            send_reply(chr(10).join(lines), from_chat_id)
        except Exception as e:
            send_reply("Count error: " + str(e), from_chat_id)

    elif cmd in ("flow", "flows") and args:
        # /flow NVDA — show today's top flows for a ticker from stored history
        tkr_f2 = args[0].upper()
        try:
            from storage import load_data as _ld4
            from datetime import datetime
            from zoneinfo import ZoneInfo
            from flow_intelligence import load_flow_history
            history  = load_flow_history() or []
            today    = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            # Filter by ticker and today — guard against non-dict entries
            ticker_flows = []
            for f in history:
                if not isinstance(f, dict):
                    continue
                f_ticker = (f.get("ticker","") or "").upper()
                f_date   = (f.get("date","") or f.get("timestamp","")[:10] or "")
                if f_ticker == tkr_f2 and f_date == today:
                    ticker_flows.append(f)
            # Also check analyses for today
            analyses_data = _ld4("analyses_today", "/tmp/analyses.json", []) or []
            for a in analyses_data:
                if not isinstance(a, dict):
                    continue
                t = a.get("trade",{}) or {}
                if not isinstance(t, dict):
                    continue
                if (t.get("ticker","").upper() == tkr_f2 and
                    a.get("date","") == today and
                    a not in ticker_flows):
                    ticker_flows.append({
                        "ticker":     t.get("ticker",""),
                        "strike":     t.get("strike",""),
                        "option_type":t.get("option_type",""),
                        "expiry":     t.get("expiry",""),
                        "premium":    t.get("premium",0),
                        "fill_type":  t.get("fill_type",""),
                        "vol_oi":     t.get("vol_oi_ratio",0),
                        "is_sweep":   t.get("is_sweep",False),
                        "source":     t.get("source",""),
                        "score":      a.get("result",{}).get("final_score","?"),
                        "verdict":    a.get("result",{}).get("verdict","?"),
                        "time":       a.get("time",""),
                    })

            if not ticker_flows:
                send_reply("No flows found for " + tkr_f2 + " today.", from_chat_id)
            else:
                # Sort by premium descending
                ticker_flows.sort(key=lambda x: float(x.get("premium",0) or 0), reverse=True)
                total_prem = sum(float(f.get("premium",0) or 0) for f in ticker_flows)
                total_str  = ("$" + str(round(total_prem/1000000,1)) + "M"
                              if total_prem >= 1000000
                              else "$" + str(round(total_prem/1000,0)) + "K")
                lines = ["=== " + tkr_f2 + " Flow Today — " +
                         datetime.now(ZoneInfo("America/New_York")).strftime("%b %d") + " ==="]
                for i, f in enumerate(ticker_flows[:10], 1):
                    otype   = (f.get("option_type","call") or "call")[0].upper()
                    strike  = f.get("strike","?")
                    expiry  = f.get("expiry","?")
                    prem    = float(f.get("premium",0) or 0)
                    prem_s  = ("$" + str(round(prem/1000000,1)) + "M"
                               if prem >= 1000000
                               else "$" + str(int(prem/1000)) + "K")
                    fill    = (f.get("fill_type","") or "").replace("_"," ")
                    vol_oi  = f.get("vol_oi",0) or f.get("vol_oi_ratio",0) or 0
                    sweep   = " ⚡Sweep" if f.get("is_sweep") else ""
                    src     = " 🅱" if f.get("source") == "bullflow" else " 🐦" if f.get("source") == "flowgod" else ""
                    score   = f.get("score","")
                    verdict = f.get("verdict","")
                    sc_str  = " [" + str(score) + "/7 " + str(verdict) + "]" if score else ""
                    tm_str  = " @" + str(f.get("time","")) if f.get("time") else ""
                    vol_str = " · " + str(round(float(vol_oi),1)) + "x Vol/OI" if vol_oi else ""
                    lines.append(
                        str(i) + ". " + strike + otype + " " + expiry +
                        " · " + prem_s + " " + fill + vol_str + sweep + src + sc_str + tm_str
                    )
                lines.append("─" * 20)
                lines.append("Total: " + total_str + " across " + str(len(ticker_flows)) + " flows")
                send_reply(chr(10).join(lines), from_chat_id)
        except Exception as e:
            send_reply("Flow search error: " + str(e), from_chat_id)

    elif cmd == "price" and args:
        # /price TICKER — real-time stock price
        ticker_p = args[0].upper()
        try:
            from fetcher import fetch_price
            px = fetch_price(ticker_p)
            if px:
                send_reply(f"🟢 {ticker_p}: ${px:.2f}", from_chat_id)
            else:
                send_reply(f"Could not fetch price for {ticker_p}", from_chat_id)
        except Exception as e:
            send_reply(f"Price error: {e}", from_chat_id)

    elif cmd == "status":
        # /status — system health summary
        try:
            import threading
            from datetime import datetime
            from zoneinfo import ZoneInfo
            from storage import load_data as _ld
            bf_alive = any(t.name == "bullflow-stream" and t.is_alive() for t in threading.enumerate())
            stream_s = "✅ Connected" if bf_alive else "❌ Disconnected"
            journal  = _ld("journal", "/tmp/journal.json", {"trades":[]})
            open_ct  = len([t for t in journal.get("trades",[]) if t.get("status","").upper() != "CLOSED"])
            today    = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
            analyses = _ld("analyses_today", "/tmp/analyses.json", []) or []
            trades_t = sum(1 for a in analyses if a.get("date","")==today and a.get("verdict","")=="TRADE")
            watches_t= sum(1 for a in analyses if a.get("date","")==today and a.get("verdict","")=="WATCH")
            balance  = __import__('os').environ.get("RAILWAY_BALANCE","?")
            now_et   = datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M %p ET")
            sep = chr(10) + "─"*20 + chr(10)
            msg_lines = [
                f"📊 FlowCheck Status — {now_et}",
                "─"*20,
                f"🅱 Bullflow: {stream_s}",
                f"📈 Open positions: {open_ct}",
                f"🔔 Today: {trades_t} TRADE · {watches_t} WATCH",
                f"💰 Railway balance: ${balance}",
            ]
            send_reply(chr(10).join(msg_lines), from_chat_id)
        except Exception as e:
            send_reply(f"Status error: {e}", from_chat_id)
    elif cmd == "entry" and len(args) >= 6:
        # Single leg: /entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE [@acct] [DATE] [TIME]
        # Spread:     /entry TICKER LONG_STRIKE/SHORT_STRIKE C/P EXPIRY CONTRACTS NET_DEBIT spread:debit_call [@acct]
        #             /entry MU 1100/1200 C 01/16/26 5 12.50 spread:debit_call @rh_trad
        #             /entry AAPL 180/185 C 06/20/26 3 2.50 spread:credit_call @rh_ira
        if len(args) >= 6 and any("spread:" in a.lower() for a in args):
            handle_spread_entry(args, from_chat_id)
        else:
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

def handle_spread_entry(args: list, reply_chat_id: str):
    """
    Log a spread entry.
    Format: /entry TICKER LONG/SHORT C/P EXPIRY CONTRACTS NET_PRICE spread:TYPE [@acct] [DATE] [TIME]
    Examples:
      /entry MU 1100/1200 C 01/16/26 5 12.50 spread:debit_call @rh_trad
      /entry NVDA 900/950 C 06/20/26 2 8.00 spread:debit_call
      /entry AAPL 185/180 P 06/20/26 3 2.50 spread:credit_put @rh_ira
    Spread types: debit_call, debit_put, credit_call, credit_put
    """
    try:
        from trade_journal import add_entry, normalize_expiry
        ticker    = args[0].upper()
        strikes   = args[1]  # e.g. "1100/1200"
        opt_type  = args[2].lower()
        expiry    = args[3]
        contracts = int(args[4])
        net_price = float(args[5])

        # Parse spread type
        spread_type = None
        for a in args:
            if a.lower().startswith("spread:"):
                spread_type = a.lower().replace("spread:","").strip()
        if not spread_type:
            spread_type = "debit_call" if "call" in opt_type else "debit_put"

        # Parse strikes
        if "/" in strikes:
            parts2 = strikes.split("/")
            long_s  = parts2[0].strip()
            short_s = parts2[1].strip()
        else:
            send_reply("Spread format: LONG/SHORT e.g. 1100/1200", reply_chat_id)
            return

        is_debit = "debit" in spread_type
        spread_width = round(abs(float(short_s) - float(long_s)), 2)

        # Account, date, time
        account_id = "default"
        entry_date = entry_time = None
        for i, a in enumerate(args):
            if a.startswith("@"):
                account_id = a[1:].lower()
            elif len(a) == 10 and "-" in a and entry_date is None:
                entry_date = a
            elif (":" in a or a.endswith("AM") or a.endswith("PM")) and entry_time is None:
                entry_time = a

        # Use long_strike as primary strike for display
        primary_strike = long_s if is_debit else short_s

        trade = add_entry(
            ticker, primary_strike, opt_type,
            normalize_expiry(expiry), contracts, net_price,
            entry_date, entry_time, account_id,
            spread_type=spread_type,
            short_strike=short_s,
            long_strike=long_s,
            spread_width=spread_width,
            credit=net_price
        )

        otype = "C" if "call" in opt_type else "P"
        max_p = trade.get("max_profit","?")
        max_l = trade.get("max_loss","?")
        lines = [
            "Spread entry recorded:",
            ticker + " " + spread_type.upper().replace("_"," ") + " " + expiry,
            "  Long  $" + long_s + otype + " | Short $" + short_s + otype,
            "  Width: $" + str(spread_width) + " | Net " +
            ("debit" if is_debit else "credit") + ": $" + str(net_price) +
            " x" + str(contracts),
            "  Max profit: $" + str(max_p) + " | Max loss: $" + str(max_l),
            "To close: /exit " + ticker + " CLOSE_PRICE",
        ]
        send_reply(chr(10).join(lines), reply_chat_id)

    except Exception as e:
        send_reply("Spread entry error: " + str(e) + chr(10) +
                   "Format: /entry MU 1100/1200 C 01/16/26 5 12.50 spread:debit_call @rh_trad",
                   reply_chat_id)


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
        '/sent TICKER — price, SMAs, RSI, news, flow, insiders',
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
        '/journal — open trade journal web page',
        '/flow TICKER — today\'s top flows for a ticker (from stream history)',
        '/eval [@account] [TICKER] [range] — AI position review: HOLD/TRIM/CLOSE',
        '/count — open position count by account',
        '/status — stream health + today alert count',
        '/price TICKER — real-time stock price',
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
        'FLOW SEARCH',
        '/flow NVDA — search today\'s captured flows for a specific ticker',
        '  Shows strike, expiry, premium, fill type, vol/OI, score',
        '  Sources: Bullflow SSE + FlowGod alerts captured today',
        '',
        'WEB JOURNAL',
        '/journal (or /jv) — opens trade journal in browser',
        '',
        'POSITION EVALUATION',
        '/eval                   — evaluate all open positions (AI: HOLD/TRIM/CLOSE)',
        '/eval 21-40             — positions 21-40 (paginate)',
        '/eval NVDA              — evaluate NVDA only',
        '/eval @rh_ira           — evaluate IRA account only',
        '/eval NVDA @rh_ira      — NVDA in IRA',
        '/eval 1-20              — positions 1-20 (paginate with /eval 21-40 etc)',
        '/eval NVDA              — evaluate NVDA positions only',
        '/eval NVDA @rh_ira      — NVDA in specific account',
        '/eval @rh_ira           — all positions in IRA account',
        '  Spread entries show real-time both-leg P&L via Massive',
        '  OTM/ITM calculated automatically — no more AI guessing',
        '  Tag a position as spread: edit legs=spread in /journal-view',
        '',
        'POSITION INFO',
        '/count                  — total open positions by account + unique tickers',
        '/status                 — stream status, open count, today alerts, Railway balance',
        '/price TICKER           — real-time stock price',
        '',
        'SPREAD ENTRY (new format)',
        '/entry MU 1100/1200 C 01/16/26 5 12.50 spread:debit_call @rh_trad',
        '/entry AAPL 185/180 P 06/20/26 3 2.50 spread:credit_put @rh_ira',
        '  Types: spread:debit_call  spread:debit_put',
        '         spread:credit_call spread:credit_put',
        '',
        '/journal_help — this message',
    ])
    send_reply(msg, reply_chat_id)

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
        "  realized_pnl: realized profit/loss if shown (e.g. 374.00 for profit, -150.00 for loss)",
        "  position_effect: exactly 'open' or 'close' from the 'Position effect' field on screen — this is critical",
        "  IMPORTANT: 'Position effect: Close' means closing an existing position (exit). 'Position effect: Open' means opening new position (entry).",
        "  action_word: exactly 'buy' or 'sell' — the first action word shown (e.g. 'Buy NVDA' = 'buy', 'Sell NVDA' = 'sell')",
        "",
        "Rules:",
        "  MOST IMPORTANT: Find the field labeled 'Position effect' on screen.",
        "  If 'Position effect' shows 'Close' or 'Closing' — this is an EXIT. Set action=exit.",
        "  If 'Position effect' shows 'Open' or 'Opening' — this is an ENTRY. Set action=entry.",
        "  Then look at whether it says Buy or Sell at the top:",
        "  Buy + Position effect Close = BTC. Sell + Position effect Close = STC.",
        "  Buy + Position effect Open = BTO. Sell + Position effect Open = STO.",
        "  BTO or buy-to-open = entry. STO or sell-to-open = entry (put sell). STC or sell-to-close = exit. BTC or buy-to-close = exit.",
        "  IMPORTANT: Sell to Open (STO) means selling a put or call to open a new position — this is an ENTRY not an exit.",
        "  If you see 'Sell to Open', 'STO', or 'Sold to Open' set action=entry and order_type=STO.",
        "  ROBINHOOD SPECIFIC FORMAT:",
        "  Robinhood fill confirmations show: action word ('Buy' or 'Sell') + option description + 'Position effect: Open or Closed'",
        "  Example STO: 'Sell NVDA $250 Call 6/5' + 'Position effect: Open' + 'Est credit' = STO, action=entry, order_type=STO",
        "  Example BTO: 'Buy NVDA $250 Call 6/5' + 'Position effect: Open' + 'Est debit' = BTO, action=entry, order_type=BTO",
        "  Example STC: 'Sell NVDA $250 Call 6/5' + 'Position effect: Close' + 'Est debit' = STC, action=exit, order_type=STC",
        "  Example BTC: 'Buy NVDA $250 Call 6/5' + 'Position effect: Close' + 'Est credit' = BTC, action=exit, order_type=BTC",
        "  KEY RULE: 'Sell' + 'Position effect: Open' = STO (selling to open new position = put/call sell)",
        "  KEY RULE: 'Buy' + 'Position effect: Open' = BTO (buying to open new position)",
        "  KEY RULE: 'Position effect: Open' means NEW position, 'Position effect: Close' means CLOSING position",
        "  The filled price comes from 'Filled quantity: X contracts at $Y.YY' — use Y.YY as the price",
        "  'Est credit' = premium received (selling). 'Est debit' or 'Total cost' = premium paid (buying).",
        "  BTC example: 'Buy NVDA $250 Call 6/5' + 'Position effect: Close' + 'Total cost' + 'Realized profit' = BTC, action=exit, order_type=BTC",
        "  If 'Realized profit' appears AND action is exit (STC/BTC), extract it as realized_pnl (e.g. 374.00 for profit, -150.00 for loss).",
        "  NEVER set realized_pnl for BTO or STO orders — those are entries, not exits.",
        "  realized_pnl should be a positive number for profit, negative for loss, omit entirely for entries.",
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
        response_text = raw  # Save full text for post-parse correction
        print("[PHOTO] Raw: " + raw[:300])
        if not raw:
            return {"error": "empty_response"}
        raw  = raw.replace("", "").strip()
        start_i = raw.find("{")
        end_i   = raw.rfind("}") + 1
        if start_i >= 0 and end_i > start_i:
            raw = raw[start_i:end_i]
        data = json.loads(raw)

        # Post-parse corrections using raw text + parsed fields
        full_lower = raw.lower()
        ot         = data.get("order_type","").upper()
        aw         = data.get("action_word","").lower()
        pe         = data.get("position_effect","").lower()

        # Search raw text directly for "Position effect" value — most reliable
        import re as _re
        pe_match = _re.search(r'position.?effect["\s:]+([a-z]+)', full_lower)
        pe_from_text = pe_match.group(1).strip() if pe_match else pe
        print(f"[PHOTO] position_effect from JSON={pe} from_text={pe_from_text}")

        has_realized   = "realized profit" in full_lower
        has_est_credit = "est credit" in full_lower
        buy_action     = aw == "buy" or ot in ("BTO","BTC")
        sell_action    = aw == "sell" or ot in ("STO","STC")
        is_close       = pe_from_text == "close" or pe == "close"
        is_open        = pe_from_text == "open" or pe == "open"

        # Priority 1: Realized profit = always an exit
        if has_realized:
            data["order_type"] = "BTC" if buy_action else "STC"
            data["action"]     = "exit"
            print(f"[PHOTO] Corrected to {data['order_type']} (Realized profit)")

        # Priority 2: Position effect close = exit
        elif is_close:
            data["order_type"] = "BTC" if buy_action else "STC"
            data["action"]     = "exit"
            print(f"[PHOTO] Corrected to {data['order_type']} (Position effect: close)")

        # Priority 3: Est credit + Sell + Open = STO
        elif has_est_credit and sell_action and is_open:
            data["order_type"] = "STO"
            data["action"]     = "entry"
            print("[PHOTO] Corrected to STO (Est credit + Sell + Open)")

        print("[PHOTO] Parsed: " + str(data))
        return data
    except Exception as e:
        print("[PHOTO] Vision error: " + str(e))
        # Try to return partial data if we have raw text
        try:
            if raw:
                print("[PHOTO] Raw response was: " + raw[:500])
        except:
            pass
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
    order_type     = data.get("order_type","")  # BTO/STO/STC/BTC
    position_effect = data.get("position_effect","").lower()  # open/close

    # Post-parse correction using position_effect field
    # Vision model sometimes gets order_type wrong — position_effect is more reliable
    action_word = data.get("action_word","").lower()  # buy/sell from screenshot
    if position_effect:
        if "close" in position_effect:
            # Closing a position
            if action_word == "buy" or order_type in ("BTO",""):
                order_type = "BTC"
                print(f"[PHOTO] Corrected to BTC (Buy + Position effect: Close)")
            elif action_word == "sell" or order_type in ("STO",""):
                order_type = "STC"
                print(f"[PHOTO] Corrected to STC (Sell + Position effect: Close)")
        elif "open" in position_effect:
            # Opening a position
            if action_word == "sell" or order_type in ("STC",""):
                order_type = "STO"
                print(f"[PHOTO] Corrected to STO (Sell + Position effect: Open)")
            elif action_word == "buy" or order_type in ("BTC",""):
                order_type = "BTO"
                print(f"[PHOTO] Corrected to BTO (Buy + Position effect: Open)")

    # Set action from order_type
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
            # Use realized_pnl from screenshot only for STC/BTC exits
            # Never use it for BTO entries (Total cost is not a loss)
            rh_pnl     = data.get("realized_pnl")
            order_type = data.get("order_type","").upper()
            is_exit_order = order_type in ("STC","BTC") or action == "exit"
            if rh_pnl and is_exit_order and float(rh_pnl) != 0:
                pnl = float(rh_pnl)
                result["pnl_total"] = pnl
            else:
                pnl = result.get("pnl_total", 0) or 0
            pct = result.get("pnl_pct", 0) or 0
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
