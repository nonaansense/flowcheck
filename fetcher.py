"""
Fetcher — Uses yfinance with cookie/crumb workaround for Railway hosting.
Falls back gracefully when data unavailable.
"""
import time, re, requests, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_price_cache = {}
_CACHE_TTL   = 120

# Free data sources in priority order:
# 1. Yahoo Finance v8 (with crumb)
# 2. Yahoo Finance v7 quote
# 3. FMP free tier (no key needed for basic quotes)
# 4. Return None gracefully

SECTOR_ETF_MAP = {
    "AAPL":"XLK","MSFT":"XLK","NVDA":"XLK","AMD":"XLK",
    "CSCO":"XLK","ORCL":"XLK","CRM":"XLK","QCOM":"XLK",
    "ANET":"XLK","CRWV":"XLK","MU":"XLK","SNOW":"XLK",
    "JPM":"XLF","BAC":"XLF","GS":"XLF","MS":"XLF",
    "XOM":"XLE","CVX":"XLE","BE":"XLE",
    "ALB":"XLB","FCX":"XLB","NEM":"XLB",
    "JNJ":"XLV","PFE":"XLV","INO":"XLV","VLN":"XLV","ENPH":"XLK",
    "AMZN":"XLY","TSLA":"XLY","META":"XLC",
    "ASTS":"XLK","RKLB":"XLI","SPCE":"XLI",
    "NOK":"XLC","BAND":"XLC","GLD":"XLB",
    "DRAM":"XLK","XYZ":"SPY",
}

# ─────────────────────────────────────────
# SESSION WITH ROTATING HEADERS
# ─────────────────────────────────────────
_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://finance.yahoo.com/",
    "Origin": "https://finance.yahoo.com",
})

_crumb = None

def get_crumb():
    """Get Yahoo Finance crumb — required for authenticated API calls."""
    global _crumb
    if _crumb:
        return _crumb
    try:
        # Step 1: Hit consent page to get cookies
        _session.get("https://finance.yahoo.com/", timeout=10)
        time.sleep(0.5)
        # Step 2: Get crumb
        r = _session.get(
            "https://query1.finance.yahoo.com/v1/test/getcrumb",
            timeout=10
        )
        if r.status_code == 200 and r.text and r.text != "null":
            _crumb = r.text.strip()
            print(f"[FETCHER] Got crumb: {_crumb[:10]}...")
            return _crumb
    except Exception as e:
        print(f"[FETCHER] Crumb error: {e}")
    return None


def yahoo_get(url, params=None, retries=2):
    """Make Yahoo Finance API call with crumb and retry logic."""
    crumb = get_crumb()
    if crumb and params is not None:
        params["crumb"] = crumb
    elif crumb:
        params = {"crumb": crumb}

    for attempt in range(retries):
        try:
            r = _session.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r
            elif r.status_code == 401:
                # Crumb expired — refresh
                global _crumb
                _crumb = None
                crumb = get_crumb()
                if crumb and params:
                    params["crumb"] = crumb
                time.sleep(1)
            elif r.status_code == 429:
                wait = (attempt + 1) * 2  # max 6s total
                print(f"[FETCHER] Rate limited — waiting {wait}s")
                time.sleep(wait)
            else:
                print(f"[FETCHER] HTTP {r.status_code} for {url[:60]}")
                break
        except Exception as e:
            print(f"[FETCHER] Request error: {e}")
            time.sleep(2)
    return None


