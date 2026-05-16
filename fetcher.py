"""
Fetcher — Uses Alpha Vantage as primary data source.
Works globally including Europe. Free tier: 25 calls/day.
We batch carefully to stay under the limit.
"""
import os, time, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_price_cache = {}
_CACHE_TTL   = 300  # 5 min cache — conserve API calls

SECTOR_ETF_MAP = {
    "AAPL":"XLK","MSFT":"XLK","NVDA":"XLK","AMD":"XLK",
    "CSCO":"XLK","ORCL":"XLK","CRM":"XLK","QCOM":"XLK",
    "ANET":"XLK","CRWV":"XLK","MU":"XLK","SNOW":"XLK",
    "JPM":"XLF","BAC":"XLF","GS":"XLF","MS":"XLF",
    "XOM":"XLE","CVX":"XLE","BE":"XLE",
    "ALB":"XLB","FCX":"XLB","NEM":"XLB",
    "JNJ":"XLV","PFE":"XLV","INO":"XLV","VLN":"XLV",
    "AMZN":"XLY","TSLA":"XLY","META":"XLC","ENPH":"XLK",
    "ASTS":"XLK","RKLB":"XLI","SPCE":"XLI",
    "NOK":"XLC","BAND":"XLC","GLD":"XLB",
    "DRAM":"XLK","TECK":"XLB","BLDP":"XLE","CIFR":"XLK",
}

AV_BASE = "https://www.alphavantage.co/query"
_api_calls_today = 0
_MAX_CALLS = 22  # Leave buffer below 25 limit


def av_key():
    return os.environ.get("ALPHA_VANTAGE_KEY")


def av_get(params: dict) -> dict | None:
    """Make Alpha Vantage API call with call tracking."""
    global _api_calls_today
    key = av_key()
    if not key:
        print("[FETCHER] ALPHA_VANTAGE_KEY not set")
        return None

    if _api_calls_today >= _MAX_CALLS:
        print(f"[FETCHER] Daily API limit reached ({_api_calls_today}/{_MAX_CALLS}) — using cached data only")
        return None

    params["apikey"] = key
    try:
        r = requests.get(AV_BASE, params=params, timeout=15)
        _api_calls_today += 1
        print(f"[FETCHER] AV call #{_api_calls_today}: {params.get('function','?')}")

        if r.status_code == 200:
            data = r.json()
            # Check for rate limit or info messages
            if "Note" in data or "Information" in data:
                msg = str(data.get("Note") or data.get("Information") or "")
                print(f"[FETCHER] AV message: {msg[:100]}")
                # Daily limit hit — don't count this as a successful call
                _api_calls_today -= 1
                return None
            return data
        else:
            print(f"[FETCHER] AV HTTP {r.status_code}")
            return None
    except Exception as e:
        print(f"[FETCHER] AV error: {e}")
        return None


# ─────────────────────────────────────────
# PRICE FETCHING
# ─────────────────────────────────────────
def fetch_price(ticker: str) -> float | None:
    """Get current stock price via Alpha Vantage GLOBAL_QUOTE."""
    # Map index tickers
    av_ticker = ticker.replace("^", "")  # ^VIX → VIX
    if ticker == "^VIX":
        av_ticker = "VIX"

    now = time.time()
    cached = _price_cache.get(ticker)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    data = av_get({"function": "GLOBAL_QUOTE", "symbol": av_ticker})
    if data:
        quote = data.get("Global Quote", {})
        price_str = quote.get("05. price") or quote.get("08. previous close")
        if price_str:
            try:
                price = round(float(price_str), 2)
                _price_cache[ticker] = (price, now)
                print(f"[FETCHER] {ticker}: ${price} via AV")
                return price
            except:
                pass

    print(f"[FETCHER] Could not get price for {ticker}")
    return None


def fetch_price_history(ticker: str, days: int = 10) -> list:
    """Fetch recent daily closes via Alpha Vantage TIME_SERIES_DAILY."""
    # Use cache aggressively for history
    cache_key = f"hist_{ticker}"
    now = time.time()
    cached = _price_cache.get(cache_key)
    if cached and (now - cached[1]) < 600:  # 10 min cache for history
        return cached[0]

    av_ticker = ticker.replace("^", "")
    data = av_get({"function": "TIME_SERIES_DAILY", "symbol": av_ticker,
                   "outputsize": "compact"})
    if data:
        ts = data.get("Time Series (Daily)", {})
        # Get last 15 trading days sorted
        closes = []
        for date_str in sorted(ts.keys(), reverse=True)[:15]:
            try:
                closes.append(float(ts[date_str]["4. close"]))
            except:
                pass
        closes.reverse()  # Oldest first
        _price_cache[cache_key] = (closes, now)
        return closes

    return []


