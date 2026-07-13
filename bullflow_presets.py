"""
bullflow_presets.py — Bullflow pre-defined alert relay.

Relays Bullflow's own built-in alert types straight to Telegram, filtered
to high-conviction, near-dated flow. Unlike the other trackers, these
are Bullflow's canned signals — we don't recompute anything, we surface
the ones that clear the premium + DTE bar and enrich them with the
contract details, earnings date, and a chart link.

Tracked preset types (BULLFLOW_PRESET_TYPES, comma-separated):
  Discord Trade, Sizable Sweep, Urgent Repeater, Grenade Trade,
  Bullflow Repeater, Position Building Repeater

Filters:
  BULLFLOW_PRESET_MIN_PREMIUM = 500000   only >= $500K total premium
  BULLFLOW_PRESET_MAX_DTE     = 14       only expiring within 14 days

Config env vars:
  BULLFLOW_PRESET_TYPES        (defaults to the six types above)
  BULLFLOW_PRESET_MIN_PREMIUM  = 500000
  BULLFLOW_PRESET_MAX_DTE      = 14
"""
import os
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _fetch_tradier_price(ticker: str) -> float:
    """
    Live last price from Tradier /markets/quotes (free tier).
    Used as a fallback when the fill payload has no stockPrice.
    Returns 0.0 on any failure.
    """
    token = os.environ.get("TRADIER_TOKEN", "")
    if not token or not ticker:
        return 0.0
    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/quotes",
            params={"symbols": ticker.upper(), "greeks": "false"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=6,
        )
        if r.status_code == 200:
            q = (r.json().get("quotes") or {}).get("quote")
            if isinstance(q, list):
                q = q[0] if q else None
            if q:
                # last trade, then close, then midpoint of bid/ask
                px = q.get("last") or q.get("close")
                if not px:
                    bid, ask = q.get("bid") or 0, q.get("ask") or 0
                    px = (bid + ask) / 2 if (bid and ask) else 0
                return float(px or 0)
    except Exception as e:
        print(f"[PRESET] Tradier quote error {ticker}: {e}")
    return 0.0

_DEFAULT_TYPES = ("Discord Trade,Sizable Sweep,Urgent Repeater,"
                  "Grenade Trade,Bullflow Repeater,Position Building Repeater")

PRESET_TYPES = [t.strip() for t in
                os.environ.get("BULLFLOW_PRESET_TYPES", _DEFAULT_TYPES).split(",")
                if t.strip()]
MIN_PREMIUM  = float(os.environ.get("BULLFLOW_PRESET_MIN_PREMIUM", "500000"))
MAX_DTE      = int(os.environ.get("BULLFLOW_PRESET_MAX_DTE", "14"))

# ATM band: strike within this fraction of stock price counts as ATM (0.5%)
ATM_BAND_PCT       = float(os.environ.get("BULLFLOW_PRESET_ATM_BAND_PCT", "0.005"))
# Entry = this fraction below the flow trade price (20% → 0.20)
ENTRY_DISCOUNT_PCT = float(os.environ.get("BULLFLOW_PRESET_ENTRY_DISCOUNT_PCT", "0.20"))
# Trailing-stop offset = this fraction of the flow trade price (75% → 0.75)
TRAIL_OFFSET_PCT   = float(os.environ.get("BULLFLOW_PRESET_TRAIL_OFFSET_PCT", "0.75"))
# Profit targets, as a fraction ABOVE entry.
#   T1 = 1.01 → +101% (entry x 2.01)   T2 = 2.01 → +201% (entry x 3.01)
TARGET1_PCT        = float(os.environ.get("BULLFLOW_PRESET_TARGET1_PCT", "1.01"))
TARGET2_PCT        = float(os.environ.get("BULLFLOW_PRESET_TARGET2_PCT", "2.01"))
# Whether to show ITM alerts. Set false to suppress in-the-money contracts
# (some traders only want OTM/ATM directional bets, not ITM).
SHOW_ITM = os.environ.get("BULLFLOW_PRESET_SHOW_ITM", "true").lower() not in ("false","0","no","off")

# Alerts before this ET hour are flagged for reversal risk (10.5 = 10:30am).
# Early-session flow often fades once the opening range resolves.
EARLY_CUTOFF_HOUR = float(os.environ.get("BULLFLOW_PRESET_EARLY_CUTOFF_HOUR", "10.5"))

# Suppress early alerts entirely (rather than just flagging them).
SUPPRESS_EARLY = os.environ.get("BULLFLOW_PRESET_SUPPRESS_EARLY", "false").lower() in ("true","1","yes","on")

# For CALL alerts whose contract expires the SAME WEEK as the alert, suggest
# rolling out to the next week's expiry at the same strike (more time, less
# gamma/theta cliff into Friday).
ROLL_SUGGEST = os.environ.get("BULLFLOW_PRESET_ROLL_SUGGEST", "true").lower() not in ("false","0","no","off")
# A CALL expiring this week OR within this many days gets a roll suggestion
# to the next weekly (same strike, +7 days).
ROLL_MAX_DTE = int(os.environ.get("BULLFLOW_PRESET_ROLL_MAX_DTE", "5"))