def fetch_price_fmp(ticker: str) -> float | None:
    """Fetch price from Financial Modeling Prep free tier — no API key needed for basic quotes."""
    try:
        # FMP free endpoint — works without auth for basic price data
        r = _session.get(
            f"https://financialmodelingprep.com/api/v3/quote-short/{ticker}",
            params={"apikey": "demo"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list) and data[0].get("price"):
                price = round(float(data[0]["price"]), 2)
                print(f"[FETCHER] {ticker}: ${price} via FMP")
                return price
    except Exception as e:
        print(f"[FETCHER] FMP error for {ticker}: {e}")
    return None


def fetch_price(ticker: str) -> float | None:
    """Get current stock price — tries Yahoo then FMP."""
    now = time.time()
    cached = _price_cache.get(ticker)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    # Skip FMP for index tickers like ^VIX
    is_index = ticker.startswith("^")

    # Try Yahoo v8 chart
    r = yahoo_get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"interval": "1d", "range": "2d"}
    )
    if r:
        try:
            data  = r.json()
            meta  = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            if price:
                price = round(float(price), 2)
                _price_cache[ticker] = (price, now)
                print(f"[FETCHER] {ticker}: ${price} via Yahoo v8")
                return price
        except Exception as e:
            print(f"[FETCHER] Yahoo v8 parse error {ticker}: {e}")

    # Try Yahoo v7 quote
    r2 = yahoo_get("https://query1.finance.yahoo.com/v7/finance/quote",
                   params={"symbols": ticker})
    if r2:
        try:
            data   = r2.json()
            result = data.get("quoteResponse", {}).get("result", [])
            if result:
                price = result[0].get("regularMarketPrice")
                if price:
                    price = round(float(price), 2)
                    _price_cache[ticker] = (price, now)
                    print(f"[FETCHER] {ticker}: ${price} via Yahoo v7")
                    return price
        except Exception as e:
            print(f"[FETCHER] Yahoo v7 parse error {ticker}: {e}")

    # Try FMP as last resort (stocks only, not indices)
    if not is_index:
        price = fetch_price_fmp(ticker)
        if price:
            _price_cache[ticker] = (price, now)
            return price

    print(f"[FETCHER] Could not get price for {ticker}")
    return None


def fetch_price_history(ticker: str, days: int = 10) -> list:
    """Fetch recent daily closes."""
    import time as t
    end   = int(t.time())
    start = end - (days * 86400)
    r = yahoo_get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"interval": "1d", "period1": start, "period2": end}
    )
    if r:
        try:
            data   = r.json()
            result = data.get("chart", {}).get("result", [])
            if result:
                closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                return [c for c in closes if c is not None]
        except Exception as e:
            print(f"[FETCHER] History parse error {ticker}: {e}")
    return []


def fetch_options_chain(ticker: str, expiry_ts: int = None) -> dict:
    """Fetch options chain."""
    params = {}
    if expiry_ts:
        params["date"] = expiry_ts
    r = yahoo_get(
        f"https://query1.finance.yahoo.com/v7/finance/options/{ticker}",
        params=params
    )
    if r:
        try:
            return r.json()
        except:
            pass
    return {}


def fetch_earnings_date(ticker: str):
    """Fetch next earnings date."""
    r = yahoo_get(
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
        params={"modules": "calendarEvents"}
    )
    if r:
        try:
            data   = r.json()
            result = data.get("quoteSummary", {}).get("result", [])
            if result:
                dates = result[0].get("calendarEvents", {}).get("earnings", {}).get("earningsDate", [])
                if dates:
                    ts = dates[0].get("raw")
                    if ts:
                        dt = datetime.fromtimestamp(ts)
                        return dt.strftime("%b %d, %Y"), dt
        except Exception as e:
            print(f"[FETCHER] Earnings parse error {ticker}: {e}")
    return None, None


def expiry_to_ts(expiry_raw: str) -> int | None:
    """Convert MM/DD/YY to Unix timestamp."""
    try:
        p = expiry_raw.split("/")
        m, d, y = p
        y = "20" + y if len(y) == 2 else y
        return int(datetime(int(y), int(m), int(d)).timestamp())
    except:
        return None


# ─────────────────────────────────────────
# TIME OF DAY
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
                "quality":"LOW","note":"Last 30 min noisy — position squaring."}
    else:
        return {"window":"PRIME","emoji":"✅","label":"Prime hours",
                "quality":"HIGH","note":"10:00 AM–3:30 PM — highest quality flow."}


