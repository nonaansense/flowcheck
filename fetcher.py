"""
Fetcher — data sources:
  Stock prices + earnings: Finnhub (free, no IP blocking)
  ETF/SPY history:         Tiingo (free, works globally)
  VIX:                     Stooq → CBOE → Yahoo fallback
"""
import os, time, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ── Caches ────────────────────────────────────────────────────────────
_price_cache  = {}
_sector_cache = {}
_market_cache = {}
_CACHE_TTL    = 120   # 2 min price cache
_MARKET_TTL   = 180   # 3 min market cache

SECTOR_ETF_MAP = {
    "AAPL":"XLK","MSFT":"XLK","NVDA":"XLK","AMD":"XLK","CSCO":"XLK",
    "ORCL":"XLK","CRM":"XLK","QCOM":"XLK","ANET":"XLK","MU":"XLK",
    "JPM":"XLF","BAC":"XLF","GS":"XLF","MS":"XLF",
    "XOM":"XLE","CVX":"XLE","BE":"XLE","PLUG":"XLE",
    "ALB":"XLB","FCX":"XLB","NEM":"XLB","TECK":"XLB","GLD":"XLB",
    "JNJ":"XLV","PFE":"XLV","INO":"XLV",
    "AMZN":"XLY","TSLA":"XLY","META":"XLC",
    "ASTS":"XLK","RKLB":"XLI","SPCE":"XLI","INTC":"XLK",
    "CIFR":"XLK","DRAM":"XLK","NOK":"XLC","NBIS":"XLK","PSKY":"XLK",
    "POET":"XLK","HPE":"XLK","MSFT":"XLK",
}

# ── Finnhub ────────────────────────────────────────────────────────────
def fh_key():
    return os.environ.get("FINNHUB_API_KEY")

def fh_get(path: str, params: dict = None):
    key = fh_key()
    if not key:
        return None
    p = {**(params or {}), "token": key}
    for attempt in range(2):
        try:
            r = requests.get(f"https://finnhub.io/api/v1{path}", params=p, timeout=6)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 429:
                time.sleep((attempt+1)*3)
            elif r.status_code == 403:
                print(f"[FETCHER] Finnhub 403 — endpoint not available on free tier: {path}")
                return None
            else:
                return None
        except requests.exceptions.Timeout:
            print(f"[FETCHER] Finnhub timeout: {path}")
            return None
        except Exception as e:
            print(f"[FETCHER] Finnhub error: {e}")
            return None
    return None

# ── Tiingo ─────────────────────────────────────────────────────────────
def tiingo_key():
    return os.environ.get("TIINGO_API_KEY")

def tiingo_get(path: str, params: dict = None):
    key = tiingo_key()
    if not key:
        return None
    try:
        r = requests.get(
            f"https://api.tiingo.com{path}",
            params=params or {},
            headers={"Content-Type":"application/json","Authorization":f"Token {key}"},
            timeout=10
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[FETCHER] Tiingo error: {e}")
    return None

def tiingo_history(ticker: str, days: int = 10) -> list:
    start = (datetime.now() - timedelta(days=days+5)).strftime("%Y-%m-%d")
    data  = tiingo_get(f"/tiingo/daily/{ticker.upper()}/prices", {"startDate": start})
    if data and isinstance(data, list):
        closes = [round(float(d.get("adjClose") or d.get("close",0)),2) for d in data if d.get("adjClose") or d.get("close")]
        if closes:
            print(f"[FETCHER] {ticker} history: {len(closes)} days via Tiingo")
            return closes
    return []

# ── VIX ────────────────────────────────────────────────────────────────
def fetch_vix() -> float | None:
    # Source 1: Stooq
    try:
        r = requests.get(
            "https://stooq.com/q/l/?s=%5Evix&f=sd2t2ohlcv&h&e=csv",
            timeout=4, headers={"User-Agent": "Mozilla/5.0"}
        )
        if r.status_code == 200 and r.text and "N/D" not in r.text:
            lines = [l for l in r.text.strip().split("\n") if l and not l.startswith("Symbol")]
            if lines:
                parts = lines[-1].split(",")
                if len(parts) >= 5:
                    p = float(parts[4])
                    if 10 <= p <= 80:
                        print(f"[FETCHER] VIX: {p} via Stooq")
                        return round(p, 1)
    except Exception as e:
        print(f"[FETCHER] Stooq error: {str(e)[:60]}")

    # Source 2: CBOE
    try:
        r = requests.get(
            "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json",
            timeout=6, headers={"User-Agent": "Mozilla/5.0"}
        )
        if r.status_code == 200:
            chart = r.json().get("data") or []
            if chart:
                last = chart[-1]
                p    = last[1] if isinstance(last, list) else last.get("price")
                if p and 10 <= float(p) <= 80:
                    print(f"[FETCHER] VIX: {p} via CBOE")
                    return round(float(p), 1)
    except Exception as e:
        print(f"[FETCHER] CBOE error: {str(e)[:60]}")

    # Source 3: Yahoo Finance chart
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX",
            params={"interval":"1d","range":"2d"},
            headers={"User-Agent":"Mozilla/5.0"}, timeout=6
        )
        if r.status_code == 200:
            result = r.json().get("chart",{}).get("result",[])
            if result:
                p = result[0].get("meta",{}).get("regularMarketPrice")
                if p and 10 <= float(p) <= 80:
                    print(f"[FETCHER] VIX: {p} via Yahoo")
                    return round(float(p), 1)
    except Exception as e:
        print(f"[FETCHER] Yahoo VIX error: {str(e)[:60]}")

    return None

