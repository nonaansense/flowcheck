"""
Backtest engine for FlowCheck.
Given a tweet URL + timestamp, fetches historical market data
from Polygon.io and scores the flow as if it just happened.

Usage:
POST /backtest
{
  "tweet": "$FLNC — $462K Call buyer",
  "tweet_url": "https://twitter.com/FL0WG0D/status/2057864981509464127",
  "tweet_time": "2026-05-22T11:51:00"  # ET time when tweet was posted
}
"""
import os, time, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def poly_key():
    return os.environ.get("POLYGON_API_KEY")

def polygon_get(path: str, params: dict = None) -> dict | None:
    key = poly_key()
    if not key:
        return None
    p = {**(params or {}), "apiKey": key}
    try:
        r = requests.get(f"https://api.polygon.io{path}", params=p, timeout=10)
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 429:
            time.sleep(12)
            r2 = requests.get(f"https://api.polygon.io{path}", params=p, timeout=10)
            if r2.status_code == 200:
                return r2.json()
        else:
            print(f"[BACKTEST] Polygon {r.status_code}: {path}")
    except Exception as e:
        print(f"[BACKTEST] Polygon error: {e}")
    return None

def get_historical_price(ticker: str, dt: datetime) -> float | None:
    """Get stock price at a specific datetime using Polygon."""
    date_str = dt.strftime("%Y-%m-%d")

    # Use Unix timestamps for minute bars (more reliable with Polygon)
    from_ts = int((dt - timedelta(minutes=10)).timestamp()) * 1000
    to_ts   = int((dt + timedelta(minutes=2)).timestamp()) * 1000

    data = polygon_get(
        f"/v2/aggs/ticker/{ticker.upper()}/range/1/minute/{from_ts}/{to_ts}",
        {"adjusted": "true", "sort": "asc", "limit": 15}
    )
    if data and data.get("results"):
        target_ts = int(dt.timestamp() * 1000)
        closest   = min(data["results"], key=lambda x: abs(x["t"] - target_ts))
        price     = round(float(closest["c"]), 2)
        bar_time  = datetime.fromtimestamp(closest["t"]/1000).strftime("%H:%M")
        print(f"[BACKTEST] {ticker} at {bar_time}: ${price} (1-min bar)")
        return price

    # Fallback: daily OHLC — use open price as closer to intraday
    data2 = polygon_get(
        f"/v2/aggs/ticker/{ticker.upper()}/range/1/day/{date_str}/{date_str}",
        {"adjusted": "true"}
    )
    if data2 and data2.get("results"):
        # Use open price since tweet may have been early in the day
        bar   = data2["results"][0]
        price = round(float(bar.get("o", bar["c"])), 2)
        print(f"[BACKTEST] {ticker} on {date_str}: ${price} (daily open)")
        return price

    return None

def get_historical_vix(dt: datetime) -> float | None:
    """Get historical VIX for a specific date."""
    date_str = dt.strftime("%Y-%m-%d")

    # Try Polygon VIX (may need premium)
    data = polygon_get(
        f"/v2/aggs/ticker/I:VIX/range/1/day/{date_str}/{date_str}",
        {"adjusted": "true"}
    )
    if data and data.get("results"):
        vix = round(float(data["results"][0]["c"]), 1)
        print(f"[BACKTEST] VIX on {date_str}: {vix} via Polygon")
        return vix

    # Fallback: Stooq historical VIX
    try:
        from_str = (dt - timedelta(days=3)).strftime("%Y%m%d")
        to_str   = dt.strftime("%Y%m%d")
        r = requests.get(
            f"https://stooq.com/q/d/l/?s=%5Evix&d1={from_str}&d2={to_str}&i=d",
            timeout=8, headers={"User-Agent": "Mozilla/5.0"}
        )
        if r.status_code == 200 and r.text and "N/D" not in r.text:
            lines = [l for l in r.text.strip().splitlines() if l and not l.startswith("Date")]
            if lines:
                parts = lines[-1].split(",")
                if len(parts) >= 5:
                    vix = float(parts[4])
                    if 10 <= vix <= 80:
                        print(f"[BACKTEST] VIX on {date_str}: {vix} via Stooq")
                        return round(vix, 1)
    except Exception as e:
        print(f"[BACKTEST] Stooq VIX error: {e}")

    # Final fallback: use current VIX from fetcher
    try:
        from fetcher import fetch_vix
        vix = fetch_vix()
        if vix:
            print(f"[BACKTEST] VIX: {vix} via fetcher (current — historical unavailable)")
            return vix
    except Exception as e:
        print(f"[BACKTEST] Fallback VIX error: {e}")

    return None

