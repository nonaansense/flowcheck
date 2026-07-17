"""
alert_toggles.py — Per-alert-type enable/disable toggles.

Persisted to Supabase (key: alert_toggles) so changes survive redeploys.
Defaults to all ON. Changed via /alert command in Telegram.

Alert types:
  trade       — Main FlowCheck TRADE score alerts (Bullflow scored ≥6/7)
  tape        — Tape watcher Rule A (intraday BM + retail) and Rule B (multi-day BM)
  conviction  — Cross-filter conviction (BM + retail threshold met)
  bm_auto     — Big money auto-conviction (same contract 2+ days, no retail needed)
  double      — Double confirmation escalation (tape + conviction both fired)
  cluster     — Ticker cluster (multi-contract sweep on same ticker)
  targeted_strikes — Targeted strikes (4+ calls or puts of same strike/expiry in sequence)
  straddle    — Straddle/strangle detection (balanced call + put flow)
  darkpool    — Dark pool print alerts
  sector      — Sector cluster (4+ tickers same sector same direction)
  expiry      — Expiry date cluster (4+ tickers same expiry date)
  reminder    — Entry reminders (10-min follow-up after tape/conviction)
  priceaction — Price action warnings (stock -1% within 5min of flow alert)
  eod         — Tape watcher EOD daily summary
"""
import os

STORAGE_KEY   = "alert_toggles"
ALL_TYPES = [
    "trade", "tape", "conviction", "bm_auto", "double",
    "cluster", "straddle", "darkpool", "sector", "expiry",
    "reminder", "priceaction", "eod", "spx_block", "pair_flow", "repeat_calls", "targeted_strikes", "technical", "swing", "bullflow_preset",
    "top_setups", "trailing_stop", "gex_monitor", "exit_signals",
    "premarket_summary", "premarket_gap", "position_check",
    "daily_pnl", "weekly_report", "theta_calendar", "spy_gex_snapshot",
]
LABELS = {
    "trade":       "🅱 Main FlowCheck TRADE alerts",
    "tape":        "🎬 Tape watcher (Rule A + Rule B)",
    "conviction":  "🔥 Cross-filter conviction",
    "bm_auto":     "🗓️  BM auto-conviction (multi-day same contract)",
    "double":      "🔥🔥 Double confirmation escalation",
    "cluster":     "🌊 Ticker cluster (multi-contract sweep — can fire on retail only)",
    "straddle":    "⚖️  Straddle / strangle detection",
    "darkpool":    "🌑 Dark pool prints",
    "sector":      "🌐 Sector clustering",
    "expiry":      "🗓️  Expiry date clustering",
    "reminder":    "⏰ Entry reminders (10-min follow-up)",
    "priceaction": "⚠️  Price action warnings (5-min reversal)",
    "eod":         "📋 Tape EOD daily summary",
    "spx_block":   "🔄 SPX block trade repeat alerts (SPX plays channel)",
    "pair_flow":   "🔥 Pair flow rapid accumulation (3+ calls or puts in 5min)",
    "repeat_calls": "🔁 Repeat flow activity ratio (calls + optional puts)",
    "targeted_strikes": "🎯 Targeted strikes (N+ same strike/expiry in sequence, default 4)",
    "technical":    "📡 Technical confirmation (STRONG multi-TF + GEX aligned)",
    "swing":        "🎯 3:45pm top-5 swing play ranking",
    "bullflow_preset": "🔔 Bullflow pre-defined alerts (Sweep/Repeater/Grenade etc.)",
    "top_setups":       "🌅 Morning top setups briefing (9:45am)",
    "trailing_stop":    "🛑 Trailing stop alerts",
    "gex_monitor":      "⚡ Intraday GEX monitor alerts",
    "exit_signals":     "🚪 Exit signal alerts",
    "premarket_summary":"🌄 Pre-market + EOD summaries",
    "premarket_gap":    "📊 Pre-market gap alerts (7:30am)",
    "position_check":   "📋 Midday position check",
    "daily_pnl":        "💰 Daily P&L summary",
    "weekly_report":    "📈 Weekly performance report",
    "theta_calendar":   "🗓️ Theta decay calendar",
    "spy_gex_snapshot": "🎯 SPY GEX snapshots (10am/1pm)",
}

# Module-level state — loaded once per process, updated on change
_toggles: dict = {t: True for t in ALL_TYPES}
_loaded:  bool = False


def _load():
    global _toggles, _loaded
    if _loaded:
        return
    try:
        from storage import db_get
        raw = db_get(STORAGE_KEY)
        if raw and isinstance(raw, dict):
            for t in ALL_TYPES:
                if t in raw:
                    _toggles[t] = bool(raw[t])
    except Exception as e:
        print(f"[TOGGLES] Load error: {e}")
    _loaded = True


def _save():
    try:
        from storage import db_set
        db_set(STORAGE_KEY, _toggles)
    except Exception as e:
        print(f"[TOGGLES] Save error: {e}")


def is_enabled(alert_type: str) -> bool:
    """Return True if the alert type is enabled. Defaults to True."""
    _load()
    return _toggles.get(alert_type, True)


def set_toggle(alert_type: str, enabled: bool) -> bool:
    """Enable or disable an alert type. Returns True on success."""
    _load()
    if alert_type not in ALL_TYPES:
        return False
    _toggles[alert_type] = enabled
    _save()
    print(f"[TOGGLES] {alert_type}: {'ON' if enabled else 'OFF'}")
    return True


def set_all(enabled: bool):
    """Enable or disable all alert types at once."""
    _load()
    for t in ALL_TYPES:
        _toggles[t] = enabled
    _save()
    print(f"[TOGGLES] All alerts: {'ON' if enabled else 'OFF'}")


def status_message() -> str:
    """Return a formatted Telegram message showing all toggle states."""
    _load()
    lines = ["📡 Alert Types — TRADE Channel", ""]
    for t in ALL_TYPES:
        state = "✅ ON " if _toggles.get(t, True) else "⏸️ OFF"
        lines.append(f"{state}  {LABELS.get(t, t)}  `{t}`")
    lines += [
        "",
        "Toggle: /alert off tape  |  /alert on tape",
        "Quiet:  /alert off all   |  /alert on all",
    ]
    return "\n".join(lines)
