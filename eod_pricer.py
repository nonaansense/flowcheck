"""
EOD Pricer — fetches closing option prices for open positions at 4:00 PM ET.
Updates journal with last_price, unrealized_pnl, unrealized_pct.
Uses Finnhub (not Polygon) to preserve rate limits.
"""
import os, requests
from datetime import datetime
from zoneinfo import ZoneInfo

def fh_key():
    return os.environ.get("FINNHUB_API_KEY","")

def get_option_quote(ticker: str, strike: str, opt_type: str, expiry: str) -> float | None:
    """
    Fetch last option price from Finnhub.
    Falls back to stock-based intrinsic estimate if option quote unavailable.
    """
    key = fh_key()
    if not key:
        return None

    # Try Finnhub option chain
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/stock/option-chain",
            params={"symbol": ticker.upper(), "token": key},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            for option_date in data.get("data", []):
                # Match expiry
                exp_str = option_date.get("expirationDate","")
                if not _expiry_matches(exp_str, expiry):
                    continue
                bucket = "call" if opt_type.lower() in ("c","call") else "put"
                for opt in option_date.get(bucket + "s", []):
                    if str(opt.get("strike","")) == str(strike):
                        last = opt.get("lastPrice") or opt.get("mark")
                        if last and float(last) > 0:
                            return float(last)
    except Exception as e:
        print(f"[EOD] Finnhub option chain error: {e}")

    # Fallback — use stock quote to estimate moneyness
    try:
        r2 = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": ticker.upper(), "token": key},
            timeout=6
        )
        if r2.status_code == 200:
            stock_price = float(r2.json().get("c", 0) or 0)
            if stock_price > 0:
                return _estimate_option_value(stock_price, float(strike), opt_type, expiry)
    except Exception as e:
        print(f"[EOD] Fallback stock quote error: {e}")

    return None

def _expiry_matches(exp_str: str, expiry: str) -> bool:
    """Check if expiry strings refer to same date."""
    try:
        # Normalize expiry to YYYY-MM-DD
        from datetime import datetime as dt
        exp_str   = exp_str.strip()
        expiry    = expiry.strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y"):
            try:
                d1 = dt.strptime(exp_str, fmt).date()
                break
            except:
                continue
        for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y"):
            try:
                d2 = dt.strptime(expiry, fmt).date()
                break
            except:
                continue
        return d1 == d2
    except:
        return False

def _estimate_option_value(stock: float, strike: float, opt_type: str,
                            expiry: str) -> float | None:
    """
    Simple intrinsic value estimate when live option price unavailable.
    Only returns intrinsic value — no time value.
    """
    try:
        from datetime import datetime as dt, date
        for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                exp_date = dt.strptime(expiry.strip(), fmt).date()
                break
            except:
                continue
        dte = (exp_date - date.today()).days
        if dte < 0:
            return 0.0
        if opt_type.lower() in ("c","call"):
            intrinsic = max(0, stock - strike)
        else:
            intrinsic = max(0, strike - stock)
        return round(intrinsic, 2) if intrinsic > 0 else None
    except:
        return None

def update_eod_prices(send_sms_fn=None):
    """
    Fetch closing prices for all open positions and update journal.
    Called at 4:00 PM ET on trading days.
    """
    from market_calendar import is_market_open
    from trade_journal import load_journal, save_journal

    if not is_market_open():
        return

    journal = load_journal()
    open_t  = journal.get("trades", [])
    if not open_t:
        return

    now_et   = datetime.now(ZoneInfo("America/New_York"))
    today    = now_et.strftime("%Y-%m-%d")
    updated  = []

    print(f"[EOD PRICER] Fetching closing prices for {len(open_t)} open positions...")

    for t in open_t:
        ticker   = t.get("ticker","")
        strike   = t.get("strike","")
        opt_type = t.get("option_type","call")
        expiry   = t.get("expiry","")

        if not ticker or not expiry:
            continue

        # Skip spreads for now — need both legs
        if t.get("is_spread"):
            continue

        try:
            last_price = get_option_quote(ticker, strike, opt_type, expiry)
            if last_price is not None:
                entry      = float(t.get("entry_price",0) or 0)
                remaining  = int(t.get("contracts_remaining") or t.get("contracts",1))
                if entry > 0:
                    unreal_pct = round(((last_price - entry) / entry) * 100, 1)
                    unreal_pnl = round((last_price - entry) * remaining * 100, 2)
                    t["last_price"]       = last_price
                    t["unrealized_pnl"]   = unreal_pnl
                    t["unrealized_pct"]   = unreal_pct
                    t["last_price_date"]  = today
                    updated.append({
                        "ticker":  ticker,
                        "strike":  strike,
                        "price":   last_price,
                        "pnl":     unreal_pnl,
                        "pct":     unreal_pct,
                        "account": t.get("account_id","default"),
                    })
                    print(f"[EOD PRICER] {ticker} {strike}{opt_type[0].upper()}: ${last_price} ({unreal_pct:+.1f}%)")
        except Exception as e:
            print(f"[EOD PRICER] Error for {ticker}: {e}")

    if updated:
        save_journal(journal)
        print(f"[EOD PRICER] Updated {len(updated)} positions")

        # Optional Telegram summary
        if send_sms_fn:
            from trade_journal import load_journal as lj
            accounts = lj().get("accounts",{})
            lines    = ["📊 EOD Position Values — " + now_et.strftime("%b %d")]
            total    = sum(u["pnl"] for u in updated)
            for u in updated:
                sign  = "+" if u["pnl"] >= 0 else ""
                emoji = "🟢" if u["pnl"] >= 0 else "🔴"
                acc   = accounts.get(u["account"],{}).get("name","")
                lines.append(
                    emoji + " " + u["ticker"] + " " + u["strike"] + ": $" +
                    str(u["price"]) + " " + sign + str(u["pct"]) + "%" +
                    " (" + sign + "$" + str(u["pnl"]) + ")" +
                    (" [" + acc + "]" if acc else "")
                )
            sign = "+" if total >= 0 else ""
            lines.append("")
            lines.append("Total unrealized: " + sign + "$" + str(round(total,2)))
            send_sms_fn(chr(10).join(lines))
    else:
        print("[EOD PRICER] No prices updated")