# ── 30M EMA trend filter ──
# Grenade Trade is a FADE: take the put only when 30M 5EMA is ABOVE 12EMA
# (i.e. fading strength), and the call only when 5EMA is BELOW 12EMA.
# Every other preset type is a FOLLOW: call needs 5EMA above 12EMA,
# put needs 5EMA below 12EMA.
EMA_FILTER    = os.environ.get("BULLFLOW_PRESET_EMA_FILTER", "true").lower() not in ("false","0","no","off")
# When EMA data is unavailable: false = take the trade anyway (fail-open),
# true = skip it (fail-closed). Fail-closed makes backtests honest — you
# never bank a result the filter could not actually have approved.
EMA_REQUIRE   = os.environ.get("BULLFLOW_PRESET_EMA_REQUIRE", "false").lower() in ("true","1","yes","on")
EMA_FAST      = int(os.environ.get("BULLFLOW_PRESET_EMA_FAST", "5"))
EMA_SLOW      = int(os.environ.get("BULLFLOW_PRESET_EMA_SLOW", "12"))

_EMA_CACHE: dict = {}   # ticker_date_epochbucket → (ema_fast, ema_slow)


def _ema(closes: list, period: int) -> float:
    """Standard EMA. Returns 0.0 if not enough data."""
    if len(closes) < period:
        return 0.0
    k = 2.0 / (period + 1)
    e = sum(closes[:period]) / period
    for c in closes[period:]:
        e = c * k + e * (1 - k)
    return e


def _norm_epoch(v) -> float:
    """
    Bullflow may send epoch in seconds OR milliseconds. Normalize to seconds.
    Anything above ~1e11 is milliseconds (year 5138+ if read as seconds).
    """
    try:
        e = float(v)
    except Exception:
        return 0.0
    if e > 1e11:
        e = e / 1000.0
    return e


_MASSIVE_CALLS: list = []   # epoch times of recent calls (free tier: 5/min)


def _massive_key() -> str:
    """Massive (formerly Polygon.io). Legacy POLYGON_API_KEY still works."""
    return (os.environ.get("MASSIVE_API_KEY", "") or
            os.environ.get("POLYGON_API_KEY", ""))


def _massive_throttle():
    """
    Free tier allows 5 requests/minute. Block until a slot is free rather than
    eating 429s — a backtest can afford the wait, and a silent rate-limit
    failure would just fail the EMA open again.
    """
    limit = int(os.environ.get("MASSIVE_CALLS_PER_MIN", "5"))
    now = time.time()
    # Drop calls older than 60s
    while _MASSIVE_CALLS and now - _MASSIVE_CALLS[0] > 60:
        _MASSIVE_CALLS.pop(0)
    if len(_MASSIVE_CALLS) >= limit:
        wait = 61 - (now - _MASSIVE_CALLS[0])
        if wait > 0:
            print(f"[EMA] Massive rate limit ({limit}/min) — waiting {wait:.0f}s")
            time.sleep(wait)
            now = time.time()
            while _MASSIVE_CALLS and now - _MASSIVE_CALLS[0] > 60:
                _MASSIVE_CALLS.pop(0)
    _MASSIVE_CALLS.append(time.time())


def _massive_30m_closes(ticker: str, start_d: str, end_d: str, ae: float) -> list:
    """
    30-minute closes from Massive (formerly Polygon.io) aggregates.

    This is what makes the EMA filter work on BACKTEST dates — Tradier's
    intraday window is only a few weeks deep, while Massive's FREE tier
    carries 2 years of minute aggregates (rate-limited to 5 calls/min).

    Endpoint: /v2/aggs/ticker/{T}/range/30/minute/{from}/{to}
    Returns closes for bars that CLOSED at or before `ae` (epoch seconds).
    """
    key = _massive_key()
    if not key:
        return []

    base = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com")
    url  = (f"{base}/v2/aggs/ticker/{ticker.upper()}"
            f"/range/30/minute/{start_d}/{end_d}")
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": key}

    for attempt in range(2):
        try:
            _massive_throttle()
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 429:
                print(f"[EMA] {ticker}: Massive 429 — backing off 20s")
                time.sleep(20)
                continue
            if r.status_code != 200:
                print(f"[EMA] {ticker}: Massive HTTP {r.status_code} — {r.text[:120]}")
                return []

            out = []
            for b in (r.json().get("results") or []):
                # 't' is epoch MILLISECONDS at the bar's START
                t_ms, c = b.get("t"), b.get("c")
                if t_ms is None or c is None:
                    continue
                # Only count bars that had CLOSED by the alert (no lookahead)
                if (float(t_ms) / 1000.0) + 1800 <= ae:
                    out.append(float(c))
            return out
        except Exception as e:
            print(f"[EMA] {ticker}: Massive error {e}")
            return []
    return []


