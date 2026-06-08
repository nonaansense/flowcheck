"""
signal_quality.py — Advanced signal quality filters for FlowCheck.

Implements:
1. Flow vs stock trend alignment (20-day SMA)
2. Relative premium vs ticker baseline
3. IV rank filter (70th percentile)
5. Strike clustering detection
"""
import os, time, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ── 1. TREND ALIGNMENT ────────────────────────────────────────────────────────

def check_trend_alignment(ticker: str, is_call: bool, price_history: list) -> dict:
    """
    Compare flow direction vs 20-day SMA.
    Bullish flow + stock below SMA = possible hedge → penalize
    Bearish flow + stock above SMA = possible protection → penalize

    price_history: list of closing prices, most recent last
    Returns: {aligned: bool, sma20: float, current: float, note: str}
    """
    if not price_history or len(price_history) < 10:
        return {"aligned": True, "note": "insufficient price history"}

    sma_period = int(os.environ.get("SIGNAL_SMA_PERIOD","20"))
    closes   = [float(p) for p in price_history[-sma_period:]]
    sma_val  = sum(closes) / len(closes)
    current  = closes[-1]
    above    = current > sma_val
    pct_diff = round((current - sma20) / sma20 * 100, 1)

    if is_call and not above:
        return {
            "aligned":  False,
            "sma20":    round(sma_val, 2),
            "current":  current,
            "pct_diff": pct_diff,
            "note":     f"⚠️ Bullish flow but stock {abs(pct_diff):.1f}% below {sma_period}d SMA ${sma_val:.2f} — possible hedge",
            "penalty":  1.5,  # score penalty
        }
    elif not is_call and above:
        return {
            "aligned":  False,
            "sma20":    round(sma_val, 2),
            "current":  current,
            "pct_diff": pct_diff,
            "note":     f"⚠️ Bearish flow but stock {pct_diff:.1f}% above {sma_period}d SMA ${sma_val:.2f} — possible protection",
            "penalty":  1.5,
        }
    else:
        direction = "above" if above else "below"
        return {
            "aligned":  True,
            "sma20":    round(sma_val, 2),
            "current":  current,
            "pct_diff": pct_diff,
            "note":     f"✅ Flow aligned — stock {direction} {sma_period}d SMA ${sma_val:.2f}",
            "penalty":  0,
        }


# ── 2. RELATIVE PREMIUM BASELINE ──────────────────────────────────────────────

BASELINE_KEY = "premium_baseline"

def update_premium_baseline(ticker: str, premium: float):
    """Record today's flow premium for ticker in rolling 30-day baseline."""
    try:
        from storage import db_get as _dg, db_set as _ds
        raw   = _dg(BASELINE_KEY) or "{}"
        data  = json.loads(raw)
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if ticker not in data:
            data[ticker] = {}
        data[ticker][today] = float(data[ticker].get(today, 0)) + premium
        # Keep only last 30 days
        cutoff = (datetime.now(ET) - timedelta(days=30)).strftime("%Y-%m-%d")
        data[ticker] = {d: v for d, v in data[ticker].items() if d >= cutoff}
        _ds(BASELINE_KEY, json.dumps(data))
    except Exception as e:
        print(f"[QUALITY] Baseline update error: {e}")


def check_relative_premium(ticker: str, premium: float) -> dict:
    """
    Compare flow premium to ticker's 30-day daily average.
    Returns multiplier and flag if unusually large.
    """
    try:
        from storage import db_get as _dg
        raw  = _dg(BASELINE_KEY) or "{}"
        data = json.loads(raw)
        hist = data.get(ticker, {})
        if len(hist) < 5:
            return {"relative": None, "note": "insufficient history (<5 days)"}

        today    = datetime.now(ET).strftime("%Y-%m-%d")
        values   = [v for d, v in hist.items() if d != today]
        if not values:
            return {"relative": None, "note": "no prior days"}

        avg_daily = sum(values) / len(values)
        multiplier = round(premium / avg_daily, 1) if avg_daily > 0 else None

        if multiplier and multiplier >= 5:
            return {
                "relative":   multiplier,
                "avg_daily":  avg_daily,
                "note":       f"🚨 {multiplier:.1f}x avg daily premium — extreme unusual activity",
                "flag":       "EXTREME",
            }
        elif multiplier and multiplier >= 3:
            return {
                "relative":   multiplier,
                "avg_daily":  avg_daily,
                "note":       f"⚡ {multiplier:.1f}x avg daily premium — significantly unusual",
                "flag":       "HIGH",
            }
        elif multiplier and multiplier >= 1.5:
            return {
                "relative":   multiplier,
                "avg_daily":  avg_daily,
                "note":       f"📊 {multiplier:.1f}x avg daily premium — above average",
                "flag":       "MODERATE",
            }
        else:
            return {
                "relative":   multiplier,
                "avg_daily":  avg_daily,
                "note":       f"📊 {multiplier:.1f}x avg daily premium — normal range for {ticker}",
                "flag":       "NORMAL",
            }
    except Exception as e:
        return {"relative": None, "note": f"error: {e}"}


