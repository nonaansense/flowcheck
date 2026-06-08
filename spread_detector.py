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
