"""
Risk management for FlowCheck.
Handles:
1. Correlation risk warning
2. Max positions enforcement
3. Theta decay calendar
4. Smart stop loss based on technical levels
"""
import os, requests, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from exit_signals import load_positions, get_open_positions

def poly_key():
    return os.environ.get("POLYGON_API_KEY")

MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS","3"))

SECTOR_MAP = {
    "XLK": ["AAPL","MSFT","NVDA","AMD","ORCL","CRM","FLNC","ASTS","INTC","CIFR"],
    "XLF": ["JPM","BAC","GS","MS","WFC"],
    "XLE": ["XOM","CVX","BE","PLUG","BLDP","FCEL"],
    "XLB": ["ALB","FCX","NEM","GLD"],
    "XLV": ["JNJ","PFE","MRNA","ABBV"],
    "XLY": ["AMZN","TSLA","HD","MCD"],
    "XLC": ["META","GOOG","NFLX","NOK"],
    "XLI": ["CAT","HON","BA","GE"],
}

def get_sector(ticker: str) -> str:
    for sector, tickers in SECTOR_MAP.items():
        if ticker.upper() in tickers:
            return sector
    return "OTHER"

# ── 1. Correlation Risk ────────────────────────────────────────────────

def check_correlation_risk(new_ticker: str, new_type: str = "call") -> dict:
    """
    Check if adding this position creates sector concentration.
    Returns warning if already holding same-sector positions.
    """
    open_pos = get_open_positions()
    if not open_pos:
        return {"has_risk": False}

    new_sector   = get_sector(new_ticker)
    same_sector  = [p for p in open_pos
                    if get_sector(p.get("ticker","")) == new_sector
                    and p.get("option_type","call") == new_type]
    all_calls    = [p for p in open_pos
                    if p.get("option_type","call") == "call"]

    result = {"has_risk": False, "warnings": []}

    # Same sector warning
    if same_sector:
        tickers = [p.get("ticker") for p in same_sector]
        result["has_risk"] = True
        result["warnings"].append(
            f"⚠️ CORRELATION: Already holding {', '.join(tickers)} ({new_sector}). "
            f"Adding {new_ticker} = {len(same_sector)+1} {new_sector} longs."
        )

    # Too many calls warning
    if len(all_calls) >= MAX_POSITIONS - 1:
        result["has_risk"] = True
        result["warnings"].append(
            f"⚠️ HEDGE ALERT: {len(all_calls)} open call positions. "
            f"Consider QQQ puts before adding more calls."
        )

    return result

# ── 2. Max Positions Enforcement ──────────────────────────────────────

def check_max_positions() -> dict:
    """Check if at max positions. Returns block dict if exceeded."""
    open_pos = get_open_positions()
    count    = len(open_pos)

    if count >= MAX_POSITIONS:
        tickers = [p.get("ticker","?") for p in open_pos]
        return {
            "blocked":  True,
            "count":    count,
            "max":      MAX_POSITIONS,
            "message":  (f"🛑 MAX POSITIONS REACHED: {count}/{MAX_POSITIONS} open trades "
                         f"({', '.join(tickers)}). Close one before entering new position."),
        }
    return {"blocked": False, "count": count, "max": MAX_POSITIONS}

# ── 3. Smart Stop Loss ─────────────────────────────────────────────────

def calc_smart_stop(ticker: str, current_price: float,
                     option_type: str = "call") -> dict:
    """
    Calculate stop loss based on technical levels instead of fixed %.
    Uses: previous day low, recent swing low, VWAP.
    """
    key = poly_key()
    if not key or not current_price:
        # Fallback to fixed %
        pct  = 0.08 if current_price < 20 else 0.05
        stop = round(current_price * (1-pct), 2)
        return {"stop_price": stop, "stop_reason": f"Fixed {pct*100:.0f}% stop",
                "stop_type": "FIXED"}

    try:
        # Get previous day's data
        r = requests.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker.upper()}",
            params={"apiKey": key},
            timeout=8
        )
        if r.status_code == 200:
            snap     = r.json().get("ticker",{})
            prev_day = snap.get("prevDay",{})
            today    = snap.get("day",{})

            prev_low  = float(prev_day.get("l",0) or 0)
            today_low = float(today.get("l",0) or 0)
            today_open= float(today.get("o",0) or 0)

            # Use previous day low as primary stop
            if prev_low > 0 and prev_low < current_price:
                # Add small buffer below prev low
                stop  = round(prev_low * 0.995, 2)
                reason = f"Below prev day low ${prev_low:.2f}"
                return {
                    "stop_price":  stop,
                    "stop_reason": reason,
                    "stop_type":   "TECHNICAL",
                    "prev_low":    prev_low,
                    "today_low":   today_low,
                }

            # Fallback to today's low
            if today_low > 0 and today_low < current_price:
                stop   = round(today_low * 0.995, 2)
                reason = f"Below today's low ${today_low:.2f}"
                return {
                    "stop_price":  stop,
                    "stop_reason": reason,
                    "stop_type":   "TECHNICAL",
                }

    except Exception as e:
        print(f"[RISK] Smart stop error: {e}")

    # Final fallback
    pct  = 0.08 if current_price < 20 else 0.05
    stop = round(current_price * (1-pct), 2)
    return {"stop_price": stop, "stop_reason": f"Fixed {pct*100:.0f}% stop",
            "stop_type": "FIXED"}