# ── 3. IV RANK ────────────────────────────────────────────────────────────────

IV_HISTORY_KEY = "iv_history"

def update_iv_history(ticker: str, iv: float):
    """Store IV reading for ticker to build 52-week range."""
    if not iv or iv <= 0:
        return
    try:
        from storage import db_get as _dg, db_set as _ds
        raw  = _dg(IV_HISTORY_KEY) or "{}"
        data = json.loads(raw)
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if ticker not in data:
            data[ticker] = {}
        data[ticker][today] = float(iv)
        # Keep only last 365 days
        cutoff = (datetime.now(ET) - timedelta(days=365)).strftime("%Y-%m-%d")
        data[ticker] = {d: v for d, v in data[ticker].items() if d >= cutoff}
        _ds(IV_HISTORY_KEY, json.dumps(data))
    except Exception as e:
        print(f"[QUALITY] IV history error: {e}")


def check_iv_rank(ticker: str, current_iv: float) -> dict:
    """
    Calculate IV rank = (current - 52w low) / (52w high - 52w low).
    High IV rank = expensive options, lower conviction signal.
    """
    if not current_iv or current_iv <= 0:
        return {"iv_rank": None, "note": "IV not available"}
    try:
        from storage import db_get as _dg
        raw  = _dg(IV_HISTORY_KEY) or "{}"
        data = json.loads(raw)
        hist = data.get(ticker, {})

        if len(hist) < 20:
            # Not enough history — just flag if IV is very high in absolute terms
            if current_iv > 100:
                return {"iv_rank": None, "note": f"⚠️ IV {current_iv:.0f}% — elevated (building history)"}
            return {"iv_rank": None, "note": f"IV {current_iv:.0f}% (building history)"}

        values   = list(hist.values())
        iv_low   = min(values)
        iv_high  = max(values)
        iv_range = iv_high - iv_low

        if iv_range < 1:
            return {"iv_rank": None, "note": "IV range too narrow"}

        iv_rank = round((current_iv - iv_low) / iv_range * 100, 1)

        if iv_rank > 70:
            return {
                "iv_rank":    iv_rank,
                "iv_low":     round(iv_low, 1),
                "iv_high":    round(iv_high, 1),
                "current_iv": current_iv,
                "note":       f"🔴 IV rank {iv_rank:.0f}% — expensive (top 30% of range) — flow may be priced in",
                "flag":       "HIGH",
                "penalty":    0.5,
            }
        elif iv_rank > 50:
            return {
                "iv_rank":    iv_rank,
                "current_iv": current_iv,
                "note":       f"🟡 IV rank {iv_rank:.0f}% — moderate",
                "flag":       "MODERATE",
                "penalty":    0,
            }
        else:
            return {
                "iv_rank":    iv_rank,
                "current_iv": current_iv,
                "note":       f"✅ IV rank {iv_rank:.0f}% — cheap (bottom half of range)",
                "flag":       "LOW",
                "penalty":    0,
            }
    except Exception as e:
        return {"iv_rank": None, "note": f"error: {e}"}


# ── 5. STRIKE CLUSTERING ──────────────────────────────────────────────────────

