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

def add_to_watchlist(ticker: str, trade: dict, result: dict):
    if ticker in _watch_list:
        print(f"[TECHNICAL] {ticker} already in watchlist — updating")
    _watch_list[ticker] = {
        "added":       time.time(),
        "strike":      trade.get("strike","?"),
        "option_type": trade.get("option_type","call"),
        "expiry":      trade.get("expiry","?"),
        "flow_score":  result.get("final_score",0),
        "verdict":     result.get("verdict","WATCH"),
        "alerted":     {},  # tf_label → last alert timestamp
    }
    print(f"[TECHNICAL] Added {ticker} to watchlist "
          f"({result.get('verdict')} {result.get('final_score')}/7) "
          f"— monitoring M5/M10/M15/M30/H1")

def remove_from_watchlist(ticker: str):
    _watch_list.pop(ticker, None)
    print(f"[TECHNICAL] Removed {ticker} from watchlist")

def get_watchlist() -> dict:
    return _watch_list

# ── Finnhub 1-min candles ──────────────────────────────────────────────
def fh_key():
    return os.environ.get("FINNHUB_API_KEY")

def fetch_1min_candles(ticker: str, count: int = 120) -> list:
    """
    Fetch last `count` 1-minute candles from Finnhub.
    Returns list oldest→newest, each dict: open/high/low/close/volume/timestamp
    """
    key = fh_key()
    if not key:
        print("[TECHNICAL] FINNHUB_API_KEY not set")
        return []

    now_ts   = int(time.time())
    from_ts  = now_ts - (count + 30) * 60  # Extra buffer for market gaps

    try:
        r = requests.get(
            "https://finnhub.io/api/v1/stock/candle",
            params={
                "symbol":     ticker,
                "resolution": "1",
                "from":       from_ts,
                "to":         now_ts,
                "token":      key,
            },
            timeout=10
        )
        if r.status_code != 200:
            print(f"[TECHNICAL] Finnhub {r.status_code} for {ticker}")
            return []

        data = r.json()
        if data.get("s") != "ok":
            print(f"[TECHNICAL] Finnhub no data for {ticker}: {data.get('s')}")
            return []

        candles = []
        opens   = data.get("o", [])
        highs   = data.get("h", [])
        lows    = data.get("l", [])
        closes  = data.get("c", [])
        volumes = data.get("v", [])
        times   = data.get("t", [])

        for i in range(len(closes)):
            candles.append({
                "timestamp": times[i] if i < len(times) else 0,
                "open":      float(opens[i]),
                "high":      float(highs[i]),
                "low":       float(lows[i]),
                "close":     float(closes[i]),
                "volume":    float(volumes[i]) if i < len(volumes) else 0,
            })

        # Return only the last `count` candles
        return candles[-count:] if len(candles) > count else candles

    except Exception as e:
        print(f"[TECHNICAL] Finnhub candle error {ticker}: {e}")
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

def check_all_timeframes(ticker: str) -> list:
    """
    Fetch 1-min candles once, aggregate into all timeframes,
    check patterns on each. Returns list of signals found.
    """
    # Fetch enough 1-min candles to cover H1 (need 60 + some history for patterns)
    candles_1min = fetch_1min_candles(ticker, count=120)
    if len(candles_1min) < 5:
        print(f"[TECHNICAL] Not enough candles for {ticker}")
        return []

    # Calculate VWAP from all 1-min candles
    vwap = calc_vwap(candles_1min)

    signals_found = []

    for tf_label, period in TIMEFRAMES.items():
        # Aggregate candles
        candles = aggregate_candles(candles_1min, period)
        if len(candles) < 3:
            continue

        c    = candles[-1]   # Most recent complete candle
        prev = candles[-2]   # Previous candle

        # Run all pattern checks
        checks = [
            is_bullish_hammer(c),
            is_bullish_engulfing(c, prev),
            is_vwap_bounce(c, vwap),
            is_break_above_prev_high(c, prev),
            is_morning_star(candles),
            is_higher_low(candles),
            is_volume_spike(c, candles),
        ]

        triggered = [(note) for ok, note in checks if ok]

        if len(triggered) >= 2:
            if len(triggered) >= 4:   strength = "STRONG 🚨"
            elif len(triggered) >= 3: strength = "MODERATE ✅"
            else:                     strength = "MILD ⚠️"

            signals_found.append({
                "ticker":    ticker,
                "timeframe": tf_label,
                "signals":   triggered,
                "strength":  strength,
                "candle":    c,
                "vwap":      vwap,
                "count":     len(triggered),
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

    # Remove end-of-day or stale watches
    to_remove = [
        t for t, e in list(_watch_list.items())
        if total >= 16*60 or (time.time() - e["added"]) > 8*3600
    ]
    for t in to_remove:
        remove_from_watchlist(t)

    if not _watch_list:
        return

    print(f"[TECHNICAL] Scanning {len(_watch_list)} tickers — {now_et.strftime('%H:%M ET')}")

    for ticker, watch_entry in list(_watch_list.items()):
        try:
            signals = check_all_timeframes(ticker)

            for signal in signals:
                tf = signal["timeframe"]
                # Don't re-alert same ticker+timeframe within 30 minutes
                last_alert = watch_entry["alerted"].get(tf, 0)
                if time.time() - last_alert < 1800:
                    continue

                msg = build_entry_alert(watch_entry, signal)
                send_sms_fn(msg)
                watch_entry["alerted"][tf] = time.time()
                print(f"[TECHNICAL] Alert sent: {ticker} {tf} — {signal['strength']}")

            # Small delay between tickers to avoid Finnhub rate limit
            time.sleep(1)

        except Exception as e:
            print(f"[TECHNICAL] Scan error {ticker}: {e}")
