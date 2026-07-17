"""
targeted_strikes_tracker.py — Same strike/expiry stacking detector.

Monitors the Targeted_Strikes_Expiry Bullflow filter. Tracks fills of a
single direction (calls OR puts, tracked independently) on a ticker for
ONE specific strike + expiry combo, accumulated over the trading day.
Other flow — different strike, different expiry, or the opposite side —
doesn't break the count; only fills matching that exact
(ticker, strike, expiry, direction) key are counted.

Example:
  GOOGL 370C 7/17/26   <- call #1
  GOOGL 370C 7/17/26   <- call #2
  AAPL  200P 7/24/26   <- unrelated, ignored for this key
  GOOGL 370C 7/17/26   <- call #3
  GOOGL 365P 7/17/26   <- different strike/side, ignored
  GOOGL 370C 7/17/26   <- call #4 -> ALERT FIRES

Fires once at TARGETED_STRIKES_THRESHOLD, then again on every additional
matching fill after that ("ADD-ON"), so conviction stacking further stays
visible instead of going silent after the first hit.

Alerts whose triggering fill lands before TARGETED_STRIKES_EARLY_CUTOFF
(10:25am ET by default) are tagged EARLY SESSION in the message.

State resets each trading day (ET calendar date), per ticker+strike+expiry+direction.

Config env vars:
  TARGETED_STRIKES_FILTER_NAME    = Targeted_Strikes_Expiry
  TARGETED_STRIKES_THRESHOLD      = 4        same strike/expiry fills needed
  TARGETED_STRIKES_EARLY_CUTOFF   = 10:25    HH:MM ET, flags early-session alerts
"""
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

TARGETED_FILTER   = os.environ.get("TARGETED_STRIKES_FILTER_NAME", "Targeted_Strikes_Expiry")
THRESHOLD         = int(os.environ.get("TARGETED_STRIKES_THRESHOLD", "4"))
EARLY_CUTOFF_STR  = os.environ.get("TARGETED_STRIKES_EARLY_CUTOFF", "10:25")
STORAGE_KEY       = "targeted_strikes_history"

_TARGETED: dict = {}   # ticker_strike_expiry_direction → {"fills": [...], "day": "YYYY-MM-DD", "last_alerted_count": 0}
_loaded:   bool = False


def _early_cutoff():
    try:
        hh, mm = EARLY_CUTOFF_STR.split(":")
        return int(hh), int(mm)
    except Exception:
        return 10, 25


def _load():
    global _TARGETED, _loaded
    if _loaded:
        return
    try:
        from storage import db_get
        raw = db_get(STORAGE_KEY)
        if raw and isinstance(raw, dict):
            _TARGETED = raw
    except Exception as e:
        print(f"[TARGETED] Load error: {e}")
    _loaded = True


def _save():
    try:
        from storage import db_set
        db_set(STORAGE_KEY, _TARGETED)
    except Exception as e:
        print(f"[TARGETED] Save error: {e}")


def _today_str() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _fmt_prem(p: float) -> str:
    return f"${p/1_000_000:.1f}M" if p >= 1_000_000 else f"${p/1_000:.0f}K"


def _key(ticker: str, strike: str, expiry: str, direction: str) -> str:
    return f"{ticker.upper()}_{strike}_{expiry}_{direction}"


def _is_early(dt: datetime) -> bool:
    hh, mm = _early_cutoff()
    return (dt.hour, dt.minute) < (hh, mm)


