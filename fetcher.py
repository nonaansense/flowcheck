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

    # Finnhub uses VIX without the ^ prefix
    fh_ticker = ticker.replace("^", "")

    data = fh_get("/quote", {"symbol": fh_ticker})
    if data:
        price = data.get("c") or data.get("pc")
        if price and float(price) > 0:
            price = round(float(price), 2)
            _price_cache[ticker] = (price, now)
            print(f"[FETCHER] {ticker}: ${price} via Finnhub")
            return price

    # Fallback: Tiingo for ETFs and indices Finnhub doesn't serve
    price = tiingo_get_price(fh_ticker)
    if price and price > 0:
        _price_cache[ticker] = (price, now)
        print(f"[FETCHER] {ticker}: ${price} via Tiingo fallback")
        return price

    print(f"[FETCHER] Could not get price for {ticker}")
    return None


def fetch_price_history(ticker: str, days: int = 10) -> list:
    """Fetch recent daily closes — Finnhub first, Tiingo fallback."""
    cache_key = f"hist_{ticker}"
    cached = _sector_cache.get(cache_key)
    if cached and (time.time() - cached[1]) < 600:
        return cached[0]

    now_ts  = int(time.time())
    from_ts = now_ts - (days + 5) * 86400

    # Try Finnhub first (works for individual stocks)
    data = fh_get("/stock/candle", {
        "symbol":     ticker,
        "resolution": "D",
        "from":       from_ts,
        "to":         now_ts,
    })
    if data and data.get("s") == "ok":
        closes = data.get("c", [])
        if closes and len(closes) >= 3:
            _sector_cache[cache_key] = (closes, time.time())
            print(f"[FETCHER] {ticker} history: {len(closes)} days via Finnhub")
            return closes

    # Fallback: Tiingo (works for ETFs, indices, SPY etc.)
    closes = tiingo_get_history(ticker, days)
    if closes:
        _sector_cache[cache_key] = (closes, time.time())
        return closes

    print(f"[FETCHER] Could not get history for {ticker}")
    return []


# ─────────────────────────────────────────
# EARNINGS DATE
# ─────────────────────────────────────────
def fetch_earnings_date(ticker: str):
    """
    Fetch earnings dates from Finnhub.
    Checks both upcoming AND recent past earnings (last 14 days).
    Returns: (date_str, datetime, is_past) tuple
    """
    try:
        today     = datetime.now().date()
        lookback  = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
        in_3mo    = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")

        # Fetch window covering past 14 days AND next 90 days
        data = fh_get("/calendar/earnings", {
            "from":   lookback,
            "to":     in_3mo,
            "symbol": ticker,
        })

        if data and data.get("earningsCalendar"):
            past_earnings   = []
            future_earnings = []

            for event in data["earningsCalendar"]:
                date_str = event.get("date", "")
                if not date_str:
                    continue
                try:
                    dt      = datetime.strptime(date_str, "%Y-%m-%d")
                    dt_date = dt.date()
                    if dt_date <= today:
                        past_earnings.append(dt)
                    else:
                        future_earnings.append(dt)
                except:
                    continue

            # Prefer upcoming earnings
            if future_earnings:
                dt = min(future_earnings)
                print(f"[FETCHER] {ticker} next earnings: {dt.strftime('%b %d, %Y')}")
                return dt.strftime("%b %d, %Y"), dt, False

            # Fall back to most recent past earnings
            if past_earnings:
                dt       = max(past_earnings)
                days_ago = (today - dt.date()).days
                print(f"[FETCHER] {ticker} last earnings: {dt.strftime('%b %d, %Y')} ({days_ago}d ago)")
                return dt.strftime("%b %d, %Y"), dt, True

    except Exception as e:
        print(f"[FETCHER] Earnings error: {e}")

    return None, None, False


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
TIINGO_BASE = "https://api.tiingo.com"

def tiingo_key():
    return os.environ.get("TIINGO_API_KEY")

