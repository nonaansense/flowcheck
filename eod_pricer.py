"""
EOD Pricer — fetches current/closing option prices for open positions.
Uses Polygon snapshot API (1 call per position — minimal rate limit impact).
Falls back to Finnhub stock price for intrinsic estimate if Polygon fails.
"""
import os, requests, time
from datetime import datetime, date
from zoneinfo import ZoneInfo

def poly_key():
    return os.environ.get("POLYGON_API_KEY","")

def fh_key():
    return os.environ.get("FINNHUB_API_KEY","")

def build_option_ticker(ticker: str, strike: str, opt_type: str, expiry: str) -> str | None:
    """Build Polygon option ticker e.g. O:AAPL251219C00150000"""
    try:
        # Parse expiry
        expiry  = (expiry or "").strip()
        exp_dt  = None
        for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                exp_dt = datetime.strptime(expiry, fmt)
                break
            except:
                continue
        if not exp_dt:
            print(f"[EOD] Option ticker build error: cannot parse expiry '{expiry}'")
            return None
        exp_str  = exp_dt.strftime("%y%m%d")
        call_put = "C" if opt_type.lower() in ("c","call") else "P"
        # Strike as 8-digit integer (strike * 1000, padded)
        strike_int = int(float(strike) * 1000)
        strike_str = str(strike_int).zfill(8)
        return f"O:{ticker.upper()}{exp_str}{call_put}{strike_str}"
    except Exception as e:
        print(f"[EOD] Option ticker build error: {e}")
        return None

def get_option_price_polygon(option_ticker: str) -> float | None:
    """Fetch last price from Polygon option snapshot."""
    key = poly_key()
    if not key:
        return None
    try:
        r = requests.get(
            f"https://api.polygon.io/v3/snapshot/options/{option_ticker}",
            params={"apiKey": key},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            result = data.get("results",{})
            # Try last trade price first, then mid-price
            last = (result.get("last_quote",{}).get("midpoint") or
                    result.get("day",{}).get("close") or
                    result.get("last_trade",{}).get("price"))
            if last and float(last) > 0:
                print(f"[EOD] Polygon: {option_ticker} = ${last}")
                return float(last)
        elif r.status_code == 404:
            print(f"[EOD] Polygon: {option_ticker} not found (expired or invalid)")
        else:
            print(f"[EOD] Polygon: {option_ticker} status {r.status_code}")
    except Exception as e:
        print(f"[EOD] Polygon error: {e}")
    return None

def get_stock_price_fh(ticker: str) -> float | None:
    """Get stock price from Finnhub as fallback."""
    key = fh_key()
    if not key:
        return None
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": ticker.upper(), "token": key},
            timeout=6
        )
        if r.status_code == 200:
            price = float(r.json().get("c",0) or 0)
            return price if price > 0 else None
    except:
        pass
    return None

def estimate_intrinsic(stock: float, strike: float, opt_type: str, expiry: str) -> float | None:
    """Simple intrinsic value estimate — no time value."""
    try:
        for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                exp_date = datetime.strptime(expiry.strip(), fmt).date()
                break
            except:
                continue
        dte = (exp_date - date.today()).days
        if dte < 0:
            return 0.0
        if opt_type.lower() in ("c","call"):
            intrinsic = max(0.0, stock - float(strike))
        else:
            intrinsic = max(0.0, float(strike) - stock)
        return round(intrinsic, 2) if intrinsic > 0 else None
    except:
        return None

