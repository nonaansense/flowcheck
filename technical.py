"""
Technical analysis engine for FlowCheck.
Uses Finnhub 1-minute candles aggregated into M5/M10/M15/M30/H1.
No additional API key needed — uses existing FINNHUB_API_KEY.
Monitors WATCH/TRADE tickers for entry signals until end of trading day.

Aggregation logic:
  M5  = 5  x 1min candles
  M10 = 10 x 1min candles
  M15 = 15 x 1min candles
  M30 = 30 x 1min candles
  H1  = 60 x 1min candles

Signal fires when 2+ conditions align on same timeframe:
  1. Bullish hammer
  2. Bullish engulfing
  3. VWAP bounce
  4. Break above previous candle high
  5. Morning star (3-candle)
  6. Higher low (3 consecutive)
  7. Volume spike (1.5x average)
"""
import os, time, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ── Timeframe definitions ──────────────────────────────────────────────
TIMEFRAMES = {
    "M5":  5,
    "M10": 10,
    "M15": 15,
    "M30": 30,
    "H1":  60,
}

# ── Active watch list ──────────────────────────────────────────────────
_watch_list: dict = {}

IMMEDIATE_ALERT_THRESHOLD = 40.0  # Stocks below this alert immediately
WATCHLIST_FILE = "/tmp/flowcheck_watchlist.json"
MIN_DTE_TO_MONITOR = 3   # Stop monitoring if fewer than 3 days to expiry
MAX_MONITOR_DAYS   = 10  # Max days to keep a ticker on watchlist

def add_to_watchlist(ticker, trade, result, data=None, send_sms_fn=None):
    """
    Stocks < $40: send immediate entry alert — pullbacks too fast.
    Stocks >= $40: monitor M5-H1 for technical pullback entry.
    """
    flow_stock_price  = (data or {}).get("stock_price")
    flow_option_price = trade.get("option_price") or (data or {}).get("flow_fill_price")

    if flow_stock_price and float(flow_stock_price) < IMMEDIATE_ALERT_THRESHOLD:
        print(f"[TECHNICAL] {ticker} ${flow_stock_price} < ${IMMEDIATE_ALERT_THRESHOLD:.0f} — immediate entry")
        if send_sms_fn:
            strike  = str(trade.get("strike","?"))
            otype   = trade.get("option_type","call")[0].upper()
            expiry  = str(trade.get("expiry","?"))
            score   = str(result.get("final_score",0))
            verdict = result.get("verdict","WATCH")
            emoji   = "✅" if verdict == "TRADE" else "👀"
            thresh  = str(int(IMMEDIATE_ALERT_THRESHOLD))
            price   = str(flow_stock_price)
            fill    = str((data or {}).get("fill_label") or "")
            vol_oi  = str((data or {}).get("vol_oi_label") or "")
            opt_p   = str(flow_option_price) if flow_option_price else ""

            parts = [
                "⚡ IMMEDIATE ENTRY: " + ticker,
                emoji + " " + ticker + " " + strike + otype + " " + expiry + " [" + score + "/7 " + verdict + "]",
                "",
                "Sub-$" + thresh + " stock — pullbacks move too fast to wait.",
                "Enter near $" + price + " if conviction is high",
                "Stop: 5-8% below entry (wide stop for small cap)",
            ]
            if fill:
                parts.append("Fill: " + fill)
            if vol_oi:
                parts.append("Signal: " + vol_oi)
            if opt_p:
                parts.append("Option avg fill: $" + opt_p)
            send_sms_fn("\n".join(parts))
        return

    if ticker in _watch_list:
        print("[TECHNICAL] " + ticker + " already in watchlist — updating")
    # Calculate initial DTE
    from datetime import datetime as _dt2
    expiry_raw = trade.get("expiry_raw","") or trade.get("expiry","")
    dte_remaining = None
    if expiry_raw:
        try:
            parts = expiry_raw.split("/")
            m, d, y = parts
            y = "20"+y if len(y)==2 else y
            exp = _dt2(int(y),int(m),int(d))
            dte_remaining = (exp - _dt2.now()).days
        except:
            pass

    _d = data or {}
    _watch_list[ticker] = {
        # Core identifiers
        "added":             time.time(),
        "added_at":          time.time(),
        "ticker":            ticker,
        "strike":            trade.get("strike","?"),
        "option_type":       trade.get("option_type","call"),
        "expiry":            trade.get("expiry","?"),
        "expiry_raw":        expiry_raw,
        "dte_remaining":     dte_remaining,
        # Flow info
        "flow_score":        result.get("final_score",0),
        "verdict":           result.get("verdict","WATCH"),
        "flow_stock_price":  flow_stock_price,
        "flow_option_price": flow_option_price,
        "premium":           trade.get("premium") or _d.get("premium_raw",0),
        "fill_type":         _d.get("fill_type",""),
        "fill_label":        _d.get("fill_label",""),
        "vol_oi_label":      _d.get("vol_oi_label",""),
        "vol_oi_ratio":      _d.get("vol_oi_ratio",""),
        "is_sweep":          _d.get("is_sweep",False),
        # GEX
        "gex_entry_score":   _d.get("_gex_entry_score",""),
        "gex_regime":        _d.get("gex_regime",""),
        "gex_flip":          _d.get("gex_flip",""),
        # Entry plan
        "entry_limit_price": _d.get("entry_limit_price",0),
        "stop_price":        _d.get("stop_price",0),
        "stock_price_at_alert": flow_stock_price,
        # Context
        "source":            _d.get("source",""),
        "analysis_id":       _d.get("analysis_id",""),
        "open_interest":     int(_d.get("open_interest",0) or _d.get("oi",0) or 0),
        "tweet_url":         trade.get("tweet_url","") or _d.get("tweet_url",""),
        # Conviction
        "conviction_total":  (_d.get("conviction") or {}).get("total",0),
        "conviction_label":  (_d.get("conviction") or {}).get("label",""),
        "alerted":           {},
    }
    dte_str = str(dte_remaining) + "d left" if dte_remaining else "DTE unknown"
    print(f"[TECHNICAL] Added {ticker} to watchlist — {dte_str} — monitoring M5/M10/M15/M30/H1")
    save_watchlist()

def remove_from_watchlist(ticker: str):
    _watch_list.pop(ticker, None)
    save_watchlist()
    print(f"[TECHNICAL] Removed {ticker} from watchlist")

def get_watchlist() -> dict:
    return _watch_list

def save_watchlist():
    """Persist watchlist to Supabase AND disk."""
    import json
    try:
        serializable = {}
        for ticker, entry in _watch_list.items():
            serializable[ticker] = {k: v for k, v in entry.items()
                                    if k != "alerted"}
            serializable[ticker]["alerted"] = {}
        payload = {"watchlist": serializable, "saved": time.time()}
        # Save to Supabase
        try:
            from storage import db_set
            db_set("watchlist", json.dumps(payload))
        except Exception as e:
            print(f"[TECHNICAL] Supabase watchlist save error: {e}")
        # Also save to /tmp as backup
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"[TECHNICAL] Watchlist save error: {e}")

