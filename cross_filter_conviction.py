"""
cross_filter_conviction.py — Cross-filter conviction detector.

Thesis: big money enters first, retail follows. A trade has high conviction
when both filters confirm the same ticker + direction within a rolling window.

Required for alert:
  - BIG_MONEY_MIN (default 1) fills from "Big_Money_Order_Flow" filter
  - RETAIL_MIN    (default 2) fills from "Retail_Order_Flow" filter

Both buckets tracked separately per ticker+direction, persisted to Supabase.

Configurable via env vars:
  CONVICTION_BIG_MONEY_MIN      = 1       min big money fills required
  CONVICTION_RETAIL_MIN         = 2       min retail fills required
  CONVICTION_WINDOW_HOURS       = 8       rolling window for both filters
  CONVICTION_BIG_MONEY_FILTER   = Big_Money_Order_Flow
  CONVICTION_RETAIL_FILTER      = Retail_Order_Flow
  CONVICTION_RETAIL_MIN_PREMIUM = 25000   retail lower bound (inclusive)
  CONVICTION_RETAIL_MAX_PREMIUM = 500000  retail upper bound (exclusive)
"""
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ── Config ──────────────────────────────────────────────────────────────
BIG_MONEY_MIN      = int(os.environ.get("CONVICTION_BIG_MONEY_MIN", "1"))
RETAIL_MIN         = int(os.environ.get("CONVICTION_RETAIL_MIN", "2"))
WINDOW_HOURS       = float(os.environ.get("CONVICTION_WINDOW_HOURS", "8"))
BIG_MONEY_FILTER   = os.environ.get("CONVICTION_BIG_MONEY_FILTER",
                                     "Big_Money_Order_Flow")
RETAIL_FILTER      = os.environ.get("CONVICTION_RETAIL_FILTER",
                                     "Retail_Order_Flow")
RETAIL_MIN_PREMIUM = float(os.environ.get("CONVICTION_RETAIL_MIN_PREMIUM", "25000"))
RETAIL_MAX_PREMIUM = float(os.environ.get("CONVICTION_RETAIL_MAX_PREMIUM", "500000"))

STORAGE_KEY = "cross_filter_conviction"

_STATE: dict = {}
_loaded = False


def _load():
    global _STATE, _loaded
    if _loaded:
        return
    try:
        from storage import db_get
        raw = db_get(STORAGE_KEY)
        if raw and isinstance(raw, dict):
            _STATE = raw
            print(f"[CONVICTION] Loaded {len(_STATE)} cross-filter entries from Supabase")
    except Exception as e:
        print(f"[CONVICTION] Load error: {e} — starting fresh")
    _loaded = True


def _save():
    try:
        from storage import db_set
        db_set(STORAGE_KEY, _STATE)
    except Exception as e:
        print(f"[CONVICTION] Save error: {e}")


def _key(ticker: str, direction: str) -> str:
    d = "call" if "call" in direction.lower() else "put"
    return f"{ticker.upper()}_{d}"


def _prune():
    """Remove fills older than the rolling window."""
    cutoff = time.time() - WINDOW_HOURS * 3600
    for key in list(_STATE.keys()):
        entry = _STATE[key]
        entry["big_money"] = [f for f in entry["big_money"] if f["ts"] >= cutoff]
        entry["retail"]    = [f for f in entry["retail"]    if f["ts"] >= cutoff]
        if not entry["big_money"] and not entry["retail"]:
            del _STATE[key]


def _fmt_prem(p: float) -> str:
    return f"${p/1_000_000:.1f}M" if p >= 1_000_000 else f"${p/1_000:.0f}K"