def get_option_price_tradier(ticker: str, strike: str, opt_type: str, expiry: str) -> float | None:
    """Fetch option price from Tradier brokerage API."""
    token = os.environ.get("TRADIER_TOKEN","")
    if not token:
        return None
    try:
        # Normalize expiry to YYYY-MM-DD
        from datetime import datetime as _dt
        expiry   = (expiry or "").strip()
        exp_str  = None

        # Try standard formats
        for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
            try:
                exp_str = _dt.strptime(expiry, fmt).strftime("%Y-%m-%d")
                break
            except:
                continue

        # Handle MM/YY (month/year, assume 3rd Friday or just use last day)
        if not exp_str:
            import re
            if re.match(r"^\d{1,2}/\d{2}$", expiry):
                try:
                    parts = expiry.split("/")
                    month = int(parts[0])
                    year  = 2000 + int(parts[1]) if int(parts[1]) < 100 else int(parts[1])
                    # Use 3rd Friday of that month as standard expiry
                    import calendar
                    first_day = _dt(year, month, 1)
                    fridays   = [d for d in range(1,32) if _dt(year,month,1).replace(day=d).weekday()==4
                                 if d <= calendar.monthrange(year,month)[1]]
                    third_fri = fridays[2] if len(fridays) >= 3 else fridays[-1]
                    exp_str   = _dt(year, month, third_fri).strftime("%Y-%m-%d")
                    print(f"[EOD] Tradier: MM/YY expiry '{expiry}' → {exp_str} (3rd Friday)")
                except Exception as _e:
                    print(f"[EOD] Tradier: cannot parse MM/YY expiry '{expiry}': {_e}")

        if not exp_str:
            print(f"[EOD] Tradier: cannot parse expiry '{expiry}'")
            return None

        call_put = "call" if opt_type.lower() in ("c","call") else "put"
        r = requests.get(
            "https://api.tradier.com/v1/markets/options/chains",
            params={
                "symbol":     ticker.upper(),
                "expiration": exp_str,
                "greeks":     "false",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Accept":        "application/json",
            },
            timeout=10
        )
        if r.status_code != 200:
            print(f"[EOD] Tradier HTTP {r.status_code} for {ticker}")
            return None

        # Handle None options gracefully
        data     = r.json()
        opts_raw = data.get("options") or {}
        options  = opts_raw.get("option") if isinstance(opts_raw, dict) else []
        if not options:
            print(f"[EOD] Tradier: no options for {ticker} {exp_str}")
            return None
        if isinstance(options, dict):
            options = [options]  # Single option returned as dict

        strike_f = float(strike)
        for opt in options:
            if not isinstance(opt, dict):
                continue
            if (abs(float(opt.get("strike",0) or 0) - strike_f) < 0.01 and
                (opt.get("option_type") or "").lower() == call_put):
                bid   = float(opt.get("bid",0) or 0)
                ask   = float(opt.get("ask",0) or 0)
                last  = float(opt.get("last",0) or 0)
                mid   = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else 0
                price = mid or last
                if price > 0:
                    print(f"[EOD] Tradier: {ticker} {strike}{call_put[0].upper()} = ${price} (bid={bid} ask={ask})")
                    return price
        print(f"[EOD] Tradier: {ticker} {strike}{call_put[0].upper()} not found in {exp_str} chain")
        return None
    except Exception as e:
        print(f"[EOD] Tradier error: {e}")
        return None

def get_option_price(ticker: str, strike: str, opt_type: str, expiry: str) -> tuple:
    """
    Get option price using best available source.
    Priority: Tradier → Polygon → Intrinsic estimate
    Returns (price, source) where source is 'tradier', 'polygon', 'intrinsic', or None.
    """
    # 1. Try Tradier (brokerage API — most accurate, real bid/ask)
    if os.environ.get("TRADIER_TOKEN"):
        price = get_option_price_tradier(ticker, strike, opt_type, expiry)
        if price:
            return price, "tradier"

    # 2. Try Polygon option snapshot
    opt_ticker = build_option_ticker(ticker, strike, opt_type, expiry)
    if opt_ticker:
        price = get_option_price_polygon(opt_ticker)
        if price:
            return price, "polygon"
        time.sleep(0.2)

    # 3. Fallback — intrinsic value from stock price
    stock = get_stock_price_fh(ticker)
    if stock:
        intrinsic = estimate_intrinsic(stock, strike, opt_type, expiry)
        if intrinsic is not None:
            print(f"[EOD] Intrinsic estimate for {ticker} {strike}: ${intrinsic} (stock=${stock})")
            return intrinsic, "intrinsic"

    return None, None

