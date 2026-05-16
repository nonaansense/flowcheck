"""
Fetcher — Uses Finnhub as primary data source.
Free tier: 60 calls/minute, no daily limit.
Works globally including Europe.
Sign up at finnhub.io for free API key.
"""
import os, time, requests, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_price_cache  = {}
_CACHE_TTL    = 120  # 2 min cache
_sector_cache = {}
_market_cache = {}
_MARKET_TTL   = 600  # 10 min market cache

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

FINNHUB_BASE = "https://finnhub.io/api/v1"


def fh_key():
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        print("[FETCHER] FINNHUB_API_KEY not set")
    return key


def fh_get(path: str, params: dict = None) -> dict | None:
    """Make a Finnhub API call."""
    key = fh_key()
    if not key:
        return None

    p = params or {}
    p["token"] = key

    for attempt in range(3):
        try:
            r = requests.get(f"{FINNHUB_BASE}{path}", params=p, timeout=15)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                wait = (attempt + 1) * 5
                print(f"[FETCHER] Finnhub rate limit — waiting {wait}s")
                time.sleep(wait)
            elif r.status_code == 403:
                print(f"[FETCHER] Finnhub auth error — check API key")
                return None
            else:
                print(f"[FETCHER] Finnhub {r.status_code}: {path}")
                break
        except Exception as e:
            print(f"[FETCHER] Finnhub error: {e}")
            time.sleep(2)
    return None


# ─────────────────────────────────────────
# PRICE FETCHING
# ─────────────────────────────────────────
def fetch_price(ticker: str) -> float | None:
    """Get current stock price via Finnhub quote."""
    now = time.time()
    cached = _price_cache.get(ticker)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    # Finnhub serves ^VIX directly
    fh_ticker = ticker

    data = fh_get("/quote", {"symbol": fh_ticker})
    if data:
        price = data.get("c") or data.get("pc")  # current or prev close
        if price and float(price) > 0:
            price = round(float(price), 2)
            _price_cache[ticker] = (price, now)
            print(f"[FETCHER] {ticker}: ${price} via Finnhub")
            return price

    print(f"[FETCHER] Could not get price for {ticker}")
    return None


def fetch_price_history(ticker: str, days: int = 10) -> list:
    """Fetch recent daily closes via Finnhub candles."""
    now_ts   = int(time.time())
    from_ts  = now_ts - (days + 5) * 86400

    cache_key = f"hist_{ticker}"
    cached = _sector_cache.get(cache_key)
    if cached and (time.time() - cached[1]) < 600:
        return cached[0]

    data = fh_get("/stock/candle", {
        "symbol":     ticker,
        "resolution": "D",
        "from":       from_ts,
        "to":         now_ts,
    })

    if data and data.get("s") == "ok":
        closes = data.get("c", [])
        if closes:
            _sector_cache[cache_key] = (closes, time.time())
            return closes

    return []


# ─────────────────────────────────────────
# EARNINGS DATE
# ─────────────────────────────────────────
def fetch_earnings_date(ticker: str):
    """Fetch next earnings date from Finnhub."""
    try:
        today    = datetime.now().strftime("%Y-%m-%d")
        in_3mo   = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")

        data = fh_get("/calendar/earnings", {
            "from":   today,
            "to":     in_3mo,
            "symbol": ticker,
        })

        if data and data.get("earningsCalendar"):
            for event in data["earningsCalendar"]:
                date_str = event.get("date", "")
                if date_str and date_str >= today:
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                    print(f"[FETCHER] {ticker} earnings: {dt.strftime('%b %d, %Y')}")
                    return dt.strftime("%b %d, %Y"), dt

    except Exception as e:
        print(f"[FETCHER] Earnings error: {e}")

    return None, None