def tiingo_get(path: str, params: dict = None) -> dict | list | None:
    """Make a Tiingo API call."""
    key = tiingo_key()
    if not key:
        print("[FETCHER] TIINGO_API_KEY not set")
        return None
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Token {key}",
    }
    p = params or {}
    try:
        r = requests.get(f"{TIINGO_BASE}{path}", params=p,
                        headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 429:
            print(f"[FETCHER] Tiingo rate limit")
        else:
            print(f"[FETCHER] Tiingo {r.status_code}: {path}")
    except Exception as e:
        print(f"[FETCHER] Tiingo error: {e}")
    return None


def tiingo_get_price(ticker: str) -> float | None:
    """Get latest price from Tiingo."""
    data = tiingo_get(f"/iex/{ticker.upper()}")
    if data and isinstance(data, list) and data[0].get("last"):
        return round(float(data[0]["last"]), 2)
    # Fallback to end-of-day
    data2 = tiingo_get(f"/tiingo/daily/{ticker.upper()}/prices",
                       params={"startDate": (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")})
    if data2 and isinstance(data2, list):
        price = data2[-1].get("close") or data2[-1].get("adjClose")
        if price:
            return round(float(price), 2)
    return None


def tiingo_get_history(ticker: str, days: int = 10) -> list:
    """Get daily closing prices from Tiingo."""
    start = (datetime.now() - timedelta(days=days + 5)).strftime("%Y-%m-%d")
    data  = tiingo_get(f"/tiingo/daily/{ticker.upper()}/prices",
                       params={"startDate": start, "resampleFreq": "daily"})
    if data and isinstance(data, list):
        closes = [d.get("adjClose") or d.get("close") for d in data if d.get("adjClose") or d.get("close")]
        if closes:
            print(f"[FETCHER] {ticker} history: {len(closes)} days via Tiingo")
            return [round(float(c), 2) for c in closes]
    return []


def fetch_vix_cboe() -> float | None:
    """Fetch VIX — tries CBOE public API then Tiingo."""
    # Try CBOE first (no key needed)
    try:
        url = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json"
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            data  = r.json()
            chart = data.get("data") or []
            if chart and isinstance(chart, list):
                last  = chart[-1]
                price = last[1] if isinstance(last, list) else last.get("price")
                if price and 10 <= float(price) <= 80:
                    vix = round(float(price), 1)
                    print(f"[FETCHER] VIX: {vix} via CBOE")
                    return vix
    except Exception as e:
        print(f"[FETCHER] CBOE VIX error: {e}")

    # Fallback: Tiingo — try multiple VIX ticker options
    for vix_ticker in ["VIX", "VIXCLS", "^VIX"]:
        try:
            data = tiingo_get(f"/iex/{vix_ticker}")
            if data and isinstance(data, list):
                price = data[0].get("last") or data[0].get("tngoLast")
                if price and 10 <= float(price) <= 80:
                    vix = round(float(price), 1)
                    print(f"[FETCHER] VIX: {vix} via Tiingo {vix_ticker}")
                    return vix
        except:
            pass

        try:
            history = tiingo_get_history(vix_ticker, days=5)
            if history:
                vix = history[-1]
                if 10 <= vix <= 80:
                    print(f"[FETCHER] VIX: {vix} via Tiingo {vix_ticker} history")
                    return vix
        except:
            pass

    return None


def fetch_market_conditions() -> dict:
    now      = time.time()
    today    = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    cache_ok = (
        _market_cache.get("ts") and
        (now - _market_cache["ts"]) < _MARKET_TTL and
        _market_cache.get("date") == today  # Reset cache each new trading day
    )
    if cache_ok:
        print("[FETCHER] Using cached market conditions")
        return _market_cache["data"]

    conditions = {
        "vix":None,"vix_label":None,"vix_emoji":None,
        "spy_5d_pct":None,"spy_trend":None,"spy_emoji":None,
        "market_bias":"FAVORABLE","market_score_adjustment":0,
        "market_summary":"Market conditions favor buying premium.",
    }

    # VIX — try sources directly inline (Stooq → CBOE → Finnhub)
    vix_val = None

    # Try Stooq first
    try:
        r = requests.get(
            "https://stooq.com/q/l/?s=%5Evix&f=sd2t2ohlcv&h&e=csv",
            timeout=8, headers={"User-Agent": "Mozilla/5.0"}
        )
        if r.status_code == 200 and r.text and "N/D" not in r.text:
            lines = [l for l in r.text.strip().split("\n") if l and not l.startswith("Symbol")]
            if lines:
                parts = lines[-1].split(",")
                if len(parts) >= 5:
                    p = float(parts[4])
                    if 10 <= p <= 80:
                        vix_val = round(p, 1)
                        print(f"[FETCHER] VIX: {vix_val} via Stooq")
    except Exception as e:
        print(f"[FETCHER] Stooq VIX error: {e}")

    # Try CBOE if Stooq failed
    if not vix_val:
        try:
            r = requests.get(
                "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json",
                timeout=8, headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 200:
                chart = r.json().get("data") or []
                if chart:
                    last = chart[-1]
                    p = last[1] if isinstance(last, list) else last.get("price")
                    if p and 10 <= float(p) <= 80:
                        vix_val = round(float(p), 1)
                        print(f"[FETCHER] VIX: {vix_val} via CBOE")
        except Exception as e:
            print(f"[FETCHER] CBOE VIX error: {e}")

    # Try Finnhub last
    if not vix_val:
        try:
            d = fh_get("/quote", {"symbol": "VIX"})
            if d:
                p = d.get("c") or d.get("pc")
                if p and 10 <= float(p) <= 80:
                    vix_val = round(float(p), 1)
                    print(f"[FETCHER] VIX: {vix_val} via Finnhub")
        except Exception as e:
            print(f"[FETCHER] Finnhub VIX error: {e}")

    if vix_val:
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
        conditions["vix"] = 18.0
        conditions["vix_label"] = "Est."
        conditions["vix_emoji"] = "📊"
        print("[FETCHER] VIX unavailable — using neutral estimate, no penalty")

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
    _market_cache["date"] = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
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

    # Seed with any data already extracted from tweet text or vision parser
    # Vision parser extracts: open_interest, bid, ask, option_price, volume, otm
    tweet_oi    = trade.get("open_interest")
    tweet_bid   = trade.get("bid")
    tweet_ask   = trade.get("ask")
    tweet_iv    = trade.get("implied_volatility")
    tweet_vol   = trade.get("volume")
    tweet_price = trade.get("option_price") or flow_premium
    tweet_otm   = trade.get("otm")

    data = {
        "ticker":ticker,"stock_price":None,
        # Pre-seed from vision/tweet data — will be overwritten by live data if available
        "bid":     round(float(tweet_bid), 2) if tweet_bid else None,
        "ask":     round(float(tweet_ask), 2) if tweet_ask else None,
        "open_interest": int(tweet_oi) if tweet_oi else None,
        "implied_volatility": float(tweet_iv) if tweet_iv else None,
        "spread_pct":None,"otm_pct":None,
        "earnings_date":None,"earnings_date_raw":None,
        "earnings_is_past":False,"days_since_earnings":None,"earnings_context":None,
        "days_to_expiry":None,"days_earnings_to_expiry":None,
        "expiry_timing_label":None,"expiry_timing_emoji":None,
        "historical_moves":[],"avg_earnings_move":None,
        "implied_move_pct":None,
        "implied_vs_historical":None,"implied_vs_historical_emoji":None,
        "earnings_surprises":[],"avg_earnings_surprise":None,"beats_pct":None,
        "flow_fill_price":tweet_price,"current_ask":None,
        "price_move_since_flow":None,"chasing_flag":None,"chasing_emoji":None,
        "time_of_day":check_time_of_day(),
        "market":{},"sector":{},
    }

    # Calculate spread from tweet bid/ask if available
    if tweet_bid is not None and tweet_ask and float(tweet_ask) > 0:
        data["spread_pct"] = round(
            ((float(tweet_ask) - float(tweet_bid)) / float(tweet_ask)) * 100, 1
        )
    # Use tweet OTM if provided
    if tweet_otm is not None:
        try:
            data["otm_pct"] = round(float(tweet_otm), 1)
        except:
            pass

    print(f"[FETCHER] Pre-seeded from tweet/vision: OI={data['open_interest']}, "
          f"bid={data['bid']}, ask={data['ask']}, spread={data['spread_pct']}%, "
          f"OTM={data['otm_pct']}%")

    print(f"[FETCHER] Starting for {ticker} via Finnhub...")

    # Market conditions — cached 10 min
    data["market"] = fetch_market_conditions()
    data["sector"] = fetch_sector_conditions(ticker or "SPY")

    # Stock price
    stock_price = fetch_price(ticker)
    data["stock_price"] = stock_price

    # Earnings date — returns 3 values: (date_str, datetime, is_past)
    earn_str, earn_dt, earn_is_past = fetch_earnings_date(ticker)
    if earn_str:
        data["earnings_date"]        = earn_str
        data["earnings_date_raw"]    = earn_dt
        data["earnings_is_past"]     = earn_is_past
        if earn_is_past and earn_dt:
            days_ago = (datetime.now().date() - earn_dt.date()).days
            data["days_since_earnings"] = days_ago
            if days_ago <= 7:
                data["earnings_context"] = f"Reported {days_ago}d ago — fresh post-earnings"
            elif days_ago <= 21:
                data["earnings_context"] = f"Reported {days_ago}d ago — post-earnings momentum"
            elif days_ago <= 45:
                data["earnings_context"] = f"Reported {days_ago}d ago — continuation play"
            else:
                data["earnings_context"] = f"Reported {days_ago}d ago"
        else:
            data["days_since_earnings"] = None
            data["earnings_context"]    = "Upcoming"

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

            if data.get("earnings_is_past"):
                days_ago = data.get("days_since_earnings", 0)
                if days_ago <= 7:
                    # Fresh post-earnings — best window
                    label_ctx = f"Fresh post-earnings ({days_ago}d ago) — IV deflated"
                    emoji = "✅" if gap >= 14 else "⚠️"
                elif days_ago <= 21:
                    # Good post-earnings window
                    label_ctx = f"Post-earnings momentum ({days_ago}d ago)"
                    emoji = "✅" if gap >= 10 else "⚠️"
                elif days_ago <= 45:
                    # Further out — pure momentum/continuation
                    label_ctx = f"Earnings {days_ago}d ago — momentum/continuation play"
                    emoji = "⚠️"
                else:
                    label_ctx = f"Earnings {days_ago}d ago — distant, thesis needed"
                    emoji = "⚠️"

                data["expiry_timing_label"] = f"{label_ctx}, {gap}d to expiry"
                data["expiry_timing_emoji"] = emoji
            else:
                # Pre-earnings play
                if gap < 0:    data["expiry_timing_label"]=f"Expiry {abs(gap)}d BEFORE earnings — misses catalyst"; data["expiry_timing_emoji"]="❌"
                elif gap == 0: data["expiry_timing_label"]="Expiry SAME DAY as earnings — max IV risk";              data["expiry_timing_emoji"]="❌"
                elif gap <= 4: data["expiry_timing_label"]=f"Expiry {gap}d after earnings — very tight";            data["expiry_timing_emoji"]="⚠️"
                elif gap <= 14:data["expiry_timing_label"]=f"Expiry {gap}d after earnings — sweet spot";            data["expiry_timing_emoji"]="✅"
                else:          data["expiry_timing_label"]=f"Expiry {gap}d after earnings — too much time";         data["expiry_timing_emoji"]="⚠️"
        except Exception as e:
            print(f"[FETCHER] Timing error: {e}")

    # If earnings date unknown, set a clear label
    if not data.get("expiry_timing_label") and data.get("days_to_expiry") is not None:
        dte = data["days_to_expiry"]
        if dte <= 1:
            data["expiry_timing_label"] = f"⚠️ {dte}-DTE — expires immediately, no catalyst known"
            data["expiry_timing_emoji"] = "❌"
        elif dte <= 7:
            data["expiry_timing_label"] = f"{dte}d to expiry — no earnings date found"
            data["expiry_timing_emoji"] = "⚠️"
        else:
            data["expiry_timing_label"] = f"{dte}d to expiry — earnings date unknown"
            data["expiry_timing_emoji"] = "❓"

    print(f"[FETCHER] Done: price=${stock_price}, earn={data['earnings_date']}, OTM={data['otm_pct']}%, DTE={data['days_to_expiry']}")
    return data