def get_historical_spy_trend(dt: datetime) -> dict:
    """Get SPY 5-day trend as of the tweet datetime."""
    from_dt  = dt - timedelta(days=10)
    date_str = dt.strftime("%Y-%m-%d")
    from_str = from_dt.strftime("%Y-%m-%d")

    data = polygon_get(
        f"/v2/aggs/ticker/SPY/range/1/day/{from_str}/{date_str}",
        {"adjusted": "true", "sort": "asc", "limit": 10}
    )
    if data and data.get("results") and len(data["results"]) >= 5:
        closes = [r["c"] for r in data["results"]]
        pct    = round(((closes[-1] - closes[-5]) / closes[-5]) * 100, 1)
        if pct > 2:    trend = f"Uptrend +{pct}%";    emoji = "✅"
        elif pct > -2: trend = f"Flat {pct:+.1f}%";   emoji = "⚠️"
        else:          trend = f"Downtrend {pct:+.1f}%"; emoji = "🔴"
        print(f"[BACKTEST] SPY 5d trend at {dt.strftime('%Y-%m-%d')}: {trend}")
        return {"spy_trend": trend, "spy_emoji": emoji, "spy_5d_pct": pct}
    return {"spy_trend": "N/A", "spy_emoji": "❓", "spy_5d_pct": None}

def get_historical_sector(ticker: str, dt: datetime) -> dict:
    """Get sector ETF trend as of tweet datetime."""
    SECTOR_MAP = {
        "AAPL":"XLK","MSFT":"XLK","NVDA":"XLK","AMD":"XLK","FLNC":"XLK",
        "JPM":"XLF","BAC":"XLF","GS":"XLF",
        "XOM":"XLE","CVX":"XLE","BE":"XLE",
        "JNJ":"XLV","PFE":"XLV",
        "AMZN":"XLY","TSLA":"XLY","META":"XLC",
        "BLDP":"XLE","NOK":"XLC","ASTS":"XLK","ORCL":"XLK",
    }
    etf      = SECTOR_MAP.get(ticker.upper(), "SPY")
    from_dt  = dt - timedelta(days=10)
    date_str = dt.strftime("%Y-%m-%d")
    from_str = from_dt.strftime("%Y-%m-%d")

    data = polygon_get(
        f"/v2/aggs/ticker/{etf}/range/1/day/{from_str}/{date_str}",
        {"adjusted": "true", "sort": "asc", "limit": 10}
    )
    if data and data.get("results") and len(data["results"]) >= 5:
        closes = [r["c"] for r in data["results"]]
        pct    = round(((closes[-1] - closes[-5]) / closes[-5]) * 100, 1)
        if pct > 1:    trend = f"Bullish +{pct}%";   emoji = "✅"
        elif pct > -1: trend = f"Neutral {pct:+.1f}%"; emoji = "⚠️"
        else:          trend = f"Bearish {pct:+.1f}%"; emoji = "🔴"
        return {"etf": etf, "sector_trend": trend, "sector_emoji": emoji}
    return {"etf": etf, "sector_trend": "N/A", "sector_emoji": "❓"}