def fetch_earnings_date(ticker: str):
    """Fetch earnings date via Alpha Vantage EARNINGS_CALENDAR."""
    try:
        data = av_get({"function": "EARNINGS_CALENDAR", "symbol": ticker,
                       "horizon": "3month"})
        if data:
            # AV returns CSV for earnings calendar
            import io, csv
            reader = csv.DictReader(io.StringIO(str(data)))
            for row in reader:
                report_date = row.get("reportDate", "")
                if report_date:
                    dt = datetime.strptime(report_date, "%Y-%m-%d")
                    if dt.date() >= datetime.now().date():
                        return dt.strftime("%b %d, %Y"), dt
    except Exception as e:
        print(f"[FETCHER] Earnings error: {e}")
    return None, None


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
# Uses cached batch fetch to conserve API calls
# ─────────────────────────────────────────
_market_cache = {}
_MARKET_TTL   = 600  # Cache market data 10 min

def fetch_market_conditions() -> dict:
    """Fetch VIX and SPY data. Cached aggressively to save API calls."""
    now = time.time()
    if _market_cache.get("conditions") and (now - _market_cache.get("ts", 0)) < _MARKET_TTL:
        print("[FETCHER] Using cached market conditions")
        return _market_cache["conditions"]

    conditions = {
        "vix":None,"vix_label":None,"vix_emoji":None,
        "spy_5d_pct":None,"spy_trend":None,"spy_emoji":None,
        "market_bias":"FAVORABLE","market_score_adjustment":0,
        "market_summary":"Market data unavailable — proceeding with trade analysis.",
    }

    # VIX
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

    time.sleep(13)  # AV free = ~5 calls/min = 12s between calls

    # SPY 5-day
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

    _market_cache["conditions"] = conditions
    _market_cache["ts"] = now
    return conditions


def fetch_sector_conditions(ticker: str) -> dict:
    """Get sector ETF trend. Uses cached history where possible."""
    etf    = SECTOR_ETF_MAP.get(ticker.upper(), "SPY")
    sector = {"etf":etf,"etf_5d_pct":None,"sector_trend":None,"sector_emoji":None}

    # Only fetch sector if we haven't used too many calls
    if _api_calls_today >= _MAX_CALLS - 5:
        print(f"[FETCHER] Skipping sector fetch — conserving API calls")
        return sector

    time.sleep(13)
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

    print(f"[FETCHER] Starting for {ticker} via Alpha Vantage (call #{_api_calls_today+1})")

    # Market conditions (cached — uses 2 calls max, then free)
    data["market"] = fetch_market_conditions()
    data["sector"] = fetch_sector_conditions(ticker or "SPY")

    # Stock price (1 call)
    time.sleep(13)
    stock_price = fetch_price(ticker)
    data["stock_price"] = stock_price

    # OTM calculation from tweet data (no API call needed)
    if stock_price and strike:
        sf = float(strike)
        if option_type == "call":
            data["otm_pct"] = round(((sf - stock_price) / stock_price) * 100, 1)
        else:
            data["otm_pct"] = round(((stock_price - sf) / stock_price) * 100, 1)

    # Days to expiry (no API call)
    if expiry_raw:
        try:
            p = expiry_raw.split("/"); m,d,y = p
            y = "20"+y if len(y)==2 else y
            data["days_to_expiry"] = (datetime(int(y),int(m),int(d)) - datetime.now()).days
        except Exception as e:
            print(f"[FETCHER] DTE error: {e}")

    # Note: Alpha Vantage free tier doesn't include options chain data
    # OI, bid/ask, IV come from the tweet/image via vision parser
    # We use flow_premium from tweet as the option price reference
    if flow_premium:
        data["flow_fill_price"] = flow_premium
        print(f"[FETCHER] Using tweet flow premium: ${flow_premium}")

    # Chasing detection (uses tweet price vs current if available)
    # Without options chain, we can't compute chasing — skip

    # Note on scoring: Claude will use tweet data for OI/spread/bid/ask
    # and Polygon data for stock price, OTM%, market conditions

    print(f"[FETCHER] Done: price=${stock_price}, OTM={data['otm_pct']}%, DTE={data['days_to_expiry']}, calls used={_api_calls_today}")
    return data