def update_eod_prices(send_sms_fn=None):
    """
    Fetch current/closing prices for all open positions and update journal.
    Called at 4:02 PM ET on trading days, and on /refresh command.
    VERSION: v2
    """
    print("[EOD PRICER] v2 starting...")
    from trade_journal import load_journal, save_journal
    from market_calendar import is_market_open

    journal = load_journal()
    open_t  = journal.get("trades", [])
    if not open_t:
        print("[EOD PRICER] No open positions")
        return

    now_et  = datetime.now(ZoneInfo("America/New_York"))
    today   = now_et.strftime("%Y-%m-%d")
    updated = []

    # Include closed trades that haven't expired yet — track through expiry
    from datetime import datetime as _dt
    today_dt = _dt.now()
    def not_expired(t):
        exp = t.get("expiry","")
        if not exp: return False
        for fmt in ("%m/%d/%y","%m/%d/%Y","%Y-%m-%d"):
            try:
                return _dt.strptime(exp, fmt) >= today_dt
            except: continue
        return False

    closed_tracking = [t for t in journal.get("closed",[])
                       if t.get("ticker") and not_expired(t)
                       and not t.get("expiry_tracking_done")]

    all_positions = [t for t in open_t if t.get("ticker") and t.get("expiry")]
    print(f"[EOD PRICER] Fetching prices for {len(all_positions)} open + {len(closed_tracking)} closed-still-tracking positions...")
    all_positions = all_positions + closed_tracking

    # Log all positions being processed
    print(f"[EOD PRICER] Processing {len(all_positions)} positions:")
    for _t in all_positions:
        print(f"  {_t.get('ticker')} expiry={_t.get('expiry')} is_spread={_t.get('is_spread')} spread_type={_t.get('spread_type')} long={_t.get('long_strike')} short={_t.get('short_strike')}")

    for t in all_positions:
        ticker    = t.get("ticker","")
        opt_type  = t.get("option_type","call")
        expiry    = t.get("expiry","")
        remaining = int(t.get("contracts_remaining") or t.get("contracts",1))
        is_spread = bool(t.get("is_spread") or t.get("spread_type"))

        if is_spread:
            # Fetch both legs separately, calculate net value
            long_strike  = str(t.get("long_strike",""))
            short_strike = str(t.get("short_strike",""))
            spread_type  = t.get("spread_type","")
            credit       = float(t.get("credit",0) or 0)

            if not long_strike or not short_strike:
                continue

            is_debit = "debit" in (spread_type or "")

            long_price,  long_src  = get_option_price(ticker, long_strike,  opt_type, expiry)
            short_price, short_src = get_option_price(ticker, short_strike, opt_type, expiry)

            if long_price is not None and short_price is not None:
                # Net spread value = long leg - short leg
                net_value = round(long_price - short_price, 2)
                if is_debit:
                    # Debit spread: paid credit to enter, profit = net_value - credit
                    pnl = round((net_value - credit) * remaining * 100, 2)
                    pct = round(((net_value - credit) / credit) * 100, 1) if credit > 0 else 0
                else:
                    # Credit spread: received credit, profit = credit - net_value
                    pnl = round((credit - net_value) * remaining * 100, 2)
                    pct = round(((credit - net_value) / credit) * 100, 1) if credit > 0 else 0

                t["last_price"]      = net_value
                t["unrealized_pnl"]  = pnl
                t["unrealized_pct"]  = pct
                t["last_price_date"] = today
                t["last_price_src"]  = long_src or short_src
                src_label = (long_src or short_src or "unknown")
                print(f"[EOD PRICER] Spread {ticker}: long=${long_price} short=${short_price} net=${net_value} P&L={pnl:+.2f}")
                updated.append({
                    "ticker":  ticker,
                    "strike":  long_strike + "/" + short_strike,
                    "opt":     opt_type[0].upper(),
                    "price":   net_value,
                    "pct":     pct,
                    "pnl":     pnl,
                    "source":  src_label,
                    "account": t.get("account_id","default"),
                })
            else:
                print(f"[EOD PRICER] Spread {ticker}: could not fetch both legs (long={long_price} short={short_price})")
        else:
            # Single leg
            strike = str(t.get("strike",""))
            entry  = float(t.get("entry_price",0) or 0)

            if not strike or not expiry or entry <= 0:
                continue

            price, source = get_option_price(ticker, strike, opt_type, expiry)

            if price is not None:
                pct = round(((price - entry) / entry) * 100, 1)
                pnl = round((price - entry) * remaining * 100, 2)
                t["last_price"]      = price
                t["unrealized_pnl"]  = pnl
                t["unrealized_pct"]  = pct
                t["last_price_date"] = today
                t["last_price_src"]  = source
                updated.append({
                    "ticker":  ticker,
                    "strike":  strike,
                    "opt":     opt_type[0].upper(),
                    "price":   price,
                    "pct":     pct,
                    "pnl":     pnl,
                    "source":  source,
                    "account": t.get("account_id","default"),
                })
            else:
                print(f"[EOD PRICER] No price for {ticker} {strike} — skipped")

    # Use Bullflow peakReturn API for closed trades that have an OCC symbol
    bf_key = os.environ.get("BULLFLOW_API_KEY","")
    if bf_key:
        try:
            from bullflow_stream import get_peak_return
            for t in journal.get("closed",[]):
                occ = t.get("occ_symbol","")
                entry = float(t.get("entry_price",0) or 0)
                ts    = t.get("flow_timestamp") or t.get("timestamp",0)
                if occ and entry > 0 and ts:
                    peak_data = get_peak_return(occ, entry, float(ts))
                    if peak_data:
                        peak_pct = float(peak_data.get("peakPercentReturnSinceTimestamp",0))
                        peak_px  = float(peak_data.get("peakPriceSinceTimestamp",0))
                        t["peak_pct"]   = round(peak_pct, 1)
                        t["peak_price"] = round(peak_px, 2)
                        exit_p = t.get("exit_price")
                        if exit_p and entry > 0:
                            exit_pct = ((float(exit_p) - entry) / entry) * 100
                            t["left_on_table"] = round(peak_pct - exit_pct, 1)
                        print(f"[EOD] Bullflow peak: {t.get('ticker')} peak=+{peak_pct}%")
        except Exception as _bfe:
            print(f"[EOD] Bullflow peak error: {_bfe}")

    # Recalculate peak/max_dd/left_on_table for all trades with price history
    for bucket in ("trades", "closed"):
        for t in journal.get(bucket, []):
            history = t.get("price_history", [])
            entry   = float(t.get("entry_price", 0) or 0)
            if not history or entry <= 0:
                continue
            is_closed      = bucket == "closed" or bool(t.get("exit_price"))
            pre_exit       = [h["price"] for h in history if h.get("price") and not h.get("post_exit")]
            all_prices     = [h["price"] for h in history if h.get("price")]
            prices_for_dd  = pre_exit if pre_exit else (all_prices if not is_closed else [])

            # Peak = max of all prices (entry → expiry)
            if all_prices:
                peak_p = max(all_prices)
                t["peak_price"] = round(peak_p, 2)
                t["peak_pct"]   = round(((peak_p - entry) / entry) * 100, 1)

            # Max DD = entry → exit only
            if prices_for_dd:
                rp = entry; max_dd = 0.0
                for p in prices_for_dd:
                    rp = max(rp, p)
                    dd = ((rp - p) / rp) * 100
                    max_dd = max(max_dd, dd)
                t["max_drawdown"] = round(max_dd, 1)

            # Left on table = peak% - exit%
            exit_p = t.get("exit_price")
            if exit_p and t.get("peak_pct") is not None:
                exit_pct = ((float(exit_p) - entry) / entry) * 100
                t["left_on_table"] = round(t["peak_pct"] - exit_pct, 1)

    if updated or closed_tracking:
        save_journal(journal)
        print(f"[EOD PRICER] Updated {len(updated)}/{len(all_positions)} positions")

        if send_sms_fn:
            accounts = journal.get("accounts",{})
            lines    = ["📊 Position Values — " + now_et.strftime("%b %d %I:%M%p ET")]
            total    = sum(u["pnl"] for u in updated)
            for u in updated:
                sign  = "+" if u["pnl"] >= 0 else ""
                emoji = "🟢" if u["pnl"] >= 0 else "🔴"
                acc   = accounts.get(u["account"],{}).get("name","")
                src   = " ~" if u["source"] == "intrinsic" else ""
                lines.append(
                    emoji + " " + u["ticker"] + " " + u["strike"] + u["opt"] +
                    ": $" + str(u["price"]) + src +
                    " " + sign + str(u["pct"]) + "%" +
                    " (" + sign + "$" + str(u["pnl"]) + ")" +
                    (" [" + acc + "]" if acc else "")
                )
            if any(u["source"] == "intrinsic" for u in updated):
                lines.append("~ = intrinsic estimate (no live quote)")
            sign = "+" if total >= 0 else ""
            lines.append("")
            lines.append("Total unrealized: " + sign + "$" + str(round(total,2)))
            send_sms_fn(chr(10).join(lines))
    else:
        print("[EOD PRICER] No prices could be fetched")
        if send_sms_fn:
            send_sms_fn(
                "📊 /refresh: No option prices available" + chr(10) +
                "Market may be closed or options not yet traded today."
            )