def build_historical_data(trade: dict, tweet_dt: datetime) -> dict:
    """
    Build data dict using historical market conditions at tweet_dt.
    Replaces live fetcher data with historical Polygon data.
    """
    from economic_calendar import get_today_warnings
    from fetcher import fetch_earnings_date

    ticker     = trade.get("ticker")
    strike     = trade.get("strike")
    opt_type   = trade.get("option_type", "call")
    expiry_raw = trade.get("expiry_raw")

    print(f"[BACKTEST] Fetching historical data for {ticker} at {tweet_dt.strftime('%Y-%m-%d %H:%M ET')}")

    # Historical stock price
    stock_price = get_historical_price(ticker, tweet_dt)
    time.sleep(13)  # Polygon rate limit

    # Historical VIX
    vix = get_historical_vix(tweet_dt)
    time.sleep(13)

    # Historical SPY trend
    spy_data = get_historical_spy_trend(tweet_dt)
    time.sleep(13)

    # Historical sector
    sector = get_historical_sector(ticker, tweet_dt)
    time.sleep(13)

    # Build VIX label
    vix_label = vix_emoji = None
    mkt_adj   = 0
    if vix:
        if vix < 18:    vix_label = "Calm";     vix_emoji = "✅"
        elif vix < 25:  vix_label = "Elevated"; vix_emoji = "⚠️";  mkt_adj -= 0.5
        elif vix < 35:  vix_label = "High";     vix_emoji = "🔴"; mkt_adj -= 1
        else:           vix_label = "Extreme";  vix_emoji = "🚨"; mkt_adj -= 2

    if spy_data.get("spy_5d_pct") and spy_data["spy_5d_pct"] < -2:
        mkt_adj -= 1

    # OTM from historical price
    otm_pct = trade.get("otm")
    if stock_price and strike and not otm_pct:
        try:
            sf = float(strike)
            otm_pct = round(((sf - stock_price) / stock_price * 100) if opt_type == "call"
                            else ((stock_price - sf) / stock_price * 100), 1)
        except:
            pass

    # Days to expiry from tweet date
    dte = None
    if expiry_raw:
        try:
            p = expiry_raw.split("/"); m, d, y = p
            y = "20"+y if len(y) == 2 else y
            exp_date = datetime(int(y), int(m), int(d))
            # Use naive datetime for comparison
            tweet_naive = tweet_dt.replace(tzinfo=None)
            dte = (exp_date - tweet_naive).days
            print(f"[BACKTEST] DTE: {dte} days ({exp_date.strftime('%b %d')} - {tweet_naive.strftime('%b %d')})")
        except Exception as e:
            print(f"[BACKTEST] DTE error: {e}")

    # Earnings date (current — Polygon doesn't have historical earnings calendars)
    earn_str, earn_dt, earn_is_past = fetch_earnings_date(ticker)

    # Time of day at tweet
    total = tweet_dt.hour * 60 + tweet_dt.minute
    if total < 9*60+30 or total > 16*60:
        tod = {"window":"AFTER_HOURS","emoji":"🌙","label":"After hours","quality":"LOW","note":"After-hours flow"}
    elif total < 10*60:
        tod = {"window":"NOISY_OPEN","emoji":"⚠️","label":"Noisy open","quality":"LOW","note":"First 30 min noisy"}
    else:
        tod = {"window":"PRIME","emoji":"✅","label":"Prime hours","quality":"HIGH","note":"Highest quality window"}

    # Fill aggression from vision/tweet data
    from fetcher import calc_fill_aggression
    fill = calc_fill_aggression(trade)

    # Vol/OI ratio
    vol = trade.get("volume")
    oi  = trade.get("open_interest")
    vol_oi_ratio = round(vol/oi, 1) if vol and oi and oi > 0 else None

    data = {
        "ticker":           ticker,
        "stock_price":      stock_price,
        "otm_pct":          otm_pct or trade.get("otm"),
        "open_interest":    oi,
        "spread_pct":       None,
        "bid":              trade.get("bid"),
        "ask":              trade.get("ask"),
        "days_to_expiry":   dte,
        "earnings_date":    earn_str,
        "earnings_date_raw":earn_dt,
        "earnings_is_past": earn_is_past,
        "days_since_earnings": None,
        "earnings_context": "Upcoming" if not earn_is_past else "Past",
        "expiry_timing_label": None,
        "expiry_timing_emoji": None,
        "is_breakout_bet":  False,
        "breakout_emoji":   "",
        "breakout_label":   "",
        "flow_fill_price":  trade.get("option_price"),
        "vol_oi_ratio":     vol_oi_ratio,
        "vol_oi_label":     f"Vol/OI {vol_oi_ratio}x" if vol_oi_ratio else None,
        "vol_oi_emoji":     "🚨" if vol_oi_ratio and vol_oi_ratio >= 10 else "⚠️" if vol_oi_ratio and vol_oi_ratio >= 3 else "",
        "premium_label":    None,
        "premium_emoji":    None,
        "premium_raw":      trade.get("premium", 0),
        "time_of_day":      tod,
        "market": {
            "vix":                    vix,
            "vix_label":              vix_label,
            "vix_emoji":              vix_emoji,
            "spy_trend":              spy_data.get("spy_trend"),
            "spy_emoji":              spy_data.get("spy_emoji"),
            "spy_5d_pct":             spy_data.get("spy_5d_pct"),
            "market_bias":            "FAVORABLE" if mkt_adj >= 0 else "CAUTION",
            "market_score_adjustment":mkt_adj,
            "market_summary":         "Historical market conditions",
        },
        "sector": sector,
        **fill,
    }

    # Breakout detection
    if otm_pct is not None and dte is not None:
        if abs(float(otm_pct)) < 2.0 and dte < 21 and not earn_str:
            s = str(strike) if strike else "ATM"
            data["is_breakout_bet"] = True
            data["breakout_emoji"]  = "⚠️"
            data["breakout_label"]  = f"Breakout bet — {otm_pct:.1f}% OTM, {dte}d expiry. <b>→ Consider straddle: buy {s}C + {s}P</b>"

    print(f"[BACKTEST] Historical data: price=${stock_price}, VIX={vix}, SPY={spy_data.get('spy_trend')}, DTE={dte}")
    return data