# ── 4. Theta Decay Calendar ────────────────────────────────────────────

def calc_theta_decay(position: dict) -> dict:
    """
    Calculate theta decay schedule for an open position.
    Theta accelerates as expiry approaches.
    """
    expiry_raw  = position.get("expiry_raw","")
    entry_opt   = position.get("entry_option")
    greeks      = position.get("greeks",{})
    theta       = greeks.get("theta") if greeks else None

    if not expiry_raw or not entry_opt:
        return {}

    try:
        parts = expiry_raw.split("/")
        m, d, y = parts
        y = "20"+y if len(y)==2 else y
        from datetime import datetime as _dt
        exp_date = _dt(int(y),int(m),int(d))
        dte      = (exp_date - _dt.now()).days

        if dte <= 0:
            return {}

        opt_price = float(entry_opt)

        # Estimate theta if not available from Greeks
        # Rule of thumb: theta ~ option_price / (DTE * 0.9)
        daily_decay = abs(float(theta)) if theta else round(opt_price / (dte * 0.9), 3)

        # Theta accelerates in last 2 weeks
        if dte <= 7:
            weekly_decay = daily_decay * 5 * 1.5
            warning      = "🚨 RAPID DECAY: Theta accelerating — close soon"
        elif dte <= 14:
            weekly_decay = daily_decay * 5 * 1.2
            warning      = "⚠️ Decay accelerating this week"
        else:
            weekly_decay = daily_decay * 5
            warning      = ""

        return {
            "dte":          dte,
            "daily_decay":  round(daily_decay, 3),
            "weekly_decay": round(weekly_decay, 3),
            "warning":      warning,
            "decay_pct":    round((daily_decay / opt_price) * 100, 1) if opt_price > 0 else 0,
        }
    except Exception as e:
        print(f"[RISK] Theta error: {e}")
        return {}

def send_theta_calendar(send_sms_fn):
    """
    Monday morning: send theta decay schedule for all open positions.
    """
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() != 0:  # Only Monday
        return

    open_pos = get_open_positions()
    if not open_pos:
        return

    lines = [f"⏰ THETA DECAY CALENDAR — {now_et.strftime('%a %b %d')}", ""]

    for p in open_pos:
        ticker  = p.get("ticker","?")
        strike  = p.get("strike","?")
        otype   = p.get("option_type","call")[0].upper()
        expiry  = p.get("expiry","?")
        decay   = calc_theta_decay(p)

        if not decay:
            continue

        dte         = decay.get("dte","?")
        daily       = decay.get("daily_decay",0)
        weekly      = decay.get("weekly_decay",0)
        decay_pct   = decay.get("decay_pct",0)
        warning     = decay.get("warning","")

        lines.append(f"📉 {ticker} {strike}{otype} {expiry} [{dte}d]")
        lines.append(f"  Decay: -${daily:.3f}/day | -${weekly:.2f}/week ({decay_pct:.1f}%/day)")
        if warning:
            lines.append(f"  {warning}")
        lines.append("")

    if len(lines) > 2:
        send_sms_fn("\n".join(lines))
        print(f"[RISK] Theta calendar sent for {len(open_pos)} positions")

# ── Master Risk Check ──────────────────────────────────────────────────

def run_risk_checks(trade: dict, data: dict, result: dict) -> dict:
    """Run all risk checks before processing a new flow."""
    ticker    = trade.get("ticker","")
    opt_type  = trade.get("option_type","call")
    verdict   = result.get("verdict","SKIP")
    risk      = {"warnings": [], "blocked": False}

    if verdict == "SKIP":
        return risk

    # Max positions check
    pos_check = check_max_positions()
    if pos_check.get("blocked") and verdict == "TRADE":
        risk["blocked"]  = True
        risk["block_msg"]= pos_check["message"]
        risk["warnings"].append(pos_check["message"])

    # Correlation risk
    corr = check_correlation_risk(ticker, opt_type)
    if corr.get("has_risk"):
        risk["warnings"].extend(corr.get("warnings",[]))

    # Smart stop loss
    stock_price = data.get("stock_price")
    if stock_price:
        smart_stop = calc_smart_stop(ticker, float(stock_price), opt_type)
        risk["smart_stop"] = smart_stop

    return risk
