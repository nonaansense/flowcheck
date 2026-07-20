"""
targeted_strikes_tracker.py — Consecutive same strike/expiry stacking detector.

Monitors the Targeted_Strikes_Expiry Bullflow filter. Tracks a CONSECUTIVE
run ("sequence") of fills on ONE specific strike + expiry, per ticker and
direction, over the trading day.

The streak is per (ticker, direction). Walking that ticker's same-direction
flow in arrival order:
  • Same strike/expiry as the current streak  -> streak += 1
  • Different strike/expiry (same ticker+dir)  -> streak BREAKS; a fresh
        streak of 1 starts on the new contract (count resets to 0 then 1)
  • Opposite direction, or a different ticker   -> IGNORED (doesn't touch
        this ticker+direction's streak at all)

So calls and puts each keep their own independent streak per ticker, and
ONLY a same-direction fill at a different strike/expiry resets it. A put
printing in the middle of a call streak, or an unrelated ticker's flow,
does not break the run.

Example (tracking GOOGL calls):
  GOOGL 370C 7/17  -> streak = 1
  GOOGL 370C 7/17  -> streak = 2
  AAPL  200P 7/24  -> ignored (different ticker)
  GOOGL 370C 7/17  -> streak = 3
  GOOGL 365P 7/17  -> ignored (put; call streak untouched)
  GOOGL 370C 7/17  -> streak = 4  -> ALERT FIRES
  GOOGL 375C 7/17  -> BREAKS: call streak resets, now 375C streak = 1
  GOOGL 370C 7/17  -> back on 370C, but streak restarts at 1 (run was broken)

Fires once when the streak reaches TARGETED_STRIKES_THRESHOLD, then again
on every additional consecutive same-contract fill ("ADD-ON").

Alerts whose triggering fill lands before TARGETED_STRIKES_EARLY_CUTOFF
(10:25am ET default) are tagged EARLY SESSION.

State resets each trading day (ET calendar date).

Config env vars:
  TARGETED_STRIKES_FILTER_NAME    = Targeted_Strikes_Expiry
  TARGETED_STRIKES_THRESHOLD      = 4        consecutive same strike/expiry fills
  TARGETED_STRIKES_EARLY_CUTOFF   = 10:25    HH:MM ET, flags early-session alerts
"""
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

TARGETED_FILTER   = os.environ.get("TARGETED_STRIKES_FILTER_NAME", "Targeted_Strikes_Expiry")
THRESHOLD         = int(os.environ.get("TARGETED_STRIKES_THRESHOLD", "4"))
EARLY_CUTOFF_STR  = os.environ.get("TARGETED_STRIKES_EARLY_CUTOFF", "10:25")
# When true, fills before the cutoff are dropped entirely — not counted, not
# stored — so no run can be built from pre-cutoff flow. Default false: count
# them and just tag the alert ⏰EARLY.
SKIP_EARLY        = os.environ.get("TARGETED_STRIKES_SKIP_EARLY", "false").lower() in ("true","1","yes","on")
# When true, pre-cutoff fills STILL COUNT toward the run, but an alert is held
# back until the fill that crosses/extends the threshold lands at or after the
# cutoff. A run that completes entirely before the cutoff stays silent until
# (and unless) another matching fill arrives post-cutoff. Independent of
# SKIP_EARLY (if SKIP_EARLY is on, pre-cutoff fills are gone, so this is moot).
GATE_UNTIL_CUTOFF = os.environ.get("TARGETED_STRIKES_GATE_UNTIL_CUTOFF", "false").lower() in ("true","1","yes","on")
STORAGE_KEY       = "targeted_strikes_history"

# State is one ACTIVE STREAK per ticker+direction:
#   "TICKER_direction" -> {
#       "day": "YYYY-MM-DD",
#       "strike": str, "expiry": str,      # the contract the current run is on
#       "fills": [...],                    # the consecutive fills in this run
#       "last_alerted_count": int,         # highest count already alerted for THIS run
#   }
_TARGETED: dict = {}
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


def _dir_key(ticker: str, direction: str) -> str:
    return f"{ticker.upper()}_{direction}"


def _is_early(dt: datetime) -> bool:
    hh, mm = _early_cutoff()
    return (dt.hour, dt.minute) < (hh, mm)


