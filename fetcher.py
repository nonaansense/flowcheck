"""
Fetcher — Uses direct Yahoo Finance API calls with browser headers.
Avoids yfinance library which gets blocked on Railway shared IPs.
"""
import time, re, requests, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Persistent session with browser headers
_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": "https://finance.yahoo.com",
    "Referer": "https://finance.yahoo.com/",
})

_price_cache = {}
_CACHE_TTL   = 120  # 2 min cache

SECTOR_ETF_MAP = {
    "AAPL":"XLK","MSFT":"XLK","NVDA":"XLK","AMD":"XLK",
    "CSCO":"XLK","ORCL":"XLK","CRM":"XLK","QCOM":"XLK",
    "ANET":"XLK","CRWV":"XLK","MU":"XLK","SNOW":"XLK",
    "JPM":"XLF","BAC":"XLF","GS":"XLF","MS":"XLF",
    "XOM":"XLE","CVX":"XLE","BE":"XLE",
    "ALB":"XLB","FCX":"XLB","NEM":"XLB",
    "JNJ":"XLV","PFE":"XLV","INO":"XLV",
    "AMZN":"XLY","TSLA":"XLY","META":"XLC",
    "ASTS":"XLK","RKLB":"XLI","SPCE":"XLI",
    "NOK":"XLC","BAND":"XLC","GLD":"XLB",
}


# ─────────────────────────────────────────
# CORE DATA FETCHERS
# ─────────────────────────────────────────
def get_crumb():
    """Get Yahoo Finance crumb for authenticated requests."""
    try:
        r = _session.get(
            "https://query1.finance.yahoo.com/v1/test/getcrumb",
            timeout=10
        )
        if r.status_code == 200 and r.text:
            return r.text.strip()
    except:
        pass
    return None


def get_crumb_and_cookie():
    """Get Yahoo Finance crumb and session cookie required for API calls."""
    try:
        # First hit the main page to get cookies
        r1 = _session.get("https://finance.yahoo.com", timeout=10)
        # Then get crumb
        r2 = _session.get(
            "https://query1.finance.yahoo.com/v1/test/getcrumb",
            timeout=10
        )
        if r2.status_code == 200:
            return r2.text.strip()
    except Exception as e:
        print(f"[FETCHER] Crumb error: {e}")
    return None


def fetch_quote(ticker: str) -> dict:
    """Fetch current price — tries multiple Yahoo endpoints with fallbacks."""
    now = time.time()
    cached = _price_cache.get(f"quote_{ticker}")
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    # Try query1 and query2 alternately
    for base in ["query1", "query2"]:
        for attempt in range(2):
            try:
                url    = f"https://{base}.finance.yahoo.com/v8/finance/chart/{ticker}"
                params = {"interval": "1d", "range": "2d", "includePrePost": "false"}
                r      = _session.get(url, params=params, timeout=15)

                if r.status_code == 429:
                    wait = (attempt + 1) * 4
                    print(f"[FETCHER] Rate limited {ticker} on {base} — waiting {wait}s")
                    time.sleep(wait)
                    continue

                if r.status_code == 200:
                    data   = r.json()
                    result = data.get("chart", {}).get("result", [])
                    if result:
                        meta  = result[0].get("meta", {})
                        price = (meta.get("regularMarketPrice") or
                                 meta.get("chartPreviousClose") or
                                 meta.get("previousClose"))
                        if price:
                            quote = {"price": round(float(price), 2), "symbol": ticker}
                            _price_cache[f"quote_{ticker}"] = (quote, now)
                            print(f"[FETCHER] {ticker} price: ${quote['price']} via {base}")
                            return quote
                break
            except Exception as e:
                print(f"[FETCHER] Quote error {ticker} via {base}: {e}")
                time.sleep(1)

    # Last resort: try the v7 quote endpoint
    try:
        url = f"https://query1.finance.yahoo.com/v7/finance/quote"
        r   = _session.get(url, params={"symbols": ticker}, timeout=15)
        if r.status_code == 200:
            data  = r.json()
            quote_data = data.get("quoteResponse", {}).get("result", [])
            if quote_data:
                price = quote_data[0].get("regularMarketPrice")
                if price:
                    quote = {"price": round(float(price), 2), "symbol": ticker}
                    _price_cache[f"quote_{ticker}"] = (quote, now)
                    print(f"[FETCHER] {ticker} price: ${quote['price']} via v7 quote")
                    return quote
    except Exception as e:
        print(f"[FETCHER] v7 quote error {ticker}: {e}")

    print(f"[FETCHER] Could not get price for {ticker}")
    return {"price": None}