def process_conviction(alert: dict, alert_name: str) -> dict | None:
    """
    Process a single fill through the cross-filter conviction detector.

    alert_name determines whether this is a big-money or retail fill.
    Returns a conviction dict when thresholds are first crossed, else None.
    """
    _load()
    _prune()

    ticker    = str(alert.get("ticker", "") or "").upper()
    strike    = str(alert.get("strike", "") or "")
    expiry    = str(alert.get("expiry", "") or "")
    opt_type  = str(alert.get("option_type", "call") or "call")
    premium   = float(alert.get("premium", 0) or 0)
    opt_price = float(alert.get("option_price", 0) or 0)
    stock_px  = float(alert.get("stock_price", 0) or 0)
    otm_pct   = float(alert.get("otm_pct", 0) or 0)
    dte       = int(alert.get("dte", 0) or 0)
    is_sweep  = bool(alert.get("is_sweep") or False)
    now       = time.time()
    time_str  = datetime.now(ET).strftime("%-I:%M %p")

    if not ticker:
        return None

    key = _key(ticker, opt_type)
    direction = "call" if "call" in opt_type.lower() else "put"

    # Classify fill source
    is_big_money = (alert_name == BIG_MONEY_FILTER)
    is_retail    = (alert_name == RETAIL_FILTER and
                    RETAIL_MIN_PREMIUM <= premium < RETAIL_MAX_PREMIUM)

    if not is_big_money and not is_retail:
        return None  # not from a tracked filter

    if key not in _STATE:
        _STATE[key] = {
            "ticker": ticker, "direction": direction,
            "big_money": [], "retail": [],
            "last_alerted_ts": 0,
        }

    fill = {
        "strike": strike, "expiry": expiry,
        "price": opt_price, "premium": premium,
        "stock_px": stock_px, "otm_pct": otm_pct, "dte": dte,
        "sweep": is_sweep, "time": time_str, "ts": now,
        "filter": "big_money" if is_big_money else "retail",
    }

    entry = _STATE[key]
    if is_big_money:
        entry["big_money"].append(fill)
        print(f"[CONVICTION] 💰 Big money: {ticker} {direction} "
              f"{strike} {expiry} {_fmt_prem(premium)}")
    else:
        entry["retail"].append(fill)
        print(f"[CONVICTION] 📊 Retail: {ticker} {direction} "
              f"{strike} {expiry} {_fmt_prem(premium)}")

    _save()

    bm_count  = len(entry["big_money"])
    ret_count = len(entry["retail"])
    qualifies = bm_count >= BIG_MONEY_MIN and ret_count >= RETAIL_MIN

    if not qualifies:
        print(f"[CONVICTION] {ticker} {direction}: {bm_count}/{BIG_MONEY_MIN} "
              f"big money, {ret_count}/{RETAIL_MIN} retail — building")
        return None

    # Re-alert if new big money comes in after already alerted
    new_bm = (is_big_money and
               entry.get("last_alerted_ts", 0) > 0 and
               fill["ts"] > entry.get("last_alerted_ts", 0))
    already_alerted = (entry.get("last_alerted_ts", 0) > 0 and not new_bm)
    if already_alerted:
        return None

    entry["last_alerted_ts"] = now
    _save()

    total_bm  = sum(f["premium"] for f in entry["big_money"])
    total_ret = sum(f["premium"] for f in entry["retail"])
    total_all = total_bm + total_ret
    bm_pct    = total_bm / total_all * 100 if total_all else 0

    # Net sentiment
    sentiment = "BULLISH" if direction == "call" else "BEARISH"
    sent_emoji = "📈" if direction == "call" else "📉"

    print(f"[CONVICTION] 🔥 CONFIRMED: {ticker} {direction} — "
          f"{bm_count} big money + {ret_count} retail | "
          f"{_fmt_prem(total_all)} total | {sentiment}")

    # Best stock price from any fill
    all_fills = entry["big_money"] + entry["retail"]
    best_stock = next((f["stock_px"] for f in reversed(all_fills)
                       if f.get("stock_px")), 0)

    return {
        "ticker":     ticker,
        "direction":  direction,
        "sentiment":  sentiment,
        "sent_emoji": sent_emoji,
        "big_money":  list(entry["big_money"]),
        "retail":     list(entry["retail"]),
        "bm_count":   bm_count,
        "ret_count":  ret_count,
        "total_bm":   total_bm,
        "total_ret":  total_ret,
        "total_all":  total_all,
        "bm_pct":     bm_pct,
        "stock_px":   best_stock,
        "new_bm":     new_bm,
        "earnings_str": alert.get("earnings_str"),
        "stn_note":   alert.get("stn_note"),
        "ipo_note":   alert.get("ipo_note"),
        "news":       alert.get("news", []),
    }