def process_targeted_strikes(alert: dict, filter_name: str) -> dict | None:
    """
    Track a CONSECUTIVE run of same strike+expiry fills per ticker+direction
    for the current trading day. A same-direction fill at a different
    strike/expiry breaks the run and starts a new one. Opposite-direction
    fills and other tickers are ignored (handled by their own keys).

    Fires when the run reaches THRESHOLD, then re-fires as an add-on on each
    additional consecutive same-contract fill.
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

    if SKIP_EARLY and _is_early(now_et):
        print(f"[TARGETED] Skipping pre-cutoff fill (SKIP_EARLY on): "
              f"{ticker} {strike}/{expiry} {direction} @ {time_str}")
        return None

    if not stock_px:
        # Payload had no stock price. Try Tradier first (matches the preset
        # alerts' source), then fall back to the Finnhub/Tiingo fetcher.
        try:
            from bullflow_presets import _fetch_tradier_price
            stock_px = _fetch_tradier_price(ticker) or 0
        except Exception:
            stock_px = 0
        if not stock_px:
            try:
                from fetcher import fetch_price
                stock_px = fetch_price(ticker) or 0
            except Exception:
                stock_px = 0

    key = _dir_key(ticker, direction)
    fill = {
        "strike": strike, "expiry": expiry, "price": price,
        "premium": premium, "sweep": is_sweep, "dte": dte,
        "stock_px": stock_px, "time": time_str, "ts": time.time(),
        "early": _is_early(now_et),
    }

    streak = _TARGETED.get(key)
    same_contract = (streak is not None
                     and streak.get("day") == today
                     and streak.get("strike") == strike
                     and streak.get("expiry") == expiry)

    if same_contract:
        # Continue the current run.
        streak["fills"].append(fill)
    else:
        # New day, first-ever fill for this ticker+dir, OR a same-direction
        # fill at a DIFFERENT strike/expiry -> the previous run (if any) is
        # broken. Start a fresh run of 1 on this contract.
        streak = {
            "day": today,
            "strike": strike,
            "expiry": expiry,
            "fills": [fill],
            "last_alerted_count": 0,
        }
        _TARGETED[key] = streak

    _save()

    count        = len(streak["fills"])
    last_alerted = streak["last_alerted_count"]

    print(f"[TARGETED] {ticker} {direction} {strike}/{expiry}: "
          f"consecutive run = {count} (need {THRESHOLD})")

    if count < THRESHOLD or count <= last_alerted:
        return None

    # GATE_UNTIL_CUTOFF: the run has reached/extended the threshold, but if the
    # triggering fill is still before the cutoff, hold the alert. Do NOT advance
    # last_alerted_count — so the first matching fill AT/AFTER the cutoff will
    # fire, showing the full accumulated count.
    if GATE_UNTIL_CUTOFF and fill.get("early"):
        print(f"[TARGETED] Holding alert (GATE_UNTIL_CUTOFF): {ticker} "
              f"{strike}/{expiry} {direction} run={count}, triggering fill "
              f"before cutoff {EARLY_CUTOFF_STR}")
        return None

    streak["last_alerted_count"] = count
    _save()

    fills      = streak["fills"]
    total_prem = sum(f["premium"] for f in fills)
    is_addon   = last_alerted > 0
    early      = any(f.get("early") for f in fills)   # ANY fill in the run before cutoff

    print(f"[TARGETED] 🎯 {'Add-on' if is_addon else 'Run threshold crossed'}: "
          f"{ticker} {strike}{'C' if direction == 'call' else 'P'} {expiry} — "
          f"{count}x consecutive{' EARLY SESSION' if early else ''}")

    # Ticker's whole-day call/put premium at this moment, for context.
    tkr_flow = None
    try:
        from ticker_premium_tracker import get_snapshot, mark_alerted
        mark_alerted(ticker)
        tkr_flow = get_snapshot(ticker)
    except Exception as e:
        print(f"[TARGETED] ticker premium snapshot error: {e}")

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
        "tkr_flow":   tkr_flow,
    }


def get_todays_alerted_runs() -> list:
    """
    Every run that fired an alert today, for downstream scoring (e.g. the
    3:30pm swing rating). Returns one dict per ticker+direction run that
    reached the threshold.
    """
    _load()
    today = _today_str()
    out = []
    for key, st in _TARGETED.items():
        if st.get("day") != today:
            continue
        if st.get("last_alerted_count", 0) < THRESHOLD:
            continue
        fills = st.get("fills", [])
        direction = "put" if key.endswith("_put") else "call"
        out.append({
            "ticker":     key.rsplit("_", 1)[0],
            "direction":  direction,
            "strike":     st.get("strike", ""),
            "expiry":     st.get("expiry", ""),
            "count":      len(fills),
            "total_prem": sum(f.get("premium", 0) for f in fills),
            "sweeps":     sum(1 for f in fills if f.get("sweep")),
            "dte":        fills[-1].get("dte", 0) if fills else 0,
            "early":      any(f.get("early") for f in fills),
            "last_px":    fills[-1].get("price", 0) if fills else 0,
            "stock_px":   fills[-1].get("stock_px", 0) if fills else 0,
            "fills":      fills,
        })
    return out


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
    subhead = f"━━━ {emoji} {n}x {dir_cap} IN A ROW ON {strike}{otype} {expiry} ━━━"

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
        f"Consecutive {direction} fills on this strike/expiry ({n} in a row, need {threshold}):",
    ] + fill_lines + [
        "",
        f"💵 Combined premium: {_fmt_prem(total_prem)}",
    ]

    if span_line:
        lines.append(span_line)

    if early:
        lines.append("⏰ EARLY SESSION — one or more fills before 10:25am ET")

    # Ticker's whole-day call/put premium context (all strikes/expiries).
    tkr_flow = result.get("tkr_flow")
    if tkr_flow:
        try:
            from ticker_premium_tracker import format_snapshot
            lines += format_snapshot(tkr_flow, f"${ticker} targeted flow today")
        except Exception:
            pass

    last_stock_px = fills[-1].get("stock_px", 0) if fills else 0
    if last_stock_px:
        lines.append(f"🟢 Stock @ ${last_stock_px:.2f} at alert time")

    lines += [
        "",
        f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
    ]

    return "\n".join(lines)
