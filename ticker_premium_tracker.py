"""
ticker_premium_tracker.py — Per-ticker daily call vs put premium.

Tallies total call premium and total put premium per ticker for the trading
day, across all strikes and expiries surfaced by the Targeted_Strikes_Expiry
Bullflow filter. Scoped to that filter only — these totals describe the
targeted-strikes flow for a ticker, not the ticker's whole options tape.

Two uses:
  1. Alert-time snapshot — when a Targeted_Strikes_Expiry alert fires, attach
     the ticker's call/put premium so far that day, so the sequence can be
     read in context (is this 4x call run swimming with or against the
     ticker's overall flow?).
  2. 3:30pm update — a scheduled report re-stating the full-day call/put
     premium for every ticker that alerted, so you can see how the day
     resolved versus how it looked when the alert fired.

State resets each ET trading day. Persisted to Supabase so a redeploy
mid-session doesn't lose the running tally.

Config:
  TICKER_PREMIUM_SNAPSHOT_TIME = 15:30   HH:MM ET for the daily update job
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
STORAGE_KEY = "ticker_premium_daily"

# ticker -> {"call": float, "put": float, "call_n": int, "put_n": int}
_PREM: dict = {}
_day:  str  = ""
_alerted_tickers: set = set()   # tickers that fired a targeted alert today
_loaded: bool = False


def _today_str() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _load():
    global _PREM, _day, _alerted_tickers, _loaded
    if _loaded:
        return
    try:
        from storage import db_get
        raw = db_get(STORAGE_KEY)
        if raw and isinstance(raw, dict) and raw.get("day") == _today_str():
            _PREM = raw.get("prem", {}) or {}
            _day  = raw.get("day", "")
            _alerted_tickers = set(raw.get("alerted", []) or [])
    except Exception as e:
        print(f"[TKRPREM] Load error: {e}")
    _loaded = True


def _save():
    try:
        from storage import db_set
        db_set(STORAGE_KEY, {"day": _day, "prem": _PREM,
                             "alerted": sorted(_alerted_tickers)})
    except Exception as e:
        print(f"[TKRPREM] Save error: {e}")


def _roll_day():
    """Reset the tally if the ET calendar day changed."""
    global _PREM, _day, _alerted_tickers
    today = _today_str()
    if _day != today:
        _PREM = {}
        _alerted_tickers = set()
        _day = today


def record_flow(ticker: str, direction: str, premium: float) -> None:
    """
    Record one Targeted_Strikes_Expiry fill toward the ticker's daily
    call/put premium. Callers must gate on the filter name — this function
    does not check it.
    """
    if not ticker or premium <= 0:
        return
    _load()
    _roll_day()
    t = ticker.upper()
    side = "call" if "call" in str(direction).lower() else "put"
    row = _PREM.setdefault(t, {"call": 0.0, "put": 0.0, "call_n": 0, "put_n": 0})
    row[side] += float(premium)
    row[f"{side}_n"] += 1
    # Persist periodically rather than on every print — the stream is chatty.
    if (row["call_n"] + row["put_n"]) % 25 == 0:
        _save()


def mark_alerted(ticker: str) -> None:
    """Remember that this ticker fired a targeted alert today (for the 3:30 report)."""
    if not ticker:
        return
    _load(); _roll_day()
    _alerted_tickers.add(ticker.upper())
    _save()


def get_snapshot(ticker: str) -> dict:
    """
    Current day-to-date call/put premium for a ticker.
    Returns {"call","put","total","call_pct","put_pct","ratio","call_n","put_n"}.
    ratio = call premium / put premium (inf-safe; 0 if no puts).
    """
    _load(); _roll_day()
    row = _PREM.get(str(ticker or "").upper(),
                    {"call": 0.0, "put": 0.0, "call_n": 0, "put_n": 0})
    c, p = float(row["call"]), float(row["put"])
    total = c + p
    return {
        "call": c, "put": p, "total": total,
        "call_pct": (c / total * 100.0) if total else 0.0,
        "put_pct":  (p / total * 100.0) if total else 0.0,
        "ratio":    (c / p) if p else (float("inf") if c else 0.0),
        "call_n": row.get("call_n", 0), "put_n": row.get("put_n", 0),
    }


def get_alerted_tickers() -> list:
    _load(); _roll_day()
    return sorted(_alerted_tickers)


def fmt_prem(v: float) -> str:
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"


def format_snapshot(snap: dict, label: str = "Ticker flow today") -> list:
    """Render a snapshot as alert message lines. Empty list if no flow."""
    if not snap or snap["total"] <= 0:
        return []
    ratio = snap["ratio"]
    if ratio == float("inf"):
        ratio_s = "calls only"
    elif ratio == 0:
        ratio_s = "puts only"
    elif ratio >= 1:
        ratio_s = f"{ratio:.1f}:1 calls"
    else:
        ratio_s = f"{(1/ratio):.1f}:1 puts"
    lean = "🟢 call-heavy" if snap["call_pct"] >= 60 else \
           "🔴 put-heavy"  if snap["put_pct"]  >= 60 else "⚪ balanced"
    return [
        f"📊 {label}: {lean} ({ratio_s})",
        f"   puts {fmt_prem(snap['put'])} ({snap['put_pct']:.0f}%, {snap['put_n']} prints) | "
        f"calls {fmt_prem(snap['call'])} ({snap['call_pct']:.0f}%, {snap['call_n']} prints)",
    ]


def build_daily_update(bot_token: str = "", chat_id: str = "") -> str:
    """
    3:30pm report: full-day call/put premium for every ticker that fired a
    targeted alert today. Returns the message text ("" if nothing to report).
    """
    _load(); _roll_day()
    tickers = get_alerted_tickers()
    if not tickers:
        return ""

    now_s = datetime.now(ET).strftime("%-I:%M %p")
    lines = [
        f"📊 DAILY PREMIUM UPDATE — {now_s} ET",
        "━━━ Targeted_Strikes_Expiry put vs call premium, full day ━━━",
        "",
    ]
    rows = []
    for t in tickers:
        s = get_snapshot(t)
        if s["total"] <= 0:
            continue
        rows.append((t, s))
    if not rows:
        return ""

    rows.sort(key=lambda x: -x[1]["total"])
    for t, s in rows:
        lean = "🟢" if s["call_pct"] >= 60 else "🔴" if s["put_pct"] >= 60 else "⚪"
        lines.append(
            f"{lean} ${t}  P {fmt_prem(s['put'])} ({s['put_pct']:.0f}%) / "
            f"C {fmt_prem(s['call'])} ({s['call_pct']:.0f}%)  "
            f"tot {fmt_prem(s['total'])}"
        )
    return "\n".join(lines)


def send_daily_update(send_fn=None) -> None:
    """Scheduler entry point for the 3:30pm update."""
    msg = build_daily_update()
    if not msg:
        print("[TKRPREM] 3:30pm update — no alerted tickers today")
        return
    try:
        if send_fn:
            send_fn(msg)
        else:
            from sms import send_telegram
            bot  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat = (os.environ.get("TELEGRAM_TRADE_CHAT_ID", "") or
                    os.environ.get("TELEGRAM_CHAT_ID", ""))
            if bot and chat:
                send_telegram(msg, bot, chat)
        print("[TKRPREM] 3:30pm daily premium update sent")
    except Exception as e:
        print(f"[TKRPREM] Send error: {e}")
