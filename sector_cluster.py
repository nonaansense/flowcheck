"""
sector_cluster.py — Sector-level flow clustering.

When 4+ tickers from the same sector get options flow in the same session,
that signals a broader sector rotation or event rather than individual
stock stories. Fires a sector cluster alert.

Config env vars:
  SECTOR_CLUSTER_MIN    = 4    (min tickers in sector to trigger)
  SECTOR_WINDOW_HOURS   = 8    (rolling session window)
"""
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SECTOR_CLUSTER_MIN  = int(os.environ.get("SECTOR_CLUSTER_MIN", "4"))
SECTOR_WINDOW_HOURS = float(os.environ.get("SECTOR_WINDOW_HOURS", "8"))
STORAGE_KEY         = "sector_cluster_history"

_SECTORS: dict = {}   # ticker → sector string cache
_FLOWS:   dict = {}   # sector → list of {ticker, ts, premium, direction}
_sc_loaded = False


def _load():
    global _FLOWS, _sc_loaded
    if _sc_loaded:
        return
    try:
        from storage import db_get
        raw = db_get(STORAGE_KEY)
        if raw and isinstance(raw, dict):
            _FLOWS = raw
    except: pass
    _sc_loaded = True


def _save():
    try:
        from storage import db_set
        db_set(STORAGE_KEY, _FLOWS)
    except: pass


def _prune():
    cutoff = time.time() - SECTOR_WINDOW_HOURS * 3600
    for sector in list(_FLOWS.keys()):
        _FLOWS[sector]["flows"] = [f for f in _FLOWS[sector]["flows"] if f["ts"] >= cutoff]
        if not _FLOWS[sector]["flows"]:
            del _FLOWS[sector]


def _get_sector(ticker: str) -> str | None:
    """Fetch ticker sector from Finnhub company profile (cached per session)."""
    if ticker in _SECTORS:
        return _SECTORS[ticker]
    try:
        from fetcher import fh_get
        profile = fh_get("/stock/profile2", {"symbol": ticker})
        if profile and profile.get("finnhubIndustry"):
            sector = profile["finnhubIndustry"]
            _SECTORS[ticker] = sector
            return sector
        if profile and profile.get("gsector"):
            sector = profile["gsector"]
            _SECTORS[ticker] = sector
            return sector
    except: pass
    return None


def process_sector(alert: dict) -> dict | None:
    """
    Track sector flow. Returns a cluster result when SECTOR_CLUSTER_MIN
    distinct tickers from the same sector fire within the window.
    Only fires once per sector per window, then on each new distinct ticker.
    """
    _load()
    _prune()

    ticker    = str(alert.get("ticker","") or "").upper()
    direction = "call" if "call" in str(alert.get("option_type","call")).lower() else "put"
    premium   = float(alert.get("premium",0) or 0)
    now       = time.time()

    if not ticker:
        return None

    sector = _get_sector(ticker)
    if not sector:
        return None

    if sector not in _FLOWS:
        _FLOWS[sector] = {"flows": [], "last_alerted_count": 0}

    # Check if this is a genuinely new ticker for this sector/direction combo
    existing_tickers = {f["ticker"] for f in _FLOWS[sector]["flows"]
                        if f["direction"] == direction}
    is_new_ticker    = ticker not in existing_tickers

    _FLOWS[sector]["flows"].append({
        "ticker": ticker, "direction": direction,
        "premium": premium, "ts": now,
        "time": datetime.now(ET).strftime("%-I:%M %p"),
    })
    _save()

    if not is_new_ticker:
        return None

    # Count distinct tickers per direction
    call_tickers = {f["ticker"] for f in _FLOWS[sector]["flows"] if f["direction"] == "call"}
    put_tickers  = {f["ticker"] for f in _FLOWS[sector]["flows"] if f["direction"] == "put"}

    for dir_tickers, dir_name in [(call_tickers,"call"),(put_tickers,"put")]:
        if len(dir_tickers) < SECTOR_CLUSTER_MIN:
            continue
        # Only fire if new ticker count exceeds prior alert count
        prior = _FLOWS[sector].get(f"alerted_{dir_name}", 0)
        if len(dir_tickers) <= prior:
            continue

        _FLOWS[sector][f"alerted_{dir_name}"] = len(dir_tickers)
        _save()

        tickers = sorted(dir_tickers)
        total   = sum(f["premium"] for f in _FLOWS[sector]["flows"]
                      if f["direction"] == dir_name)
        tot_s   = f"${total/1_000_000:.1f}M" if total>=1_000_000 else f"${total/1_000:.0f}K"
        emoji   = "📈" if dir_name=="call" else "📉"
        sentiment = "BULLISH" if dir_name=="call" else "BEARISH"

        print(f"[SECTOR] 🌐 Cluster: {sector} {sentiment} — "
              f"{len(tickers)} tickers: {tickers}")

        return {
            "sector":    sector,
            "direction": dir_name,
            "sentiment": sentiment,
            "emoji":     emoji,
            "tickers":   tickers,
            "total":     total,
            "tot_s":     tot_s,
            "flows":     [f for f in _FLOWS[sector]["flows"] if f["direction"]==dir_name],
        }

    print(f"[SECTOR] {sector} {direction}: "
          f"{len(existing_tickers)+1}/{SECTOR_CLUSTER_MIN} tickers — building")
    return None


def build_sector_alert(result: dict) -> str:
    ticker_list  = ", ".join(f"${t}" for t in result["tickers"])
    flow_lines   = [f"  {f['time']} ${f['ticker']} — ${f['premium']/1_000:.0f}K"
                    for f in result["flows"]]
    return "\n".join([
        f"🌐 SECTOR CLUSTER: {result['sector']}",
        f"━━━ {result['emoji']} {result['sentiment']} ROTATION SIGNAL ━━━",
        f"",
        f"{len(result['tickers'])} tickers with {result['direction']} flow: {ticker_list}",
        f"Total premium: {result['tot_s']}",
        f"",
        f"Flow activity:",
    ] + flow_lines + [
        f"",
        f"💡 Multiple names in same sector buying {result['direction']}s "
        f"= possible sector rotation, not just stock-specific",
    ])