# ── Price ──────────────────────────────────────────────────────────────
def fetch_price(ticker: str) -> float | None:
    now    = time.time()
    cached = _price_cache.get(ticker)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    fh_ticker = ticker.replace("^","")
    data      = fh_get("/quote", {"symbol": fh_ticker})
    if data:
        p = data.get("c") or data.get("pc")
        if p and float(p) > 0:
            price = round(float(p), 2)
            _price_cache[ticker] = (price, now)
            print(f"[FETCHER] {ticker}: ${price} via Finnhub")
            return price

    # Tiingo fallback for ETFs
    try:
        data2 = tiingo_get(f"/iex/{fh_ticker}")
        if data2 and isinstance(data2, list):
            p = data2[0].get("last") or data2[0].get("tngoLast")
            if p and float(p) > 0:
                price = round(float(p), 2)
                _price_cache[ticker] = (price, now)
                print(f"[FETCHER] {ticker}: ${price} via Tiingo")
                return price
    except:
        pass

    print(f"[FETCHER] Could not get price for {ticker}")
    return None

def fetch_price_history(ticker: str, days: int = 10) -> list:
    cache_key = f"hist_{ticker}"
    cached    = _sector_cache.get(cache_key)
    if cached and (time.time() - cached[1]) < 600:
        return cached[0]

    # Finnhub candles
    now_ts  = int(time.time())
    from_ts = now_ts - (days+5)*86400
    data    = fh_get("/stock/candle", {"symbol": ticker, "resolution":"D", "from":from_ts, "to":now_ts})
    if data and data.get("s") == "ok":
        closes = data.get("c", [])
        if closes and len(closes) >= 3:
            _sector_cache[cache_key] = (closes, time.time())
            return closes

    # Tiingo fallback
    closes = tiingo_history(ticker, days)
    if closes:
        _sector_cache[cache_key] = (closes, time.time())
        return closes

    return []

# ── Earnings ───────────────────────────────────────────────────────────
def fetch_earnings_date(ticker: str):
    """Returns (date_str, datetime_obj, is_past)"""
    try:
        today    = datetime.now().date()
        lookback = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
        forward  = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")
        data     = fh_get("/calendar/earnings", {"from": lookback, "to": forward, "symbol": ticker})
        if data and data.get("earningsCalendar"):
            past, future = [], []
            for e in data["earningsCalendar"]:
                ds = e.get("date","")
                if not ds: continue
                try:
                    dt = datetime.strptime(ds, "%Y-%m-%d")
                    (future if dt.date() > today else past).append(dt)
                except:
                    pass
            if future:
                dt = min(future)
                print(f"[FETCHER] {ticker} next earnings: {dt.strftime('%b %d, %Y')}")
                return dt.strftime("%b %d, %Y"), dt, False
            if past:
                dt      = max(past)
                days_ago = (today - dt.date()).days
                print(f"[FETCHER] {ticker} last earnings: {dt.strftime('%b %d, %Y')} ({days_ago}d ago)")
                return dt.strftime("%b %d, %Y"), dt, True
    except Exception as e:
        print(f"[FETCHER] Earnings error: {e}")
    return None, None, False