def fetch_price_history(ticker: str, days: int = 7) -> list:
    """Fetch recent daily closing prices."""
    try:
        import time as t
        end   = int(t.time())
        start = end - (days * 86400)
        url   = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"interval": "1d", "period1": start, "period2": end}
        r = _session.get(url, params=params, timeout=15)
        if r.status_code == 200:
            data    = r.json()
            result  = data.get("chart", {}).get("result", [])
            if result:
                closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                return [c for c in closes if c is not None]
    except Exception as e:
        print(f"[FETCHER] History error for {ticker}: {e}")
    return []


def fetch_options_chain(ticker: str, expiry_timestamp: int = None) -> dict:
    """Fetch options chain for a ticker."""
    try:
        url = f"https://query1.finance.yahoo.com/v7/finance/options/{ticker}"
        params = {}
        if expiry_timestamp:
            params["date"] = expiry_timestamp
        r = _session.get(url, params=params, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[FETCHER] Options error for {ticker}: {e}")
    return {}


def fetch_earnings_date(ticker: str) -> str | None:
    """Fetch next earnings date."""
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
        params = {"modules": "calendarEvents"}
        r = _session.get(url, params=params, timeout=15)
        if r.status_code == 200:
            data = r.json()
            result = data.get("quoteSummary", {}).get("result", [])
            if result:
                earnings = result[0].get("calendarEvents", {}).get("earnings", {})
                dates    = earnings.get("earningsDate", [])
                if dates:
                    ts = dates[0].get("raw")
                    if ts:
                        dt = datetime.fromtimestamp(ts)
                        return dt.strftime("%b %d, %Y"), dt
    except Exception as e:
        print(f"[FETCHER] Earnings date error for {ticker}: {e}")
    return None, None


def expiry_to_timestamp(expiry_raw: str) -> int | None:
    """Convert MM/DD/YY expiry to Unix timestamp."""
    try:
        parts  = expiry_raw.split("/")
        m, d, y = parts
        y = "20" + y if len(y) == 2 else y
        dt = datetime(int(y), int(m), int(d))
        return int(dt.timestamp())
    except:
        return None


# ─────────────────────────────────────────
# MARKET CONDITIONS
# ─────────────────────────────────────────
def check_time_of_day() -> dict:
    now_et = datetime.now(ZoneInfo("America/New_York"))
    total  = now_et.hour * 60 + now_et.minute
    if total < 9*60+30 or total > 16*60:
        return {"window":"AFTER_HOURS","emoji":"🌙","label":"After hours",
                "quality":"LOW","note":"After-hours flow — lower reliability."}
    elif total < 10*60:
        return {"window":"NOISY_OPEN","emoji":"⚠️","label":"Opening 30 min",
                "quality":"LOW","note":"First 30 min noisy — avoid entries until 10:00 AM ET."}
    elif total > 15*60+30:
        return {"window":"NOISY_CLOSE","emoji":"⚠️","label":"Closing 30 min",
                "quality":"LOW","note":"Last 30 min noisy — position squaring, not directional."}
    else:
        return {"window":"PRIME","emoji":"✅","label":"Prime hours",
                "quality":"HIGH","note":"10:00 AM–3:30 PM — highest quality flow."}


def fetch_market_conditions() -> dict:
    conditions = {
        "vix":None,"vix_label":None,"vix_emoji":None,
        "spy_5d_pct":None,"spy_trend":None,"spy_emoji":None,
        "market_bias":None,"market_score_adjustment":0,"market_summary":None,
    }

    # VIX
    vix_quote = fetch_quote("^VIX")
    v = vix_quote.get("price")
    if v:
        conditions["vix"] = v
        if v < 18:
            conditions["vix_label"]="Calm";     conditions["vix_emoji"]="✅"
        elif v < 25:
            conditions["vix_label"]="Elevated"; conditions["vix_emoji"]="⚠️"
            conditions["market_score_adjustment"] -= 0.5
        elif v < 35:
            conditions["vix_label"]="High";     conditions["vix_emoji"]="🔴"
            conditions["market_score_adjustment"] -= 1
        else:
            conditions["vix_label"]="Extreme";  conditions["vix_emoji"]="🚨"
            conditions["market_score_adjustment"] -= 2

    # SPY 5-day
    spy_prices = fetch_price_history("SPY", days=10)
    if len(spy_prices) >= 5:
        pct = round(((spy_prices[-1] - spy_prices[-5]) / spy_prices[-5]) * 100, 1)
        conditions["spy_5d_pct"] = pct
        if pct > 2:
            conditions["spy_trend"]=f"Uptrend +{pct}%";    conditions["spy_emoji"]="✅"
        elif pct > -2:
            conditions["spy_trend"]=f"Flat {pct:+.1f}%";   conditions["spy_emoji"]="⚠️"
        else:
            conditions["spy_trend"]=f"Downtrend {pct:+.1f}%"; conditions["spy_emoji"]="🔴"
            conditions["market_score_adjustment"] -= 1

    adj = conditions["market_score_adjustment"]
    if adj >= 0:
        conditions["market_bias"]="FAVORABLE"
        conditions["market_summary"]="Market conditions favor buying premium."
    elif adj >= -1:
        conditions["market_bias"]="CAUTION"
        conditions["market_summary"]="Elevated volatility — be selective, size smaller."
    elif adj >= -2:
        conditions["market_bias"]="UNFAVORABLE"
        conditions["market_summary"]="High VIX or downtrend — avoid buying premium."
    else:
        conditions["market_bias"]="AVOID"
        conditions["market_summary"]="Extreme conditions — do not buy premium today."
    return conditions


def fetch_sector_conditions(ticker: str) -> dict:
    etf    = SECTOR_ETF_MAP.get(ticker.upper(), "SPY")
    sector = {"etf":etf,"etf_5d_pct":None,"sector_trend":None,"sector_emoji":None}
    prices = fetch_price_history(etf, days=10)
    if len(prices) >= 5:
        pct = round(((prices[-1] - prices[-5]) / prices[-5]) * 100, 1)
        sector["etf_5d_pct"] = pct
        if pct > 1:
            sector["sector_trend"]=f"Bullish +{pct}%";  sector["sector_emoji"]="✅"
        elif pct > -1:
            sector["sector_trend"]=f"Neutral {pct:+.1f}%"; sector["sector_emoji"]="⚠️"
        else:
            sector["sector_trend"]=f"Bearish {pct:+.1f}%"; sector["sector_emoji"]="🔴"
    return sector


# ─────────────────────────────────────────
# MAIN TRADE DATA FETCHER
# ─────────────────────────────────────────
def fetch_trade_data(trade, flow_premium=None) -> dict:
    ticker      = trade.get("ticker")
    strike      = trade.get("strike")
    option_type = trade.get("option_type", "call")
    expiry_raw  = trade.get("expiry_raw")

    data = {
        "ticker":ticker,"stock_price":None,"bid":None,"ask":None,
        "open_interest":None,"spread_pct":None,"otm_pct":None,
        "earnings_date":None,"earnings_date_raw":None,
        "days_to_expiry":None,"days_earnings_to_expiry":None,
        "expiry_timing_label":None,"expiry_timing_emoji":None,
        "historical_moves":[],"avg_earnings_move":None,
        "implied_volatility":None,"implied_move_pct":None,
        "implied_vs_historical":None,"implied_vs_historical_emoji":None,
        "earnings_surprises":[],"avg_earnings_surprise":None,"beats_pct":None,
        "flow_fill_price":flow_premium,"current_ask":None,
        "price_move_since_flow":None,"chasing_flag":None,"chasing_emoji":None,
        "time_of_day":check_time_of_day(),
        "market":{},"sector":{},
    }

    print(f"[FETCHER] Fetching market conditions...")
    data["market"] = fetch_market_conditions()
    data["sector"] = fetch_sector_conditions(ticker or "SPY")

    # Stock price
    print(f"[FETCHER] Fetching {ticker} price...")
    quote = fetch_quote(ticker)
    stock_price = quote.get("price")
    data["stock_price"] = stock_price

    # Earnings date
    print(f"[FETCHER] Fetching {ticker} earnings date...")
    earn_str, earn_dt = fetch_earnings_date(ticker)
    if earn_str:
        data["earnings_date"]     = earn_str
        data["earnings_date_raw"] = earn_dt

    # Days to expiry
    if expiry_raw:
        try:
            parts  = expiry_raw.split("/")
            m, d, y = parts
            y = "20" + y if len(y) == 2 else y
            data["days_to_expiry"] = (datetime(int(y),int(m),int(d)) - datetime.now()).days
        except Exception as e:
            print(f"[FETCHER] DTE error: {e}")

    # Options chain
    current_ask = None
    if expiry_raw and strike and stock_price:
        print(f"[FETCHER] Fetching {ticker} options chain...")
        exp_ts  = expiry_to_timestamp(expiry_raw)
        chain_data = fetch_options_chain(ticker, exp_ts)

        try:
            result    = chain_data.get("optionChain", {}).get("result", [])
            if result:
                opts_key = "calls" if option_type == "call" else "puts"
                options  = result[0].get("options", [{}])[0].get(opts_key, [])
                sf       = float(strike)

                # Find closest strike
                closest = min(options, key=lambda x: abs(x.get("strike",99999) - sf), default=None)
                if closest:
                    data["bid"]              = round(float(closest.get("bid", 0)), 2)
                    data["ask"]              = round(float(closest.get("ask", 0)), 2)
                    data["open_interest"]    = int(closest.get("openInterest", 0))
                    data["implied_volatility"] = round(float(closest.get("impliedVolatility",0))*100, 1)
                    current_ask = data["ask"]
                    data["current_ask"] = current_ask

                    if data["ask"] and data["ask"] > 0:
                        data["spread_pct"] = round(((data["ask"]-data["bid"])/data["ask"])*100, 1)
                    if option_type == "call":
                        data["otm_pct"] = round(((sf-stock_price)/stock_price)*100, 1)
                    else:
                        data["otm_pct"] = round(((stock_price-sf)/stock_price)*100, 1)

                # ATM straddle for implied move
                try:
                    calls = result[0].get("options",[{}])[0].get("calls",[])
                    puts  = result[0].get("options",[{}])[0].get("puts",[])
                    atm_call = min(calls, key=lambda x: abs(x.get("strike",99999)-stock_price), default=None)
                    atm_put  = min(puts,  key=lambda x: abs(x.get("strike",99999)-stock_price), default=None)
                    if atm_call and atm_put:
                        straddle = float(atm_call.get("lastPrice",0)) + float(atm_put.get("lastPrice",0))
                        if straddle > 0:
                            data["implied_move_pct"] = round((straddle/stock_price)*100, 1)
                except Exception as e:
                    print(f"[FETCHER] Straddle error: {e}")
        except Exception as e:
            print(f"[FETCHER] Options parse error: {e}")

    # Chasing detection
    if flow_premium and current_ask and flow_premium > 0:
        mv = round(((current_ask-flow_premium)/flow_premium)*100, 1)
        data["price_move_since_flow"] = mv
        if mv > 75:
            data["chasing_flag"]="HIGH";     data["chasing_emoji"]="🚨"
        elif mv > 40:
            data["chasing_flag"]="MODERATE"; data["chasing_emoji"]="⚠️"
        else:
            data["chasing_flag"]="LOW";      data["chasing_emoji"]="✅"

    # Expiry timing vs earnings
    if data.get("earnings_date_raw") and data.get("days_to_expiry") is not None:
        try:
            ed = data["earnings_date_raw"]
            if hasattr(ed, 'date'): ed = ed.date()
            parts  = expiry_raw.split("/"); m,d,y = parts
            y = "20"+y if len(y)==2 else y
            exp_date = datetime(int(y),int(m),int(d)).date()
            gap = (exp_date - ed).days
            data["days_earnings_to_expiry"] = gap
            if gap < 0:
                data["expiry_timing_label"]="Expiry BEFORE earnings";          data["expiry_timing_emoji"]="❌"
            elif gap == 0:
                data["expiry_timing_label"]="Expiry SAME DAY as earnings";     data["expiry_timing_emoji"]="❌"
            elif gap <= 4:
                data["expiry_timing_label"]=f"Expiry {gap}d after — very tight"; data["expiry_timing_emoji"]="⚠️"
            elif gap <= 14:
                data["expiry_timing_label"]=f"Expiry {gap}d after — sweet spot"; data["expiry_timing_emoji"]="✅"
            else:
                data["expiry_timing_label"]=f"Expiry {gap}d after — too long";  data["expiry_timing_emoji"]="⚠️"
        except Exception as e:
            print(f"[FETCHER] Timing error: {e}")

    # Implied vs historical
    if data.get("implied_move_pct") and data.get("avg_earnings_move"):
        implied = data["implied_move_pct"]; actual = data["avg_earnings_move"]
        ratio = implied/actual if actual > 0 else 1
        if ratio < 0.85:
            data["implied_vs_historical"]=f"Options CHEAP — implied {implied}% vs avg {actual}%"
            data["implied_vs_historical_emoji"]="✅"
        elif ratio < 1.15:
            data["implied_vs_historical"]=f"Options FAIR — implied {implied}% vs avg {actual}%"
            data["implied_vs_historical_emoji"]="⚠️"
        else:
            data["implied_vs_historical"]=f"Options EXPENSIVE — implied {implied}% vs avg {actual}%"
            data["implied_vs_historical_emoji"]="❌"

    print(f"[FETCHER] Done for {ticker}: price=${stock_price}, earnings={data.get('earnings_date')}, OI={data.get('open_interest')}")
    return data