def load_watchlist():
    """Load persisted watchlist from Supabase on startup. Skip expired entries."""
    import json
    from datetime import datetime
    try:
        # Try Supabase first
        data = None
        try:
            from storage import db_get
            raw = db_get("watchlist")
            if raw:
                data = json.loads(raw)
                print("[TECHNICAL] Loaded watchlist from Supabase")
        except:
            pass
        # Fall back to /tmp
        if not data:
            with open(WATCHLIST_FILE) as f:
                data = json.load(f)
        loaded = 0
        for ticker, entry in data.get("watchlist", {}).items():
            # Check DTE — skip if option expires in < MIN_DTE days
            expiry_raw = entry.get("expiry_raw") or entry.get("expiry") or ""
            dte        = None
            if expiry_raw:
                try:
                    parts = expiry_raw.split("/")
                    m, d, y = parts
                    y   = "20" + y if len(y) == 2 else y
                    exp = datetime(int(y), int(m), int(d))
                    dte = (exp - datetime.now()).days
                    entry["dte_remaining"] = dte
                except:
                    pass

            if dte is not None and dte < MIN_DTE_TO_MONITOR:
                print(f"[TECHNICAL] Skipping {ticker} on reload — {dte}d left, too late")
                continue
            if dte is not None and dte < 0:
                print(f"[TECHNICAL] Removing {ticker} from watchlist — expired {abs(dte)}d ago")
                continue  # Don't add to _watch_list — effectively removes it

            age_days = (time.time() - entry.get("added", time.time())) / 86400
            if age_days > MAX_MONITOR_DAYS:
                print(f"[TECHNICAL] Removing {ticker} from watchlist — {age_days:.0f}d old")
                continue

            _watch_list[ticker] = entry
            loaded += 1
            dte_display = f"{dte}d" if dte is not None else "unknown"
            print(f"[TECHNICAL] Reloaded {ticker} — {dte_display} left on option")

        if loaded:
            print(f"[TECHNICAL] Loaded {loaded} tickers from saved watchlist")
    except FileNotFoundError:
        print("[TECHNICAL] No saved watchlist — starting fresh")
    except Exception as e:
        print(f"[TECHNICAL] Watchlist load error: {e}")

# Load persisted watchlist on module import
load_watchlist()

# ── Finnhub 1-min candles ──────────────────────────────────────────────
# Using Polygon.io (Massive.com) for 1-minute candles
# Free tier: unlimited calls, 5/minute rate limit, real-time US data

def fetch_1min_candles(ticker: str, count: int = 120) -> list:
    """
    Fetch 1-minute candles via Tradier time_and_sales endpoint.
    Uses existing TRADIER_TOKEN — no extra cost or plan needed.
    Returns list oldest→newest.
    """
    import time as _time
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo as _ZI2

    token = os.environ.get("TRADIER_TOKEN","")
    if not token:
        print("[TECHNICAL] No TRADIER_TOKEN — cannot fetch candles")
        return []

    now_et = _dt.now(_ZI2("America/New_York"))
    # Skip weekends
    if now_et.weekday() >= 5:
        return []

    # Tradier time_and_sales uses start/end in YYYY-MM-DD HH:MM format
    from_dt  = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    to_dt    = now_et
    start_str = from_dt.strftime("%Y-%m-%d %H:%M")
    end_str   = to_dt.strftime("%Y-%m-%d %H:%M")

    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/timesales",
            params={
                "symbol":    ticker.upper(),
                "interval":  "1min",
                "start":     start_str,
                "end":       end_str,
                "session_filter": "open",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Accept":        "application/json",
            },
            timeout=10
        )
        if r.status_code == 200:
            data  = r.json()
            series = (data.get("series") or {})
            items  = series.get("data", []) if isinstance(series, dict) else []
            if not items:
                return []
            if isinstance(items, dict):
                items = [items]
            candles = []
            for bar in items[-count:]:
                try:
                    candles.append({
                        "timestamp": int(_dt.strptime(bar["time"], "%Y-%m-%dT%H:%M:%S").timestamp()),
                        "open":      float(bar.get("open",  bar.get("price", 0))),
                        "high":      float(bar.get("high",  bar.get("price", 0))),
                        "low":       float(bar.get("low",   bar.get("price", 0))),
                        "close":     float(bar.get("close", bar.get("price", 0))),
                        "volume":    float(bar.get("volume", 0)),
                    })
                except Exception:
                    continue
            if candles:
                print(f"[TECHNICAL] {ticker}: {len(candles)} 1min candles via Tradier")
            return candles
        elif r.status_code == 429:
            print(f"[TECHNICAL] Tradier rate limit for {ticker}")
            _time.sleep(5)
        else:
            print(f"[TECHNICAL] Tradier {r.status_code} for {ticker}")
    except Exception as e:
        print(f"[TECHNICAL] Tradier candle error {ticker}: {e}")

    return []

def aggregate_candles(candles_1min: list, period: int) -> list:
    """
    Aggregate 1-minute candles into higher timeframe candles.
    period = number of 1-min candles per aggregated candle (5, 10, 15, 30, 60)
    Returns list oldest→newest of aggregated candles.
    """
    if len(candles_1min) < period:
        return []

    aggregated = []
    # Work in groups of `period` candles
    # Align to complete periods only
    n = len(candles_1min)
    # Start from the most recent complete period boundary
    start = n % period  # Skip partial leading candles

    for i in range(start, n, period):
        group = candles_1min[i:i+period]
        if len(group) < period:
            break  # Skip incomplete final group

        agg = {
            "timestamp": group[0]["timestamp"],
            "open":      group[0]["open"],
            "high":      max(c["high"]   for c in group),
            "low":       min(c["low"]    for c in group),
            "close":     group[-1]["close"],
            "volume":    sum(c["volume"] for c in group),
        }
        aggregated.append(agg)

    return aggregated

def calc_vwap(candles_1min: list) -> float | None:
    """
    Calculate VWAP from 1-minute candles (today's session).
    VWAP = Σ(typical_price × volume) / Σ(volume)
    typical_price = (high + low + close) / 3
    """
    total_pv  = 0.0
    total_vol = 0.0
    for c in candles_1min:
        if c["volume"] > 0:
            typical   = (c["high"] + c["low"] + c["close"]) / 3
            total_pv  += typical * c["volume"]
            total_vol += c["volume"]
    if total_vol > 0:
        return round(total_pv / total_vol, 2)
    return None

# ── Pattern Detection ──────────────────────────────────────────────────

