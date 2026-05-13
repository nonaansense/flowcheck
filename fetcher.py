"""
Fetcher — Fixed for Railway/yfinance rate limiting.
Uses retry logic and caches stock data to avoid 429s.
"""
import time, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Simple in-memory cache to avoid hammering Yahoo
_price_cache = {}   # {ticker: (price, timestamp)}
_CACHE_TTL   = 60   # seconds

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


def yf_get(ticker_symbol, **kwargs):
    """Wrapper around yfinance with retry on 429."""
    import yfinance as yf
    for attempt in range(3):
        try:
            t    = yf.Ticker(ticker_symbol)
            hist = t.history(**kwargs)
            if not hist.empty:
                return t, hist
            time.sleep(1)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "Too Many" in msg:
                wait = (attempt + 1) * 3
                print(f"[FETCHER] Rate limited on {ticker_symbol}, waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"[FETCHER] yfinance error {ticker_symbol}: {e}")
                break
    return None, None


def get_price_cached(ticker_symbol: str):
    """Get price with 60s cache to reduce API calls."""
    now = time.time()
    cached = _price_cache.get(ticker_symbol)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    _, hist = yf_get(ticker_symbol, period="1d", interval="5m")
    if hist is not None and not hist.empty:
        price = round(hist["Close"].iloc[-1], 2)
        _price_cache[ticker_symbol] = (price, now)
        return price
    return None


def check_time_of_day() -> dict:
    now_et  = datetime.now(ZoneInfo("America/New_York"))
    total   = now_et.hour * 60 + now_et.minute
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
                "quality":"HIGH","note":"10:00 AM–3:30 PM prime window — highest quality flow."}


def fetch_market_conditions() -> dict:
    conditions = {
        "vix":None,"vix_label":None,"vix_emoji":None,
        "spy_5d_pct":None,"spy_trend":None,"spy_emoji":None,
        "market_bias":None,"market_score_adjustment":0,"market_summary":None,
    }
    # VIX
    vix_price = get_price_cached("^VIX")
    if vix_price:
        v = vix_price
        conditions["vix"] = v
        if v < 18:
            conditions["vix_label"]="Calm"; conditions["vix_emoji"]="✅"
        elif v < 25:
            conditions["vix_label"]="Elevated"; conditions["vix_emoji"]="⚠️"
            conditions["market_score_adjustment"] -= 0.5
        elif v < 35:
            conditions["vix_label"]="High"; conditions["vix_emoji"]="🔴"
            conditions["market_score_adjustment"] -= 1
        else:
            conditions["vix_label"]="Extreme"; conditions["vix_emoji"]="🚨"
            conditions["market_score_adjustment"] -= 2

    # SPY 5-day trend
    try:
        import yfinance as yf
        spy_hist = yf.Ticker("SPY").history(period="7d", interval="1d")
        if len(spy_hist) >= 5:
            pct = round(((spy_hist["Close"].iloc[-1] - spy_hist["Close"].iloc[-5])
                         / spy_hist["Close"].iloc[-5]) * 100, 1)
            conditions["spy_5d_pct"] = pct
            if pct > 2:
                conditions["spy_trend"]=f"Uptrend +{pct}%"; conditions["spy_emoji"]="✅"
            elif pct > -2:
                conditions["spy_trend"]=f"Flat {pct:+.1f}%"; conditions["spy_emoji"]="⚠️"
            else:
                conditions["spy_trend"]=f"Downtrend {pct:+.1f}%"; conditions["spy_emoji"]="🔴"
                conditions["market_score_adjustment"] -= 1
    except Exception as e:
        print(f"[FETCHER] SPY error: {e}")

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
    etf = SECTOR_ETF_MAP.get(ticker.upper(), "SPY")
    sector = {"etf":etf,"etf_5d_pct":None,"sector_trend":None,"sector_emoji":None}
    try:
        import yfinance as yf
        hist = yf.Ticker(etf).history(period="7d", interval="1d")
        if len(hist) >= 5:
            pct = round(((hist["Close"].iloc[-1] - hist["Close"].iloc[-5])
                         / hist["Close"].iloc[-5]) * 100, 1)
            sector["etf_5d_pct"] = pct
            if pct > 1:
                sector["sector_trend"]=f"Bullish +{pct}%"; sector["sector_emoji"]="✅"
            elif pct > -1:
                sector["sector_trend"]=f"Neutral {pct:+.1f}%"; sector["sector_emoji"]="⚠️"
            else:
                sector["sector_trend"]=f"Bearish {pct:+.1f}%"; sector["sector_emoji"]="🔴"
    except Exception as e:
        print(f"[FETCHER] Sector error: {e}")
    return sector


