"""
Fetcher — Uses Polygon.io as primary data source.
Reliable from Railway hosted servers, no IP blocking.
Free tier: 5 calls/minute, unlimited historical data.
"""
import os, time, re, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_price_cache = {}
_CACHE_TTL   = 120  # 2 min cache

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

# ─────────────────────────────────────────
# POLYGON API
# ─────────────────────────────────────────
def polygon_key():
    key = os.environ.get("POLYGON_API_KEY")
    if not key:
        print("[FETCHER] POLYGON_API_KEY not set")
    return key


def polygon_get(path: str, params: dict = None) -> dict | None:
    """Make a Polygon.io API call."""
    key = polygon_key()
    if not key:
        return None

    base_url = "https://api.polygon.io"
    p = params or {}
    p["apiKey"] = key

    for attempt in range(3):
        try:
            r = requests.get(f"{base_url}{path}", params=p, timeout=15)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                wait = (attempt + 1) * 15  # Polygon free = 5/min
                print(f"[FETCHER] Polygon rate limit — waiting {wait}s")
                time.sleep(wait)
            elif r.status_code == 403:
                print(f"[FETCHER] Polygon auth error — check API key")
                return None
            else:
                print(f"[FETCHER] Polygon {r.status_code}: {path}")
                return None
        except Exception as e:
            print(f"[FETCHER] Polygon error: {e}")
            time.sleep(2)
    return None


# ─────────────────────────────────────────
# PRICE FETCHING
# ─────────────────────────────────────────
def fetch_price(ticker: str) -> float | None:
    """Get current stock price via Polygon."""
    # Special handling for index tickers
    if ticker.startswith("^"):
        # Map to Polygon format
        index_map = {"^VIX": "VIX", "^SPX": "SPX", "^NDX": "NDX"}
        poly_ticker = index_map.get(ticker)
        if not poly_ticker:
            return None
        # Use Polygon indices endpoint
        data = polygon_get(f"/v2/aggs/ticker/I:{poly_ticker}/prev")
        if data and data.get("results"):
            price = round(float(data["results"][0].get("c", 0)), 2)
            if price:
                print(f"[FETCHER] {ticker}: ${price} via Polygon index")
                return price
        return None

    now = time.time()
    cached = _price_cache.get(ticker)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    # Try previous close (most reliable)
    data = polygon_get(f"/v2/aggs/ticker/{ticker}/prev")
    if data and data.get("results"):
        price = round(float(data["results"][0].get("c", 0)), 2)
        if price:
            _price_cache[ticker] = (price, now)
            print(f"[FETCHER] {ticker}: ${price} via Polygon prev close")
            return price

    # Try snapshot for real-time price
    data2 = polygon_get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
    if data2:
        snap = data2.get("ticker", {})
        price = snap.get("day", {}).get("c") or snap.get("prevDay", {}).get("c")
        if price:
            price = round(float(price), 2)
            _price_cache[ticker] = (price, now)
            print(f"[FETCHER] {ticker}: ${price} via Polygon snapshot")
            return price

    print(f"[FETCHER] Could not get price for {ticker}")
    return None


def fetch_price_history(ticker: str, days: int = 10) -> list:
    """Fetch recent daily closing prices."""
    if ticker.startswith("^"):
        return []  # Skip indices for history

    try:
        end_date   = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days+5)).strftime("%Y-%m-%d")

        data = polygon_get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}",
            params={"adjusted": "true", "sort": "asc", "limit": 20}
        )
        if data and data.get("results"):
            closes = [r["c"] for r in data["results"]]
            return closes
    except Exception as e:
        print(f"[FETCHER] History error {ticker}: {e}")
    return []


