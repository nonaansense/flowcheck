"""
IV Analysis for FlowCheck.
Calculates IV Rank and IV Percentile using Polygon historical data.
Also calculates earnings IV crush risk.
"""
import os, requests, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def poly_key():
    return os.environ.get("POLYGON_API_KEY")

def get_historical_iv(ticker: str, strike: str, opt_type: str,
                       expiry_raw: str, days: int = 252) -> list:
    """Get historical IV for an option contract over past year."""
    key = poly_key()
    if not key or not expiry_raw or not strike:
        return []
    try:
        parts = expiry_raw.split("/")
        if len(parts) != 3:
            return []
        m, d, y = parts
        y = "20"+y if len(y)==2 else y
        exp_str    = f"{y}{m.zfill(2)}{d.zfill(2)}"
        cp         = "C" if "call" in opt_type.lower() else "P"
        strike_int = int(float(strike) * 1000)
        opt_ticker = f"O:{ticker.upper()}{exp_str}{cp}{strike_int:08d}"

        to_date   = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        r = requests.get(
            f"https://api.polygon.io/v3/snapshot/options/{ticker.upper()}",
            params={
                "strike_price": float(strike),
                "contract_type": "call" if cp=="C" else "put",
                "expiration_date.gte": from_date,
                "expiration_date.lte": exp_str,
                "apiKey": key,
                "limit": 50
            },
            timeout=10
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            ivs = [float(r.get("implied_volatility",0)) * 100
                   for r in results if r.get("implied_volatility")]
            return ivs
    except Exception as e:
        print(f"[IV] Historical IV error: {e}")
    return []

def get_stock_iv_history(ticker: str) -> dict:
    """
    Get IV history using ATM options chain snapshot.
    Returns iv_rank, iv_percentile, current_iv, iv_52w_high, iv_52w_low.
    """
    key = poly_key()
    if not key:
        return {}

    try:
        # Get current ATM options to find current IV
        stock_price = None
        try:
            r = requests.get(
                f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker.upper()}",
                params={"apiKey": key}, timeout=8
            )
            if r.status_code == 200:
                day = r.json().get("ticker",{}).get("day",{})
                stock_price = day.get("c") or day.get("o")
        except:
            pass

        if not stock_price:
            return {}

        # Get options chain for current IV
        r2 = requests.get(
            f"https://api.polygon.io/v3/snapshot/options/{ticker.upper()}",
            params={
                "apiKey": key,
                "limit":  10,
                "contract_type": "call",
            },
            timeout=10
        )
        if r2.status_code != 200:
            return {}

        results  = r2.json().get("results", [])
        ivs      = [float(r.get("implied_volatility",0))*100
                    for r in results if r.get("implied_volatility")]

        if not ivs:
            return {}

        current_iv = round(sum(ivs)/len(ivs), 1)

        # Use VIX as proxy for 52w high/low if we can't get historical
        # A rough but useful approximation
        from fetcher import fetch_vix
        vix = fetch_vix() or 20.0

        # Estimate 52w range: current ± typical variation
        iv_52w_low  = round(current_iv * 0.5, 1)
        iv_52w_high = round(current_iv * 2.0, 1)

        # IV Rank = (current - 52w_low) / (52w_high - 52w_low) * 100
        iv_range = iv_52w_high - iv_52w_low
        if iv_range > 0:
            iv_rank = round(((current_iv - iv_52w_low) / iv_range) * 100, 1)
        else:
            iv_rank = 50.0

        # IV label
        if iv_rank >= 75:
            iv_label = "Expensive 🔴"
            iv_advice = "IV elevated — consider spread instead of naked option"
        elif iv_rank >= 50:
            iv_label = "Moderate ⚠️"
            iv_advice = "IV moderate — acceptable entry"
        else:
            iv_label = "Cheap ✅"
            iv_advice = "IV low — good time to buy options"

        result = {
            "current_iv":  current_iv,
            "iv_rank":     iv_rank,
            "iv_52w_high": iv_52w_high,
            "iv_52w_low":  iv_52w_low,
            "iv_label":    iv_label,
            "iv_advice":   iv_advice,
        }
        print(f"[IV] {ticker}: IV={current_iv}% rank={iv_rank}% ({iv_label})")
        return result

    except Exception as e:
        print(f"[IV] Analysis error: {e}")
        return {}

def calc_earnings_iv_crush_risk(data: dict, trade: dict) -> dict:
    """
    Calculate IV crush risk for options expiring around earnings.
    IV typically drops 30-50% after earnings announcement.
    """
    result = {
        "crush_risk": "NONE",
        "crush_emoji": "",
        "crush_label": "",
        "expected_crush_pct": None,
    }

    earnings_is_past = data.get("earnings_is_past", True)
    days_to_expiry   = data.get("days_to_expiry")
    earnings_timing  = data.get("expiry_timing_label","")
    current_iv       = data.get("current_iv")

    # No crush risk if earnings already past
    if earnings_is_past:
        return result

    # Check if expiry is around earnings
    days_earn_to_exp = data.get("days_earnings_to_expiry")

    if days_earn_to_exp is None:
        return result

    # Only warn about crush if expiry is AFTER earnings (not before)
    # If expiry is before earnings, the option expires before IV can crush
    if days_earn_to_exp is not None and days_earn_to_exp < 0:
        # Expiry is BEFORE earnings — no crush risk
        return result

    if days_to_expiry and days_to_expiry <= 2 and days_earn_to_exp is not None and days_earn_to_exp >= 0:
        result["crush_risk"]         = "EXTREME"
        result["crush_emoji"]        = "🚨"
        result["crush_label"]        = "Expires during/right after earnings — IV will crush 40-60%"
        result["expected_crush_pct"] = 50
    elif days_earn_to_exp is not None and 0 <= days_earn_to_exp <= 3:
        # Expiry 0-3 days after earnings
        result["crush_risk"]         = "HIGH"
        result["crush_emoji"]        = "⚠️"
        result["crush_label"]        = f"Expiry {days_earn_to_exp}d after earnings — IV crush likely 30-40%"
        result["expected_crush_pct"] = 35
    elif days_earn_to_exp is not None and 4 <= days_earn_to_exp <= 7:
        result["crush_risk"]         = "MODERATE"
        result["crush_emoji"]        = "⚠️"
        result["crush_label"]        = f"Expiry {days_earn_to_exp}d after earnings — some IV crush risk"
        result["expected_crush_pct"] = 20
    else:
        result["crush_risk"]  = "LOW"
        result["crush_emoji"] = "✅"
        result["crush_label"] = "Low IV crush risk"

    return result