def fetch_earnings_surprise_history(stock) -> list:
    surprises = []
    try:
        ed = stock.earnings_dates
        if ed is None or ed.empty:
            return surprises
        for _, row in ed.iterrows():
            s = row.get("Surprise(%)")
            if s is not None and s == s:  # not NaN
                surprises.append(round(float(s), 1))
            if len(surprises) >= 8:
                break
    except Exception as e:
        print(f"[FETCHER] Surprise history error: {e}")
    return surprises


def find_closest_expiry(available, target_date: str):
    if not available:
        return None
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d")
        return min(available, key=lambda d: abs(
            (datetime.strptime(d, "%Y-%m-%d") - target).days
        ))
    except:
        return available[0] if available else None


def estimate_earnings_moves(stock, hist) -> list:
    moves = []
    try:
        ed = stock.earnings_dates
        if ed is None or ed.empty:
            return moves
        for date, _ in ed.iterrows():
            try:
                ds     = date.strftime("%Y-%m-%d")
                before = hist[hist.index.strftime("%Y-%m-%d") < ds]
                after  = hist[hist.index.strftime("%Y-%m-%d") >= ds]
                if before.empty or after.empty:
                    continue
                pct = ((after.iloc[0]["Close"] - before.iloc[-1]["Close"])
                       / before.iloc[-1]["Close"]) * 100
                moves.append(round(pct, 1))
                if len(moves) >= 8:
                    break
            except:
                continue
    except Exception as e:
        print(f"[FETCHER] Earnings moves error: {e}")
    return moves


