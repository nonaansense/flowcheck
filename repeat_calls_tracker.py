"""
repeat_calls_tracker.py — Repeat flow activity ratio detector.

Monitors the Repeat_Flow_Tracker Bullflow filter. Tracks ALL fills of a
single direction (calls OR puts, tracked independently) on a ticker — any
strike, any expiry — accumulated over the trading day. Sums total premium
deployed and divides by the average stock price seen across those fills,
producing a "premium-to-price ratio" — effectively how many share-
equivalents of capital have flowed into that side of the name.

Calls are always tracked. Puts tracking can be toggled independently via
REPEAT_PUTS_ENABLED — when disabled, put fills are skipped entirely
(not stored, not counted) since calls and puts are fully independent state.

When a direction's ratio crosses REPEAT_CALLS_RATIO_THRESHOLD (default
$50,000), fires an alert highlighting the single contract furthest out
from today's flow — defined as the fill with the longest DTE, and among
ties, the highest (most OTM) strike. That contract is surfaced as the
"trade idea" since it represents the most aggressive, longest-duration
thesis implied by the accumulation pattern.

State resets each trading day (ET calendar date), per ticker+direction.

Config env vars:
  REPEAT_FLOW_FILTER_NAME       = Repeat_Flow_Tracker   (falls back to
                                   REPEAT_CALLS_FILTER_NAME for old deploys)
  REPEAT_CALLS_RATIO_THRESHOLD  = 50000     user-defined ratio trigger
  REPEAT_PUTS_ENABLED           = true      track puts in addition to calls
"""
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

REPEAT_FLOW_FILTER = (os.environ.get("REPEAT_FLOW_FILTER_NAME") or
                      os.environ.get("REPEAT_CALLS_FILTER_NAME") or
                      "Repeat_Flow_Tracker")
RATIO_THRESHOLD     = float(os.environ.get("REPEAT_CALLS_RATIO_THRESHOLD", "50000"))
STORAGE_KEY         = "repeat_flow_history"


def _puts_enabled() -> bool:
    return os.environ.get("REPEAT_PUTS_ENABLED", "true").lower() not in ("false","0","no","off")


_REPEAT: dict = {}   # ticker_direction → {"fills": [...], "day": "YYYY-MM-DD", "last_alerted_ratio": 0}
_loaded: bool = False


def _load():
    global _REPEAT, _loaded
    if _loaded:
        return
    try:
        from storage import db_get
        raw = db_get(STORAGE_KEY)
        if raw and isinstance(raw, dict):
            _REPEAT = raw
    except Exception as e:
        print(f"[REPEAT] Load error: {e}")
    _loaded = True


def _save():
    try:
        from storage import db_set
        db_set(STORAGE_KEY, _REPEAT)
    except Exception as e:
        print(f"[REPEAT] Save error: {e}")


def _today_str() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _fmt_prem(p: float) -> str:
    return f"${p/1_000_000:.1f}M" if p >= 1_000_000 else f"${p/1_000:.0f}K"


def _strike_val(strike: str) -> float:
    try:
        return float(strike)
    except Exception:
        return 0.0


def _furthest_out_fill(fills: list) -> dict:
    """
    Identify the single most aggressive contract in today's flow:
    longest DTE first, then highest (most OTM) strike as tiebreaker.
    """
    return sorted(
        fills,
        key=lambda f: (f.get("dte", 0), _strike_val(f.get("strike", "0"))),
        reverse=True,
    )[0]


