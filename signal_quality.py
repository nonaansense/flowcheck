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
    Returns: {aligned: bool, sma_val: float, current: float, note: str}
    """
    if not price_history or len(price_history) < 10:
        return {"aligned": True, "note": "insufficient price history"}

    sma_period = int(os.environ.get("SIGNAL_SMA_PERIOD","20"))
    closes   = [float(p) for p in price_history[-sma_period:]]
    sma_val  = sum(closes) / len(closes)
    current  = closes[-1]
    above    = current > sma_val
    pct_diff = round((current - sma_val) / sma_val * 100, 1)

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


def check_sell_the_news_risk(ticker: str, days_to_earnings: int | None,
                              earnings_is_past: bool, iv_rank: float | None,
                              days_to_macro: int | None = None,
                              macro_event: str | None = None) -> dict:
    """
    Estimates whether a trade setup looks like a "buy the rumor, sell the
    news" pattern — i.e. a nearby catalyst plus elevated IV suggests the
    move may already be priced in.

    Returns: {risk: "HIGH"/"MODERATE"/"LOW"/"UNKNOWN", note: str}
    """
    if days_to_earnings is None and iv_rank is None and days_to_macro is None:
        return {"risk": "UNKNOWN", "note": "Insufficient data for sell-the-news check"}

    earnings_near      = (days_to_earnings is not None and not earnings_is_past
                           and 0 <= days_to_earnings <= 5)
    earnings_very_near  = (days_to_earnings is not None and not earnings_is_past
                            and 0 <= days_to_earnings <= 2)
    earnings_far_or_none = (days_to_earnings is None or earnings_is_past
                             or days_to_earnings > 5)

    iv_elevated      = iv_rank is not None and iv_rank >= 60
    iv_very_elevated = iv_rank is not None and iv_rank >= 75
    iv_not_elevated  = iv_rank is None or iv_rank < 60

    macro_near      = days_to_macro is not None and 0 <= days_to_macro <= 2
    macro_very_near = days_to_macro is not None and days_to_macro == 0
    macro_far_or_none = days_to_macro is None or days_to_macro > 2

    # ── HIGH risk ──────────────────────────────────────────────────────
    if macro_very_near:
        return {"risk": "HIGH",
                "note": f"⚠️ {macro_event} today — high reversal risk if priced in"}
    if earnings_very_near and iv_elevated:
        return {"risk": "HIGH",
                "note": f"⚠️ Earnings in {days_to_earnings}d + IV rank "
                        f"{iv_rank:.0f}th — likely priced in, sell-the-news risk"}
    if earnings_near and iv_very_elevated:
        return {"risk": "HIGH",
                "note": f"⚠️ Earnings in {days_to_earnings}d + IV rank "
                        f"{iv_rank:.0f}th (very elevated) — sell-the-news risk"}

    # ── MODERATE risk ─────────────────────────────────────────────────
    if earnings_near or macro_near or iv_very_elevated:
        parts = []
        if earnings_near:
            parts.append(f"earnings in {days_to_earnings}d")
        if macro_near:
            parts.append(f"{macro_event} in {days_to_macro}d")
        if iv_very_elevated and not earnings_near:
            parts.append(f"IV rank {iv_rank:.0f}th (elevated)")
        return {"risk": "MODERATE",
                "note": f"📋 {' + '.join(parts)} — some sell-the-news risk, "
                        f"consider tighter exit"}

    # ── LOW risk ──────────────────────────────────────────────────────
    if earnings_far_or_none and iv_not_elevated and macro_far_or_none:
        return {"risk": "LOW",
                "note": "✅ No nearby catalyst, IV not elevated — room to run"}

    # ── Fallback ──────────────────────────────────────────────────────
    return {"risk": "MODERATE", "note": "📋 Mixed signals — monitor for reversal"}



def check_recent_ipo_risk(ticker: str, price_history: list = None,
                           ipo_days_ago: int | None = None,
                           threshold_days: int = 60) -> dict:
    """
    Flags tickers that recently IPO'd. Technical and flow-confirmation
    signals are inherently less reliable here: thin public float, no
    earnings history yet, and mechanical supply dynamics (lockup
    expirations, insider unlocks) can override normal price action
    regardless of how clean the flow or technicals look.

    Uses the explicit IPO date when available (from Finnhub's IPO
    calendar), falling back to price-history length as a proxy when the
    date isn't found — short history is itself a tell.

    Returns: {is_recent_ipo: bool, days_since_ipo: int|None,
              trading_days: int, note: str|None}
    """
    trading_days = len(price_history) if price_history else 0

    if ipo_days_ago is not None:
        if ipo_days_ago <= threshold_days:
            # Calculate standard lockup expiry (180 calendar days after IPO)
            lockup_days_remaining = max(0, 180 - ipo_days_ago)
            if lockup_days_remaining > 0:
                lockup_note = (f" Lockup expires in ~{lockup_days_remaining}d "
                               f"— float expansion risk.")
            else:
                lockup_note = " Lockup may have expired — check for insider selling."
            return {
                "is_recent_ipo":         True,
                "days_since_ipo":        ipo_days_ago,
                "trading_days":          trading_days,
                "lockup_days_remaining": lockup_days_remaining,
                "note": (f"🆕 IPO'd {ipo_days_ago}d ago — thin float, no "
                         f"earnings history yet.{lockup_note}"
                         f" Technical/flow signals less reliable."),
            }
        return {
            "is_recent_ipo": False,
            "days_since_ipo": ipo_days_ago,
            "trading_days": trading_days,
            "note": None,
        }

    # Fallback: short price history is itself a signal of a recent listing
    # (~20 trading days/month, so 40 days ≈ 2 months of history)
    if 0 < trading_days < 40:
        return {
            "is_recent_ipo":         True,
            "days_since_ipo":        None,
            "trading_days":          trading_days,
            "lockup_days_remaining": None,
            "note": (f"🆕 Only {trading_days}d of price history — likely recent IPO. "
                     f"Standard lockup is 180d from listing. "
                     f"Technical/flow signals less reliable until more history builds."),
        }

    return {
        "is_recent_ipo": False,
        "days_since_ipo": None,
        "trading_days": trading_days,
        "note": None,
    }