def is_bullish_hammer(c: dict) -> tuple:
    """
    Small body at top with long lower wick (≥2x body).
    Signals rejection of lower prices — buyers stepped in.
    """
    body       = abs(c["close"] - c["open"])
    total      = c["high"] - c["low"]
    if total == 0 or body == 0:
        return False, ""
    lower_wick = min(c["open"], c["close"]) - c["low"]
    upper_wick = c["high"] - max(c["open"], c["close"])
    body_ratio = body / total

    if (body_ratio < 0.35
            and lower_wick >= 2 * body
            and upper_wick <= body * 0.5
            and c["close"] >= c["open"]):
        wick_ratio = round(lower_wick / body, 1)
        return True, f"Bullish hammer (wick {wick_ratio}x body, close ${c['close']:.2f})"
    return False, ""

def is_bullish_engulfing(c: dict, prev: dict) -> tuple:
    """
    Current green candle body fully engulfs previous red candle body.
    Strong momentum shift signal.
    """
    prev_body = abs(prev["close"] - prev["open"])
    curr_body = abs(c["close"] - c["open"])
    if prev_body == 0:
        return False, ""

    if (prev["close"] < prev["open"]     # Prev was bearish
            and c["close"] > c["open"]   # Current is bullish
            and c["open"] <= prev["close"]
            and c["close"] >= prev["open"]):
        size_pct = round((curr_body / prev_body) * 100)
        return True, f"Bullish engulfing ({size_pct}% of prev candle)"
    return False, ""

def is_vwap_bounce(c: dict, vwap: float | None) -> tuple:
    """
    Candle low touched VWAP zone and closed above it bullishly.
    VWAP is the key institutional reference level.
    """
    if not vwap or vwap <= 0:
        return False, ""
    vwap_zone_low  = vwap * 0.998  # 0.2% below VWAP
    vwap_zone_high = vwap * 1.002  # 0.2% above VWAP

    touched = c["low"] <= vwap_zone_high
    closed_above = c["close"] > vwap
    bullish = c["close"] > c["open"]

    if touched and closed_above and bullish:
        pct = round(((c["close"] - vwap) / vwap) * 100, 2)
        return True, f"VWAP bounce — close {pct:+.2f}% vs VWAP ${vwap:.2f}"
    return False, ""

def is_break_above_prev_high(c: dict, prev: dict) -> tuple:
    """
    Closed above previous candle's high — momentum confirmation.
    Shows buyers have control and broke resistance.
    """
    if c["close"] > prev["high"] and c["close"] > c["open"]:
        pct = round(((c["close"] - prev["high"]) / prev["high"]) * 100, 2)
        return True, f"Break above prev high ${prev['high']:.2f} (+{pct}%)"
    return False, ""

def is_morning_star(candles: list) -> tuple:
    """
    3-candle reversal:
    1. Large bearish candle
    2. Small indecision candle (doji)
    3. Large bullish candle closing into first candle's body
    """
    if len(candles) < 3:
        return False, ""
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]

    c1_range = c1["high"] - c1["low"]
    c1_body  = abs(c1["close"] - c1["open"])
    c2_body  = abs(c2["close"] - c2["open"])
    c3_body  = abs(c3["close"] - c3["open"])

    if c1_range == 0:
        return False, ""

    if (c1["close"] < c1["open"]                    # C1 bearish
            and c1_body > c1_range * 0.5            # C1 large body
            and c2_body < c1_body * 0.3             # C2 small (indecision)
            and c3["close"] > c3["open"]            # C3 bullish
            and c3_body > c1_body * 0.5             # C3 substantial
            and c3["close"] > (c1["open"] + c1["close"]) / 2):  # C3 recovers into C1
        return True, f"Morning star reversal (${c1['close']:.2f}→${c2['close']:.2f}→${c3['close']:.2f})"
    return False, ""

def is_bearish_engulfing(c: dict, prev: dict) -> tuple:
    """Large bearish candle engulfs prior bullish candle — reversal signal."""
    if (prev["close"] > prev["open"]    # Prev was bullish
    and c["open"]  >= prev["close"]     # Gap up or flat
    and c["close"] <= prev["open"]      # Engulfs prior body
    and c["close"] < c["open"]):        # Current bearish
        pct = round(abs(c["close"]-c["open"]) / max(abs(prev["close"]-prev["open"]),0.01) * 100)
        return True, f"Bearish engulfing ({pct}% of prev candle)"
    return False, ""


def is_vwap_rejection(c: dict, vwap: float) -> tuple:
    """Price touched VWAP and closed below it — bearish signal."""
    if not vwap: return False, ""
    high_pct  = (c["high"] - vwap) / vwap * 100
    close_pct = (c["close"] - vwap) / vwap * 100
    if high_pct > 0 and close_pct < -0.1:
        return True, f"VWAP rejection — close {abs(close_pct):.2f}% below VWAP ${vwap:.2f}"
    return False, ""


def is_break_below_prev_low(c: dict, prev: dict) -> tuple:
    """Close below prior candle low — breakdown signal."""
    if c["close"] < prev["low"]:
        pct = round((prev["low"] - c["close"]) / prev["low"] * 100, 2)
        return True, f"Break below prev low ${prev['low']:.2f} (-{pct}%)"
    return False, ""


def is_lower_high(candles: list) -> tuple:
    """Three consecutive lower highs — downtrend confirmation."""
    if len(candles) < 3: return False, ""
    highs = [c["high"] for c in candles[-3:]]
    if highs[0] > highs[1] > highs[2]:
        return True, f"Lower highs: ${highs[0]:.2f} → ${highs[1]:.2f} → ${highs[2]:.2f}"
    return False, ""


def is_higher_low(candles: list) -> tuple:
    """
    Three consecutive higher lows = uptrend resuming.
    Shows systematic buying at progressively higher prices.
    """
    if len(candles) < 4:
        return False, ""
    lows = [c["low"] for c in candles[-4:]]
    if lows[-1] > lows[-2] > lows[-3]:
        return True, (f"Higher lows: ${lows[-3]:.2f} → ${lows[-2]:.2f} → ${lows[-1]:.2f}")
    return False, ""

def is_volume_spike(c: dict, candles: list) -> tuple:
    """
    Current candle volume 1.5x+ average of last 5 candles on a green candle.
    Institutional accumulation signal.
    """
    if len(candles) < 6 or c["volume"] == 0:
        return False, ""
    recent_vols = [x["volume"] for x in candles[-6:-1] if x["volume"] > 0]
    if not recent_vols:
        return False, ""
    avg_vol = sum(recent_vols) / len(recent_vols)
    if avg_vol == 0:
        return False, ""
    ratio = c["volume"] / avg_vol
    if ratio >= 1.5 and c["close"] > c["open"]:
        return True, f"Volume {ratio:.1f}x average — institutional buying"
    return False, ""

# ── Signal Aggregator ──────────────────────────────────────────────────