# ── Time of day ────────────────────────────────────────────────────────
def check_time_of_day() -> dict:
    now_et = datetime.now(ZoneInfo("America/New_York"))
    total  = now_et.hour * 60 + now_et.minute
    if total < 9*60+30 or total > 16*60:
        return {"window":"AFTER_HOURS","emoji":"🌙","label":"After hours","quality":"LOW",
                "note":"After-hours flow — lower reliability."}
    elif total < 10*60:
        return {"window":"NOISY_OPEN","emoji":"⚠️","label":"Noisy open 9:30-10:00 AM","quality":"LOW",
                "note":"First 30 min noisy — spreads wide, avoid entries until 10:00 AM ET."}
    elif total > 15*60+30:
        return {"window":"NOISY_CLOSE","emoji":"⚠️","label":"Closing 30 min","quality":"LOW",
                "note":"Last 30 min noisy — position squaring."}
    else:
        return {"window":"PRIME","emoji":"✅","label":"Prime hours 10AM-3:30PM","quality":"HIGH",
                "note":"Highest quality flow window."}

# ── Market conditions ──────────────────────────────────────────────────
def fetch_market_conditions() -> dict:
    now   = time.time()
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    if (_market_cache.get("ts") and (now - _market_cache["ts"]) < _MARKET_TTL
            and _market_cache.get("date") == today):
        print("[FETCHER] Using cached market conditions")
        return _market_cache["data"]

    cond = {
        "vix":None,"vix_label":None,"vix_emoji":None,
        "spy_5d_pct":None,"spy_trend":None,"spy_emoji":None,
        "market_bias":"FAVORABLE","market_score_adjustment":0,
        "market_summary":"Market conditions favor buying premium.",
    }

    # VIX
    vix = fetch_vix()
    if vix:
        cond["vix"] = vix
        if vix < 18:    cond["vix_label"]="Calm";     cond["vix_emoji"]="✅"
        elif vix < 25:  cond["vix_label"]="Elevated"; cond["vix_emoji"]="⚠️";  cond["market_score_adjustment"]-=0.5
        elif vix < 35:  cond["vix_label"]="High";     cond["vix_emoji"]="🔴"; cond["market_score_adjustment"]-=1
        else:           cond["vix_label"]="Extreme";  cond["vix_emoji"]="🚨"; cond["market_score_adjustment"]-=2
    else:
        cond["vix"] = 18.0; cond["vix_label"] = "Est."; cond["vix_emoji"] = "📊"
        print("[FETCHER] VIX unavailable — using 18.0 neutral estimate")

    # SPY 5-day trend
    spy = fetch_price_history("SPY", days=10)
    if len(spy) >= 5:
        pct = round(((spy[-1]-spy[-5])/spy[-5])*100, 1)
        cond["spy_5d_pct"] = pct
        if pct > 2:    cond["spy_trend"]=f"Uptrend +{pct}%";    cond["spy_emoji"]="✅"
        elif pct > -2: cond["spy_trend"]=f"Flat {pct:+.1f}%";   cond["spy_emoji"]="⚠️"
        else:          cond["spy_trend"]=f"Downtrend {pct:+.1f}%"; cond["spy_emoji"]="🔴"; cond["market_score_adjustment"]-=1

    adj = cond["market_score_adjustment"]
    if adj >= 0:    cond["market_bias"]="FAVORABLE";   cond["market_summary"]="Market conditions favor buying premium."
    elif adj >= -1: cond["market_bias"]="CAUTION";     cond["market_summary"]="Elevated volatility — be selective."
    elif adj >= -2: cond["market_bias"]="UNFAVORABLE"; cond["market_summary"]="High VIX/downtrend — avoid buying premium."
    else:           cond["market_bias"]="AVOID";       cond["market_summary"]="Extreme conditions — do not buy premium."

    _market_cache["data"] = cond
    _market_cache["ts"]   = now
    _market_cache["date"] = today
    return cond

