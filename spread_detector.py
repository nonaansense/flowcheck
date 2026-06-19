"""
spread_detector.py — Detect if flow is likely a spread leg vs naked directional.

Heuristics:
1. Vol/OI very high on both a call AND put at same expiry (straddle/strangle)
2. Opposing flow on same expiry same day (one side of spread)
3. Bullflow 'side' field showing MID fill (spread legs often fill at mid)
4. Premium much larger than typical single-leg size for this ticker
5. Multiple strikes hit same expiry rapidly (spread legging in)
"""
import time, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def check_spread_likelihood(
    ticker: str,
    strike: str,
    expiry: str,
    option_type: str,  # "call" or "put"
    fill_type: str,    # FULL_ASK / MOSTLY_ASK / MID / UNKNOWN
    premium: float,
    flow_history: list,
) -> dict:
    """
    Assess likelihood that this flow is a spread leg rather than naked directional.
    Returns: {spread_likely: bool, confidence: str, reasons: list, note: str}
    """
    reasons   = []
    score     = 0  # higher = more likely a spread
    is_call   = "put" not in (option_type or "call").lower()
    today_cut = time.time() - 86400  # last 24h

    # ── Heuristic 1: MID fill ──────────────────────────────────────────
    if fill_type in ("MID", "MOSTLY_MID"):
        score  += 2
        reasons.append("fill at mid (spread legs often fill at mid)")

    # ── Heuristic 2: Opposing flow on same expiry today ────────────────
    opposing_type = "put" if is_call else "call"
    opposing_flows = [
        h for h in flow_history
        if h.get("ticker","").upper() == ticker.upper()
        and str(h.get("expiry","")) == str(expiry)
        and (opposing_type in (h.get("option_type","") or "").lower())
        and float(h.get("timestamp_unix", h.get("time", 0)) or 0) > today_cut
    ]
    if opposing_flows:
        opp_prem = sum(float(h.get("premium",0) or 0) for h in opposing_flows)
        ratio    = min(premium, opp_prem) / max(premium, opp_prem) if max(premium, opp_prem) > 0 else 0
        if ratio > 0.5:  # opposing side is similar size
            score  += 3
            reasons.append(f"opposing {opposing_type} flow ${opp_prem/1000:.0f}K on same expiry ({ratio:.0%} of this premium)")

    # ── Heuristic 3: Multiple strikes same expiry today ────────────────
    same_expiry_flows = [
        h for h in flow_history
        if h.get("ticker","").upper() == ticker.upper()
        and str(h.get("expiry","")) == str(expiry)
        and (is_call == ("put" not in (h.get("option_type","call") or "call").lower()))
        and str(h.get("strike","")) != str(strike)
        and float(h.get("timestamp_unix", h.get("time", 0)) or 0) > today_cut
    ]
    if len(same_expiry_flows) >= 2:
        score  += 2
        strikes_seen = list(set(str(h.get("strike","")) for h in same_expiry_flows))
        reasons.append(f"{len(strikes_seen)} other {option_type} strikes on same expiry today — possible spread legging")

    # ── Heuristic 4: Very large relative to typical single-leg ────────
    # Over $5M on a single expiry often involves spreads (pure naked this size is rare)
    if premium >= 5_000_000:
        score  += 1
        reasons.append(f"${premium/1_000_000:.1f}M size — common for institutional spread")

    # ── Determine likelihood ───────────────────────────────────────────
    if score >= 5:
        spread_likely = True
        confidence    = "HIGH"
        note = f"⚠️ Likely SPREAD LEG — {'; '.join(reasons[:2])}"
    elif score >= 3:
        spread_likely = True
        confidence    = "MODERATE"
        note = f"⚠️ Possible spread leg — {reasons[0] if reasons else 'MID fill'}"
    elif score >= 1:
        spread_likely = False
        confidence    = "LOW"
        note = f"📊 Mostly directional — minor spread signals ({reasons[0] if reasons else ''})"
    else:
        spread_likely = False
        confidence    = "NONE"
        note = "✅ Directional — no spread indicators"

    return {
        "spread_likely": spread_likely,
        "confidence":    confidence,
        "score":         score,
        "reasons":       reasons,
        "note":          note,
    }


# ── Straddle / Strangle detector ──────────────────────────────────────────
# Tracks same-ticker call+put flows within a rolling window. When both sides
# hit with similar premium and matching/nearby expiry, flags as a possible
# volatility bet rather than a directional conviction signal.

import os, time
from zoneinfo import ZoneInfo
from datetime import datetime

ET = ZoneInfo("America/New_York")
STRADDLE_STORAGE_KEY   = "straddle_history"
STRADDLE_WINDOW_HOURS  = float(os.environ.get("STRADDLE_WINDOW_HOURS", "2"))
STRADDLE_SKEW_MAX      = float(os.environ.get("STRADDLE_SKEW_MAX", "0.4"))  # max premium ratio imbalance

_STRADDLE: dict = {}
_st_loaded = False


