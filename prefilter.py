"""
Pre-filter for raw order flow before scoring.
Used when FLOW_SOURCE=bullflow to filter high-conviction trades only.
Rejects ~90% of raw flow — only survivors go to Claude scorer.

Hard filters — auto-reject if ANY fail:
  - Premium >= MIN_PREMIUM (default $150K)
  - OI >= MIN_OI (default 500)
  - DTE between MIN_DTE and MAX_DTE (default 7-90)
  - OTM <= MAX_OTM (default 20%)
  - Not already up >50% from flow price
  - Not before 10AM or after 3:30PM ET (with exceptions)
  - Not on earnings day

Conviction boosters — raise score:
  - FULL_ASK fill
  - Vol/OI > 5x
  - Sweep across exchanges
  - Premium > $500K
  - DTE 14-45 (sweet spot)
"""
import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

# ── Configurable thresholds ────────────────────────────────────────────
def cfg(key: str, default):
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return type(default)(val)
    except:
        return default

MIN_PREMIUM   = cfg("FILTER_MIN_PREMIUM",   150_000)
MIN_OI        = cfg("FILTER_MIN_OI",        500)
MIN_DTE       = cfg("FILTER_MIN_DTE",       7)
MAX_DTE       = cfg("FILTER_MAX_DTE",       90)
MAX_OTM_PCT   = cfg("FILTER_MAX_OTM",       20.0)
MAX_CHASING   = cfg("FILTER_MAX_CHASING",   50.0)  # option already up this %

def get_now_et():
    return datetime.now(ZoneInfo("America/New_York"))

# ── Hard filters ───────────────────────────────────────────────────────

def check_premium(data: dict) -> tuple:
    premium = float(data.get("premium") or data.get("total_premium") or 0)
    if premium < MIN_PREMIUM:
        return False, f"Premium ${premium:,.0f} < minimum ${MIN_PREMIUM:,.0f}"
    return True, f"Premium ${premium:,.0f} ✅"

def check_oi(data: dict) -> tuple:
    oi = int(data.get("open_interest") or data.get("oi") or 0)
    if oi > 0 and oi < MIN_OI:
        return False, f"OI {oi} < minimum {MIN_OI}"
    return True, f"OI {oi} ✅" if oi > 0 else "OI unknown — passing"

def check_dte(data: dict) -> tuple:
    dte = data.get("dte") or data.get("days_to_expiry")
    if dte is None:
        return True, "DTE unknown — passing"
    dte = int(dte)
    if dte < MIN_DTE:
        return False, f"DTE {dte} < minimum {MIN_DTE} (too short)"
    if dte > MAX_DTE:
        return False, f"DTE {dte} > maximum {MAX_DTE} (too long)"
    return True, f"DTE {dte} ✅"

def check_otm(data: dict) -> tuple:
    otm = data.get("otm_percentage") or data.get("otm_pct")
    if otm is None:
        return True, "OTM unknown — passing"
    otm = float(otm)
    if otm > MAX_OTM_PCT:
        return False, f"OTM {otm:.1f}% > maximum {MAX_OTM_PCT}% (too far OTM)"
    return True, f"OTM {otm:.1f}% ✅"

def check_chasing(data: dict) -> tuple:
    """Option already moved significantly from flow price — chasing."""
    fill_price = float(data.get("fill_price") or data.get("price") or 0)
    ask_now    = float(data.get("ask_now") or data.get("current_ask") or 0)
    if fill_price > 0 and ask_now > 0:
        pct_move = ((ask_now - fill_price) / fill_price) * 100
        if pct_move > MAX_CHASING:
            return False, f"Already up {pct_move:.0f}% from flow — chasing"
    return True, "Not chasing ✅"

def check_timing(data: dict) -> tuple:
    """Reject pre-10AM and post-3:30PM (with exceptions)."""
    now_et = get_now_et()
    t      = now_et.time()
    dte    = int(data.get("dte") or data.get("days_to_expiry") or 30)

    # Before 10:00 AM — no entries
    if t < dtime(10, 0):
        return False, f"Before 10:00 AM ET ({t.strftime('%I:%M%p')}) — no entries"

    # After 3:30 PM — only allow if DTE < 30 (late day stealth signal)
    if t > dtime(15, 30) and dte >= 30:
        return False, f"After 3:30 PM ET — only DTE<30 allowed"

    return True, f"Timing OK ({t.strftime('%I:%M%p')} ET) ✅"

def check_earnings(data: dict) -> tuple:
    """Skip if earnings within 2 days."""
    earn_days = data.get("days_to_earnings") or data.get("earnings_days")
    if earn_days is not None and int(earn_days) <= 2:
        return False, f"Earnings in {earn_days} days — skip"
    return True, "No imminent earnings ✅"

