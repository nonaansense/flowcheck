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
        expiry = expiry.strip()
        for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                exp_dt = datetime.strptime(expiry, fmt)
                break
            except:
                continue
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

def get_option_price(ticker: str, strike: str, opt_type: str, expiry: str) -> tuple:
    """
    Get option price using best available source.
    Returns (price, source) where source is 'polygon', 'intrinsic', or None.
    """
    # 1. Try Polygon option snapshot
    opt_ticker = build_option_ticker(ticker, strike, opt_type, expiry)
    if opt_ticker:
        price = get_option_price_polygon(opt_ticker)
        if price:
            return price, "polygon"
        time.sleep(0.2)  # Respect rate limit

    # 2. Fallback — intrinsic value from stock price
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
    """
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

    single_legs = [t for t in open_t if not t.get("is_spread") and t.get("ticker") and t.get("expiry")]
    print(f"[EOD PRICER] Fetching prices for {len(single_legs)} positions...")

    for t in single_legs:
        ticker   = t.get("ticker","")
        strike   = str(t.get("strike",""))
        opt_type = t.get("option_type","call")
        expiry   = t.get("expiry","")
        entry    = float(t.get("entry_price",0) or 0)
        remaining= int(t.get("contracts_remaining") or t.get("contracts",1))

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

    if updated:
        save_journal(journal)
        print(f"[EOD PRICER] Updated {len(updated)}/{len(single_legs)} positions")

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