def _load_straddle():
    global _STRADDLE, _st_loaded
    if _st_loaded:
        return
    try:
        from storage import db_get
        raw = db_get(STRADDLE_STORAGE_KEY)
        if raw and isinstance(raw, dict):
            _STRADDLE = raw
    except: pass
    _st_loaded = True


def _save_straddle():
    try:
        from storage import db_set
        db_set(STRADDLE_STORAGE_KEY, _STRADDLE)
    except: pass


def _prune_straddle():
    cutoff = time.time() - STRADDLE_WINDOW_HOURS * 3600
    for k in list(_STRADDLE.keys()):
        _STRADDLE[k]["calls"] = [f for f in _STRADDLE[k]["calls"] if f["ts"] >= cutoff]
        _STRADDLE[k]["puts"]  = [f for f in _STRADDLE[k]["puts"]  if f["ts"] >= cutoff]
        if not _STRADDLE[k]["calls"] and not _STRADDLE[k]["puts"]:
            del _STRADDLE[k]


def process_straddle(alert: dict, alert_name: str) -> dict | None:
    """
    Track call and put flows per ticker. Returns a straddle-detection result
    when both sides show up with similar premium on matching/nearby expiry.
    Only fires once per ticker per window.
    """
    _load_straddle()
    _prune_straddle()

    ticker    = str(alert.get("ticker","") or "").upper()
    option_type = str(alert.get("option_type","call") or "call").lower()
    expiry    = str(alert.get("expiry","") or "")
    premium   = float(alert.get("premium",0) or 0)
    now       = time.time()
    time_str  = datetime.now(ET).strftime("%-I:%M %p")

    if not ticker or premium < 25000:
        return None

    if ticker not in _STRADDLE:
        _STRADDLE[ticker] = {"calls":[], "puts":[], "alerted_ts": 0}

    entry = _STRADDLE[ticker]
    fill = {"expiry": expiry, "premium": premium, "time": time_str, "ts": now, "alert": alert_name}

    if "call" in option_type:
        entry["calls"].append(fill)
    else:
        entry["puts"].append(fill)
    _save_straddle()

    calls = entry["calls"]
    puts  = entry["puts"]
    if not calls or not puts:
        return None

    # Already alerted this window
    if entry.get("alerted_ts",0) > now - STRADDLE_WINDOW_HOURS * 3600:
        return None

    total_calls = sum(f["premium"] for f in calls)
    total_puts  = sum(f["premium"] for f in puts)
    total       = total_calls + total_puts
    if total == 0: return None

    skew = abs(total_calls - total_puts) / total
    # Only flag if relatively balanced (both sides within STRADDLE_SKEW_MAX of each other)
    if skew > STRADDLE_SKEW_MAX:
        return None

    # Check for matching expiry between any call and put
    call_expiries = {f["expiry"] for f in calls}
    put_expiries  = {f["expiry"] for f in puts}
    same_expiry   = bool(call_expiries & put_expiries)

    entry["alerted_ts"] = now
    _save_straddle()

    call_pct = total_calls / total * 100
    put_pct  = total_puts  / total * 100

    print(f"[STRADDLE] ⚖️  Possible straddle/strangle: {ticker} "
          f"${total/1000:.0f}K | calls {call_pct:.0f}% / puts {put_pct:.0f}%")

    return {
        "ticker":      ticker,
        "total_calls": total_calls,
        "total_puts":  total_puts,
        "total":       total,
        "call_pct":    call_pct,
        "put_pct":     put_pct,
        "same_expiry": same_expiry,
        "call_fills":  list(calls),
        "put_fills":   list(puts),
    }


def build_straddle_alert(result: dict) -> str:
    """Alert message for a detected straddle/strangle pattern."""
    ticker = result["ticker"]
    total  = result["total"]
    cp     = result["call_pct"]
    pp     = result["put_pct"]
    same   = result["same_expiry"]
    tot_s  = f"${total/1_000_000:.1f}M" if total >= 1_000_000 else f"${total/1_000:.0f}K"

    call_lines = [f"  📈 {f['time']} | ${f['premium']/1_000:.0f}K {f['expiry']} [{f['alert']}]"
                  for f in result["call_fills"]]
    put_lines  = [f"  📉 {f['time']} | ${f['premium']/1_000:.0f}K {f['expiry']} [{f['alert']}]"
                  for f in result["put_fills"]]

    kind = "STRADDLE" if same else "STRANGLE"
    lines = [
        f"⚖️  POSSIBLE {kind}: ${ticker}",
        f"━━━ Both calls AND puts buying — volatility bet? ━━━",
        f"",
        f"Total deployed: {tot_s} | Calls {cp:.0f}% / Puts {pp:.0f}%",
        f"",
        f"Calls:",
    ] + call_lines + ["", "Puts:"] + put_lines + [
        f"",
        f"💡 {'Same expiry = classic straddle — bet on a big move, not direction' if same else 'Different expiries = strangle — event volatility play'}",
        f"⚠️  Do NOT treat as directional conviction — this is a volatility bet",
        f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
    ]
    return "\n".join(lines)