# ─────────────────────────────────────────
# MARKET CONDITIONS
# ─────────────────────────────────────────
def fetch_market_conditions() -> dict:
    conditions = {
        "vix":None,"vix_label":None,"vix_emoji":None,
        "spy_5d_pct":None,"spy_trend":None,"spy_emoji":None,
        "market_bias":None,"market_score_adjustment":0,"market_summary":None,
    }

    vix = fetch_price("^VIX")
    if vix:
        conditions["vix"] = vix
        if vix < 18:
            conditions["vix_label"]="Calm";     conditions["vix_emoji"]="✅"
        elif vix < 25:
            conditions["vix_label"]="Elevated"; conditions["vix_emoji"]="⚠️"
            conditions["market_score_adjustment"] -= 0.5
        elif vix < 35:
            conditions["vix_label"]="High";     conditions["vix_emoji"]="🔴"
            conditions["market_score_adjustment"] -= 1
        else:
            conditions["vix_label"]="Extreme";  conditions["vix_emoji"]="🚨"
            conditions["market_score_adjustment"] -= 2

    spy = fetch_price_history("SPY", days=10)
    if len(spy) >= 5:
        pct = round(((spy[-1] - spy[-5]) / spy[-5]) * 100, 1)
        conditions["spy_5d_pct"] = pct
        if pct > 2:
            conditions["spy_trend"]=f"Uptrend +{pct}%";     conditions["spy_emoji"]="✅"
        elif pct > -2:
            conditions["spy_trend"]=f"Flat {pct:+.1f}%";    conditions["spy_emoji"]="⚠️"
        else:
            conditions["spy_trend"]=f"Downtrend {pct:+.1f}%"; conditions["spy_emoji"]="🔴"
            conditions["market_score_adjustment"] -= 1

    adj = conditions["market_score_adjustment"]
    if adj >= 0:
        conditions["market_bias"]="FAVORABLE"
        conditions["market_summary"]="Market conditions favor buying premium."
    elif adj >= -1:
        conditions["market_bias"]="CAUTION"
        conditions["market_summary"]="Elevated volatility — be selective."
    elif adj >= -2:
        conditions["market_bias"]="UNFAVORABLE"
        conditions["market_summary"]="High VIX/downtrend — avoid buying premium."
    else:
        conditions["market_bias"]="AVOID"
        conditions["market_summary"]="Extreme conditions — do not buy premium."
    return conditions


def fetch_sector_conditions(ticker: str) -> dict:
    etf    = SECTOR_ETF_MAP.get(ticker.upper(), "SPY")
    sector = {"etf":etf,"etf_5d_pct":None,"sector_trend":None,"sector_emoji":None}
    prices = fetch_price_history(etf, days=10)
    if len(prices) >= 5:
        pct = round(((prices[-1] - prices[-5]) / prices[-5]) * 100, 1)
        sector["etf_5d_pct"] = pct
        if pct > 1:
            sector["sector_trend"]=f"Bullish +{pct}%";   sector["sector_emoji"]="✅"
        elif pct > -1:
            sector["sector_trend"]=f"Neutral {pct:+.1f}%"; sector["sector_emoji"]="⚠️"
        else:
            sector["sector_trend"]=f"Bearish {pct:+.1f}%"; sector["sector_emoji"]="🔴"
    return sector