def _ema_30m(ticker: str, as_of_epoch) -> tuple:
    """
    30-minute EMA(fast) and EMA(slow) on the UNDERLYING, computed from bars
    at or before as_of_epoch — so it reflects what the chart looked like at
    the moment the alert fired (works identically live and in backtest).

    Source order:
      1. Tradier 15-min timesales (aggregated in pairs) — fast, and fine for
         LIVE alerts, but Tradier's intraday history is only weeks deep.
      2. Massive (ex-Polygon) 30-min aggregates — FREE tier gives 2 years of
         history, which is what makes the filter work on backtest dates.

    Returns (ema_fast, ema_slow). (0.0, 0.0) means UNAVAILABLE — always logged,
    so this can never fail silently.
    """
    if not ticker:
        return (0.0, 0.0)

    ae = _norm_epoch(as_of_epoch)
    if ae <= 0:
        print(f"[EMA] {ticker}: bad/missing timestamp ({as_of_epoch!r})")
        return (0.0, 0.0)

    try:
        as_of_dt = datetime.fromtimestamp(ae, ET)
    except Exception as e:
        print(f"[EMA] {ticker}: un-convertible epoch {ae} ({e})")
        return (0.0, 0.0)

    end_d   = as_of_dt.strftime("%Y-%m-%d")
    start_d = (as_of_dt - timedelta(days=15)).strftime("%Y-%m-%d")

    ckey = f"{ticker}_{int(ae // 1800)}"
    if ckey in _EMA_CACHE:
        return _EMA_CACHE[ckey]

    need     = EMA_SLOW + 1
    closes30 = []
    src      = ""

    # ── Source 1: Tradier 15-min → 30-min ──
    token = os.environ.get("TRADIER_TOKEN", "")
    if token:
        bars15, raw_n = [], 0
        try:
            r = requests.get(
                "https://api.tradier.com/v1/markets/timesales",
                params={"symbol": ticker.upper(), "interval": "15min",
                        "start": f"{start_d} 09:30", "end": f"{end_d} 16:00",
                        "session_filter": "open"},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=10,
            )
            if r.status_code == 200:
                data = ((r.json().get("series") or {}).get("data"))
                if isinstance(data, dict):
                    data = [data]
                raw_n = len(data or [])
                for b in (data or []):
                    ts, close = b.get("timestamp"), b.get("close")
                    if ts is None or not close:
                        continue
                    if float(ts) <= ae:
                        bars15.append((float(ts), float(close)))
                bars15.sort(key=lambda x: x[0])
            else:
                print(f"[EMA] {ticker}: Tradier HTTP {r.status_code}")
        except Exception as e:
            print(f"[EMA] {ticker}: Tradier error {e}")

        cand = [bars15[i + 1][1] for i in range(0, len(bars15) - 1, 2)]
        if len(cand) >= need:
            closes30, src = cand, "tradier"
        else:
            print(f"[EMA] {ticker} @ {end_d}: Tradier thin "
                  f"({raw_n} 15m bars → {len(cand)} 30m closes, need {need}) "
                  f"— trying Massive")

    # ── Source 2: Massive 30-min aggregates (2yr history on the FREE tier) ──
    if not closes30:
        cand = _massive_30m_closes(ticker, start_d, end_d, ae)
        if len(cand) >= need:
            closes30, src = cand, "massive"

    if len(closes30) < need:
        _has_key = bool(_massive_key())
        print(f"[EMA] {ticker} @ {end_d}: NO DATA "
              f"(Tradier thin; Massive {'returned too little' if _has_key else 'KEY NOT SET'}). "
              f"Need {need} 30m closes. Filter will "
              f"{'SKIP the trade' if EMA_REQUIRE else 'FAIL OPEN'}.")
        _EMA_CACHE[ckey] = (0.0, 0.0)
        return (0.0, 0.0)

    out = (_ema(closes30, EMA_FAST), _ema(closes30, EMA_SLOW))
    print(f"[EMA] {ticker} @ {as_of_dt.strftime('%Y-%m-%d %H:%M')}: "
          f"{EMA_FAST}EMA={out[0]:.2f} {EMA_SLOW}EMA={out[1]:.2f} "
          f"({len(closes30)} 30m bars via {src})")
    _EMA_CACHE[ckey] = out
    return out


