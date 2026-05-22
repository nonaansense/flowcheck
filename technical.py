"""
Technical analysis engine for FlowCheck.
Monitors WATCH/TRADE tickers for entry signals on M5/M10/M15/M30/H1.
Uses Twelve Data API (free tier: 800 calls/day).

Signal fires when 2+ conditions align on same timeframe:
1. Bullish hammer
2. Bullish engulfing
3. VWAP bounce
4. Break above previous candle high
5. Morning star (3-candle)
6. Higher low forming
7. Volume confirmation
"""
import os, time, requests
from datetime import datetime
from zoneinfo import ZoneInfo

TD_BASE    = "https://api.twelvedata.com"
TIMEFRAMES = ["5min", "10min", "15min", "30min", "1h"]

# ── Active watch list ──────────────────────────────────────────────────
# { ticker: {"added": timestamp, "strike": "435", "expiry": "Jun 5", "flow_score": 5} }
_watch_list: dict = {}

def add_to_watchlist(ticker: str, trade: dict, result: dict):
    """Add a WATCH/TRADE ticker to the technical monitoring list."""
    _watch_list[ticker] = {
        "added":      time.time(),
        "strike":     trade.get("strike","?"),
        "option_type":trade.get("option_type","call"),
        "expiry":     trade.get("expiry","?"),
        "flow_score": result.get("final_score",0),
        "verdict":    result.get("verdict","WATCH"),
        "alerted":    {},  # timeframe → last alert timestamp (avoid spam)
    }
    print(f"[TECHNICAL] Added {ticker} to watchlist "
          f"({result.get('verdict')} {result.get('final_score')}/7)")

def remove_from_watchlist(ticker: str):
    _watch_list.pop(ticker, None)
    print(f"[TECHNICAL] Removed {ticker} from watchlist")

def get_watchlist() -> dict:
    return _watch_list

# ── Twelve Data ────────────────────────────────────────────────────────
def td_key():
    return os.environ.get("TWELVE_DATA_API_KEY")