def is_good_entry_price(current_price: float, flow_stock_price: float,
                          flow_option_price: float, current_candle: dict) -> tuple:
    """
    Verify current price offers better entry than the flow alert.
    Returns (is_good, note).

    Good entry conditions:
    1. Stock is at or below flow entry price (haven't chased)
    2. Stock is near VWAP or a clear support level
    3. Option hasn't spiked far above flow fill price
    """
    notes = []
    score = 0

    # Check 1: Stock price vs flow entry
    if flow_stock_price and flow_stock_price > 0:
        pct_vs_flow = ((current_price - flow_stock_price) / flow_stock_price) * 100
        if pct_vs_flow <= 0:
            notes.append(f"Stock at/below flow entry (${current_price:.2f} vs ${flow_stock_price:.2f})")
            score += 2  # Best case — stock pulled back
        elif pct_vs_flow <= 1.5:
            notes.append(f"Stock near flow entry (+{pct_vs_flow:.1f}%)")
            score += 1  # Acceptable
        elif pct_vs_flow <= 3.0:
            notes.append(f"Stock {pct_vs_flow:.1f}% above flow — slight chase risk")
            score += 0  # Neutral
        else:
            notes.append(f"⚠️ Stock {pct_vs_flow:.1f}% above flow entry — chasing")
            score -= 1  # Penalize chasing

    # Check 2: Candle structure suggests support
    body = abs(current_candle["close"] - current_candle["open"])
    total = current_candle["high"] - current_candle["low"]
    if total > 0:
        lower_wick = min(current_candle["open"], current_candle["close"]) - current_candle["low"]
        if lower_wick > body * 1.5:
            notes.append("Strong lower wick — support holding")
            score += 1

    is_good = score >= 1
    return is_good, " | ".join(notes) if notes else "Entry price not verified"


def check_all_timeframes(ticker: str, flow_stock_price: float = None,
                          flow_option_price: float = None,
                          option_type: str = "call") -> list:
    """
    Fetch 1-min candles once, aggregate into all timeframes,
    check patterns on each. Returns list of signals found.

    flow_stock_price: stock price at time of flow alert (for entry quality check)
    flow_option_price: option avg fill at time of flow (for chasing check)
    """
    # Fetch enough 1-min candles to cover H1
    candles_1min = fetch_1min_candles(ticker, count=120)
    if len(candles_1min) < 5:
        print(f"[TECHNICAL] Not enough candles for {ticker}")
        return []

    # Calculate VWAP from all 1-min candles
    vwap = calc_vwap(candles_1min)

    # Current stock price = last 1-min close
    current_price = candles_1min[-1]["close"] if candles_1min else None

    # Check entry quality vs flow price
    entry_ok   = True
    entry_note = ""
    if flow_stock_price and current_price:
        entry_ok, entry_note = is_good_entry_price(
            current_price, flow_stock_price, flow_option_price, candles_1min[-1]
        )
        if not entry_ok:
            print(f"[TECHNICAL] {ticker} entry quality poor: {entry_note}")

    signals_found = []
    is_put = "put" in (option_type or "call").lower()

    for tf_label, period in TIMEFRAMES.items():
        candles = aggregate_candles(candles_1min, period)
        if len(candles) < 3:
            continue

        c    = candles[-1]
        prev = candles[-2]

        if is_put:
            # For puts: look for BEARISH signals (stock weakness = put gains)
            checks = [
                is_bearish_engulfing(c, prev),
                is_vwap_rejection(c, vwap),
                is_break_below_prev_low(c, prev),
                is_lower_high(candles),
                is_volume_spike(c, candles),  # Volume on down move
            ]
        else:
            # For calls: look for BULLISH signals (stock strength = call gains)
            checks = [
                is_bullish_hammer(c),
                is_bullish_engulfing(c, prev),
                is_vwap_bounce(c, vwap),
                is_break_above_prev_high(c, prev),
                is_morning_star(candles),
                is_higher_low(candles),
                is_volume_spike(c, candles),
            ]

        triggered = [note for ok, note in checks if ok]

        print(f"[TECHNICAL] {ticker} {tf_label}: {len(triggered)} signals — {triggered[:2]}")
        if len(triggered) >= 2:
            if len(triggered) >= 4:   strength = "STRONG 🚨"
            elif len(triggered) >= 3: strength = "MODERATE ✅"
            else:                     strength = "MILD ⚠️"

            # Downgrade strength if entry quality is poor
            if not entry_ok:
                if strength == "STRONG 🚨":   strength = "MODERATE ✅ ⚠️chasing"
                elif strength == "MODERATE ✅": strength = "MILD ⚠️ chasing"
                else:
                    print(f"[TECHNICAL] Skipping mild signal on {ticker} {tf_label} — entry quality poor")
                    continue  # Skip mild signals entirely if chasing

            signals_found.append({
                "ticker":       ticker,
                "timeframe":    tf_label,
                "signals":      triggered,
                "strength":     strength,
                "candle":       c,
                "vwap":         vwap,
                "current_price":current_price,
                "entry_ok":     entry_ok,
                "entry_note":   entry_note,
                "count":        len(triggered),
            })
            print(f"[TECHNICAL] 🎯 {ticker} {tf_label}: {len(triggered)} signals — {strength}")

    return signals_found

def build_entry_alert(watch_entry: dict, signal: dict) -> str:
    """Build Telegram entry alert message."""
    ticker   = signal["ticker"]
    tf       = signal["timeframe"]
    strength = signal["strength"]
    c        = signal["candle"]
    vwap     = signal["vwap"]
    strike   = watch_entry.get("strike","?")
    opt_type = watch_entry.get("option_type","call")[0].upper()
    expiry   = watch_entry.get("expiry","?")
    score    = watch_entry.get("flow_score","?")
    verdict  = watch_entry.get("verdict","WATCH")
    v_emoji  = {"TRADE":"✅","WATCH":"👀"}.get(verdict,"👀")

    body_pct = round(abs(c["close"]-c["open"]) / c["open"] * 100, 2) if c["open"] else 0
    candle_color = "🟢" if c["close"] >= c["open"] else "🔴"

    lines = [
        f"🎯 ENTRY SIGNAL: <b>{ticker}</b> {strength}",
        f"{v_emoji} Flow: {ticker} {strike}{opt_type} {expiry} [{score}/7 {verdict}]",
        f"",
        f"📊 <b>{tf}</b> {candle_color} O:{c['open']:.2f} H:{c['high']:.2f} "
        f"L:{c['low']:.2f} C:{c['close']:.2f} ({body_pct:+.2f}%)",
    ]

    if vwap:
        vwap_diff = round(((c["close"]-vwap)/vwap)*100, 2)
        lines.append(f"VWAP: ${vwap:.2f} | Price: {vwap_diff:+.2f}% vs VWAP")

    lines.append("")
    lines.append("✅ <b>Conditions met:</b>")
    for s in signal["signals"]:
        lines.append(f"  • {s}")

    lines.append("")
    lines.append(f"<b>→ Consider entry near ${c['close']:.2f}</b>")
    lines.append(f"<b>→ Stop loss: below ${c['low']:.2f}</b>")
    lines.append(f"<b>→ Target: ${round(c['close'] + (c['close'] - c['low']) * 2, 2):.2f} "
                 f"(2:1 R/R)</b>")

    return "\n".join(lines)

