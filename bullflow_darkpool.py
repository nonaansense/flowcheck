"""
bullflow_darkpool.py — Real dark pool prints from the Bullflow API.

Replaces the previous "unusual volume" proxy (a Polygon snapshot comparing
today's volume to yesterday's), which inferred institutional activity rather
than observing it. This reads actual off-exchange prints reported through
Nasdaq TRF.

Endpoint: GET /v1/data/darkPoolTrades
  from, to (YYYY-MM-DD, inclusive, max 30 days), ticker, limit (<=5000),
  minNotional, cursor
Rate limit: 10 requests / minute / key — the tightest limit on the Bullflow
API, so results are cached per (ticker, date, minNotional) and the caller is
expected to hit only a handful of tickers per run.

Row fields used here: price, size, notional, pctDayVolume,
percent30DayVolume, sipTimestampMs, tradeDate.
"""
import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BASE = "https://api.bullflow.io"

# Only prints at/above this notional count as institutional-sized.
MIN_NOTIONAL = float(os.environ.get("DARKPOOL_MIN_NOTIONAL", "1000000"))
# Bullflow allows 10 req/min on this endpoint; stay under it.
_CALLS: list = []
_CALL_LIMIT = int(os.environ.get("DARKPOOL_CALLS_PER_MIN", "8"))

_CACHE: dict = {}


def _api_key() -> str:
    return os.environ.get("BULLFLOW_API_KEY", "").strip()


def _throttle():
    """Block until a request slot is free (10/min endpoint limit)."""
    global _CALLS
    while True:
        now = time.time()
        _CALLS = [t for t in _CALLS if now - t < 60]
        if len(_CALLS) < _CALL_LIMIT:
            _CALLS.append(now)
            return
        wait = 61 - (now - _CALLS[0])
        print(f"[DARKPOOL] rate limit ({_CALL_LIMIT}/min) — waiting {wait:.0f}s")
        time.sleep(max(1, min(wait, 62)))


def fetch_dark_pool_trades(ticker: str, date: str = None,
                           min_notional: float = None,
                           max_pages: int = 3) -> list:
    """
    Dark pool prints for one ticker on one date. Returns [] on any failure —
    dark pool data is a scoring input, never load-bearing.

    Paginates via nextCursor up to max_pages (default 3 x 500 = 1500 prints),
    which is far more than enough for a single ticker-day.
    """
    key = _api_key()
    if not key:
        return []
    ticker = str(ticker or "").upper()
    if not ticker:
        return []
    date = date or datetime.now(ET).strftime("%Y-%m-%d")
    min_notional = MIN_NOTIONAL if min_notional is None else min_notional

    ck = f"{ticker}_{date}_{min_notional}"
    if ck in _CACHE:
        return _CACHE[ck]

    rows, cursor = [], None
    for _ in range(max_pages):
        params = {"key": key, "from": date, "to": date, "ticker": ticker,
                  "limit": 500, "minNotional": min_notional}
        if cursor:
            params["cursor"] = cursor
        try:
            _throttle()
            r = requests.get(f"{BASE}/v1/data/darkPoolTrades",
                             params=params, timeout=20)
            if r.status_code != 200:
                print(f"[DARKPOOL] HTTP {r.status_code} for {ticker}: {r.text[:120]}")
                break
            body = r.json() or {}
            rows.extend(body.get("rows") or [])
            if not body.get("hasMore"):
                break
            cursor = body.get("nextCursor")
            if not cursor:
                break
        except Exception as e:
            print(f"[DARKPOOL] error {ticker}: {e}")
            break

    _CACHE[ck] = rows
    return rows


def get_dark_pool_summary(ticker: str, date: str = None,
                          spot: float = 0.0,
                          cutoff_ts: float = 0.0) -> dict:
    """
    Aggregate a ticker's dark pool activity for a day.

    `cutoff_ts` (epoch SECONDS) restricts the aggregate to prints that
    occurred at or before that moment. Backtests MUST pass it — the endpoint
    returns the whole day, so without it a score computed at 11:00am would
    include 3:00pm prints, which is forward-looking. Live callers leave it 0.

    Returns {} when there's no data. Otherwise:
      total_notional, print_count, largest, avg_price, pct_day_volume,
      pct_30d_volume, above_spot, below_spot, lean
    """
    rows = fetch_dark_pool_trades(ticker, date)
    if not rows:
        return {}

    if cutoff_ts:
        cut_ms = cutoff_ts * 1000.0
        rows = [r for r in rows
                if float(r.get("sipTimestampMs", 0) or 0) <= cut_ms]
        if not rows:
            return {}

    total = sum(float(r.get("notional", 0) or 0) for r in rows)
    if total <= 0:
        return {}

    sizes = [float(r.get("notional", 0) or 0) for r in rows]
    prices = [float(r.get("price", 0) or 0) for r in rows if r.get("price")]
    # Notional-weighted average print price.
    wsum = sum(float(r.get("price", 0) or 0) * float(r.get("notional", 0) or 0)
               for r in rows)
    avg_price = (wsum / total) if total else 0.0

    above = below = 0
    if spot:
        for r in rows:
            p = float(r.get("price", 0) or 0)
            if not p:
                continue
            if p > spot:
                above += float(r.get("notional", 0) or 0)
            elif p < spot:
                below += float(r.get("notional", 0) or 0)

    lean = "neutral"
    if above or below:
        share = above / (above + below)
        lean = "accumulation" if share >= 0.6 else \
               "distribution"  if share <= 0.4 else "neutral"

    return {
        "total_notional":  total,
        "print_count":     len(rows),
        "largest":         max(sizes) if sizes else 0.0,
        "avg_price":       avg_price,
        "pct_day_volume":  max((float(r.get("pctDayVolume", 0) or 0)
                                for r in rows), default=0.0),
        "pct_30d_volume":  max((float(r.get("percent30DayVolume", 0) or 0)
                                for r in rows), default=0.0),
        "above_spot":      above,
        "below_spot":      below,
        "lean":            lean,
        "spot":            spot,
    }


def fmt_notional(v: float) -> str:
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.0f}M"
    return f"${v/1_000:.0f}K"


def format_summary(s: dict) -> list:
    """Render a dark pool summary as message lines. [] if empty."""
    if not s or s.get("total_notional", 0) <= 0:
        return []
    emoji = {"accumulation": "🟢", "distribution": "🔴"}.get(s["lean"], "⚪")
    lines = [
        f"🌑 Dark pool: {fmt_notional(s['total_notional'])} across "
        f"{s['print_count']} prints (largest {fmt_notional(s['largest'])})"
    ]
    if s.get("spot") and (s.get("above_spot") or s.get("below_spot")):
        lines.append(
            f"   {emoji} {s['lean']} — {fmt_notional(s['above_spot'])} above / "
            f"{fmt_notional(s['below_spot'])} below ${s['spot']:.2f}"
        )
    return lines

