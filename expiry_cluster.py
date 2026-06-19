"""
expiry_cluster.py — Expiry date clustering detector.

When 4+ different tickers buy the same option expiry date within a session,
that signals an event-driven bet on a specific date (FOMC, earnings cluster,
macro event) rather than individual stock conviction.

Config env vars:
  EXPIRY_CLUSTER_MIN      = 4     distinct tickers required
  EXPIRY_CLUSTER_WINDOW_H = 6.5   rolling session window (hours)
"""
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
EXPIRY_CLUSTER_MIN    = int(os.environ.get("EXPIRY_CLUSTER_MIN", "4"))
EXPIRY_WINDOW_HOURS   = float(os.environ.get("EXPIRY_CLUSTER_WINDOW_H", "6.5"))
STORAGE_KEY           = "expiry_cluster_history"

_EXPIRY: dict = {}   # expiry_date → {tickers: {ticker: fill_info}, last_alerted_count}
_ec_loaded = False


def _load():
    global _EXPIRY, _ec_loaded
    if _ec_loaded:
        return
    try:
        from storage import db_get
        raw = db_get(STORAGE_KEY)
        if raw and isinstance(raw, dict):
            _EXPIRY = raw
    except: pass
    _ec_loaded = True


def _save():
    try:
        from storage import db_set
        db_set(STORAGE_KEY, _EXPIRY)
    except: pass


def _prune():
    cutoff = time.time() - EXPIRY_WINDOW_HOURS * 3600
    for exp in list(_EXPIRY.keys()):
        _EXPIRY[exp]["tickers"] = {
            t: f for t, f in _EXPIRY[exp]["tickers"].items()
            if f.get("ts", 0) >= cutoff
        }
        if not _EXPIRY[exp]["tickers"]:
            del _EXPIRY[exp]


def _fmt_expiry(expiry: str) -> str:
    """Normalise MM/DD/YY to readable date."""
    try:
        parts = expiry.split("/")
        if len(parts) == 3:
            m, d, y = parts
            return f"{m}/{d}/{y}"
    except: pass
    return expiry


def process_expiry(alert: dict, filter_name: str = "") -> dict | None:
    """
    Track fills by expiry date. Fires when EXPIRY_CLUSTER_MIN distinct
    tickers hit the same expiry date within the session window.
    Only counts Big_Money_Order_Flow fills (retail noise excluded).
    """
    _load()
    _prune()

    bm_filter = os.environ.get("CONVICTION_BIG_MONEY_FILTER", "Big_Money_Order_Flow")
    if filter_name != bm_filter:
        return None   # only track big money fills

    ticker     = str(alert.get("ticker","") or "").upper()
    expiry     = str(alert.get("expiry","") or "")
    option_type = str(alert.get("option_type","call") or "call")
    premium    = float(alert.get("premium",0) or 0)
    strike     = str(alert.get("strike","") or "")
    now        = time.time()
    time_str   = datetime.now(ET).strftime("%-I:%M %p")

    if not ticker or not expiry:
        return None

    # Key: expiry + direction (calls and puts tracked separately)
    direction = "call" if "call" in option_type.lower() else "put"
    key       = f"{expiry}_{direction}"

    if key not in _EXPIRY:
        _EXPIRY[key] = {"tickers": {}, "last_alerted_count": 0, "expiry": expiry, "direction": direction}

    _EXPIRY[key]["tickers"][ticker] = {
        "strike": strike, "premium": premium,
        "time":   time_str, "ts":     now,
    }
    _save()

    n_tickers = len(_EXPIRY[key]["tickers"])
    last_alerted = _EXPIRY[key]["last_alerted_count"]

    print(f"[EXPIRY] {expiry} {direction}: {n_tickers} tickers "
          f"({', '.join(sorted(_EXPIRY[key]['tickers'].keys())[:5])})")

    if n_tickers < EXPIRY_CLUSTER_MIN or n_tickers <= last_alerted:
        return None

    _EXPIRY[key]["last_alerted_count"] = n_tickers
    _save()

    tickers     = sorted(_EXPIRY[key]["tickers"].keys())
    total_prem  = sum(f["premium"] for f in _EXPIRY[key]["tickers"].values())
    fills       = [dict(t=t, **_EXPIRY[key]["tickers"][t]) for t in tickers]
    emoji       = "📈" if direction == "call" else "📉"
    sentiment   = "BULLISH" if direction == "call" else "BEARISH"
    prem_s      = f"${total_prem/1_000_000:.1f}M" if total_prem>=1_000_000 else f"${total_prem/1_000:.0f}K"

    print(f"[EXPIRY] 🗓️  Cluster: {expiry} {direction} — "
          f"{n_tickers} tickers | {prem_s}")

    return {
        "expiry":    expiry,
        "direction": direction,
        "sentiment": sentiment,
        "emoji":     emoji,
        "tickers":   tickers,
        "fills":     fills,
        "total_prem": total_prem,
        "prem_s":    prem_s,
        "n_tickers": n_tickers,
    }


def build_expiry_alert(result: dict) -> str:
    expiry    = result["expiry"]
    direction = result["direction"]
    sentiment = result["sentiment"]
    emoji     = result["emoji"]
    tickers   = result["tickers"]
    fills     = result["fills"]
    prem_s    = result["prem_s"]
    n         = result["n_tickers"]

    fill_lines = [
        f"  {emoji} ${f['t']} {f['strike']}{'C' if direction=='call' else 'P'} | "
        f"${f['premium']/1_000:.0f}K | {f['time']}"
        for f in fills
    ]

    return "\n".join([
        f"🗓️  EXPIRY CLUSTER: {_fmt_expiry(expiry)}",
        f"━━━ {emoji} {sentiment} EVENT BET — {n} tickers ━━━",
        f"",
        f"All buying {direction}s expiring {_fmt_expiry(expiry)}:",
    ] + fill_lines + [
        f"",
        f"💵 Total deployed: {prem_s}",
        f"",
        f"💡 {n} names buying same expiry = event-driven play",
        f"   Check: FOMC dates, sector earnings clusters, macro events",
    ])