def _ema_filter_passes(ticker: str, direction: str, is_reversal: bool,
                       as_of_epoch) -> tuple:
    """
    Apply the 30M EMA rule. Returns (passes, ema_fast, ema_slow, note).

      Grenade (reversal / fade):  put needs fast ABOVE slow
                                  call needs fast BELOW slow
      All others (follow):        call needs fast ABOVE slow
                                  put needs fast BELOW slow

    If EMA data is unavailable, the trade is ALLOWED (fail-open) and flagged,
    so a data outage never silently kills every alert.
    """
    fast, slow = _ema_30m(ticker, as_of_epoch)
    if fast <= 0 or slow <= 0:
        # No EMA data. Fail-closed if required (honest backtests), else allow.
        return (not EMA_REQUIRE, 0.0, 0.0, "EMA unavailable")

    fast_above = fast > slow
    if is_reversal:
        want_above = (direction == "put")     # fade the trend
    else:
        want_above = (direction == "call")    # follow the trend

    passes = (fast_above == want_above)
    # NB: '<' and '>' are HTML-special — Telegram sends with parse_mode=HTML and
    # a bare '<' makes it 400 the whole message. Use arrows instead.
    note = (f"30M {EMA_FAST}EMA {'▲' if fast_above else '▼'} {EMA_SLOW}EMA "
            f"({fast:.2f} vs {slow:.2f})")
    return (passes, fast, slow, note)


def _expiry_to_date(expiry: str):
    """'07/18/26' → date(2026, 7, 18). None on failure."""
    try:
        mm, dd, yy = expiry.split("/")
        return datetime(2000 + int(yy), int(mm), int(dd)).date()
    except Exception:
        return None


def _same_week(alert_d, expiry_d) -> bool:
    """True if both dates fall in the same Mon-Sun ISO week."""
    if not alert_d or not expiry_d:
        return False
    return alert_d.isocalendar()[:2] == expiry_d.isocalendar()[:2]


def _next_week_expiry(expiry_d):
    """
    The roll target: the FRIDAY of the week after the original expiry.

    Weeklies expire Friday, but not every contract does (SPX/QQQ have Mon/Wed
    expiries too), so a naive +7 days could land on a Wednesday. Snap to the
    Friday of that week instead. If that Friday is a market holiday (e.g. Good
    Friday, or July 3rd observed), step back to the last trading day of the
    week — Thursday, or earlier if that's closed too.
    """
    if not expiry_d:
        return None
    # Move into the following week, then snap to that week's Friday.
    nxt = expiry_d + timedelta(days=7)
    friday = nxt + timedelta(days=(4 - nxt.weekday()))   # Mon=0 … Fri=4

    try:
        from market_calendar import MARKET_HOLIDAYS
        holidays = MARKET_HOLIDAYS
    except Exception:
        holidays = set()

    # Back off to the last open day of that week (Fri → Thu → Wed → …)
    d = friday
    for _ in range(5):
        if d.weekday() < 5 and d not in holidays:
            return d
        d -= timedelta(days=1)
    return friday


def _build_occ(ticker: str, exp_d, otype: str, strike: float) -> str:
    """Build an OCC symbol, e.g. NVDA260718C00220000 (no 'O:' prefix)."""
    try:
        yy = exp_d.strftime("%y")
        mm = exp_d.strftime("%m")
        dd = exp_d.strftime("%d")
        cp = "C" if otype == "call" else "P"
        strike_int = int(round(float(strike) * 1000))
        return f"{ticker.upper()}{yy}{mm}{dd}{cp}{strike_int:08d}"
    except Exception:
        return ""


_OPT_PX_CACHE: dict = {}   # occ_epochbucket → price


def _option_price_at(occ_symbol: str, as_of_epoch=None) -> float:
    """
    Price of an option contract AT a point in time.

      • Alert is today (or no timestamp) → live Tradier quote.
      • Alert is a past date (backtest)  → the option's own intraday bar at
        that moment, falling back to that day's close.

    This is what makes a BACKTESTED roll honest: pricing the next-week contract
    at today's quote would be pure lookahead. Returns 0.0 if unavailable.
    """
    sym = str(occ_symbol or "").replace("O:", "").strip().upper()
    token = os.environ.get("TRADIER_TOKEN", "")
    if not sym or not token:
        return 0.0

    ae = _norm_epoch(as_of_epoch) if as_of_epoch else 0.0
    today = datetime.now(ET).date()
    as_of_d = None
    if ae > 0:
        try:
            as_of_d = datetime.fromtimestamp(ae, ET).date()
        except Exception:
            as_of_d = None

    # ── Live path: no timestamp, or the alert is from today ──
    if as_of_d is None or as_of_d >= today:
        return _fetch_option_quote(sym)

    ckey = f"{sym}_{int(ae // 1800)}"
    if ckey in _OPT_PX_CACHE:
        return _OPT_PX_CACHE[ckey]

    ds = as_of_d.strftime("%Y-%m-%d")
    px = 0.0
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # ── Historical: intraday bar at/just before the alert ──
    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/timesales",
            params={"symbol": sym, "interval": "15min",
                    "start": f"{ds} 09:30", "end": f"{ds} 16:00",
                    "session_filter": "open"},
            headers=hdrs, timeout=10,
        )
        if r.status_code == 200:
            data = ((r.json().get("series") or {}).get("data"))
            if isinstance(data, dict):
                data = [data]
            best = None
            for b in (data or []):
                ts, c = b.get("timestamp"), b.get("close")
                if ts is None or not c:
                    continue
                if float(ts) <= ae:
                    best = float(c)     # bars are ascending; keep the last one <= alert
                else:
                    break
            if best:
                px = best
    except Exception as e:
        print(f"[PRESET] roll timesales error {sym}: {e}")

    # ── Fallback: that day's close ──
    if px <= 0:
        try:
            r = requests.get(
                "https://api.tradier.com/v1/markets/history",
                params={"symbol": sym, "interval": "daily",
                        "start": ds, "end": ds},
                headers=hdrs, timeout=8,
            )
            if r.status_code == 200:
                day = (r.json().get("history") or {}).get("day")
                if isinstance(day, list) and day:
                    px = float(day[0].get("close", 0) or 0)
                elif isinstance(day, dict):
                    px = float(day.get("close", 0) or 0)
        except Exception as e:
            print(f"[PRESET] roll history error {sym}: {e}")

    _OPT_PX_CACHE[ckey] = px
    return px