def td_get(path: str, params: dict = None) -> dict | None:
    key = td_key()
    if not key:
        return None
    p = {**(params or {}), "apikey": key}
    try:
        r = requests.get(f"{TD_BASE}{path}", params=p, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "error":
                print(f"[TECHNICAL] TD error: {data.get('message','')[:60]}")
                return None
            return data
        elif r.status_code == 429:
            print("[TECHNICAL] Twelve Data rate limit")
        else:
            print(f"[TECHNICAL] TD {r.status_code}: {path}")
    except Exception as e:
        print(f"[TECHNICAL] TD exception: {str(e)[:60]}")
    return None

def fetch_candles(ticker: str, interval: str, count: int = 20) -> list:
    """Fetch recent candles. Returns list oldest→newest."""
    data = td_get("/time_series", {
        "symbol":     ticker,
        "interval":   interval,
        "outputsize": count,
        "format":     "JSON",
    })
    if not data or "values" not in data:
        return []
    candles = []
    for v in reversed(data["values"]):  # TD returns newest first → reverse
        try:
            candles.append({
                "datetime": v.get("datetime",""),
                "open":     float(v["open"]),
                "high":     float(v["high"]),
                "low":      float(v["low"]),
                "close":    float(v["close"]),
                "volume":   float(v.get("volume", 0) or 0),
            })
        except:
            continue
    return candles

def fetch_vwap(ticker: str) -> float | None:
    """Fetch current VWAP from Twelve Data."""
    data = td_get("/vwap", {"symbol": ticker, "interval": "5min"})
    if data and data.get("values"):
        try:
            return float(data["values"][0]["vwap"])
        except:
            pass
    return None

# ── Pattern Detection ──────────────────────────────────────────────────

def is_bullish_hammer(c: dict) -> tuple:
    body       = abs(c["close"] - c["open"])
    total      = c["high"] - c["low"]
    if total == 0: return False, ""
    lower_wick = min(c["open"], c["close"]) - c["low"]
    upper_wick = c["high"] - max(c["open"], c["close"])
    body_pct   = body / total
    if (body_pct < 0.35 and lower_wick >= 2 * body
            and upper_wick < body and c["close"] >= c["open"]):
        return True, f"Bullish hammer (wick {lower_wick:.2f} / body {body:.2f})"
    return False, ""

def is_bullish_engulfing(c: dict, prev: dict) -> tuple:
    if (prev["close"] < prev["open"] and c["close"] > c["open"]
            and c["open"] <= prev["close"] and c["close"] >= prev["open"]):
        pct = round(abs(c["close"]-c["open"]) / max(abs(prev["close"]-prev["open"]),0.01) * 100)
        return True, f"Bullish engulfing ({pct:.0f}% body vs prev)"
    return False, ""

def is_vwap_bounce(c: dict, vwap: float) -> tuple:
    if not vwap: return False, ""
    if (c["low"] <= vwap * 1.002 and c["close"] > vwap
            and c["close"] > c["open"]):
        dist = round(((c["close"] - vwap) / vwap) * 100, 2)
        return True, f"VWAP bounce — close {dist}% above VWAP ${vwap:.2f}"
    return False, ""

def is_break_above_prev_high(c: dict, prev: dict) -> tuple:
    if c["close"] > prev["high"] and c["close"] > c["open"]:
        pct = round(((c["close"] - prev["high"]) / prev["high"]) * 100, 2)
        return True, f"Break above prev high ${prev['high']:.2f} (+{pct}%)"
    return False, ""

def is_morning_star(candles: list) -> tuple:
    if len(candles) < 3: return False, ""
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    if (c1["close"] < c1["open"]
            and abs(c1["close"]-c1["open"]) > (c1["high"]-c1["low"]) * 0.5
            and abs(c2["close"]-c2["open"]) < abs(c1["close"]-c1["open"]) * 0.3
            and c3["close"] > c3["open"]
            and c3["close"] > (c1["open"] + c1["close"]) / 2):
        return True, "Morning star (3-candle reversal)"
    return False, ""

def is_higher_low(candles: list) -> tuple:
    if len(candles) < 4: return False, ""
    lows = [c["low"] for c in candles[-4:]]
    if lows[-1] > lows[-2] > lows[-3]:
        return True, f"Higher low: {lows[-3]:.2f} → {lows[-2]:.2f} → {lows[-1]:.2f}"
    return False, ""

def is_volume_spike(c: dict, candles: list) -> tuple:
    if len(candles) < 6 or c["volume"] == 0: return False, ""
    avg = sum(x["volume"] for x in candles[-6:-1]) / 5
    if avg == 0: return False, ""
    ratio = c["volume"] / avg
    if ratio > 1.5 and c["close"] > c["open"]:
        return True, f"Volume {ratio:.1f}x average — institutional buying"
    return False, ""

# ── Signal Aggregator ──────────────────────────────────────────────────

def check_entry_signals(ticker: str, interval: str) -> dict | None:
    """
    Check all entry conditions on given timeframe.
    Returns signal dict if 2+ conditions align, else None.
    """
    candles = fetch_candles(ticker, interval, count=20)
    if len(candles) < 3:
        return None

    c    = candles[-1]
    prev = candles[-2]
    vwap = fetch_vwap(ticker)

    # Check all patterns
    signals = []
    checks = [
        is_bullish_hammer(c),
        is_bullish_engulfing(c, prev),
        is_vwap_bounce(c, vwap),
        is_break_above_prev_high(c, prev),
        is_morning_star(candles),
        is_higher_low(candles),
        is_volume_spike(c, candles),
    ]

    for triggered, note in checks:
        if triggered:
            signals.append(note)

    if len(signals) < 2:
        return None

    # Calculate signal strength
    strength = len(signals)
    if strength >= 4:   strength_label = "STRONG 🚨"
    elif strength >= 3: strength_label = "MODERATE ✅"
    else:               strength_label = "MILD ⚠️"

    tf_label = {
        "5min":"M5","10min":"M10","15min":"M15",
        "30min":"M30","1h":"H1"
    }.get(interval, interval)

    return {
        "ticker":    ticker,
        "timeframe": tf_label,
        "interval":  interval,
        "signals":   signals,
        "strength":  strength_label,
        "candle":    c,
        "vwap":      vwap,
        "timestamp": c.get("datetime",""),
    }

def build_entry_alert(watch_entry: dict, signal: dict) -> str:
    """Build Telegram message for entry signal."""
    ticker    = signal["ticker"]
    tf        = signal["timeframe"]
    strength  = signal["strength"]
    c         = signal["candle"]
    vwap      = signal["vwap"]
    strike    = watch_entry.get("strike","?")
    opt_type  = watch_entry.get("option_type","call")[0].upper()
    expiry    = watch_entry.get("expiry","?")
    score     = watch_entry.get("flow_score","?")
    verdict   = watch_entry.get("verdict","WATCH")

    verdict_emoji = {"TRADE":"✅","WATCH":"👀"}.get(verdict,"👀")

    lines = [
        f"🎯 ENTRY SIGNAL: {ticker} {strength}",
        f"{verdict_emoji} Flow: {ticker} {strike}{opt_type} {expiry} [{score}/7 {verdict}]",
        f"",
        f"📊 {tf} candle: O:{c['open']:.2f} H:{c['high']:.2f} L:{c['low']:.2f} C:{c['close']:.2f}",
    ]

    if vwap:
        lines.append(f"VWAP: ${vwap:.2f} | Price vs VWAP: {((c['close']-vwap)/vwap*100):+.2f}%")

    lines.append("")
    lines.append("✅ Conditions met:")
    for s in signal["signals"]:
        lines.append(f"  • {s}")

    lines.append("")
    lines.append(f"<b>→ Consider entry near ${c['close']:.2f}</b>")
    lines.append(f"<b>→ Stop loss: below ${c['low']:.2f}</b>")
    lines.append(f"⏰ Signal time: {signal['timestamp']}")

    return "\n".join(lines)

# ── Main Scanner ───────────────────────────────────────────────────────

def run_technical_scan(send_sms_fn):
    """
    Called every 5 minutes by scheduler.
    Scans all watched tickers across all timeframes.
    Fires alert if 2+ conditions align and no recent alert for that tf.
    """
    now_et = datetime.now(ZoneInfo("America/New_York"))
    total  = now_et.hour * 60 + now_et.minute

    # Only scan during market hours 9:30 AM - 4:00 PM ET
    if total < 9*60+30 or total > 16*60:
        return

    if not _watch_list:
        return

    print(f"[TECHNICAL] Scanning {len(_watch_list)} tickers across {len(TIMEFRAMES)} timeframes...")

    # Remove expired watches (end of day = after 4 PM)
    to_remove = []
    for ticker, entry in list(_watch_list.items()):
        age_hours = (time.time() - entry["added"]) / 3600
        if age_hours > 8 or total >= 16*60:  # Max 8 hours or end of day
            to_remove.append(ticker)
    for t in to_remove:
        remove_from_watchlist(t)

    # Scan each ticker
    for ticker, watch_entry in list(_watch_list.items()):
        for interval in TIMEFRAMES:
            # Throttle — don't re-alert same ticker+timeframe within 30 min
            last_alert = watch_entry["alerted"].get(interval, 0)
            if time.time() - last_alert < 1800:
                continue

            try:
                signal = check_entry_signals(ticker, interval)
                if signal:
                    msg = build_entry_alert(watch_entry, signal)
                    print(f"[TECHNICAL] 🎯 Entry signal: {ticker} {signal['timeframe']} "
                          f"— {len(signal['signals'])} conditions")
                    send_sms_fn(msg)
                    watch_entry["alerted"][interval] = time.time()

                time.sleep(1)  # Avoid hitting TD rate limit

            except Exception as e:
                print(f"[TECHNICAL] Scan error {ticker} {interval}: {e}")