# ── Main Scanner ───────────────────────────────────────────────────────

def run_technical_scan(send_sms_fn):
    """
    Called every 5 minutes by scheduler.
    Scans all watched tickers across all timeframes.
    One Finnhub call per ticker (1-min candles), then aggregates locally.
    """
    now_et = datetime.now(ZoneInfo("America/New_York"))
    total  = now_et.hour * 60 + now_et.minute

    # Market hours only: 9:30 AM - 4:00 PM ET
    if total < 9*60+30 or total > 16*60:
        return

    if not _watch_list:
        return

    # Clean up expired or too-old watches
    to_remove = []
    for t, e in list(_watch_list.items()):
        age_days = (time.time() - e["added"]) / 86400
        dte      = e.get("dte_remaining")

        # Remove if: too old, DTE too low, or option expired
        if age_days > MAX_MONITOR_DAYS:
            print(f"[TECHNICAL] {t} removed — exceeded {MAX_MONITOR_DAYS} day monitor limit")
            to_remove.append(t)
        elif dte is not None and dte < MIN_DTE_TO_MONITOR:
            print(f"[TECHNICAL] {t} removed — only {dte}d left, too late to enter")
            to_remove.append(t)
        else:
            # Remove if option is >8% ITM — thesis fully played out, no entry value
            _strike_exp = float(e.get("strike", 0) or 0)
            _px_exp     = float(e.get("stock_price_at_alert", 0) or 0)
            _is_put_exp = "put" in (e.get("option_type","call") or "call").lower()
            if _strike_exp > 0 and _px_exp > 0:
                try:
                    from fetcher import fetch_price as _fpx_exp
                    _cur_px = _fpx_exp(t) or _px_exp
                    if not _is_put_exp:
                        _itm = (_cur_px - _strike_exp) / _strike_exp * 100
                    else:
                        _itm = (_strike_exp - _cur_px) / _strike_exp * 100
                    if _itm > 8.0:
                        print(f"[TECHNICAL] {t} removed — {_itm:.1f}% ITM, thesis complete")
                        to_remove.append(t)
                except: pass

    for t in to_remove:
        remove_from_watchlist(t)

    if not _watch_list:
        return


    # Prioritise tickers with active flow score >= 5.0
    _active  = {t: e for t, e in _watch_list.items() if float(e.get("flow_score",0) or 0) >= 5.0}
    _general = {t: e for t, e in _watch_list.items() if t not in _active}
    _wl_ordered = {**_active, **_general}

    print(f"[TECHNICAL] Scanning {len(_watch_list)} tickers — {now_et.strftime('%H:%M ET')}: {list(_watch_list.keys())}")

    for ticker, watch_entry in list(_wl_ordered.items()):
        try:
            time.sleep(0.3)  # ~120 req/min — at Tradier limit but fine
            # Update DTE remaining for cleanup logic
            expiry_raw = watch_entry.get("expiry_raw","")
            if expiry_raw:
                try:
                    from datetime import datetime as _dt
                    parts = expiry_raw.split("/")
                    m, d, y = parts
                    y   = "20" + y if len(y) == 2 else y
                    exp = _dt(int(y), int(m), int(d))
                    watch_entry["dte_remaining"] = (exp - _dt.now()).days
                except:
                    pass

            signals = check_all_timeframes(
                ticker,
                flow_stock_price=watch_entry.get("flow_stock_price"),
                flow_option_price=watch_entry.get("flow_option_price"),
                option_type=(watch_entry.get("option_type","call") or "call"),
            )

            # Filter signals not alerted in last 30 min
            new_signals = []
            for signal in signals:
                tf = signal["timeframe"]
                last_alert = watch_entry["alerted"].get(tf, 0)
                if time.time() - last_alert < 1800:
                    continue
                new_signals.append(signal)

            if new_signals:
                # Group all timeframes into ONE message per ticker
                strike   = watch_entry.get("strike","?")
                opt_type = watch_entry.get("option_type","call")[0].upper()
                expiry   = watch_entry.get("expiry","?")
                score    = watch_entry.get("flow_score","?")
                verdict  = watch_entry.get("verdict","WATCH")
                v_emoji  = {"TRADE":"✅","WATCH":"👀"}.get(verdict,"👀")
                # Best signal = highest timeframe (most reliable)
                # Pick the strongest signal (by signal count), break ties by highest timeframe
                _str_order = {"STRONG 🚨": 3, "MODERATE ✅": 2, "MILD ⚠️": 1}
                _tf_order  = ["M5","M10","M15","M30","H1"]
                best = max(new_signals, key=lambda s: (
                    _str_order.get(s.get("strength",""), 0),
                    _tf_order.index(s["timeframe"]) if s["timeframe"] in _tf_order else 0
                ))
                is_put   = "put" in (watch_entry.get("option_type","call") or "call").lower()
                tfs      = " + ".join(s["timeframe"] for s in new_signals)
                strength = best["strength"]
                # Ensure chasing warning has space
                if "chasing" in strength and "⚠️" in strength and "⚠️ " not in strength:
                    strength = strength.replace("⚠️chasing", "⚠️ chasing")
                c        = best["candle"]
                vwap     = best.get("vwap")

                lines = [
                    f"🎯 ENTRY: {ticker} {strength} [{tfs}]",
                    f"{v_emoji} {ticker} {strike}{opt_type} {expiry} [{score}/7 {verdict}]",
                ]
                if vwap:
                    vwap_diff  = round(((c["close"]-vwap)/vwap)*100, 2)
                    _direction = "above" if vwap_diff >= 0 else "below"
                    _vwap_good = (vwap_diff < 0) if is_put else (vwap_diff >= 0)
                    lines.append(f"VWAP: ${vwap:.2f} | Price {abs(vwap_diff):.2f}% {_direction}{'✅' if _vwap_good else ''}")
                lines.append(f"Best setup: {best['timeframe']} — {', '.join(best['signals'][:2])}")
                if is_put:
                    _stop_put   = round(c["high"], 2)
                    _target_put = round(c["close"] - (c["high"] - c["close"]) * 2, 2)
                    lines.append(f"→ Entry ~${c['close']:.2f} | Stop ${_stop_put:.2f} | Target ${_target_put:.2f}")
                else:
                    lines.append(f"→ Entry ~${c['close']:.2f} | Stop ${c['low']:.2f} | Target ${round(c['close']+(c['close']-c['low'])*2,2):.2f}")

                msg = chr(10).join(lines)
                import os as _os_tech
                from sms import send_telegram
                _bot  = _os_tech.environ.get("TELEGRAM_BOT_TOKEN","")
                # MILD → all-alerts channel (FYI); MODERATE+ → main channel
                if strength.startswith("MILD"):
                    _cid = _os_tech.environ.get("TELEGRAM_ALL_CHAT_ID","") or _os_tech.environ.get("TELEGRAM_CHAT_ID","")
                else:
                    _cid = _os_tech.environ.get("TELEGRAM_CHAT_ID","")
                if _bot and _cid:
                    send_telegram(msg, _bot, _cid)
                # Store technical confirmation timestamp for conviction scoring
                try:
                    from conviction import store_tech_confirmation
                    store_tech_confirmation(ticker)
                except: pass

                # Push to priority channel only when:
                # TRADE + STRONG signal + GEX aligned + 24h cooldown
                _verdict  = watch_entry.get("verdict","")
                _trade_ch = _os_tech.environ.get("TELEGRAM_TRADE_CHAT_ID","")
                _is_strong = strength.startswith("STRONG")  # STRONG only, not MODERATE
                if _trade_ch and _is_strong and _verdict == "TRADE":
                    # Staleness: >5% ITM = thesis played out
                    _stale     = False
                    _str_tc    = float(watch_entry.get("strike",0) or 0)
                    _isp_tc    = "put" in (watch_entry.get("option_type","call") or "call").lower()
                    _cur_px_tc = float(watch_entry.get("flow_stock_price",0) or 0)
                    if _str_tc > 0 and _cur_px_tc > 0:
                        _itm_tc = (_cur_px_tc-_str_tc)/_str_tc*100 if not _isp_tc else (_str_tc-_cur_px_tc)/_str_tc*100
                        if _itm_tc > 5.0:
                            _stale = True
                            print(f"[TECHNICAL] {ticker} {'call' if not _isp_tc else 'put'} {_itm_tc:.1f}% ITM — thesis done")
                    # Check 24h cooldown
                    _last_tc  = watch_entry.get("tech_confirm_alerted", 0)
                    _tc_age   = time.time() - float(_last_tc or 0)
                    if _tc_age > 86400:
                        # Quick GEX check — only alert if GEX also favorable
                        try:
                            from fetcher import fetch_gex as _fgex_tc, fetch_price as _fpx_tc
                            import time as _ttc; _ttc.sleep(5)
                            _gex_tc  = _fgex_tc(ticker)
                            _px_tc   = _fpx_tc(ticker) or _cur_px_tc
                            _flip_tc = _gex_tc.get("gamma_flip") if _gex_tc else None
                            _reg_tc  = _gex_tc.get("regime","") if _gex_tc else ""
                            _is_put_tc = "put" in (watch_entry.get("option_type","call") or "call").lower()
                            # GEX ok for calls: above flip; for puts: below flip
                            _gex_ok_tc = True
                            if _flip_tc and _px_tc:
                                if _is_put_tc:
                                    _gex_ok_tc = _px_tc <= (_flip_tc * 1.01)
                                else:
                                    _gex_ok_tc = _px_tc >= (_flip_tc * 0.995)
                            if _gex_ok_tc:
                                watch_entry["tech_confirm_alerted"] = time.time()
                                _flip_str_tc = f"| flip ${_flip_tc:.0f}" if _flip_tc else ""
                                _base_url_tc  = _os_tech.environ.get("BASE_URL","https://web-production-19e44.up.railway.app")
                                _aid_tc       = watch_entry.get("analysis_id","")
                                _prem_tc      = float(watch_entry.get("premium",0) or 0)
                                _fill_tc      = watch_entry.get("fill_label","") or watch_entry.get("fill_type","")
                                _sweep_tc     = watch_entry.get("is_sweep",False)
                                _voi_tc       = watch_entry.get("vol_oi_label","") or watch_entry.get("vol_oi_ratio","")
                                _entry_lim_tc = float(watch_entry.get("entry_limit_price",0) or 0)
                                _stop_tc      = float(watch_entry.get("stop_price",0) or 0)
                                _conv_tc      = watch_entry.get("conviction_total",0)
                                _conv_lbl_tc  = watch_entry.get("conviction_label","")
                                _tweet_tc     = watch_entry.get("tweet_url","")
                                _src_tc       = watch_entry.get("source","").upper()
                                _src_badge_tc = "🐦" if _src_tc == "FLOWGOD" else "🅱" if _src_tc == "BULLFLOW" else ""
                                _prem_str_tc  = (f"${_prem_tc/1_000_000:.1f}M" if _prem_tc >= 1_000_000
                                                 else f"${_prem_tc/1_000:.0f}K" if _prem_tc > 0 else "")

                                _tc_parts = [
                                    f"📡 TECHNICAL CONFIRMATION: ${ticker} {_src_badge_tc}",
                                    f"Signal: {strength} [{tfs}] + GEX aligned",
                                    f"Stock: ${_px_tc:.2f} {_flip_str_tc}",
                                    f"{msg.split(chr(10),2)[2] if chr(10) in msg else ''}",
                                    "",
                                    f"━━━ ORIGINAL FLOW ━━━",
                                ]
                                if _prem_str_tc:
                                    _flow_line_tc = _prem_str_tc
                                    if _fill_tc:  _flow_line_tc += f" {_fill_tc}"
                                    if _sweep_tc: _flow_line_tc += " ⚡ SWEEP"
                                    if _voi_tc:   _flow_line_tc += f" | {_voi_tc}"
                                    _tc_parts.append(_flow_line_tc)
                                if _entry_lim_tc:
                                    _tc_parts.append(f"💰 Limit: ${_entry_lim_tc:.2f} | Stop: ${_stop_tc:.2f}")
                                if _conv_tc:
                                    _tc_parts.append(f"📊 Conviction: {_conv_tc}/6 {_conv_lbl_tc}")
                                _otype_tc = (watch_entry.get("option_type","C") or "C")[0].upper()
                                _tc_parts += [
                                    "",
                                    f"Flow: ${ticker} {watch_entry.get('strike','')}{_otype_tc} "
                                    f"{watch_entry.get('expiry','')} [{watch_entry.get('flow_score','?')}/7 TRADE]",
                                    f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
                                ]
                                if _aid_tc:
                                    _tc_parts.append(f"🔗 {_base_url_tc}/analysis/{_aid_tc}")
                                if _tweet_tc:
                                    _tc_parts.append(f"🐦 {_tweet_tc}")
                                _upgrade_msg = "\n".join(p for p in _tc_parts if p is not None)
                                send_telegram(_upgrade_msg, _bot, _trade_ch)
                                print(f"[TECHNICAL] ⬆️ Pushed {ticker} STRONG+GEX to priority (24h cooldown started)")
                            else:
                                print(f"[TECHNICAL] {ticker} STRONG but GEX not aligned — skipping priority alert")
                        except Exception as _tce:
                            print(f"[TECHNICAL] Trade confirm GEX error: {_tce}")
                    else:
                        _h = round((86400 - _tc_age) / 3600, 1)
                        print(f"[TECHNICAL] {ticker} STRONG but in cooldown ({_h}h left)")

                # ENTRY WINDOW alert — fires on WATCH positions when GEX also aligns
                # This is the "when to enter" signal for positions you're monitoring
                if _trade_ch and _is_strong and _verdict == "WATCH":
                    try:
                        from fetcher import fetch_gex as _fgex_tw, fetch_price as _fpx_tw
                        import time as _t_tw; _t_tw.sleep(5)  # GEX rate limit
                        _gex_tw  = _fgex_tw(ticker)
                        _px_tw   = _fpx_tw(ticker) or 0
                        _flip_tw = _gex_tw.get("gamma_flip") if _gex_tw else None
                        _regime  = _gex_tw.get("regime","") if _gex_tw else ""
                        _cwall   = _gex_tw.get("call_wall") if _gex_tw else None
                        _pwall   = _gex_tw.get("put_wall")  if _gex_tw else None
                        _strikes = _gex_tw.get("strikes",[]) if _gex_tw else []

                        # Check cascade zones within 1% below
                        _cascade_near = False
                        if _strikes and _px_tw:
                            _danger = [s for s in _strikes
                                       if float(s["strike"]) < _px_tw
                                       and float(s["strike"]) > _px_tw * 0.99
                                       and float(s.get("net_gex",0)) < -2_000_000]
                            _cascade_near = len(_danger) > 0

                        # Entry window conditions differ for calls vs puts
                        # KEY INSIGHT: best entry is APPROACHING the flip, not after it
                        # The gamma flip break triggers the dealer cascade — enter before/at it
                        _is_call_ew = "put" not in (watch_entry.get("option_type","call") or "call").lower()

                        if _is_call_ew:
                            # CALLS: best entry = positive GEX + stock DECLINING TOWARD support
                            # Dealers mechanically buy at support → stock reverses up → call profits
                            # Need: (1) near support, (2) stock moving DOWN toward it

                            _regime_ok = True  # Both regimes can work but different logic

                            if _regime == "positive":
                                # Find nearest positive GEX support below spot
                                _supp_list = sorted(
                                    [s for s in _strikes
                                     if float(s["strike"]) < _px_tw
                                     and float(s.get("net_gex", 0)) > 2_000_000],
                                    key=lambda s: float(s["strike"]), reverse=True
                                )
                                if _supp_list:
                                    _s_strike = float(_supp_list[0]["strike"])
                                    _s_gex    = float(_supp_list[0].get("net_gex", 0))
                                    _dist_pct = (_px_tw - _s_strike) / _px_tw * 100

                                    # Check if stock is DECLINING toward support
                                    _declining = False
                                    try:
                                        from fetcher import fetch_1min_candles as _f1m
                                        _c5 = _f1m(ticker, count=10)
                                        if len(_c5) >= 4:
                                            _declining = _c5[-1]["close"] < _c5[-4]["close"]
                                    except: pass

                                    _near_support = 0.3 <= _dist_pct <= 2.0
                                    _at_support   = _dist_pct < 0.3
                                    _direction    = "declining" if _declining else "flat"

                                    if (_near_support and _declining) or _at_support:
                                        _gex_good = True
                                        _supp_str_ew = (
                                            f"✅ Pos GEX support ${_s_strike:.0f} "
                                            f"({_s_gex/1_000_000:.0f}M, {_dist_pct:.1f}% below) — "
                                            f"{'stock declining toward it' if _approaching else 'stock at support'}"
                                        )
                                    else:
                                        _gex_good = False
                                        # Stock near support but not declining — wait
                                else:
                                    _gex_good = False  # No nearby support found

                            else:  # Negative GEX
                                # For negative GEX calls: approaching gamma flip from below
                                _flip_ok = True
                                if _flip_tw and _px_tw:
                                    _dist = (_flip_tw - _px_tw) / _px_tw * 100
                                    _flip_ok = 0 <= _dist <= 3.0
                                _room_ok = True
                                if _cwall and _px_tw:
                                    _room_ok = (_cwall - _px_tw) / _px_tw >= 0.015
                                _gex_good = _flip_ok and _room_ok and not _cascade_near
                        else:
                            # PUTS: best entry = positive GEX + stock RISING TOWARD resistance
                            # Dealers mechanically SELL at resistance → stock reverses down → put profits
                            # Need: (1) near resistance wall, (2) stock moving UP toward it

                            if _regime == "positive":
                                # Find nearest positive GEX resistance ABOVE spot
                                _res_list = sorted(
                                    [s for s in _strikes
                                     if float(s["strike"]) > _px_tw
                                     and float(s.get("net_gex", 0)) > 2_000_000],
                                    key=lambda s: float(s["strike"])
                                )
                                if _res_list:
                                    _r_strike = float(_res_list[0]["strike"])
                                    _r_gex    = float(_res_list[0].get("net_gex", 0))
                                    _dist_pct = (_r_strike - _px_tw) / _px_tw * 100

                                    # Check if stock is RISING toward resistance
                                    _rising = False
                                    try:
                                        from fetcher import fetch_1min_candles as _f1m_p
                                        _cp5 = _f1m_p(ticker, count=10)
                                        if len(_cp5) >= 4:
                                            _rising = _cp5[-1]["close"] > _cp5[-4]["close"]
                                    except: pass

                                    _near_resist = 0.3 <= _dist_pct <= 2.0
                                    _at_resist   = _dist_pct < 0.3
                                    _direction   = "rising" if _rising else "flat"

                                    if (_near_resist and _rising) or _at_resist:
                                        _gex_good = True
                                    else:
                                        _gex_good = False
                                else:
                                    _gex_good = False  # No nearby resistance

                            else:  # Negative GEX puts
                                # Approaching gamma flip from above
                                _flip_ok = True
                                if _flip_tw and _px_tw:
                                    _dist = (_px_tw - _flip_tw) / _px_tw * 100
                                    _flip_ok = 0 <= _dist <= 3.0
                                _room_ok = True
                                if _pwall and _px_tw:
                                    _room_ok = (_px_tw - _pwall) / _px_tw >= 0.015
                                _gex_good = _flip_ok and _room_ok

                        # Staleness check: option too deep ITM = thesis already played out
                        _strike_ew   = float(watch_entry.get("strike", 0) or 0)
                        _is_put_ew   = not _is_call_ew
                        _stale_ew    = False
                        if _strike_ew > 0 and _px_tw > 0:
                            if not _is_put_ew:
                                _itm_ew = (_px_tw - _strike_ew) / _strike_ew * 100
                                if _itm_ew > 5.0:
                                    _stale_ew = True
                                    print(f"[TECHNICAL] {ticker} call {_itm_ew:.1f}% ITM — thesis done, skipping entry window")
                            else:
                                _itm_ew = (_strike_ew - _px_tw) / _strike_ew * 100
                                if _itm_ew > 5.0:
                                    _stale_ew = True
                                    print(f"[TECHNICAL] {ticker} put {_itm_ew:.1f}% ITM — thesis done, skipping entry window")

                        # Cooldown: only fire entry window once per 24h per ticker
                        _last_ew = watch_entry.get("entry_window_alerted", 0)
                        _ew_age  = time.time() - float(_last_ew or 0)
                        _ew_ok   = _ew_age > 86400 and not _stale_ew  # 24 hours

                        if _gex_good and _ew_ok:
                            watch_entry["entry_window_alerted"] = time.time()
                            # Build entry window message
                            _opt_type = watch_entry.get("option_type","call")
                            _strike   = watch_entry.get("strike","?")
                            _expiry   = watch_entry.get("expiry","?")
                            _score    = watch_entry.get("flow_score","?")
                            _flip_str = f"gamma flip ${_flip_tw:.0f}" if _flip_tw else "no flip data"
                            if _is_call_ew:
                                _wall_str = f"Call wall ${_cwall:.0f} (+{round((_cwall-_px_tw)/_px_tw*100,1):.1f}%) — target/resistance" if _cwall else ""
                                _supp_str = f"Stop: ${_pwall:.0f} (put wall)" if _pwall else ""
                            else:
                                _wall_str = f"Put wall ${_pwall:.0f} (-{round((_px_tw-_pwall)/_px_tw*100,1):.1f}%) — target/support" if _pwall else ""
                                _supp_str = f"Stop: ${_cwall:.0f} (call wall — if stock reverses above)" if _cwall else ""
                            if _is_call_ew:
                                if _regime == "positive":
                                    _regime_str = "🧲 Pos GEX — dealers BUY dips (support bounce setup)"
                                else:
                                    _regime_str = "⚡ Neg GEX — moves amplify (breakout setup)"
                            else:
                                if _regime == "positive":
                                    _regime_str = "🧲 Pos GEX — dealers SELL rallies (resistance fade setup)"
                                else:
                                    _regime_str = "⚡ Neg GEX — drops accelerate"
                            _cascade_str = "✅ No cascade zones nearby" if not _cascade_near else ""

                            # Days since original flow alert
                            _flow_ts    = float(watch_entry.get("added_at", time.time()))
                            _days_since = int((time.time() - _flow_ts) / 86400)
                            _age_str    = f"Day {_days_since+1} since flow" if _days_since > 0 else "Same day as flow"

                            _base_url_ew    = _os_tech.environ.get("BASE_URL","https://web-production-19e44.up.railway.app")
                            _analysis_id_ew = watch_entry.get("analysis_id","")
                            _premium_ew     = float(watch_entry.get("premium",0) or 0)
                            _fill_ew        = watch_entry.get("fill_label","") or watch_entry.get("fill_type","")
                            _voi_ew         = watch_entry.get("vol_oi_label","") or watch_entry.get("vol_oi_ratio","")
                            _sweep_ew       = watch_entry.get("is_sweep",False)
                            _entry_lim_ew   = float(watch_entry.get("entry_limit_price",0) or 0)
                            _stop_ew        = float(watch_entry.get("stop_price",0) or 0)
                            _conv_ew        = watch_entry.get("conviction_total",0)
                            _conv_lbl_ew    = watch_entry.get("conviction_label","")
                            _tweet_ew       = watch_entry.get("tweet_url","")
                            _src_ew         = watch_entry.get("source","").upper()
                            _src_badge_ew   = "🐦" if _src_ew == "FLOWGOD" else "🅱" if _src_ew == "BULLFLOW" else ""

                            # Format premium
                            _prem_str_ew = (f"${_premium_ew/1_000_000:.1f}M" if _premium_ew >= 1_000_000
                                           else f"${_premium_ew/1_000:.0f}K" if _premium_ew > 0 else "")

                            _entry_parts = [
                                f"🎯 ENTRY WINDOW: ${ticker} {_src_badge_ew}",
                                f"Technical confirmed — {strength} [{tfs}] | 📅 {_age_str}",
                                "",
                                f"━━━ ORIGINAL FLOW ━━━",
                            ]
                            if _prem_str_ew:
                                _flow_line = _prem_str_ew
                                if _fill_ew:  _flow_line += f" {_fill_ew}"
                                if _sweep_ew: _flow_line += " ⚡ SWEEP"
                                if _voi_ew:   _flow_line += f" | {_voi_ew}"
                                _entry_parts.append(_flow_line)
                            if _entry_lim_ew:
                                _entry_parts.append(f"💰 Limit: ${_entry_lim_ew:.2f} | Stop: ${_stop_ew:.2f}")
                            if _conv_ew:
                                _entry_parts.append(f"📊 Conviction: {_conv_ew}/6 {_conv_lbl_ew}")
                            _entry_parts += [
                                "",
                                f"━━━ ENTRY SIGNAL ━━━",
                                f"Stock: ${_px_tw:.2f} | {_flip_str}",
                                f"{_regime_str}",
                                f"{_wall_str}",
                                f"{_supp_str}",
                                f"{_cascade_str}",
                                f"→ {msg.split(chr(10))[-2] if chr(10) in msg else ''}",
                                "",
                                f"👀 ${ticker} {_strike}{str(_opt_type)[0].upper()} {_expiry} [{_score}/7 WATCH]",
                                f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
                            ]
                            if _analysis_id_ew:
                                _entry_parts.append(f"🔗 {_base_url_ew}/analysis/{_analysis_id_ew}")
                            if _tweet_ew:
                                _entry_parts.append(f"🐦 {_tweet_ew}")

                            _entry_msg = "\n".join(p for p in _entry_parts if p is not None)
                            send_telegram(_entry_msg, _bot, _trade_ch)
                            print(f"[TECHNICAL] 🎯 ENTRY WINDOW sent: {ticker} — GEX+tech aligned")
                            # Persist cooldown timestamp
                            try:
                                from storage import save_data as _sd_ew
                                _sd_ew("watchlist", _get_watchlist_raw())
                            except: pass
                        elif _gex_good and not _ew_ok:
                            _hours_left = round((86400 - _ew_age) / 3600, 1)
                            print(f"[TECHNICAL] {ticker} GEX+tech aligned but entry window cooldown ({_hours_left}h left)")
                        else:
                            _reason = "cascade zone nearby" if _cascade_near else "below flip" if not _above_flip else "wall too close"
                            print(f"[TECHNICAL] {ticker} signal good but GEX not aligned ({_reason})")
                    except Exception as _ew_e:
                        print(f"[TECHNICAL] Entry window GEX error: {_ew_e}")

                for signal in new_signals:
                    watch_entry["alerted"][signal["timeframe"]] = time.time()
                print(f"[TECHNICAL] Alert sent: {ticker} [{tfs}] {strength}")

            # Polygon free tier: 5 calls/minute = 12s between calls
            time.sleep(13)

        except Exception as e:
            print(f"[TECHNICAL] Scan error {ticker}: {e}")
