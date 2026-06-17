"""
ticker_cluster.py — Broad ticker-level call/put cluster detector.

Complements tape_watcher.py (which catches exact-match repeat buying on
the SAME strike+expiry). This module catches the broader pattern: multiple
DIFFERENT strikes/expiries on the same ticker, same direction (calls or
puts), accumulating within a rolling window — e.g. someone building a
position across the chain rather than betting on one contract.

Example: 4 different HOOD call strikes/expiries bought within 3 hours,
$1.2M combined, even though no single contract repeated.
"""
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

CLUSTER_STORAGE_KEY      = "ticker_cluster_history"
CLUSTER_WINDOW_HOURS     = float(os.environ.get("CLUSTER_WINDOW_HOURS", "4"))
CLUSTER_MIN_DISTINCT     = int(os.environ.get("CLUSTER_MIN_DISTINCT", "3"))
CLUSTER_MIN_TOTAL_PREMIUM = float(os.environ.get("CLUSTER_MIN_TOTAL_PREMIUM", "300000"))

_CLUSTERS: dict = {}
_loaded = False


def _load_clusters():
    global _CLUSTERS, _loaded
    if _loaded:
        return
    try:
        from storage import db_get
        raw = db_get(CLUSTER_STORAGE_KEY)
        if raw and isinstance(raw, dict):
            _CLUSTERS = raw
            print(f"[CLUSTER] Loaded {len(_CLUSTERS)} ticker clusters from Supabase")
    except Exception as e:
        print(f"[CLUSTER] Load error: {e} — starting fresh")
    _loaded = True


def _save_clusters():
    try:
        from storage import db_set
        db_set(CLUSTER_STORAGE_KEY, _CLUSTERS)
    except Exception as e:
        print(f"[CLUSTER] Save error: {e}")


def _cluster_key(ticker: str, option_type: str) -> str:
    direction = "call" if "call" in option_type.lower() else "put"
    return f"{ticker.upper()}_{direction}"


def _contract_key(strike: str, expiry: str) -> str:
    return f"{strike}_{expiry}"


def _prune_stale():
    now    = time.time()
    cutoff = now - CLUSTER_WINDOW_HOURS * 3600
    to_del = []
    for key, cluster in _CLUSTERS.items():
        cluster["fills"] = [f for f in cluster["fills"] if f["ts"] >= cutoff]
        if not cluster["fills"]:
            to_del.append(key)
    for k in to_del:
        del _CLUSTERS[k]


def process_cluster(alert: dict) -> dict | None:
    """
    Process a single flow fill through the ticker-level cluster detector.
    Returns alert dict when a NEW distinct contract joins a qualifying
    cluster (>= CLUSTER_MIN_DISTINCT distinct strikes/expiries, same
    direction, within the rolling window). Returns None otherwise.
    """
    _load_clusters()
    _prune_stale()

    ticker      = str(alert.get("ticker", "") or "").upper()
    strike      = str(alert.get("strike", "") or "")
    expiry      = str(alert.get("expiry", "") or "")
    option_type = str(alert.get("option_type", "call") or "call")
    trade_px    = float(alert.get("option_price", 0) or 0)
    premium     = float(alert.get("premium", 0) or 0)
    fill_type   = str(alert.get("fill_type", "") or "")
    is_sweep    = bool(alert.get("is_sweep") or False)
    stock_px    = float(alert.get("stock_price", 0) or 0)
    otm_pct     = float(alert.get("otm_pct", 0) or 0)
    dte         = int(alert.get("dte", 0) or 0)
    now         = time.time()

    if not ticker or not strike or not expiry:
        return None

    key      = _cluster_key(ticker, option_type)
    ckey     = _contract_key(strike, expiry)
    today_str = datetime.now(ET).strftime("%b %-d")
    time_str  = datetime.now(ET).strftime("%-I:%M %p")

    if key not in _CLUSTERS:
        _CLUSTERS[key] = {
            "ticker": ticker, "option_type": option_type,
            "fills": [], "alerted_contracts": [],
        }

    cluster = _CLUSTERS[key]
    is_new_contract = not any(f["contract_key"] == ckey for f in cluster["fills"])

    cluster["fills"].append({
        "contract_key": ckey, "strike": strike, "expiry": expiry,
        "price": trade_px, "premium": premium, "fill": fill_type,
        "sweep": is_sweep, "stock_px": stock_px, "otm_pct": otm_pct,
        "dte": dte, "ts": now, "date": today_str, "time": time_str,
    })
    _save_clusters()

    distinct_contracts = list({f["contract_key"] for f in cluster["fills"]})
    distinct_count      = len(distinct_contracts)
    total_premium       = sum(f["premium"] for f in cluster["fills"])

    qualifies = (distinct_count >= CLUSTER_MIN_DISTINCT and
                 total_premium >= CLUSTER_MIN_TOTAL_PREMIUM)

    # Only fire when a genuinely NEW contract joins a qualifying cluster —
    # avoids re-alerting on repeat fills of a contract already counted
    # (that's tape_watcher's job).
    if not (qualifies and is_new_contract):
        if is_new_contract:
            print(f"[CLUSTER] {ticker} {option_type}: {distinct_count} distinct "
                  f"contracts, ${total_premium/1000:.0f}K — building "
                  f"(need {CLUSTER_MIN_DISTINCT})")
        return None

    cluster["alerted_contracts"].append(ckey)
    print(f"[CLUSTER] 🌊 Cluster confirmed: {ticker} {option_type} — "
          f"{distinct_count} distinct contracts, ${total_premium/1000:.0f}K")

    return {
        "ticker":         ticker,
        "option_type":    option_type,
        "fills":          list(cluster["fills"]),
        "distinct_count": distinct_count,
        "total_premium":  total_premium,
        "stock_px":       stock_px or next(
            (f["stock_px"] for f in reversed(cluster["fills"]) if f.get("stock_px")), 0),
        "otm_pct":        otm_pct,
        "dte":            dte,
        "earnings_str":   alert.get("earnings_str"),
        "iv_pct":         alert.get("iv_pct"),
        "iv_rank":        alert.get("iv_rank"),
        "news":           alert.get("news", []),
    }


