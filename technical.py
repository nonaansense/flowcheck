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

    _watch_list[ticker] = {
        "added":            time.time(),
        "strike":           trade.get("strike","?"),
        "option_type":      trade.get("option_type","call"),
        "expiry":           trade.get("expiry","?"),
        "expiry_raw":       expiry_raw,
        "flow_score":       result.get("final_score",0),
        "verdict":          result.get("verdict","WATCH"),
        "flow_stock_price": flow_stock_price,
        "flow_option_price":flow_option_price,
        "dte_remaining":    dte_remaining,
        "alerted":          {},
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
                          flow_option_price: float = None) -> list:
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

    for tf_label, period in TIMEFRAMES.items():
        candles = aggregate_candles(candles_1min, period)
        if len(candles) < 3:
            continue

        c    = candles[-1]
        prev = candles[-2]

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

    for t in to_remove:
        remove_from_watchlist(t)

    if not _watch_list:
        return

    print(f"[TECHNICAL] Scanning {len(_watch_list)} tickers — {now_et.strftime('%H:%M ET')}: {list(_watch_list.keys())}")

    for ticker, watch_entry in list(_watch_list.items()):
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
                best     = max(new_signals, key=lambda s: ["M5","M10","M15","M30","H1"].index(s["timeframe"])
                               if s["timeframe"] in ["M5","M10","M15","M30","H1"] else 0)
                tfs      = " + ".join(s["timeframe"] for s in new_signals)
                strength = best["strength"]
                c        = best["candle"]
                vwap     = best.get("vwap")

                lines = [
                    f"🎯 ENTRY: {ticker} {strength} [{tfs}]",
                    f"{v_emoji} {ticker} {strike}{opt_type} {expiry} [{score}/7 {verdict}]",
                ]
                if vwap:
                    vwap_diff = round(((c["close"]-vwap)/vwap)*100, 2)
                    lines.append(f"VWAP: ${vwap:.2f} | Price {vwap_diff:+.2f}% above")
                lines.append(f"Best setup: {best['timeframe']} — {', '.join(best['signals'][:2])}")
                lines.append(f"→ Entry ~${c['close']:.2f} | Stop ${c['low']:.2f} | Target ${round(c['close']+(c['close']-c['low'])*2,2):.2f}")

                msg = chr(10).join(lines)
                import os as _os_tech
                from sms import send_telegram
                _bot  = _os_tech.environ.get("TELEGRAM_BOT_TOKEN","")
                _cid  = _os_tech.environ.get("TELEGRAM_CHAT_ID","")
                if _bot and _cid:
                    send_telegram(msg, _bot, _cid)
                for signal in new_signals:
                    watch_entry["alerted"][signal["timeframe"]] = time.time()
                print(f"[TECHNICAL] Alert sent: {ticker} [{tfs}] {strength}")

            # Polygon free tier: 5 calls/minute = 12s between calls
            time.sleep(13)

        except Exception as e:
            print(f"[TECHNICAL] Scan error {ticker}: {e}")
