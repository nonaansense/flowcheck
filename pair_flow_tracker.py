"""
pair_flow_tracker.py — Rapid flow accumulation detector.

Monitors the Pair_of_3_in_5_mins Bullflow filter. When a ticker receives
PAIR_FLOW_MIN_COUNT or more flows of the SAME direction (all calls OR all
puts) within a rolling PAIR_FLOW_WINDOW_MINS window, fires an alert.

Fires again on every additional fill after the threshold is met, so a 4th,
5th fill each trigger their own alert.

Highlights alerts where total premium >= PAIR_FLOW_PREMIUM_HIGHLIGHT.

Config env vars:
  PAIR_FLOW_FILTER_NAME          = Pair_of_3_in_5_mins
  PAIR_FLOW_WINDOW_MINS          = 5          rolling window in minutes
  PAIR_FLOW_MIN_COUNT            = 3          minimum fills to trigger
  PAIR_FLOW_PREMIUM_HIGHLIGHT    = 200000     highlight threshold ($200K)
"""
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

PAIR_FILTER       = os.environ.get("PAIR_FLOW_FILTER_NAME", "Pair_of_3_in_5_mins")
WINDOW_MINS       = float(os.environ.get("PAIR_FLOW_WINDOW_MINS", "5"))
MIN_COUNT         = int(os.environ.get("PAIR_FLOW_MIN_COUNT", "3"))
PREMIUM_HIGHLIGHT = float(os.environ.get("PAIR_FLOW_PREMIUM_HIGHLIGHT", "200000"))
STORAGE_KEY       = "pair_flow_history"

_PAIRS:  dict = {}   # ticker_direction → {"fills": [...], "last_alerted_count": int}
_loaded: bool = False


def _load():
    global _PAIRS, _loaded
    if _loaded:
        return
    try:
        from storage import db_get
        raw = db_get(STORAGE_KEY)
        if raw and isinstance(raw, dict):
            _PAIRS = raw
    except Exception as e:
        print(f"[PAIR] Load error: {e}")
    _loaded = True


def _save():
    try:
        from storage import db_set
        db_set(STORAGE_KEY, _PAIRS)
    except Exception as e:
        print(f"[PAIR] Save error: {e}")


def _prune():
    cutoff = time.time() - WINDOW_MINS * 60
    for key in list(_PAIRS.keys()):
        _PAIRS[key]["fills"] = [
            f for f in _PAIRS[key]["fills"] if f.get("ts", 0) >= cutoff
        ]
        if not _PAIRS[key]["fills"]:
            del _PAIRS[key]


def _key(ticker: str, direction: str) -> str:
    d = "call" if "call" in direction.lower() else "put"
    return f"{ticker.upper()}_{d}"


def _fmt_prem(p: float) -> str:
    return f"${p/1_000_000:.1f}M" if p >= 1_000_000 else f"${p/1_000:.0f}K"


