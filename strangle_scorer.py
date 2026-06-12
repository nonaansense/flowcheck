"""
strangle_scorer.py — Strangle opportunity scorer for FlowCheck.

For every flow alert, evaluates whether buying a strangle makes sense by checking:
1. Historical quarterly range vs strangle cost (expected value)
2. IV rank — is premium cheap or expensive?
3. Bid/ask spread quality — is the market liquid?
4. Best expiry to target (60-90 DTE sweet spot)

Returns a STRONG / MODERATE / PASS verdict with full math shown in alert.
"""
import os, math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _get_options_chain(ticker: str, expiry_str: str) -> list:
    """Fetch options chain from Tradier for a given expiry (YYYY-MM-DD)."""
    token = os.environ.get("TRADIER_TOKEN", "")
    if not token:
        return []
    try:
        import requests
        r = requests.get(
            "https://api.tradier.com/v1/markets/options/chains",
            params={"symbol": ticker.upper(), "expiration": expiry_str, "greeks": "true"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json().get("options", {}).get("option", []) or []
    except: pass
    return []


def _get_expirations(ticker: str) -> list:
    """Fetch available expiry dates from Tradier."""
    token = os.environ.get("TRADIER_TOKEN", "")
    if not token:
        return []
    try:
        import requests
        r = requests.get(
            "https://api.tradier.com/v1/markets/options/expirations",
            params={"symbol": ticker.upper(), "includeAllRoots": "true"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=8,
        )
        if r.status_code == 200:
            dates = r.json().get("expirations", {}).get("date", []) or []
            return dates if isinstance(dates, list) else [dates]
    except: pass
    return []


def _best_expiry(expirations: list, target_dte: int = 75) -> tuple[str, int]:
    """Pick expiry closest to target DTE."""
    today = datetime.now(ET).date()
    best_exp, best_dte, best_diff = None, 0, 9999
    for exp_str in expirations:
        try:
            exp_dt = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte    = (exp_dt - today).days
            if dte < 30:
                continue  # too short
            diff = abs(dte - target_dte)
            if diff < best_diff:
                best_exp, best_dte, best_diff = exp_str, dte, diff
        except: pass
    return best_exp or "", best_dte


def _find_atm_legs(chain: list, current_price: float) -> tuple[dict, dict]:
    """Find ATM call and put closest to current price."""
    calls = sorted(
        [o for o in chain if o.get("option_type","").lower() == "call" and o.get("ask",0) > 0],
        key=lambda o: abs(float(o.get("strike", 9999)) - current_price)
    )
    puts = sorted(
        [o for o in chain if o.get("option_type","").lower() == "put" and o.get("ask",0) > 0],
        key=lambda o: abs(float(o.get("strike", 9999)) - current_price)
    )
    return (calls[0] if calls else {}), (puts[0] if puts else {})


def _historical_quarterly_range(price_history: list) -> float:
    """
    Calculate average quarterly price range as % of starting price.
    Uses last 4 quarters from price history.
    Returns average range % (e.g. 22.4 means ±22.4% average quarterly move).
    """
    if len(price_history) < 60:
        return 0.0
    closes = [float(p) for p in price_history]
    quarter = 63  # ~63 trading days per quarter
    ranges = []
    # Split into quarterly windows
    for i in range(0, min(len(closes) - quarter, quarter * 4), quarter):
        window = closes[i:i + quarter]
        if len(window) < 30:
            continue
        hi  = max(window)
        lo  = min(window)
        rng = (hi - lo) / window[0] * 100
        ranges.append(rng)
    return round(sum(ranges) / len(ranges), 1) if ranges else 0.0


def _spread_quality(option: dict) -> tuple[float, str]:
    """Calculate spread as % of mid. Returns (pct, label)."""
    bid = float(option.get("bid", 0) or 0)
    ask = float(option.get("ask", 0) or 0)
    if bid <= 0 or ask <= 0:
        return 0, "unknown"
    mid    = (bid + ask) / 2
    spread = (ask - bid) / mid * 100
    if spread > 30:   return spread, "WIDE ⚠️"
    elif spread > 15: return spread, "MODERATE"
    else:             return spread, "TIGHT ✅"


def score_strangle(
    ticker:        str,
    current_price: float,
    price_history: list,
    iv_rank:       float = None,
) -> dict:
    """
    Score a strangle opportunity for the given ticker.

    Returns dict with verdict, math, and formatted alert line.
    """
    if not current_price or current_price <= 0:
        return {"verdict": "SKIP", "reason": "no price data"}

    # ── Step 1: Find best expiry ─────────────────────────────────────
    expirations = _get_expirations(ticker)
    if not expirations:
        return {"verdict": "SKIP", "reason": "no expiry data"}

    exp_str, dte = _best_expiry(expirations, target_dte=75)
    if not exp_str:
        return {"verdict": "SKIP", "reason": "no suitable expiry"}

    # ── Step 2: Fetch ATM legs ────────────────────────────────────────
    chain = _get_options_chain(ticker, exp_str)
    if not chain:
        return {"verdict": "SKIP", "reason": "no chain data"}

    call, put = _find_atm_legs(chain, current_price)
    if not call or not put:
        return {"verdict": "SKIP", "reason": "no ATM options found"}

    call_strike = float(call.get("strike", 0))
    put_strike  = float(put.get("strike", 0))
    call_mid    = round((float(call.get("bid",0)) + float(call.get("ask",0))) / 2, 2)
    put_mid     = round((float(put.get("bid",0)) + float(put.get("ask",0))) / 2, 2)
    total_cost  = round(call_mid + put_mid, 2)

    if total_cost <= 0:
        return {"verdict": "SKIP", "reason": "zero cost"}

    # ── Step 3: Required move % ───────────────────────────────────────
    req_move_pct = round(total_cost / current_price * 100, 1)
    upper_be     = round(call_strike + total_cost, 2)
    lower_be     = round(put_strike  - total_cost, 2)

    # ── Step 4: Historical range ──────────────────────────────────────
    hist_range = _historical_quarterly_range(price_history)

    # Scale to DTE (quarterly = 63 days, adjust for actual DTE)
    if hist_range > 0 and dte > 0:
        scaled_range = round(hist_range * math.sqrt(dte / 63), 1)
    else:
        scaled_range = 0.0

    # ── Step 5: Spread quality ────────────────────────────────────────
    call_spread_pct, call_spread_lbl = _spread_quality(call)
    put_spread_pct,  put_spread_lbl  = _spread_quality(put)
    avg_spread = round((call_spread_pct + put_spread_pct) / 2, 1)
    spread_ok  = avg_spread < 25

    # ── Step 6: Verdict ───────────────────────────────────────────────
    ratio = round(scaled_range / req_move_pct, 2) if req_move_pct > 0 else 0

    if ratio >= 2.0:      base = "STRONG"
    elif ratio >= 1.5:    base = "MODERATE"
    else:                 base = "PASS"

    # IV rank adjustments
    iv_note = ""
    if iv_rank and iv_rank > 60:
        if base == "STRONG":   base = "MODERATE"
        elif base == "MODERATE": base = "PASS"
        iv_note = f" (IV rank {iv_rank:.0f}% — expensive)"
    elif iv_rank and iv_rank < 30:
        iv_note = f" (IV rank {iv_rank:.0f}% — cheap ✅)"

    # Spread penalty
    spread_note = ""
    if not spread_ok:
        spread_note = f" ⚠️ wide spread {avg_spread:.0f}%"
        if base == "STRONG": base = "MODERATE"

    # Verdict emoji
    verdict_emoji = {"STRONG": "🔥", "MODERATE": "✅", "PASS": "❌"}.get(base, "")

    # ── Format output ─────────────────────────────────────────────────
    exp_display = datetime.strptime(exp_str, "%Y-%m-%d").strftime("%m/%d/%y")

    lines = [
        f"━━━ STRANGLE SETUP ━━━",
        f"{verdict_emoji} {base}{iv_note}{spread_note}",
        f"💰 Cost: ${total_cost:.2f} (${call_mid:.2f}C + ${put_mid:.2f}P) × {exp_display} ({dte}d)",
        f"📏 Required move: ±{req_move_pct:.1f}%",
        f"📊 Hist range {dte}d: ~{scaled_range:.1f}% ({ratio:.1f}x required)" if scaled_range else "",
        f"🎯 Buy {call_strike:.0f}C / {put_strike:.0f}P",
        f"   Breakevens: ${lower_be:.2f} ↕ ${upper_be:.2f}",
    ]
    if iv_rank:
        iv_bar = "▓" * int(iv_rank / 10) + "░" * (10 - int(iv_rank / 10))
        lines.append(f"📉 IV rank: {iv_rank:.0f}% [{iv_bar}]")

    formatted = "\n".join(l for l in lines if l)

    return {
        "verdict":       base,
        "ratio":         ratio,
        "total_cost":    total_cost,
        "call_mid":      call_mid,
        "put_mid":       put_mid,
        "call_strike":   call_strike,
        "put_strike":    put_strike,
        "req_move_pct":  req_move_pct,
        "hist_range":    hist_range,
        "scaled_range":  scaled_range,
        "upper_be":      upper_be,
        "lower_be":      lower_be,
        "dte":           dte,
        "expiry":        exp_display,
        "iv_rank":       iv_rank,
        "spread_pct":    avg_spread,
        "spread_ok":     spread_ok,
        "formatted":     formatted,
    }