def process_repeat_calls(alert: dict, filter_name: str) -> dict | None:
    """
    Track same-direction fills (calls OR puts) on a ticker for the current
    trading day. Fires when total_premium / avg_stock_price crosses
    RATIO_THRESHOLD. Re-fires on every subsequent qualifying fill the
    same day. Calls always tracked; puts gated by REPEAT_PUTS_ENABLED.
    """
    if filter_name != REPEAT_FLOW_FILTER:
        return None

    option_type = str(alert.get("option_type", "call") or "call")
    direction   = "call" if "call" in option_type.lower() else "put"

    if direction == "put" and not _puts_enabled():
        print(f"[REPEAT] Puts tracking disabled — skipping {alert.get('ticker','?')}")
        return None

    _load()

    ticker   = str(alert.get("ticker", "") or "").upper()
    strike   = str(alert.get("strike", "") or "")
    expiry   = str(alert.get("expiry", "") or "")
    price    = float(alert.get("option_price") or 0)
    premium  = float(alert.get("premium", 0) or 0)
    is_sweep = bool(alert.get("is_sweep", False))
    dte      = int(alert.get("dte", 0) or 0)
    stock_px = float(alert.get("stock_price") or 0)
    today    = _today_str()
    time_str = datetime.now(ET).strftime("%-I:%M:%S %p")

    if not ticker or not strike or not expiry:
        return None

    # Fetch a live stock price if one wasn't passed with the fill
    if not stock_px:
        try:
            from fetcher import fetch_price
            stock_px = fetch_price(ticker) or 0
        except Exception:
            stock_px = 0

    key = f"{ticker}_{direction}"

    # Reset state on a new trading day
    if key not in _REPEAT or _REPEAT[key].get("day") != today:
        _REPEAT[key] = {"fills": [], "day": today, "last_alerted_ratio": 0,
                        "ticker": ticker, "direction": direction}

    fill = {
        "strike": strike, "expiry": expiry, "price": price,
        "premium": premium, "sweep": is_sweep, "dte": dte,
        "stock_px": stock_px, "time": time_str, "ts": time.time(),
    }
    _REPEAT[key]["fills"].append(fill)
    _save()

    fills        = _REPEAT[key]["fills"]
    total_prem   = sum(f["premium"] for f in fills)
    px_samples   = [f["stock_px"] for f in fills if f.get("stock_px", 0) > 0]
    avg_px       = sum(px_samples) / len(px_samples) if px_samples else 0
    ratio        = (total_prem / avg_px) if avg_px > 0 else 0
    last_alerted = _REPEAT[key]["last_alerted_ratio"]

    print(f"[REPEAT] {ticker} {direction}: {len(fills)} fills today | "
          f"{_fmt_prem(total_prem)} total | avg px ${avg_px:.2f} | "
          f"ratio {ratio:,.0f} (need {RATIO_THRESHOLD:,.0f})")

    if avg_px <= 0 or ratio < RATIO_THRESHOLD or ratio <= last_alerted:
        return None

    _REPEAT[key]["last_alerted_ratio"] = ratio
    _save()

    furthest = _furthest_out_fill(fills)
    otype    = "C" if direction == "call" else "P"

    print(f"[REPEAT] 🔥 Ratio threshold crossed: {ticker} {direction} — "
          f"{ratio:,.0f} | furthest: {furthest['strike']}{otype} {furthest['expiry']}")

    return {
        "ticker":      ticker,
        "direction":   direction,
        "fills":       fills,
        "fill_count":  len(fills),
        "total_prem":  total_prem,
        "avg_px":      avg_px,
        "ratio":       ratio,
        "furthest":    furthest,
        "threshold":   RATIO_THRESHOLD,
    }


def build_repeat_calls_alert(result: dict) -> str:
    ticker     = result["ticker"]
    direction  = result.get("direction", "call")
    fills      = result["fills"]
    n          = result["fill_count"]
    total_prem = result["total_prem"]
    avg_px     = result["avg_px"]
    ratio      = result["ratio"]
    threshold  = result["threshold"]
    f          = result["furthest"]

    otype   = "C" if direction == "call" else "P"
    emoji   = "📈" if direction == "call" else "📉"
    dir_cap = direction.upper()

    fill_lines = []
    for i, fl in enumerate(fills, 1):
        sweep_s = " ⚡" if fl.get("sweep") else "  "
        fill_lines.append(
            f"  #{i}{sweep_s} {fl['strike']}{otype} {fl['expiry']} "
            f"@ ${fl['price']:.2f}  {_fmt_prem(fl['premium'])}  {fl['time']}"
        )

    lines = [
        f"🔁 REPEAT {dir_cap} ACTIVITY: ${ticker}",
        f"━━━ {emoji} RATIO THRESHOLD CROSSED ━━━",
        "",
        f"All {direction} fills today ({n}):",
    ] + fill_lines + [
        "",
        f"💵 Total premium: {_fmt_prem(total_prem)}",
        f"📊 Avg stock price: ${avg_px:.2f}",
        f"⚖️  Ratio: {ratio:,.0f}  (threshold: {threshold:,.0f})",
        "",
        f"💡 TRADE IDEA — furthest out from today's flow:",
        f"   {f['strike']}{otype} {f['expiry']}  ({f['dte']}d DTE)  @ ${f['price']:.2f}",
        "",
        f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
    ]

    return "\n".join(lines)