def process_targeted_strikes(alert: dict, filter_name: str) -> dict | None:
    """
    Track same strike+expiry+direction fills on a ticker for the current
    trading day. Fires when the count crosses THRESHOLD, then re-fires as
    an add-on on every subsequent matching fill.
    """
    if filter_name != TARGETED_FILTER:
        return None

    _load()

    ticker      = str(alert.get("ticker", "") or "").upper()
    strike      = str(alert.get("strike", "") or "")
    expiry      = str(alert.get("expiry", "") or "")
    option_type = str(alert.get("option_type", "call") or "call")
    direction   = "call" if "call" in option_type.lower() else "put"
    price       = float(alert.get("option_price") or alert.get("trade_price") or 0)
    premium     = float(alert.get("premium", 0) or 0)
    is_sweep    = bool(alert.get("is_sweep", False))
    dte         = int(alert.get("dte", 0) or 0)
    stock_px    = float(alert.get("stock_price") or 0)
    today       = _today_str()
    now_et      = datetime.now(ET)
    time_str    = now_et.strftime("%-I:%M:%S %p")

    if not ticker or not strike or not expiry:
        return None

    if not stock_px:
        try:
            from fetcher import fetch_price
            stock_px = fetch_price(ticker) or 0
        except Exception:
            stock_px = 0

    key = _key(ticker, strike, expiry, direction)

    # Reset state on a new trading day
    if key not in _TARGETED or _TARGETED[key].get("day") != today:
        _TARGETED[key] = {"fills": [], "day": today, "last_alerted_count": 0,
                          "ticker": ticker, "strike": strike, "expiry": expiry,
                          "direction": direction}

    fill = {
        "strike": strike, "expiry": expiry, "price": price,
        "premium": premium, "sweep": is_sweep, "dte": dte,
        "stock_px": stock_px, "time": time_str, "ts": time.time(),
        "early": _is_early(now_et),
    }
    _TARGETED[key]["fills"].append(fill)
    _save()

    fills        = _TARGETED[key]["fills"]
    count        = len(fills)
    last_alerted = _TARGETED[key]["last_alerted_count"]

    print(f"[TARGETED] {key}: {count} {direction}s on this strike/expiry today "
          f"(need {THRESHOLD})")

    if count < THRESHOLD or count <= last_alerted:
        return None

    _TARGETED[key]["last_alerted_count"] = count
    _save()

    total_prem = sum(f["premium"] for f in fills)
    is_addon   = last_alerted > 0
    early      = fill["early"]   # early-session status of the TRIGGERING fill

    print(f"[TARGETED] 🎯 {'Add-on' if is_addon else 'Threshold crossed'}: "
          f"{ticker} {strike}{'C' if direction == 'call' else 'P'} {expiry} — "
          f"{count}x{' EARLY SESSION' if early else ''}")

    return {
        "ticker":     ticker,
        "strike":     strike,
        "expiry":     expiry,
        "direction":  direction,
        "fills":      fills,
        "count":      count,
        "total_prem": total_prem,
        "is_addon":   is_addon,
        "early":      early,
        "threshold":  THRESHOLD,
    }


def build_targeted_strikes_alert(result: dict) -> str:
    ticker     = result["ticker"]
    strike     = result["strike"]
    expiry     = result["expiry"]
    direction  = result.get("direction", "call")
    fills      = result["fills"]
    n          = result["count"]
    total_prem = result["total_prem"]
    is_addon   = result.get("is_addon", False)
    early      = result.get("early", False)
    threshold  = result["threshold"]

    otype   = "C" if direction == "call" else "P"
    emoji   = "📈" if direction == "call" else "📉"
    dir_cap = direction.upper()
    header  = f"🎯 TARGETED STRIKE{' (ADD-ON)' if is_addon else ''}: ${ticker}"
    subhead = f"━━━ {emoji} {n}x {dir_cap} STACKED ON {strike}{otype} {expiry} ━━━"

    def _gap_str(secs: float) -> str:
        secs = max(0, secs)
        if secs < 60:
            return f"+{secs:.0f}s"
        mins = secs / 60
        if mins < 60:
            return f"+{mins:.1f}m"
        return f"+{mins/60:.1f}h"

    fill_lines = []
    prev_ts = None
    for i, fl in enumerate(fills, 1):
        sweep_s = " ⚡" if fl.get("sweep") else "  "
        gap_s   = "" if prev_ts is None else f"  ({_gap_str(fl.get('ts', 0) - prev_ts)})"
        prev_ts = fl.get("ts", prev_ts)
        fill_lines.append(
            f"  #{i}{sweep_s} {fl['strike']}{otype} {fl['expiry']} "
            f"@ ${fl['price']:.2f}  {_fmt_prem(fl['premium'])}  {fl['time']} ET{gap_s}"
        )

    span_line = ""
    if len(fills) >= 2 and fills[0].get("ts") and fills[-1].get("ts"):
        span_secs = fills[-1]["ts"] - fills[0]["ts"]
        span_line = f"⏱️  Span: {_gap_str(span_secs)[1:]} from first to last fill"

    lines = [
        header,
        subhead,
        "",
        f"All {direction} fills on this strike/expiry today ({n}, need {threshold}):",
    ] + fill_lines + [
        "",
        f"💵 Combined premium: {_fmt_prem(total_prem)}",
    ]

    if span_line:
        lines.append(span_line)

    if early:
        lines.append("⏰ EARLY SESSION — triggering fill before 10:25am ET")

    last_stock_px = fills[-1].get("stock_px", 0) if fills else 0
    if last_stock_px:
        lines.append(f"🟢 Stock @ ${last_stock_px:.2f} at time of last flow")

    lines += [
        "",
        f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
    ]

    return "\n".join(lines)
