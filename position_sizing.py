"""
Position sizing calculator for FlowCheck.
Uses Kelly criterion adjusted for options trading.
Calculates max contracts based on account size and win rate.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# Default account size — can be overridden via env var
DEFAULT_ACCOUNT = 10000
MAX_RISK_PCT    = 0.02   # Never risk more than 2% per trade
KELLY_FRACTION  = 0.25   # Use 1/4 Kelly for safety

def get_account_size() -> float:
    """Get account size from env var or use default."""
    val = os.environ.get("ACCOUNT_SIZE")
    if val:
        try:
            return float(val)
        except:
            pass
    return DEFAULT_ACCOUNT

def calc_position_size(option_price: float, verdict: str,
                        win_rate: float = None, score: float = None) -> dict:
    """
    Calculate recommended position size.

    Args:
        option_price: Current option price per contract
        verdict: TRADE/WATCH/SKIP
        win_rate: Historical win rate (0-100)
        score: FlowCheck score (0-7)

    Returns:
        dict with max_contracts, max_dollar_risk, kelly_contracts, recommendation
    """
    account      = get_account_size()
    max_dollar   = account * MAX_RISK_PCT
    contract_cost = option_price * 100  # 1 contract = 100 shares

    if contract_cost <= 0:
        return {"error": "Invalid option price"}

    # Max contracts based on 2% rule
    max_by_risk = int(max_dollar / contract_cost)
    max_by_risk = max(1, max_by_risk)

    # Kelly criterion adjustment
    # Kelly = (W*R - L) / R where W=win rate, R=reward/risk, L=loss rate
    # For options: assume 100% loss if wrong, 2x gain if right (conservative)
    kelly_contracts = max_by_risk  # Default to max

    if win_rate is not None and win_rate > 0:
        w = win_rate / 100
        l = 1 - w
        r = 2.0  # Assume 2:1 reward:risk
        kelly_full  = (w * r - l) / r
        kelly_frac  = kelly_full * KELLY_FRACTION  # Quarter Kelly
        kelly_pct   = max(0, min(kelly_frac, MAX_RISK_PCT))
        kelly_dollar = account * kelly_pct
        kelly_contracts = max(1, int(kelly_dollar / contract_cost))

    # Score-based adjustment
    if score is not None:
        if score >= 6.5:    size_mult = 1.0   # Full size on TRADE
        elif score >= 5.5:  size_mult = 0.75  # 75% on strong WATCH
        elif score >= 4.5:  size_mult = 0.5   # 50% on weak WATCH
        else:               size_mult = 0.25  # 25% on anything below
        kelly_contracts = max(1, int(kelly_contracts * size_mult))

    # Cap at max by risk
    recommended = min(kelly_contracts, max_by_risk)
    total_cost  = recommended * contract_cost

    # Verdict override
    if verdict == "SKIP":
        return {
            "max_contracts":  0,
            "recommendation": "DO NOT ENTER — SKIP verdict",
            "total_cost":     0,
        }

    # Build recommendation text
    if recommended == 1:
        rec_text = f"1 contract (${total_cost:.0f}) — minimum size"
    elif recommended <= 3:
        rec_text = f"{recommended} contracts (${total_cost:.0f})"
    else:
        rec_text = f"{recommended} contracts (${total_cost:.0f}) — consider starting with {recommended//2}"

    return {
        "account_size":    account,
        "max_dollar_risk": round(max_dollar, 2),
        "max_contracts":   max_by_risk,
        "kelly_contracts": kelly_contracts,
        "recommended":     recommended,
        "total_cost":      round(total_cost, 2),
        "pct_of_account":  round((total_cost/account)*100, 1),
        "recommendation":  rec_text,
    }

def format_sizing_for_sms(sizing: dict, option_price: float = None,
                          flow_price: float = None) -> str:
    """Format position sizing for Telegram.
    option_price: entry price (may be discounted limit)
    flow_price: original flow fill price (for multiplier display)
    """
    if sizing.get("error") or not sizing.get("recommended"):
        return ""

    rec  = sizing["recommended"]
    cost = sizing["total_cost"]
    pct  = sizing["pct_of_account"]
    acct = sizing["account_size"]

    if option_price:
        # Show multiplier if using discounted limit price
        if flow_price and flow_price > option_price and option_price > 0:
            _mult = round(flow_price / option_price, 2)
            _note = f" limit ({_mult:.2f}x leverage vs flow price)"
        else:
            _note = ""
        return (f"💰 Size: {rec} contract{'s' if rec>1 else ''} "
                f"@ ${option_price:.2f}{_note} = ${cost:.0f} "
                f"({pct}% of ${acct:,.0f})")
    return (f"💰 Size: {rec} contract{'s' if rec>1 else ''} "
            f"= ${cost:.0f} ({pct}% of account)")