# ─────────────────────────────────────────
# OPTIONS CHAIN
# ─────────────────────────────────────────
def fetch_options_chain_polygon(ticker: str, strike: str,
                                 option_type: str, expiry_raw: str) -> dict:
    """Fetch specific option contract data from Polygon."""
    result = {"bid": None, "ask": None, "open_interest": None,
              "implied_volatility": None, "spread_pct": None}
    try:
        # Convert expiry MM/DD/YY to YYYY-MM-DD
        parts  = expiry_raw.split("/")
        m, d, y = parts
        y = "20" + y if len(y) == 2 else y
        expiry_ymd = f"{y}-{m.zfill(2)}-{d.zfill(2)}"

        # Get options contracts for this ticker/expiry/type
        opt_type = "call" if option_type == "call" else "put"
        data = polygon_get("/v3/reference/options/contracts", params={
            "underlying_ticker": ticker,
            "contract_type":     opt_type,
            "expiration_date":   expiry_ymd,
            "limit":             250,
        })

        if not data or not data.get("results"):
            print(f"[FETCHER] No options contracts found for {ticker} {expiry_ymd}")
            return result

        # Find closest strike
        sf       = float(strike)
        options  = data["results"]
        closest  = min(options, key=lambda x: abs(x.get("strike_price", 99999) - sf),
                       default=None)

        if not closest:
            return result

        ticker_sym = closest.get("ticker")
        print(f"[FETCHER] Found option contract: {ticker_sym}")

        # Get snapshot for bid/ask/OI
        snap_data = polygon_get(
            f"/v3/snapshot/options/{ticker}/{ticker_sym}"
        )
        if snap_data and snap_data.get("results"):
            snap = snap_data["results"]
            details = snap.get("details", {})
            greeks  = snap.get("greeks", {})
            day     = snap.get("day", {})

            result["bid"]              = round(float(snap.get("last_quote", {}).get("bid", 0) or 0), 2)
            result["ask"]              = round(float(snap.get("last_quote", {}).get("ask", 0) or 0), 2)
            result["open_interest"]    = int(snap.get("open_interest", 0) or 0)
            result["implied_volatility"] = round(float(snap.get("implied_volatility", 0) or 0) * 100, 1)

            if result["ask"] and result["ask"] > 0 and result["bid"] is not None:
                result["spread_pct"] = round(
                    ((result["ask"] - result["bid"]) / result["ask"]) * 100, 1
                )

            print(f"[FETCHER] Options: bid={result['bid']} ask={result['ask']} OI={result['open_interest']} IV={result['implied_volatility']}%")

    except Exception as e:
        print(f"[FETCHER] Options chain error: {e}")

    return result


def fetch_atm_straddle(ticker: str, stock_price: float,
                        expiry_raw: str) -> float | None:
    """Fetch ATM straddle price for implied move calculation."""
    try:
        parts  = expiry_raw.split("/")
        m, d, y = parts
        y = "20" + y if len(y) == 2 else y
        expiry_ymd = f"{y}-{m.zfill(2)}-{d.zfill(2)}"

        total = 0.0
        for opt_type in ["call", "put"]:
            data = polygon_get("/v3/reference/options/contracts", params={
                "underlying_ticker": ticker,
                "contract_type":     opt_type,
                "expiration_date":   expiry_ymd,
                "limit":             50,
            })
            if data and data.get("results"):
                sf      = stock_price
                closest = min(data["results"],
                              key=lambda x: abs(x.get("strike_price", 99999) - sf),
                              default=None)
                if closest:
                    sym  = closest.get("ticker")
                    snap = polygon_get(f"/v3/snapshot/options/{ticker}/{sym}")
                    if snap and snap.get("results"):
                        last_price = snap["results"].get("day", {}).get("close") or \
                                     snap["results"].get("last_quote", {}).get("ask", 0)
                        total += float(last_price or 0)
            time.sleep(0.2)  # Stay under free tier rate limit

        if total > 0 and stock_price > 0:
            return round((total / stock_price) * 100, 1)
    except Exception as e:
        print(f"[FETCHER] Straddle error: {e}")
    return None