def _fetch_option_quote(occ_symbol: str) -> float:
    """
    Last/mid price for an option contract via Tradier /markets/quotes.
    Returns 0.0 if the contract doesn't exist or the call fails.
    """
    token = os.environ.get("TRADIER_TOKEN", "")
    if not token or not occ_symbol:
        return 0.0
    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/quotes",
            params={"symbols": occ_symbol, "greeks": "false"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=6,
        )
        if r.status_code == 200:
            q = (r.json().get("quotes") or {}).get("quote")
            if isinstance(q, list):
                q = q[0] if q else None
            if q:
                px = q.get("last") or q.get("close")
                if not px:
                    bid, ask = q.get("bid") or 0, q.get("ask") or 0
                    px = (bid + ask) / 2 if (bid and ask) else 0
                return float(px or 0)
    except Exception as e:
        print(f"[PRESET] Tradier option quote error {occ_symbol}: {e}")
    return 0.0

# Preset types to play as 30M trend REVERSAL (all others → 30M trend FOLLOW).
_DEFAULT_REVERSAL_TYPES = "Grenade Trade"
REVERSAL_TYPES = [t.strip().lower() for t in
                  os.environ.get("BULLFLOW_PRESET_REVERSAL_TYPES",
                                 _DEFAULT_REVERSAL_TYPES).split(",")
                  if t.strip()]

# Case-insensitive lookup set for matching incoming alert names
_PRESET_LOWER = {t.lower() for t in PRESET_TYPES}


def _round_up_tenth(value: float) -> float:
    """
    Round UP to the nearest $0.10.  2.44 → 2.50,  2.40 → 2.40,  1.61 → 1.70.

    Float-safe: 2.40 * 10 is 24.000000000000004 in binary floating point, so a
    naive ceil() would wrongly bump it to 2.50. Rounding to 6dp first snaps
    that back to 24.0 before the ceiling is applied.
    """
    import math
    if value <= 0:
        return 0.0
    return math.ceil(round(value * 10, 6)) / 10.0


def _floor_cent(value: float) -> float:
    """
    Round DOWN to the nearest cent. Used for profit targets (sell limits):
    flooring keeps the target reachable and never overstates the % gain,
    and avoids float-rounding ambiguity (2.50 x 2.01 = 5.025 → 5.02).
    """
    import math
    if value <= 0:
        return 0.0
    return math.floor(round(value * 100, 6)) / 100.0


def _fmt_prem(p: float) -> str:
    return f"${p/1_000_000:.1f}M" if p >= 1_000_000 else f"${p/1_000:.0f}K"


def is_preset(alert_name: str) -> bool:
    return str(alert_name or "").strip().lower() in _PRESET_LOWER