def check_strike_clustering(ticker: str, strike: str, expiry: str,
                             flow_history: list) -> dict:
    """
    Check if today's flow clusters on one strike (high conviction)
    or scatters across many strikes (low conviction / hedging).
    """
    today   = datetime.now(ET).strftime("%Y-%m-%d")
    cutoff  = datetime.now(ET) - timedelta(hours=24)

    # Find today's flows for this ticker
    today_flows = []
    for h in flow_history:
        ts_raw = h.get("timestamp","") or h.get("time","")
        try:
            ts = datetime.fromisoformat(str(ts_raw)).replace(tzinfo=ET) if "T" in str(ts_raw) else None
            if ts and ts >= cutoff and h.get("ticker","").upper() == ticker.upper():
                today_flows.append(h)
        except: pass

    if len(today_flows) < 2:
        return {"clustering": None, "note": "first flow today"}

    # Count unique strikes
    strikes_seen = set()
    for f in today_flows:
        s = str(f.get("strike","") or "")
        e = str(f.get("expiry","") or "")
        if s:
            strikes_seen.add(f"{s}_{e}")

    # Include current strike
    strikes_seen.add(f"{strike}_{expiry}")
    unique_strikes = len(strikes_seen)
    this_strike_count = sum(
        1 for f in today_flows
        if str(f.get("strike","")) == str(strike)
        and str(f.get("expiry","")) == str(expiry)
    ) + 1  # +1 for current

    if unique_strikes == 1 or (this_strike_count >= 2 and unique_strikes <= 2):
        return {
            "clustering":   "CONCENTRATED",
            "count":        this_strike_count,
            "unique":       unique_strikes,
            "note":         f"✅ Strike clustering: {this_strike_count}x on same strike — concentrated conviction",
            "bonus":        0.3,
        }
    elif unique_strikes >= 4:
        return {
            "clustering":   "SCATTERED",
            "count":        this_strike_count,
            "unique":       unique_strikes,
            "note":         f"⚠️ Flow scattered across {unique_strikes} different strikes — lower conviction",
            "penalty":      0.5,
        }
    else:
        return {
            "clustering":   "MIXED",
            "unique":       unique_strikes,
            "note":         f"📊 {unique_strikes} strikes active today",
            "bonus":        0,
        }


# ── COMBINED QUALITY SCORE ────────────────────────────────────────────────────

def run_quality_checks(ticker: str, trade: dict, result: dict,
                        price_history: list, flow_history: list) -> dict:
    """
    Run all quality checks and return combined assessment.
    Returns dict with individual results and net score adjustment.
    """
    is_call  = "put" not in (trade.get("option_type","call") or "call").lower()
    premium  = float(trade.get("premium",0) or 0)
    current_iv = float(trade.get("iv",0) or trade.get("implied_volatility",0) or 0)
    strike   = str(trade.get("strike",""))
    expiry   = str(trade.get("expiry",""))

    # Run checks
    trend    = check_trend_alignment(ticker, is_call, price_history)
    relative = check_relative_premium(ticker, premium)
    iv_rank  = check_iv_rank(ticker, current_iv)
    cluster  = check_strike_clustering(ticker, strike, expiry, flow_history)

    # Update baseline with this flow
    if premium > 0:
        update_premium_baseline(ticker, premium)
    if current_iv > 0:
        update_iv_history(ticker, current_iv)

    # Net score adjustment
    net_adj = 0
    net_adj -= trend.get("penalty",   0)
    net_adj -= iv_rank.get("penalty", 0)
    net_adj -= cluster.get("penalty", 0)
    net_adj += cluster.get("bonus",   0)

    # Overall quality label
    flags = []
    if not trend.get("aligned", True):
        flags.append("TREND_MISMATCH")
    if iv_rank.get("flag") == "HIGH":
        flags.append("HIGH_IV")
    if cluster.get("clustering") == "SCATTERED":
        flags.append("SCATTERED_FLOW")
    if relative.get("flag") in ("EXTREME","HIGH"):
        flags.append(f"UNUSUAL_{relative['flag']}")

    quality = "HIGH" if not flags else ("MODERATE" if len(flags) == 1 else "LOW")

    return {
        "quality":  quality,
        "flags":    flags,
        "net_adj":  round(net_adj, 2),
        "trend":    trend,
        "relative": relative,
        "iv_rank":  iv_rank,
        "cluster":  cluster,
    }