def build_conviction_alert(result: dict) -> str:
    """Build the Telegram alert for a cross-filter conviction signal."""
    ticker    = result["ticker"]
    direction = result["direction"]
    sentiment = result["sentiment"]
    emoji     = result["sent_emoji"]
    bm_fills  = result["big_money"]
    ret_fills = result["retail"]
    total_bm  = result["total_bm"]
    total_ret = result["total_ret"]
    total_all = result["total_all"]
    bm_pct    = result["bm_pct"]
    stock_px  = result["stock_px"]
    new_bm    = result["new_bm"]
    earn_str  = result.get("earnings_str")
    stn_note  = result.get("stn_note")
    ipo_note  = result.get("ipo_note")
    news      = result.get("news", [])
    base_url  = os.environ.get("BASE_URL",
                "https://web-production-19e44.up.railway.app").rstrip("/")

    otype = "C" if direction == "call" else "P"

    header = "🔥 NEW BIG MONEY" if new_bm else "🔥 CROSS-FILTER CONVICTION"

    def _fmt(p): return f"${p/1_000_000:.1f}M" if p >= 1_000_000 else f"${p/1_000:.0f}K"
    def _fill_line(f, label):
        sweep = " ⚡" if f.get("sweep") else ""
        return (f"  [{label}] {f['strike']}{otype} {f['expiry']} | "
                f"${f['price']:.2f} | {_fmt(f['premium'])}{sweep} | {f['time']}")

    bm_lines  = [_fill_line(f, "BIG $") for f in bm_fills]
    ret_lines = [_fill_line(f, "RETAIL") for f in ret_fills]

    # Net skew bar
    bm_blocks  = int(bm_pct / 10)
    ret_blocks = 10 - bm_blocks
    skew_bar   = "█" * bm_blocks + "░" * ret_blocks

    # Analysis link
    analysis_link = f"{base_url}/watchlist"
    try:
        from technical import get_watchlist
        wl    = get_watchlist()
        entry = wl.get(ticker.upper(), {}) if isinstance(wl, dict) else {}
        aid   = str(entry.get("analysis_id", "") or "")
        if aid and aid != "0":
            analysis_link = f"{base_url}/analysis/{aid}"
    except:
        pass

    lines = [
        f"{header}",
        f"━━━ {emoji} {sentiment} CONVICTION: ${ticker} ━━━",
        f"",
        f"💰 BIG MONEY ({len(bm_fills)} fill{'s' if len(bm_fills)>1 else ''} | {_fmt(total_bm)}):",
    ] + bm_lines + [
        f"",
        f"📊 RETAIL FOLLOW ({len(ret_fills)} fill{'s' if len(ret_fills)>1 else ''} | {_fmt(total_ret)}):",
    ] + ret_lines + [
        f"",
        f"💵 Total deployed: {_fmt(total_all)}",
        f"⚖️  Flow skew: {bm_pct:.0f}% big money [{skew_bar}]",
        f"Stock: ${stock_px:.2f}" if stock_px else "",
    ]

    if earn_str:
        lines.append(f"📅 Earnings: {earn_str}")
    if stn_note:
        lines.append(stn_note)
    if ipo_note:
        lines.append(ipo_note)

    if news:
        lines.append("")
        lines.append("📰 Recent news:")
        for art in news[:3]:
            hl  = (art.get("headline", "") or "")[:70]
            src = art.get("source", "") or ""
            url = art.get("url", "") or ""
            age_h = int((time.time() - art.get("datetime", 0)) / 3600)
            age_s = f"{age_h}h ago" if age_h < 24 else f"{age_h//24}d ago"
            lines.append(f"  • {hl} ({src}, {age_s})")
            if url:
                lines.append(f"    {url}")

    lines += [
        f"",
        f"💡 Big money entered → retail confirmed = high conviction {direction} play",
        f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
        f"📋 Full analysis → {analysis_link}",
    ]

    return "\n".join(l for l in lines if l is not None)