def process_preset(alert: dict, filter_name: str) -> dict | None:
    """
    Evaluate a Bullflow pre-defined alert against the premium + DTE filters.
    Returns an enriched result dict when it qualifies, else None.
    """
    if not is_preset(filter_name):
        return None

    ticker  = str(alert.get("ticker", "") or "").upper()
    strike  = str(alert.get("strike", "") or "")
    expiry  = str(alert.get("expiry", "") or "")
    otype   = str(alert.get("option_type", "call") or "call")
    price   = float(alert.get("option_price") or 0)
    premium = float(alert.get("premium", 0) or 0)
    dte     = int(alert.get("dte", 0) or 0)
    sweep   = bool(alert.get("is_sweep", False))
    stock_px = float(alert.get("stock_price") or 0)

    if not ticker or not strike or not expiry:
        return None

    # ── Sanity-check the payload before deriving anything from it ──
    # Bad upstream data would otherwise produce alerts with $0.00 entries,
    # unparseable strikes, or negative DTE.
    if price <= 0:
        print(f"[PRESET] {filter_name} {ticker}: invalid trade price {price} — skipping")
        return None
    if dte < 0:
        print(f"[PRESET] {filter_name} {ticker}: negative DTE {dte} — skipping")
        return None
    try:
        _sv = float(strike)
        if _sv <= 0:
            raise ValueError
    except Exception:
        print(f"[PRESET] {filter_name} {ticker}: unparseable strike {strike!r} — skipping")
        return None
    if _expiry_to_date(expiry) is None:
        print(f"[PRESET] {filter_name} {ticker}: invalid expiry {expiry!r} — skipping")
        return None

    # Alert timestamp — prefer Bullflow's est_timestamp string, then epoch,
    # then fall back to now. Displayed in ET as HH:MM:SS AM/PM.
    # Also capture alert_hour (float ET) for the early-session check, and
    # alert_date (date obj) for the same-week expiry roll suggestion.
    time_str   = ""
    alert_hour = None
    alert_date = None
    est = str(alert.get("est_timestamp", "") or "")
    if len(est) >= 19:
        # e.g. "2026-06-05 09:32:26 EST" → "9:32:26 AM"
        try:
            dt = datetime.strptime(est[:19], "%Y-%m-%d %H:%M:%S")
            time_str   = dt.strftime("%-I:%M:%S %p")
            alert_hour = dt.hour + dt.minute / 60.0
            alert_date = dt.date()
        except Exception:
            time_str = est[11:19]
    if not time_str or alert_hour is None or alert_date is None:
        epoch = alert.get("timestamp")
        try:
            if epoch:
                _dt = datetime.fromtimestamp(float(epoch), ET)
                if not time_str:
                    time_str = _dt.strftime("%-I:%M:%S %p")
                if alert_hour is None:
                    alert_hour = _dt.hour + _dt.minute / 60.0
                if alert_date is None:
                    alert_date = _dt.date()
        except Exception:
            pass
    if not time_str:
        _now = datetime.now(ET)
        time_str = _now.strftime("%-I:%M:%S %p")
        if alert_hour is None:
            alert_hour = _now.hour + _now.minute / 60.0
    if alert_date is None:
        alert_date = datetime.now(ET).date()

    # Fall back to a live Tradier quote if the fill carried no stock price
    if not stock_px:
        stock_px = _fetch_tradier_price(ticker)

    # ── Filters ──
    if premium < MIN_PREMIUM:
        print(f"[PRESET] {filter_name} {ticker}: {_fmt_prem(premium)} < "
              f"{_fmt_prem(MIN_PREMIUM)} min — skipping")
        return None
    if dte > MAX_DTE:
        print(f"[PRESET] {filter_name} {ticker}: {dte}d DTE > {MAX_DTE}d max — skipping")
        return None

    direction = "call" if "call" in otype.lower() else "put"

    # Earnings enrichment (best-effort)
    earnings_str = None
    earnings_flag = ""
    try:
        from fetcher import fetch_earnings_date
        e_str, e_dt, e_past, e_timing = fetch_earnings_date(ticker)
        if e_str and not e_past:
            earnings_str = f"{e_str}{' ' + e_timing if e_timing else ''}"
            if e_dt:
                days_to = (e_dt.date() - datetime.now().date()).days
                if 0 <= days_to <= dte:
                    earnings_flag = f"⚠️ earnings {earnings_str} — inside contract window"
                else:
                    earnings_flag = f"📅 earnings {earnings_str}"
    except Exception as _ee:
        print(f"[PRESET] earnings fetch error {ticker}: {_ee}")

    # ── Moneyness (ITM / ATM / OTM) relative to stock price ──
    # ATM band = within ATM_BAND_PCT of the strike (default 0.5%).
    moneyness = ""
    try:
        strike_f = float(strike)
    except Exception:
        strike_f = 0.0
    if stock_px > 0 and strike_f > 0:
        diff_pct = abs(stock_px - strike_f) / stock_px
        if diff_pct <= ATM_BAND_PCT:
            moneyness = "ATM"
        elif direction == "call":
            moneyness = "ITM" if stock_px > strike_f else "OTM"
        else:  # put
            moneyness = "ITM" if stock_px < strike_f else "OTM"

    # Suppress ITM alerts when disabled
    if moneyness == "ITM" and not SHOW_ITM:
        print(f"[PRESET] {filter_name} {ticker}: ITM suppressed (SHOW_ITM off)")
        return None

    # ── Trade size in # of contracts ──
    # Each contract controls 100 shares, so cost per contract = price * 100.
    # contracts = total premium / (price per contract * 100).
    contracts = 0
    if price > 0:
        contracts = int(round(premium / (price * 100)))

    # ── Suggested entry + trailing stop, derived from flow trade price ──
    # Entry = 20% below flow trade price. Trail stop OFFSET = 75% of trade
    # price (e.g. $2.00 → entry $1.60, trail offset $1.50).
    # Entry = 20% below flow trade price, rounded UP to the nearest $0.10
    # (a slightly higher limit is more likely to actually fill).
    entry_price  = _round_up_tenth(price * (1 - ENTRY_DISCOUNT_PCT)) if price > 0 else 0.0
    trail_offset = round(price * TRAIL_OFFSET_PCT, 2)                if price > 0 else 0.0

    # Profit targets measured from the ENTRY (not the flow price).
    # Floored to the cent — a sell limit should never overstate the target.
    target1 = _floor_cent(entry_price * (1 + TARGET1_PCT)) if entry_price > 0 else 0.0
    target2 = _floor_cent(entry_price * (1 + TARGET2_PCT)) if entry_price > 0 else 0.0

    # ── Short-dated CALL → roll to next week's expiry, same strike ──
    # Fires when the call expires THIS WEEK **or** within ROLL_MAX_DTE days.
    # A near-dated call faces a hard theta/gamma cliff; the next weekly at the
    # same strike keeps the thesis with more time. The rolled contract has its
    # OWN price, so its entry limit and targets are derived from THAT price —
    # not from the original contract's fill.
    roll = None
    if ROLL_SUGGEST and direction == "call":
        exp_d = _expiry_to_date(expiry)
        if exp_d and (_same_week(alert_date, exp_d) or dte <= ROLL_MAX_DTE):
            next_d = _next_week_expiry(exp_d)
            if next_d:
                next_occ = _build_occ(ticker, next_d, "call", strike_f)
                # Price the rolled contract AS OF THE ALERT — a live quote here
                # would be lookahead in any backtest.
                next_px  = (_option_price_at(next_occ, alert.get("timestamp"))
                            if next_occ else 0.0)
                # Entry + targets for the ROLLED contract, off its own price
                r_entry = _round_up_tenth(next_px * (1 - ENTRY_DISCOUNT_PCT)) if next_px > 0 else 0.0
                r_t1    = _floor_cent(r_entry * (1 + TARGET1_PCT)) if r_entry > 0 else 0.0
                r_t2    = _floor_cent(r_entry * (1 + TARGET2_PCT)) if r_entry > 0 else 0.0
                r_trail = round(next_px * TRAIL_OFFSET_PCT, 2)     if next_px > 0 else 0.0
                roll = {
                    "expiry":    next_d.strftime("%m/%d/%y"),
                    "strike":    strike,
                    "dte":       max(0, (next_d - alert_date).days),
                    "price":     next_px,
                    "occ":       next_occ,
                    "available": next_px > 0,
                    "reason":    ("expires this week" if _same_week(alert_date, exp_d)
                                  else f"{dte}d DTE"),
                    "entry":     r_entry,
                    "target1":   r_t1,
                    "target2":   r_t2,
                    "trail":     r_trail,
                }

    # ── Early-session reversal risk ──
    # Flow printed before EARLY_CUTOFF_HOUR (10:30am ET default) lands while the
    # opening range is still resolving and frequently fades — flag it.
    is_early = alert_hour is not None and alert_hour < EARLY_CUTOFF_HOUR

    if is_early and SUPPRESS_EARLY:
        print(f"[PRESET] {filter_name} {ticker}: pre-{EARLY_CUTOFF_HOUR:.2f} ET "
              f"suppressed (SUPPRESS_EARLY on)")
        return None

    # ── 30M playbook ──
    # Grenade Trades (and any other configured type) are played as 30M trend
    # REVERSALS; every other preset type is played as 30M trend FOLLOWING.
    is_reversal = str(filter_name).strip().lower() in REVERSAL_TYPES
    playbook    = "reversal" if is_reversal else "follow"

    # ── 30M EMA trend gate ──
    # Grenade fades the 30M trend; every other type follows it.
    ema_note, ema_fast, ema_slow = "", 0.0, 0.0
    if EMA_FILTER:
        _epoch = alert.get("timestamp")
        if not _epoch and alert_date and alert_hour is not None:
            # Rebuild an epoch from the parsed date+hour if the raw one is absent
            try:
                _h = int(alert_hour); _m = int(round((alert_hour - _h) * 60))
                _epoch = datetime(alert_date.year, alert_date.month, alert_date.day,
                                  _h, _m, tzinfo=ET).timestamp()
            except Exception:
                _epoch = None
        _ok, ema_fast, ema_slow, ema_note = _ema_filter_passes(
            ticker, direction, is_reversal, _epoch)
        if not _ok:
            _why = ("no EMA data (EMA_REQUIRE=true)"
                    if ema_note == "EMA unavailable" else ema_note)
            print(f"[PRESET] {filter_name} {ticker} {direction}: EMA filter FAILED "
                  f"({_why}) — skipping")
            return None

    print(f"[PRESET] 🎯 {filter_name}: {ticker} {strike}{'C' if direction=='call' else 'P'} "
          f"{expiry} | {_fmt_prem(premium)} | {dte}d | {moneyness} | {contracts} contracts "
          f"| 30M {playbook}{' | EARLY' if is_early else ''}")

    return {
        "preset_type":  filter_name,
        "ticker":       ticker,
        "strike":       strike,
        "expiry":       expiry,
        "direction":    direction,
        "price":        price,
        "premium":      premium,
        "dte":          dte,
        "sweep":        sweep,
        "stock_px":     stock_px,
        "moneyness":    moneyness,
        "contracts":    contracts,
        "entry_price":  entry_price,
        "trail_offset": trail_offset,
        "target1":      target1,
        "target2":      target2,
        "earnings_str": earnings_str,
        "earnings_flag": earnings_flag,
        "time_str":     time_str,
        "alert_hour":   alert_hour,
        "alert_date":   alert_date.strftime("%Y-%m-%d") if alert_date else "",
        "is_early":     is_early,
        "playbook":     playbook,
        "ema_fast":     round(ema_fast, 2),
        "ema_slow":     round(ema_slow, 2),
        "ema_note":     ema_note,
        "roll":         roll,
    }