# ─────────────────────────────────────────
# MAIN FETCHER
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

    # Initialize crumb early
    get_crumb()

    print(f"[FETCHER] Starting data fetch for {ticker}...")
    data["market"] = fetch_market_conditions()
    data["sector"] = fetch_sector_conditions(ticker or "SPY")

    # Stock price
    stock_price = fetch_price(ticker)
    data["stock_price"] = stock_price

    # Earnings date
    earn_str, earn_dt = fetch_earnings_date(ticker)
    if earn_str:
        data["earnings_date"]     = earn_str
        data["earnings_date_raw"] = earn_dt
        print(f"[FETCHER] {ticker} earnings: {earn_str}")

    # Days to expiry
    if expiry_raw:
        try:
            p = expiry_raw.split("/"); m,d,y = p
            y = "20"+y if len(y)==2 else y
            data["days_to_expiry"] = (datetime(int(y),int(m),int(d)) - datetime.now()).days
        except Exception as e:
            print(f"[FETCHER] DTE error: {e}")

    # Options chain
    current_ask = None
    if expiry_raw and strike and stock_price:
        exp_ts     = expiry_to_ts(expiry_raw)
        chain_data = fetch_options_chain(ticker, exp_ts)
        try:
            result = chain_data.get("optionChain",{}).get("result",[])
            if result:
                opts_key = "calls" if option_type == "call" else "puts"
                options  = result[0].get("options",[{}])[0].get(opts_key,[])
                sf = float(strike)
                closest = min(options, key=lambda x: abs(x.get("strike",99999)-sf), default=None)
                if closest:
                    data["bid"]           = round(float(closest.get("bid",0)),2)
                    data["ask"]           = round(float(closest.get("ask",0)),2)
                    data["open_interest"] = int(closest.get("openInterest",0))
                    data["implied_volatility"] = round(float(closest.get("impliedVolatility",0))*100,1)
                    current_ask = data["ask"]
                    data["current_ask"] = current_ask
                    if data["ask"] and data["ask"]>0:
                        data["spread_pct"] = round(((data["ask"]-data["bid"])/data["ask"])*100,1)
                    if option_type=="call":
                        data["otm_pct"] = round(((sf-stock_price)/stock_price)*100,1)
                    else:
                        data["otm_pct"] = round(((stock_price-sf)/stock_price)*100,1)
                    print(f"[FETCHER] {ticker} options: bid={data['bid']} ask={data['ask']} OI={data['open_interest']}")

                # ATM straddle
                try:
                    calls = result[0].get("options",[{}])[0].get("calls",[])
                    puts  = result[0].get("options",[{}])[0].get("puts",[])
                    ac = min(calls, key=lambda x: abs(x.get("strike",99999)-stock_price), default=None)
                    ap = min(puts,  key=lambda x: abs(x.get("strike",99999)-stock_price), default=None)
                    if ac and ap:
                        straddle = float(ac.get("lastPrice",0)) + float(ap.get("lastPrice",0))
                        if straddle > 0:
                            data["implied_move_pct"] = round((straddle/stock_price)*100,1)
                except Exception as e:
                    print(f"[FETCHER] Straddle error: {e}")
        except Exception as e:
            print(f"[FETCHER] Options parse error: {e}")

    # Chasing
    if flow_premium and current_ask and flow_premium > 0:
        mv = round(((current_ask-flow_premium)/flow_premium)*100,1)
        data["price_move_since_flow"] = mv
        data["chasing_flag"] = "HIGH" if mv>75 else "MODERATE" if mv>40 else "LOW"
        data["chasing_emoji"] = "🚨" if mv>75 else "⚠️" if mv>40 else "✅"

    # Expiry timing vs earnings
    if data.get("earnings_date_raw") and data.get("days_to_expiry") is not None:
        try:
            ed = data["earnings_date_raw"]
            if hasattr(ed,'date'): ed = ed.date()
            p = expiry_raw.split("/"); m,d,y = p
            y = "20"+y if len(y)==2 else y
            exp_date = datetime(int(y),int(m),int(d)).date()
            gap = (exp_date - ed).days
            data["days_earnings_to_expiry"] = gap
            if gap<0:   data["expiry_timing_label"]="Expiry BEFORE earnings";           data["expiry_timing_emoji"]="❌"
            elif gap==0:data["expiry_timing_label"]="Expiry SAME DAY as earnings";      data["expiry_timing_emoji"]="❌"
            elif gap<=4:data["expiry_timing_label"]=f"Expiry {gap}d after — very tight"; data["expiry_timing_emoji"]="⚠️"
            elif gap<=14:data["expiry_timing_label"]=f"Expiry {gap}d after — sweet spot";data["expiry_timing_emoji"]="✅"
            else:       data["expiry_timing_label"]=f"Expiry {gap}d after — too long";  data["expiry_timing_emoji"]="⚠️"
        except Exception as e:
            print(f"[FETCHER] Timing error: {e}")

    # Implied vs historical
    if data.get("implied_move_pct") and data.get("avg_earnings_move"):
        implied = data["implied_move_pct"]; actual = data["avg_earnings_move"]
        ratio = implied/actual if actual>0 else 1
        if ratio<0.85:
            data["implied_vs_historical"]=f"Options CHEAP — implied {implied}% vs avg {actual}%"
            data["implied_vs_historical_emoji"]="✅"
        elif ratio<1.15:
            data["implied_vs_historical"]=f"Options FAIR — implied {implied}% vs avg {actual}%"
            data["implied_vs_historical_emoji"]="⚠️"
        else:
            data["implied_vs_historical"]=f"Options EXPENSIVE — implied {implied}% vs avg {actual}%"
            data["implied_vs_historical_emoji"]="❌"

    print(f"[FETCHER] Done: price=${data['stock_price']}, earn={data['earnings_date']}, OI={data['open_interest']}, spread={data['spread_pct']}%")
    return data