def build_cluster_alert(result: dict, alert_name: str) -> str:
    """Build the Telegram alert message for a confirmed ticker cluster."""
    ticker      = result["ticker"]
    fills       = result["fills"]
    distinct    = result["distinct_count"]
    total       = result["total_premium"]
    stock_px    = result["stock_px"]
    dte         = result["dte"]
    earn_str    = result.get("earnings_str")
    iv_pct      = result.get("iv_pct")
    iv_rank     = result.get("iv_rank")
    news        = result.get("news", [])
    base_url    = os.environ.get("BASE_URL",
                  "https://web-production-19e44.up.railway.app").rstrip("/")

    direction = "CALL" if "call" in result.get("option_type","call").lower() else "PUT"
    otype_lbl = "C" if direction == "CALL" else "P"
    dir_emoji = "📈" if direction == "CALL" else "📉"

    tot_str = (f"${total/1_000_000:.1f}M" if total >= 1_000_000
               else f"${total/1_000:.0f}K")

    # Time span
    first_ts = min(f["ts"] for f in fills)
    last_ts  = max(f["ts"] for f in fills)
    span_hr  = (last_ts - first_ts) / 3600

    fill_lines = []
    for i, f in enumerate(fills, 1):
        prem_str = (f"${f['premium']/1_000_000:.1f}M" if f['premium'] >= 1_000_000
                    else f"${f['premium']/1_000:.0f}K")
        sweep_str = " ⚡" if f.get("sweep") else ""
        fill_lines.append(
            f"  {i}. {f['strike']}{otype_lbl} {f['expiry']} | "
            f"${f['price']:.2f} | {prem_str}{sweep_str} | {f['time']}"
        )

    ctx_parts = []
    if stock_px: ctx_parts.append(f"${stock_px:.2f}")
    if dte:      ctx_parts.append(f"{dte}d DTE (latest)")
    ctx_line = " | ".join(ctx_parts) if ctx_parts else "—"

    earn_line = f"📅 Earnings: {earn_str}" if earn_str else "📅 Earnings: unknown"

    iv_line = None
    if iv_pct and iv_rank is not None:
        from tape_watcher import _ordinal
        iv_bar  = "█" * int(iv_rank / 10) + "░" * (10 - int(iv_rank / 10))
        iv_line = f"📊 IV: {iv_pct:.1f}% | Rank {_ordinal(iv_rank)} [{iv_bar}]"
    elif iv_pct:
        iv_line = f"📊 IV: {iv_pct:.1f}% (rank building)"

    # Analysis link
    analysis_link = f"{base_url}/watchlist"
    try:
        from technical import get_watchlist
        wl = get_watchlist()
        entry = wl.get(ticker.upper(), {}) if isinstance(wl, dict) else {}
        aid = str(entry.get("analysis_id","") or "")
        if aid and aid != "0":
            analysis_link = f"{base_url}/analysis/{aid}"
    except:
        pass

    lines = [
        f"🌊 {alert_name} — TICKER CLUSTER",
        f"━━━ BROAD {direction} ACCUMULATION ━━━",
        f"{dir_emoji} ${ticker} — {distinct} distinct strikes/expiries in "
        f"{span_hr:.1f}h | {tot_str} total",
        f"",
        f"📊 ALL FILLS:",
    ] + fill_lines + [
        f"",
        f"Stock: {ctx_line}",
        earn_line,
    ]
    if iv_line:
        lines.append(iv_line)

    if news:
        lines.append("")
        lines.append("📰 Recent news:")
        for art in news[:3]:
            hl  = (art.get("headline","") or "")[:70]
            src = art.get("source","") or ""
            url = art.get("url","") or ""
            age_h = int((time.time() - art.get("datetime",0)) / 3600)
            age_str = f"{age_h}h ago" if age_h < 24 else f"{age_h//24}d ago"
            lines.append(f"  • {hl} ({src}, {age_str})")
            if url:
                lines.append(f"    {url}")

    lines += [
        f"",
        f"💡 Multiple strikes bought, same direction — possible "
        f"institutional positioning across the chain",
        f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
        f"📋 Full analysis → {analysis_link}",
    ]

    return "\n".join(lines)