def fetch_sector_conditions(ticker: str) -> dict:
    etf    = SECTOR_ETF_MAP.get(ticker.upper(), "SPY")
    sector = {"etf":etf,"etf_5d_pct":None,"sector_trend":None,"sector_emoji":None}
    prices = fetch_price_history(etf, days=10)
    if len(prices) >= 5:
        pct = round(((prices[-1]-prices[-5])/prices[-5])*100, 1)
        sector["etf_5d_pct"] = pct
        if pct > 1:    sector["sector_trend"]=f"Bullish +{pct}%";   sector["sector_emoji"]="✅"
        elif pct > -1: sector["sector_trend"]=f"Neutral {pct:+.1f}%"; sector["sector_emoji"]="⚠️"
        else:          sector["sector_trend"]=f"Bearish {pct:+.1f}%"; sector["sector_emoji"]="🔴"
    return sector

# ── Fill aggression ────────────────────────────────────────────────────
def calc_fill_aggression(trade: dict) -> dict:
    ask_size  = trade.get("ask_size") or 0
    bid_size  = trade.get("bid_size") or 0
    mid_size  = trade.get("mid_size") or 0
    multi_pct = trade.get("multi_pct") or 0
    total     = ask_size + bid_size + mid_size

    if total > 0:
        ask_pct = round((ask_size/total)*100, 1)
        bid_pct = round((bid_size/total)*100, 1)
        if ask_pct >= 90:
            fill_type = "FULL_ASK"; emoji = "🚨"
            label = f"100% at ask ({ask_size:,} contracts) — maximum aggression"
        elif ask_pct >= 70:
            fill_type = "MOSTLY_ASK"; emoji = "✅"
            label = f"{ask_pct}% at ask — aggressive buyer"
        elif ask_pct >= 40:
            fill_type = "MIXED"; emoji = "⚠️"
            label = f"{ask_pct}% ask/{bid_pct}% bid — mixed fill"
        else:
            fill_type = "MOSTLY_BID"; emoji = "❌"
            label = f"{bid_pct}% at bid — passive fill, possible hedge/close"
        if float(multi_pct) > 20:
            fill_type = "SPREAD_LEG"; label += f" ⚠️ {multi_pct}% multi-leg"
    else:
        tweet_text = str(trade.get("raw_text","")).lower()
        if "above ask" in tweet_text or "ask buyer" in tweet_text:
            fill_type = "FULL_ASK"; emoji = "🚨"
            label = "At ask (from tweet text)"
        else:
            fill_type = "UNKNOWN"; emoji = "❓"
            label = "Fill type unknown — see Bullflow screenshot"

    return {"fill_type": fill_type, "fill_emoji": emoji, "fill_label": label,
            "ask_pct": ask_size/(total or 1)*100 if total else None,
            "multi_pct": multi_pct}

# ── Main fetch ─────────────────────────────────────────────────────────
def fetch_greeks(ticker: str, strike: str, opt_type: str,
                  expiry_raw: str) -> dict | None:
    """Fetch options Greeks from Polygon free tier."""
    key = os.environ.get("POLYGON_API_KEY")
    if not key or not expiry_raw or not strike:
        return None
    try:
        parts = expiry_raw.split("/")
        if len(parts) != 3:
            return None
        m, d, y = parts
        y = "20"+y if len(y)==2 else y
        exp_str    = f"{y}{m.zfill(2)}{d.zfill(2)}"
        cp         = "C" if "call" in opt_type.lower() else "P"
        strike_int = int(float(strike) * 1000)
        opt_ticker = f"O:{ticker.upper()}{exp_str}{cp}{strike_int:08d}"

        r = requests.get(
            f"https://api.polygon.io/v3/snapshot/options/{ticker.upper()}/{opt_ticker}",
            params={"apiKey": key},
            timeout=8
        )
        if r.status_code == 200:
            data    = r.json().get("results", {})
            greeks  = data.get("greeks", {})
            details = data.get("details", {})
            if greeks:
                result = {
                    "delta": round(float(greeks.get("delta",0)),3),
                    "theta": round(float(greeks.get("theta",0)),3),
                    "gamma": round(float(greeks.get("gamma",0)),4),
                    "vega":  round(float(greeks.get("vega",0)),3),
                    "iv":    round(float(data.get("implied_volatility",0))*100,1),
                }
                print(f"[FETCHER] Greeks: delta={result['delta']} theta={result['theta']} IV={result['iv']}%")
                return result
    except Exception as e:
        print(f"[FETCHER] Greeks error: {e}")
    return None