def process_pair_flow(alert: dict, filter_name: str) -> dict | None:
    """
    Track fills per ticker+direction in rolling window.
    Returns alert dict when MIN_COUNT fills accumulate, then on every
    additional fill. Only calls fire a call alert, only puts fire a put alert.
    """
    if filter_name != PAIR_FILTER:
        return None

    _load()
    _prune()

    ticker      = str(alert.get("ticker", "") or "").upper()
    strike      = str(alert.get("strike", "") or "")
    expiry      = str(alert.get("expiry", "") or "")
    option_type = str(alert.get("option_type", "call") or "call")
    price       = float(alert.get("option_price") or alert.get("trade_price") or 0)
    premium     = float(alert.get("premium", 0) or 0)
    is_sweep    = bool(alert.get("is_sweep", False))
    dte         = int(alert.get("dte", 0) or 0)
    stock_px    = float(alert.get("stock_price") or 0)
    now         = time.time()
    time_str    = datetime.now(ET).strftime("%-I:%M:%S %p")

    if not ticker:
        return None

    direction = "call" if "call" in option_type.lower() else "put"
    key       = _key(ticker, direction)

    if key not in _PAIRS:
        _PAIRS[key] = {"fills": [], "last_alerted_count": 0,
                       "ticker": ticker, "direction": direction}

    fill = {
        "strike":    strike,
        "expiry":    expiry,
        "price":     price,
        "premium":   premium,
        "sweep":     is_sweep,
        "dte":       dte,
        "stock_px":  stock_px,
        "time":      time_str,
        "ts":        now,
    }
    _PAIRS[key]["fills"].append(fill)
    _save()

    # Only count fills within the rolling window
    cutoff       = now - WINDOW_MINS * 60
    window_fills = [f for f in _PAIRS[key]["fills"] if f["ts"] >= cutoff]
    count        = len(window_fills)
    last_alerted = _PAIRS[key]["last_alerted_count"]

    print(f"[PAIR] {key}: {count} {direction}s in last {WINDOW_MINS:.0f}min "
          f"(need {MIN_COUNT})")

    if count < MIN_COUNT or count <= last_alerted:
        return None

    _PAIRS[key]["last_alerted_count"] = count
    _save()

    total_prem  = sum(f["premium"] for f in window_fills)
    span_secs   = now - min(f["ts"] for f in window_fills)
    span_str    = (f"{span_secs/60:.1f}min" if span_secs >= 60
                   else f"{span_secs:.0f}s")
    above_highlight = total_prem >= PREMIUM_HIGHLIGHT

    print(f"[PAIR] 🔥 {count} {direction}s: {ticker} | "
          f"{_fmt_prem(total_prem)} in {span_str}"
          f"{' ✅ ABOVE THRESHOLD' if above_highlight else ''}")

    return {
        "ticker":           ticker,
        "direction":        direction,
        "fills":            window_fills,
        "count":            count,
        "total_prem":       total_prem,
        "above_highlight":  above_highlight,
        "span_str":         span_str,
        "window_mins":      WINDOW_MINS,
        "min_count":        MIN_COUNT,
        "premium_highlight": PREMIUM_HIGHLIGHT,
    }


def build_pair_alert(result: dict) -> str:
    ticker    = result["ticker"]
    direction = result["direction"]
    fills     = result["fills"]
    count     = result["count"]
    total     = result["total_prem"]
    above     = result["above_highlight"]
    span      = result["span_str"]
    win_mins  = result["window_mins"]
    highlight = result["premium_highlight"]

    emoji     = "📈" if direction == "call" else "📉"
    otype     = "C" if direction == "call" else "P"
    dir_cap   = direction.upper()
    header    = f"🔥 {count} {dir_cap} FLOWS: ${ticker} ({win_mins:.0f}min window)"
    subhead   = f"━━━ {emoji} RAPID {dir_cap} ACCUMULATION ━━━"

    fill_lines = []
    for i, f in enumerate(fills, 1):
        sweep_s = " ⚡" if f.get("sweep") else "  "
        strike  = f.get("strike", "?")
        expiry  = f.get("expiry", "?")
        px      = f.get("price", 0)
        prem    = f.get("premium", 0)
        t       = f.get("time", "")
        dte     = f.get("dte", 0)
        dte_s   = f" {dte}d" if dte else ""
        fill_lines.append(
            f"  {dir_cap} #{i}{sweep_s} {strike}{otype} {expiry}{dte_s} "
            f"@ ${px:.2f}  {_fmt_prem(prem)}  {t}"
        )

    prem_line = f"💵 Total: {_fmt_prem(total)}"
    if above:
        hl_s = _fmt_prem(highlight)
        prem_line += f"  ✅ ABOVE {hl_s} THRESHOLD"

    lines = [
        header,
        subhead,
        "",
    ] + fill_lines + [
        "",
        prem_line,
        f"⏱️  {count} {direction}s in {span}",
    ]

    # Stock price at time of last flow
    last_stock_px = fills[-1].get("stock_px", 0) if fills else 0
    if last_stock_px:
        lines.append(f"🟢 Stock @ ${last_stock_px:.2f} at time of last flow")

    lines += [
        f"",
        f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
    ]

    return "\n".join(lines)