def build_preset_alert(result: dict) -> str:
    ptype   = result["preset_type"]
    ticker  = result["ticker"]
    strike  = result["strike"]
    expiry  = result["expiry"]
    direction = result["direction"]
    price   = result["price"]
    premium = result["premium"]
    dte     = result["dte"]
    sweep   = result["sweep"]
    stock_px = result["stock_px"]
    eflag   = result["earnings_flag"]
    time_str = result.get("time_str", "")
    moneyness    = result.get("moneyness", "")
    contracts    = result.get("contracts", 0)
    entry_price  = result.get("entry_price", 0)
    trail_offset = result.get("trail_offset", 0)
    is_early     = result.get("is_early", False)
    playbook     = result.get("playbook", "follow")

    otype = "C" if direction == "call" else "P"
    emoji = "📈" if direction == "call" else "📉"
    sweep_s = " ⚡ SWEEP" if sweep else ""
    money_s = f"  [{moneyness}]" if moneyness else ""

    lines = [
        f"🔔 BULLFLOW: {ptype}",
        f"━━━ {emoji} {direction.upper()} {ticker} ━━━",
        "",
        f"Contract: {strike}{otype} {expiry}  ({dte}d DTE){sweep_s}",
        f"💵 Total premium: {_fmt_prem(premium)}",
        f"💲 Trade price: ${price:.2f}{money_s}",
    ]
    if contracts:
        lines.append(f"📦 Trade size: {contracts:,} contracts")
    if stock_px:
        lines.append(f"📊 Stock: ${stock_px:.2f}")
    if time_str:
        lines.append(f"🕐 Alert time: {time_str} ET")
    if eflag:
        lines.append(eflag)

    # ── Playbook + timing warnings ──
    lines.append("")
    if playbook == "reversal":
        lines.append("🔄 PLAY: watch 30M for TREND REVERSAL")
    else:
        lines.append("➡️ PLAY: watch 30M for TREND CONTINUATION")
    _en = result.get("ema_note", "")
    if _en and _en != "EMA unavailable":
        lines.append(f"✅ {_en}")
    if is_early:
        _cut_h = int(EARLY_CUTOFF_HOUR)
        _cut_m = int(round((EARLY_CUTOFF_HOUR - _cut_h) * 60))
        _cut_s = f"{(_cut_h - 12) if _cut_h > 12 else _cut_h}:{_cut_m:02d}{'pm' if _cut_h >= 12 else 'am'}"
        lines.append(f"⚠️ EARLY (pre-{_cut_s}) — elevated reversal risk, "
                     "let the opening range resolve")

    if entry_price:
        _t1 = result.get("target1", 0)
        _t2 = result.get("target2", 0)
        lines += [
            "",
            f"🎯 Entry: ${entry_price:.2f}  ({ENTRY_DISCOUNT_PCT*100:.0f}% below flow)",
            f"🥇 Target 1: ${_t1:.2f}  (+{TARGET1_PCT*100:.0f}% from entry)",
            f"🥈 Target 2: ${_t2:.2f}  (+{TARGET2_PCT*100:.0f}% from entry)",
            f"🛑 Trail stop offset: -${trail_offset:.2f}  "
            f"({TRAIL_OFFSET_PCT*100:.0f}% of flow price)",
        ]

    # ── Roll suggestion: short-dated call → next week, same strike ──
    roll = result.get("roll")
    if roll:
        _why = roll.get("reason", "")
        lines += ["",
                  f"🔁 SHORT-DATED ({_why}) — take next week instead:",
                  f"   {roll['strike']}C {roll['expiry']}  ({roll['dte']}d DTE)"]
        if roll.get("available") and roll.get("price"):
            lines += [
                f"   Contract price: ${roll['price']:.2f}",
                f"   🎯 Entry: ${roll['entry']:.2f}  "
                f"({ENTRY_DISCOUNT_PCT*100:.0f}% below its price)",
                f"   🥇 T1: ${roll['target1']:.2f}   🥈 T2: ${roll['target2']:.2f}",
                f"   🛑 Trail: -${roll['trail']:.2f}",
            ]
        else:
            lines.append("   (quote unavailable — check the chain)")

    lines += [
        "",
        f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
    ]
    return "\n".join(lines)