def fetch_float_and_short(ticker: str) -> dict:
    """Fetch float shares and short interest from Finnhub."""
    data = fh_get("/stock/profile2", {"symbol": ticker})
    result = {"float_shares": None, "short_interest": None, "short_ratio": None}
    if data:
        shares_float = data.get("shareOutstanding")
        if shares_float:
            result["float_shares"] = round(float(shares_float) * 1e6)
    # Short interest
    short = fh_get("/stock/short-interest", {"symbol": ticker})
    if short and short.get("data"):
        latest = short["data"][0] if short["data"] else {}
        si     = latest.get("shortInterest")
        if si and result["float_shares"]:
            result["short_interest"]  = int(si)
            result["short_ratio"]     = round(si / result["float_shares"] * 100, 1)
            print(f"[FETCHER] Short interest: {result['short_ratio']}% of float")
    return result

def fetch_trade_data(trade: dict, flow_premium=None) -> dict:
    ticker     = trade.get("ticker")
    strike     = trade.get("strike")
    opt_type   = trade.get("option_type","call")
    expiry_raw = trade.get("expiry_raw")

    # Pre-seed from tweet/vision data
    tweet_oi    = trade.get("open_interest")
    tweet_price = trade.get("option_price") or flow_premium
    tweet_otm   = trade.get("otm")
    tweet_bid   = trade.get("bid")
    tweet_ask   = trade.get("ask")

    fill = calc_fill_aggression(trade)

    data = {
        "ticker": ticker, "stock_price": None,
        "bid":   round(float(tweet_bid),2) if tweet_bid else None,
        "ask":   round(float(tweet_ask),2) if tweet_ask else None,
        "open_interest": int(tweet_oi) if tweet_oi else None,
        "spread_pct": None, "otm_pct": None,
        "earnings_date": None, "earnings_date_raw": None,
        "earnings_is_past": False, "days_since_earnings": None, "earnings_context": None,
        "days_to_expiry": None, "days_earnings_to_expiry": None,
        "expiry_timing_label": None, "expiry_timing_emoji": None,
        "implied_volatility": None, "implied_move_pct": None,
        "flow_fill_price": tweet_price, "current_ask": None,
        "is_breakout_bet": False, "breakout_emoji": "", "breakout_label": "",
        "time_of_day": check_time_of_day(),
        "market": {}, "sector": {},
        **fill,
    }

    if tweet_bid is not None and tweet_ask and float(tweet_ask) > 0:
        data["spread_pct"] = round(((float(tweet_ask)-float(tweet_bid))/float(tweet_ask))*100,1)
    if tweet_otm is not None:
        try: data["otm_pct"] = round(float(tweet_otm),1)
        except: pass

    # ── Premium size analysis ─────────────────────────────────────────
    premium     = trade.get("premium") or 0
    stock_px    = None  # will be updated after price fetch
    premium_label = None
    premium_emoji = None

    # Absolute size tiers
    if premium >= 5000000:
        premium_label = f"MEGA flow ${premium/1000000:.1f}M — whale activity"
        premium_emoji = "🐋🐋"
    elif premium >= 1000000:
        premium_label = f"LARGE flow ${premium/1000000:.1f}M — institutional size"
        premium_emoji = "🐋"
    elif premium >= 500000:
        premium_label = f"NOTABLE flow ${premium/1000:.0f}K — above retail threshold"
        premium_emoji = "👀"
    elif premium >= 100000:
        premium_label = f"Moderate flow ${premium/1000:.0f}K"
        premium_emoji = ""
    
    data["premium_label"] = premium_label
    data["premium_emoji"] = premium_emoji
    data["premium_raw"]   = premium

    # Vol/OI ratio — one of the cleanest informed money signals
    vol = trade.get("volume")
    oi  = data.get("open_interest")
    if vol and oi and oi > 0:
        vol_oi_ratio = round(vol / oi, 1)
        data["vol_oi_ratio"] = vol_oi_ratio
        if vol_oi_ratio >= 5:
            data["vol_oi_label"] = f"Vol/OI {vol_oi_ratio}x — massive new position opening"
            data["vol_oi_emoji"] = "🚨"
        elif vol_oi_ratio >= 3:
            data["vol_oi_label"] = f"Vol/OI {vol_oi_ratio}x — unusual accumulation"
            data["vol_oi_emoji"] = "⚠️"
        elif vol_oi_ratio >= 1:
            data["vol_oi_label"] = f"Vol/OI {vol_oi_ratio}x — notable volume"
            data["vol_oi_emoji"] = ""
        else:
            data["vol_oi_label"] = None
            data["vol_oi_emoji"] = ""
        print(f"[FETCHER] Vol/OI ratio: {vol_oi_ratio}x ({vol:,} vol / {oi:,} OI)")
    else:
        data["vol_oi_ratio"] = None
        data["vol_oi_label"] = None
        data["vol_oi_emoji"] = ""

    print(f"[FETCHER] Pre-seeded from tweet/vision: OI={data['open_interest']}, "
          f"bid={data['bid']}, ask={data['ask']}, spread={data['spread_pct']}%, "
          f"OTM={data['otm_pct']}%, fill={data.get('fill_type','?')}, "
          f"vol_oi={data.get('vol_oi_ratio','?')}x")

    print(f"[FETCHER] Starting for {ticker} via Finnhub...")

    # Market + sector
    data["market"] = fetch_market_conditions()
    data["sector"] = fetch_sector_conditions(ticker or "SPY")

    # Stock price
    stock_price = fetch_price(ticker)
    data["stock_price"] = stock_price

    # ── Relative premium analysis (needs stock price) ────────────────
    if stock_price and premium:
        contract_equiv = premium / stock_price  # how many shares worth
        relative_label = None
        relative_emoji = None

        prem_str = f"${premium/1000000:.1f}M" if premium >= 1000000 else f"${premium/1000:.0f}K"

        if stock_price < 10 and premium >= 200000:
            multiple = round(premium / (stock_price * 100), 0)
            relative_label = (f"{prem_str} on ${stock_price:.2f} stock "
                              f"= {multiple:.0f}x typical daily notional — UNUSUAL SIZE")
            relative_emoji = "🚨"
        elif stock_price < 50 and premium >= 200000:
            # Covers $21 stocks with $462K — clearly notable
            ratio = round(premium / (stock_price * 10000), 1)
            relative_label = (f"{prem_str} on ${stock_price:.0f} stock "
                              f"— large relative to stock price")
            relative_emoji = "⚠️"
        elif premium >= 1000000:
            relative_label = f"MEGA flow {prem_str} — whale activity"
            relative_emoji = "🐋🐋"
        elif contract_equiv > 50000:
            relative_label = (f"{prem_str} = {contract_equiv/1000:.0f}K contract-equiv "
                              f"— significant size")
            relative_emoji = "👀"

        if relative_label:
            data["premium_label"] = relative_label
            data["premium_emoji"] = relative_emoji
            print(f"[FETCHER] Premium signal: {relative_label}")

    # OTM from live price
    if stock_price and strike and data["otm_pct"] is None:
        try:
            sf = float(strike)
            data["otm_pct"] = round(((sf-stock_price)/stock_price)*100 if opt_type=="call"
                                    else ((stock_price-sf)/stock_price)*100, 1)
        except: pass

    # Earnings
    earn_str, earn_dt, earn_is_past = fetch_earnings_date(ticker)
    if earn_str:
        data["earnings_date"]     = earn_str
        data["earnings_date_raw"] = earn_dt
        data["earnings_is_past"]  = earn_is_past
        if earn_is_past and earn_dt:
            days_ago = (datetime.now().date() - earn_dt.date()).days
            data["days_since_earnings"] = days_ago
            if days_ago <= 7:    data["earnings_context"] = f"Reported {days_ago}d ago — fresh post-earnings"
            elif days_ago <= 21: data["earnings_context"] = f"Reported {days_ago}d ago — post-earnings momentum"
            elif days_ago <= 45: data["earnings_context"] = f"Reported {days_ago}d ago — continuation play"
            else:                data["earnings_context"] = f"Reported {days_ago}d ago"
        else:
            data["earnings_context"] = "Upcoming"

    # Days to expiry
    if expiry_raw:
        try:
            p = expiry_raw.split("/"); m,d,y = p
            y = "20"+y if len(y)==2 else y
            data["days_to_expiry"] = (datetime(int(y),int(m),int(d)) - datetime.now()).days
        except Exception as e:
            print(f"[FETCHER] DTE error: {e}")

    # Expiry timing vs earnings
    if data.get("earnings_date_raw") and expiry_raw:
        try:
            ed = data["earnings_date_raw"]
            if hasattr(ed,"date"): ed = ed.date()
            p = expiry_raw.split("/"); m,d,y = p
            y = "20"+y if len(y)==2 else y
            exp_date = datetime(int(y),int(m),int(d)).date()
            gap = (exp_date - ed).days
            data["days_earnings_to_expiry"] = gap
            days_ago = data.get("days_since_earnings", 0) or 0

            if data.get("earnings_is_past"):
                if days_ago <= 7:
                    label = f"Fresh post-earnings ({days_ago}d ago) — IV deflated"
                    emoji = "✅" if gap >= 14 else "⚠️"
                elif days_ago <= 21:
                    label = f"Post-earnings momentum ({days_ago}d ago)"
                    emoji = "✅" if gap >= 10 else "⚠️"
                elif days_ago <= 45:
                    label = f"Earnings {days_ago}d ago — continuation play"
                    emoji = "⚠️"
                else:
                    label = f"Earnings {days_ago}d ago — distant"
                    emoji = "⚠️"
                data["expiry_timing_label"] = f"{label}, {gap}d to expiry"
                data["expiry_timing_emoji"] = emoji
            else:
                if gap < 0:    data["expiry_timing_label"]=f"Expiry {abs(gap)}d BEFORE earnings — misses catalyst"; data["expiry_timing_emoji"]="❌"
                elif gap == 0: data["expiry_timing_label"]="Expiry SAME DAY as earnings";   data["expiry_timing_emoji"]="❌"
                elif gap <= 4: data["expiry_timing_label"]=f"Expiry {gap}d after earnings — very tight"; data["expiry_timing_emoji"]="⚠️"
                elif gap <= 14:data["expiry_timing_label"]=f"Expiry {gap}d after earnings — sweet spot"; data["expiry_timing_emoji"]="✅"
                else:          data["expiry_timing_label"]=f"Expiry {gap}d after earnings — too much time"; data["expiry_timing_emoji"]="⚠️"
        except Exception as e:
            print(f"[FETCHER] Timing error: {e}")

    if not data.get("expiry_timing_label") and data.get("days_to_expiry") is not None:
        dte = data["days_to_expiry"]
        data["expiry_timing_label"] = f"{dte}d to expiry — earnings date unknown"
        data["expiry_timing_emoji"] = "❓"

    # Breakout detection
    otm = data.get("otm_pct")
    dte = data.get("days_to_expiry")
    has_upcoming_catalyst = bool(data.get("earnings_date") and not data.get("earnings_is_past"))

    if otm is not None and dte is not None:
        near_atm    = abs(float(otm)) < 2.0
        short_dated = dte < 21
        round_strike = False
        if strike:
            try: round_strike = (float(strike) % 5 == 0)
            except: pass

        if near_atm and short_dated and not has_upcoming_catalyst:
            data["is_breakout_bet"] = True
            data["breakout_emoji"]  = "⚠️"
            resistance_note = "Near round number resistance. " if round_strike else ""
            strike_str      = str(strike) if strike else "ATM"
            straddle_note   = f"<b>→ Consider straddle: buy {strike_str}C + {strike_str}P same expiry</b>"
            data["breakout_label"] = (
                f"Breakout bet — {otm:.1f}% OTM, {dte}d expiry, no catalyst. "
                + resistance_note
                + "Breakouts fail ~60%. " + straddle_note
            )
            print(f"[FETCHER] ⚠️ Breakout bet: OTM={otm}%, DTE={dte}d")
        elif near_atm and not has_upcoming_catalyst:
            data["is_breakout_bet"] = True
            data["breakout_emoji"]  = "⚠️"
            strike_str     = str(strike) if strike else "ATM"
            straddle_note2 = f"<b>→ Consider straddle: buy {strike_str}C + {strike_str}P</b>"
            data["breakout_label"] = (
                f"Near-ATM ({otm:.1f}% OTM), no catalyst — momentum play. " + straddle_note2
            )

    # Greeks
    greeks = fetch_greeks(ticker, trade.get("strike"), trade.get("option_type","call"),
                          trade.get("expiry_raw",""))
    if greeks:
        data["greeks"] = greeks

    # IV rank and percentile
    try:
        from iv_analysis import get_stock_iv_history, calc_earnings_iv_crush_risk
        iv_data = get_stock_iv_history(ticker)
        if iv_data:
            data["iv_rank"]     = iv_data.get("iv_rank")
            data["iv_label"]    = iv_data.get("iv_label")
            data["iv_advice"]   = iv_data.get("iv_advice")
            data["current_iv"]  = iv_data.get("current_iv")
        crush = calc_earnings_iv_crush_risk(data, trade)
        data["crush_risk"]  = crush.get("crush_risk","NONE")
        data["crush_emoji"] = crush.get("crush_emoji","")
        data["crush_label"] = crush.get("crush_label","")
    except Exception as e:
        print(f"[FETCHER] IV analysis error: {e}")

    # News context
    try:
        from news_check import analyze_news_context
        news_data = analyze_news_context(ticker)
        data["news"]            = news_data
        data["has_recent_news"] = news_data.get("has_recent_news",False)
        data["news_context"]    = news_data.get("flow_context","")
        data["news_emoji"]      = news_data.get("flow_context_emoji","")
        data["has_insider_buying"] = news_data.get("has_insider_buying",False)
        data["insider_summary"]    = news_data.get("insider_summary","")
    except Exception as e:
        print(f"[FETCHER] News check error: {e}")

    # Sweep detection from fill data
    fill_type = data.get("fill_type","")
    ask_size  = trade.get("ask_size",0) or 0
    bid_size  = trade.get("bid_size",0) or 0
    multi_pct = trade.get("multi_pct",0) or 0
    if fill_type == "FULL_ASK" and float(multi_pct) < 5:
        data["is_sweep"]    = True
        data["sweep_label"] = "SWEEP — single aggressive buyer, not a spread leg"
        data["sweep_emoji"] = "⚡"
    elif fill_type == "FULL_ASK" and float(multi_pct) >= 20:
        data["is_sweep"]    = False
        data["sweep_label"] = "Likely spread leg — high multi%"
        data["sweep_emoji"] = "⚠️"
    else:
        data["is_sweep"]    = False
        data["sweep_label"] = ""
        data["sweep_emoji"] = ""

    # Float + short interest
    float_data = fetch_float_and_short(ticker)
    data["float_shares"]   = float_data.get("float_shares")
    data["short_interest"] = float_data.get("short_interest")
    data["short_ratio"]    = float_data.get("short_ratio")
    if float_data.get("short_ratio") and float_data["short_ratio"] > 15:
        data["short_squeeze_potential"] = True
        print(f"[FETCHER] ⚠️ High short interest: {float_data['short_ratio']}% — squeeze potential")
    else:
        data["short_squeeze_potential"] = False

    print(f"[FETCHER] Done: price=${stock_price}, earn={data['earnings_date']}, "
          f"OTM={data['otm_pct']}%, DTE={data['days_to_expiry']}, "
          f"fill={data.get('fill_type')}, breakout={data['is_breakout_bet']}")
    return data