# ─────────────────────────────────────────
# EARNINGS DATE
# ─────────────────────────────────────────
def fetch_earnings_date(ticker: str):
    """Fetch next earnings date from Polygon."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        data  = polygon_get("/vX/reference/financials", params={
            "ticker":      ticker,
            "timeframe":   "quarterly",
            "limit":       4,
            "sort":        "filing_date",
            "order":       "desc",
        })
        # Polygon financials gives past earnings — use to estimate next
        # Better: use the earnings calendar endpoint
        data2 = polygon_get(f"/v1/meta/symbols/{ticker}/company")
        # Fallback: try to find upcoming from news/events
        # For now return None gracefully — scorer handles missing earnings
        print(f"[FETCHER] Earnings date not available via Polygon free tier for {ticker}")
        return None, None
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
# ─────────────────────────────────────────
def fetch_market_conditions() -> dict:
    conditions = {
        "vix":None,"vix_label":None,"vix_emoji":None,
        "spy_5d_pct":None,"spy_trend":None,"spy_emoji":None,
        "market_bias":None,"market_score_adjustment":0,"market_summary":None,
    }

    # VIX via Polygon index
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
    else:
        print("[FETCHER] VIX unavailable — skipping market adjustment")

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

    print(f"[FETCHER] Starting data fetch for {ticker} via Polygon...")

    # Market conditions
    data["market"] = fetch_market_conditions()
    time.sleep(0.5)
    data["sector"] = fetch_sector_conditions(ticker or "SPY")
    time.sleep(0.5)

    # Stock price
    stock_price = fetch_price(ticker)
    data["stock_price"] = stock_price
    time.sleep(0.3)

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

    # Options chain
    current_ask = None
    if expiry_raw and strike and stock_price:
        time.sleep(0.5)
        opts = fetch_options_chain_polygon(ticker, strike, option_type, expiry_raw)
        data["bid"]              = opts.get("bid")
        data["ask"]              = opts.get("ask")
        data["open_interest"]    = opts.get("open_interest")
        data["implied_volatility"] = opts.get("implied_volatility")
        data["spread_pct"]       = opts.get("spread_pct")
        current_ask = data["ask"]
        data["current_ask"] = current_ask

        if stock_price and strike:
            sf = float(strike)
            if option_type == "call":
                data["otm_pct"] = round(((sf - stock_price) / stock_price) * 100, 1)
            else:
                data["otm_pct"] = round(((stock_price - sf) / stock_price) * 100, 1)

        # ATM straddle for implied move
        time.sleep(0.5)
        impl_move = fetch_atm_straddle(ticker, stock_price, expiry_raw)
        if impl_move:
            data["implied_move_pct"] = impl_move

    # Chasing detection
    if flow_premium and current_ask and float(flow_premium) > 0:
        mv = round(((current_ask - float(flow_premium)) / float(flow_premium)) * 100, 1)
        data["price_move_since_flow"] = mv
        data["chasing_flag"] = "HIGH" if mv > 75 else "MODERATE" if mv > 40 else "LOW"
        data["chasing_emoji"] = "🚨" if mv > 75 else "⚠️" if mv > 40 else "✅"

    # Expiry timing vs earnings
    if data.get("earnings_date_raw") and data.get("days_to_expiry") is not None:
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

    # Implied vs historical
    if data.get("implied_move_pct") and data.get("avg_earnings_move"):
        implied = data["implied_move_pct"]; actual = data["avg_earnings_move"]
        ratio = implied / actual if actual > 0 else 1
        if ratio < 0.85:
            data["implied_vs_historical"] = f"Options CHEAP — implied {implied}% vs avg {actual}%"
            data["implied_vs_historical_emoji"] = "✅"
        elif ratio < 1.15:
            data["implied_vs_historical"] = f"Options FAIR — implied {implied}% vs avg {actual}%"
            data["implied_vs_historical_emoji"] = "⚠️"
        else:
            data["implied_vs_historical"] = f"Options EXPENSIVE — implied {implied}% vs avg {actual}%"
            data["implied_vs_historical_emoji"] = "❌"

    print(f"[FETCHER] Done: price=${data['stock_price']}, OI={data['open_interest']}, spread={data['spread_pct']}%, OTM={data['otm_pct']}%")
    return data