# ─────────────────────────────────────────
# OPTIONS DATA
# Note: Finnhub free tier has limited options data
# We use what's in the tweet + vision parser for OI/bid/ask
# Finnhub provides IV via stock metrics
# ─────────────────────────────────────────
def fetch_stock_metrics(ticker: str) -> dict:
    """Fetch stock metrics including volatility data."""
    data = fh_get("/stock/metric", {
        "symbol": ticker,
        "metric": "all",
    })
    if data:
        metrics = data.get("metric", {})
        return {
            "52w_high":     metrics.get("52WeekHigh"),
            "52w_low":      metrics.get("52WeekLow"),
            "beta":         metrics.get("beta"),
            "avg_volume":   metrics.get("10DayAverageTradingVolume"),
        }
    return {}


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
    now = time.time()
    if _market_cache.get("ts") and (now - _market_cache["ts"]) < _MARKET_TTL:
        print("[FETCHER] Using cached market conditions")
        return _market_cache["data"]

    conditions = {
        "vix":None,"vix_label":None,"vix_emoji":None,
        "spy_5d_pct":None,"spy_trend":None,"spy_emoji":None,
        "market_bias":"FAVORABLE","market_score_adjustment":0,
        "market_summary":"Market conditions favor buying premium.",
    }

    # VIX direct from Finnhub
    vix_val = fetch_price("^VIX")
    if vix_val and vix_val > 0:
        conditions["vix"] = vix_val
        if vix_val < 18:
            conditions["vix_label"]="Calm";     conditions["vix_emoji"]="✅"
        elif vix_val < 25:
            conditions["vix_label"]="Elevated"; conditions["vix_emoji"]="⚠️"
            conditions["market_score_adjustment"] -= 0.5
        elif vix_val < 35:
            conditions["vix_label"]="High";     conditions["vix_emoji"]="🔴"
            conditions["market_score_adjustment"] -= 1
        else:
            conditions["vix_label"]="Extreme";  conditions["vix_emoji"]="🚨"
            conditions["market_score_adjustment"] -= 2
    else:
        print("[FETCHER] VIX unavailable — no market adjustment applied")

    # SPY 5-day trend
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

    _market_cache["data"] = conditions
    _market_cache["ts"]   = now
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

    print(f"[FETCHER] Starting for {ticker} via Finnhub...")

    # Market conditions — cached 10 min
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

    # Days to expiry
    if expiry_raw:
        try:
            p = expiry_raw.split("/"); m,d,y = p
            y = "20"+y if len(y)==2 else y
            data["days_to_expiry"] = (datetime(int(y),int(m),int(d)) - datetime.now()).days
        except Exception as e:
            print(f"[FETCHER] DTE error: {e}")

    # OTM % from live price
    if stock_price and strike:
        try:
            sf = float(strike)
            if option_type == "call":
                data["otm_pct"] = round(((sf - stock_price) / stock_price) * 100, 1)
            else:
                data["otm_pct"] = round(((stock_price - sf) / stock_price) * 100, 1)
        except:
            pass

    # Flow premium from tweet
    if flow_premium:
        data["flow_fill_price"] = flow_premium

    # Expiry timing vs earnings
    if data.get("earnings_date_raw") and expiry_raw:
        try:
            ed = data["earnings_date_raw"]
            if hasattr(ed, 'date'): ed = ed.date()
            p = expiry_raw.split("/"); m,d,y = p
            y = "20"+y if len(y)==2 else y
            exp_date = datetime(int(y),int(m),int(d)).date()
            gap = (exp_date - ed).days
            data["days_earnings_to_expiry"] = gap
            if gap < 0:    data["expiry_timing_label"]="Expiry BEFORE earnings";           data["expiry_timing_emoji"]="❌"
            elif gap == 0: data["expiry_timing_label"]="Expiry SAME DAY as earnings";      data["expiry_timing_emoji"]="❌"
            elif gap <= 4: data["expiry_timing_label"]=f"Expiry {gap}d after — very tight"; data["expiry_timing_emoji"]="⚠️"
            elif gap <= 14:data["expiry_timing_label"]=f"Expiry {gap}d after — sweet spot"; data["expiry_timing_emoji"]="✅"
            else:          data["expiry_timing_label"]=f"Expiry {gap}d after — too long";  data["expiry_timing_emoji"]="⚠️"
        except Exception as e:
            print(f"[FETCHER] Timing error: {e}")

    print(f"[FETCHER] Done: price=${stock_price}, earn={data['earnings_date']}, OTM={data['otm_pct']}%, DTE={data['days_to_expiry']}")
    return data