def fetch_trade_data(trade, flow_premium=None) -> dict:
    """Fetch all live data. Gracefully handles yfinance failures."""
    import yfinance as yf

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

    # Market conditions (with retry protection already in helpers)
    time.sleep(0.5)  # small delay before batch of requests
    data["market"] = fetch_market_conditions()
    data["sector"] = fetch_sector_conditions(ticker or "SPY")

    try:
        stock       = yf.Ticker(ticker)
        stock_price = get_price_cached(ticker)
        data["stock_price"] = stock_price

        # Earnings date
        try:
            cal = stock.calendar
            if cal is not None and not cal.empty:
                earnings = cal.get("Earnings Date")
                if earnings is not None and len(earnings) > 0:
                    ed = earnings[0]
                    if hasattr(ed, 'strftime'):
                        data["earnings_date"]     = ed.strftime("%b %d, %Y")
                        data["earnings_date_raw"] = ed
                    else:
                        data["earnings_date"] = str(ed)
        except Exception as e:
            print(f"[FETCHER] Earnings date error: {e}")

        # Options chain
        current_ask = None
        if expiry_raw and strike and stock_price:
            try:
                parts = expiry_raw.split("/")
                if len(parts) == 3:
                    m, d, y = parts
                    y = "20" + y if len(y) == 2 else y
                    expiry_yf = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
                    time.sleep(0.3)
                    available      = stock.options
                    closest_expiry = find_closest_expiry(available, expiry_yf)
                    if closest_expiry:
                        chain   = stock.option_chain(closest_expiry)
                        options = chain.calls if option_type == "call" else chain.puts
                        options = options.copy()
                        sf = float(strike)
                        options["diff"] = abs(options["strike"] - sf)
                        closest = options.nsmallest(1, "diff").iloc[0]

                        data["bid"]              = round(float(closest.get("bid",0)), 2)
                        data["ask"]              = round(float(closest.get("ask",0)), 2)
                        data["open_interest"]    = int(closest.get("openInterest",0))
                        data["implied_volatility"] = round(float(closest.get("impliedVolatility",0))*100,1)
                        current_ask = data["ask"]
                        data["current_ask"] = current_ask

                        if data["ask"] and data["ask"] > 0:
                            data["spread_pct"] = round(((data["ask"]-data["bid"])/data["ask"])*100,1)

                        if option_type == "call":
                            data["otm_pct"] = round(((sf-stock_price)/stock_price)*100,1)
                        else:
                            data["otm_pct"] = round(((stock_price-sf)/stock_price)*100,1)

                        # ATM straddle implied move
                        try:
                            atm_c = chain.calls.copy(); atm_p = chain.puts.copy()
                            atm_c["d"] = abs(atm_c["strike"]-stock_price)
                            atm_p["d"] = abs(atm_p["strike"]-stock_price)
                            ac = atm_c.nsmallest(1,"d").iloc[0]
                            ap = atm_p.nsmallest(1,"d").iloc[0]
                            straddle = float(ac.get("lastPrice",0))+float(ap.get("lastPrice",0))
                            if straddle > 0:
                                data["implied_move_pct"] = round((straddle/stock_price)*100,1)
                        except Exception as e:
                            print(f"[FETCHER] Straddle error: {e}")
            except Exception as e:
                print(f"[FETCHER] Options chain error: {e}")

        # Chasing detection
        if flow_premium and current_ask and flow_premium > 0:
            mv = round(((current_ask-flow_premium)/flow_premium)*100,1)
            data["price_move_since_flow"] = mv
            if mv > 75:
                data["chasing_flag"]="HIGH";     data["chasing_emoji"]="🚨"
            elif mv > 40:
                data["chasing_flag"]="MODERATE"; data["chasing_emoji"]="⚠️"
            else:
                data["chasing_flag"]="LOW";      data["chasing_emoji"]="✅"

        # Days to expiry
        if expiry_raw:
            try:
                p = expiry_raw.split("/"); m,d,y = p
                y = "20"+y if len(y)==2 else y
                data["days_to_expiry"] = (datetime(int(y),int(m),int(d))-datetime.now()).days
            except Exception as e:
                print(f"[FETCHER] DTE error: {e}")

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
                if gap < 0:
                    data["expiry_timing_label"]="Expiry BEFORE earnings"; data["expiry_timing_emoji"]="❌"
                elif gap == 0:
                    data["expiry_timing_label"]="Expiry SAME DAY as earnings"; data["expiry_timing_emoji"]="❌"
                elif gap <= 4:
                    data["expiry_timing_label"]=f"Expiry {gap}d after earnings — very tight"; data["expiry_timing_emoji"]="⚠️"
                elif gap <= 14:
                    data["expiry_timing_label"]=f"Expiry {gap}d after earnings — sweet spot ✅"; data["expiry_timing_emoji"]="✅"
                else:
                    data["expiry_timing_label"]=f"Expiry {gap}d after earnings — too much time"; data["expiry_timing_emoji"]="⚠️"
            except Exception as e:
                print(f"[FETCHER] Timing error: {e}")

        # Historical earnings moves
        try:
            time.sleep(0.3)
            hist = stock.history(period="2y", interval="1d")
            moves = estimate_earnings_moves(stock, hist)
            if moves:
                data["historical_moves"]  = moves
                data["avg_earnings_move"] = round(sum(abs(m) for m in moves)/len(moves),1)
        except Exception as e:
            print(f"[FETCHER] Historical moves error: {e}")

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

        # Earnings surprise history
        try:
            surprises = fetch_earnings_surprise_history(stock)
            if surprises:
                data["earnings_surprises"]    = surprises
                data["avg_earnings_surprise"] = round(sum(surprises)/len(surprises),1)
                data["beats_pct"]             = round(sum(1 for s in surprises if s>0)/len(surprises)*100)
        except Exception as e:
            print(f"[FETCHER] Surprise error: {e}")

    except Exception as e:
        print(f"[FETCHER] General error for {ticker}: {e}")

    return data
