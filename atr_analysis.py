"""
atr_analysis.py — ATR-based move probability for FlowCheck.

For each flow alert:
- Calculate 14-day ATR from price history
- Estimate expected max move within DTE
- Flag if strike is likely unreachable
"""
import os, math
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def calc_atr(price_history: list, period: int = 14) -> float | None:
    """
    Simplified ATR using close-to-close absolute changes.
    Returns average daily move in dollars.
    """
    if len(price_history) < period + 1:
        return None
    closes = [float(p) for p in price_history[-(period+1):]]
    daily_moves = [abs(closes[i] - closes[i-1]) for i in range(1, len(closes))]
    return round(sum(daily_moves) / len(daily_moves), 2)


def calc_move_probability(
    current_price: float,
    strike: float,
    is_call: bool,
    dte: int,
    atr: float,
) -> dict:
    """
    Estimate if strike is reachable within DTE.

    Uses random walk approximation:
    Expected max move over N days ≈ ATR × √N × 1.5 (1.5 = confidence buffer)
    """
    if not atr or atr <= 0 or not current_price or not strike:
        return {"reachable": None, "note": "insufficient data"}

    # Distance to strike
    if is_call:
        distance = max(0, strike - current_price)
    else:
        distance = max(0, current_price - strike)

    # Already ITM
    if distance <= 0:
        pct_itm = abs((current_price - strike) / strike * 100)
        return {
            "reachable": True,
            "distance":  0,
            "atr":       atr,
            "note":      f"✅ Already {'ITM' if pct_itm > 0 else 'ATM'} — strike cleared",
            "pct_move":  0,
        }

    pct_move    = round(distance / current_price * 100, 1)
    atr_days    = round(distance / atr, 1)          # days to reach at 1 ATR/day
    expected_max = atr * math.sqrt(dte) * 1.5       # statistical max move

    reachable   = expected_max >= distance
    atr_pct     = round(atr / current_price * 100, 2)

    if reachable:
        if atr_days <= dte * 0.3:
            note = (f"✅ Easily reachable — needs {pct_move:.1f}% move, "
                    f"ATR ${atr:.2f}/day ({atr_pct:.1f}%), est. {atr_days:.1f} ATR-days vs {dte}d DTE")
        else:
            note = (f"⚠️ Reachable but aggressive — needs {pct_move:.1f}% move "
                    f"({atr_days:.1f} ATR-days vs {dte}d DTE)")
    else:
        note = (f"🚨 Strike likely UNREACHABLE — needs {pct_move:.1f}% move "
                f"but expected max {round(expected_max/current_price*100,1):.1f}% in {dte}d "
                f"(ATR ${atr:.2f}/day)")

    return {
        "reachable":    reachable,
        "distance":     round(distance, 2),
        "pct_move":     pct_move,
        "atr":          atr,
        "atr_pct":      atr_pct,
        "atr_days":     atr_days,
        "expected_max": round(expected_max, 2),
        "dte":          dte,
        "note":         note,
    }


def format_atr_line(analysis: dict) -> str:
    """One-line summary for alert."""
    if not analysis or analysis.get("reachable") is None:
        return ""
    return analysis.get("note", "")