def check_fill_type(data: dict) -> tuple:
    """Prefer FULL_ASK or ABOVE_ASK fills."""
    fill = (data.get("fill_type") or data.get("aggressor") or "").upper()
    if fill in ("BELOW_BID", "AT_BID"):
        return False, f"Fill type {fill} — not aggressive"
    return True, f"Fill type: {fill or 'unknown'} ✅"

# ── Conviction score ───────────────────────────────────────────────────

def conviction_score(data: dict) -> tuple:
    """
    Score the flow on conviction (0-10).
    Returns (score, reasons list).
    """
    score   = 0
    reasons = []

    premium = float(data.get("premium") or 0)
    vol_oi  = float(data.get("vol_oi_ratio") or data.get("volume_oi") or 0)
    fill    = (data.get("fill_type") or "").upper()
    dte     = int(data.get("dte") or 30)
    is_sweep= bool(data.get("is_sweep") or data.get("sweep"))

    # Premium size
    if premium >= 1_000_000:
        score += 3; reasons.append("$1M+ premium 🔥")
    elif premium >= 500_000:
        score += 2; reasons.append("$500K+ premium")
    elif premium >= 250_000:
        score += 1; reasons.append("$250K+ premium")

    # Fill aggression
    if fill in ("FULL_ASK", "ABOVE_ASK"):
        score += 2; reasons.append("FULL_ASK fill")
    elif fill == "AT_ASK":
        score += 1; reasons.append("AT_ASK fill")

    # Vol/OI ratio
    if vol_oi >= 10:
        score += 2; reasons.append(f"Vol/OI {vol_oi:.0f}x 🚨")
    elif vol_oi >= 5:
        score += 1; reasons.append(f"Vol/OI {vol_oi:.0f}x")

    # Sweep
    if is_sweep:
        score += 1; reasons.append("Sweep ⚡")

    # DTE sweet spot
    if 14 <= dte <= 45:
        score += 1; reasons.append(f"DTE {dte} (sweet spot)")

    return score, reasons

# ── Master pre-filter ──────────────────────────────────────────────────

def prefilter(data: dict) -> dict:
    """
    Run all hard filters and conviction scoring on raw flow data.

    Returns:
      {
        "pass": True/False,
        "reason": "why it passed or failed",
        "conviction": 0-10,
        "conviction_reasons": [...],
        "filters": {filter_name: (pass, reason), ...}
      }
    """
    filters = {
        "premium":   check_premium(data),
        "oi":        check_oi(data),
        "dte":       check_dte(data),
        "otm":       check_otm(data),
        "chasing":   check_chasing(data),
        "timing":    check_timing(data),
        "earnings":  check_earnings(data),
        "fill_type": check_fill_type(data),
    }

    failed = [(name, reason) for name, (passed, reason) in filters.items() if not passed]

    if failed:
        fail_reasons = " | ".join(r for _, r in failed)
        print(f"[PREFILTER] REJECTED: {fail_reasons}")
        return {
            "pass":               False,
            "reason":             fail_reasons,
            "filters":            filters,
            "conviction":         0,
            "conviction_reasons": [],
        }

    conv_score, conv_reasons = conviction_score(data)
    print(f"[PREFILTER] PASSED — conviction {conv_score}/10: {', '.join(conv_reasons)}")

    return {
        "pass":               True,
        "reason":             "All filters passed",
        "filters":            filters,
        "conviction":         conv_score,
        "conviction_reasons": conv_reasons,
    }

def parse_bullflow_webhook(payload: dict) -> dict:
    """
    Parse Bullflow webhook payload into standard FlowCheck format.
    Bullflow sends different field names than @FL0WG0D tweets.
    Maps to the same structure the scorer expects.
    """
    # Bullflow field mapping (adjust if their actual payload differs)
    return {
        "ticker":          payload.get("ticker") or payload.get("symbol",""),
        "strike":          payload.get("strike") or payload.get("strike_price",""),
        "option_type":     payload.get("option_type") or payload.get("type","call"),
        "expiry":          payload.get("expiration") or payload.get("expiry",""),
        "dte":             payload.get("dte") or payload.get("days_to_expiry"),
        "premium":         payload.get("premium") or payload.get("total_premium",0),
        "fill_type":       payload.get("fill_type") or payload.get("sentiment",""),
        "open_interest":   payload.get("open_interest") or payload.get("oi",0),
        "volume":          payload.get("volume") or payload.get("vol",0),
        "vol_oi_ratio":    payload.get("vol_oi_ratio") or payload.get("volume_oi_ratio",0),
        "is_sweep":        payload.get("is_sweep") or payload.get("sweep", False),
        "fill_price":      payload.get("fill_price") or payload.get("price",0),
        "ask_now":         payload.get("ask_now") or payload.get("current_ask",0),
        "otm_percentage":  payload.get("otm_percentage") or payload.get("otm_pct",0),
        "days_to_earnings":payload.get("days_to_earnings"),
        "source":          "bullflow",
        "raw":             payload,
    }
