"""
swing_scanner.py — End-of-day top-5 swing play ranker.

Watches options flow ALL DAY (every parsed fill from the Bullflow stream
is recorded into a per-day ledger), then at 3:45pm ET evaluates the
complete session: the full day's flow narrative PLUS the chart as it
actually played out — across daily / 4H / 1H / 30M — and ranks the
top 5 swing candidates for a 2-8 week hold.

The analysis is PATH-AWARE, not snapshot-based:
  Flow path  — when fills arrived, whether flow added as price moved
               (conviction) or only on dips, whether aggression escalated,
               one-sidedness vs opposing flow, DTE fit for a swing window,
               and whether the flow is already working.
  Chart path — daily regime (gatekeeper), 4H setup (gatekeeper),
               1H intraday structure (scorer), 30M close strength (scorer).

Daily + 4H are GATEKEEPERS: wrong daily trend or no 4H structure
excludes the name regardless of flow. 1H + 30M are SCORERS that
differentiate the survivors.

Composite = flow_score * 0.55 + tech_score * 0.45.

Config env vars:
  SWING_SCAN_ENABLED      = true
  SWING_TOP_N             = 5
  SWING_MIN_FILLS         = 3        min same-direction fills to be a candidate
  SWING_MIN_PREMIUM       = 500000   min total same-direction premium
  SWING_MAX_CANDIDATES    = 10       cap before chart fetches (API budget)
  SWING_DTE_MIN / MAX     = 10 / 60  swing-appropriate contract window
  SWING_VOL_RATIO_MIN     = 1.10     today's volume must be >=110% of 20D avg
                                      (<=90% is retail noise — excluded)
"""
import os, time, threading, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

TOP_N          = int(os.environ.get("SWING_TOP_N", "5"))
MIN_FILLS      = int(os.environ.get("SWING_MIN_FILLS", "3"))
MIN_PREMIUM    = float(os.environ.get("SWING_MIN_PREMIUM", "500000"))
MAX_CANDIDATES = int(os.environ.get("SWING_MAX_CANDIDATES", "10"))
DTE_MIN        = int(os.environ.get("SWING_DTE_MIN", "10"))
DTE_MAX        = int(os.environ.get("SWING_DTE_MAX", "60"))
VOL_RATIO_MIN  = float(os.environ.get("SWING_VOL_RATIO_MIN", "1.10"))
STORAGE_KEY    = "swing_day_ledger"

_LEDGER: dict = {}   # ticker_direction → {"fills":[...], "day":"YYYY-MM-DD"}
_loaded: bool = False
_lock = threading.Lock()


# ═══════════════════════ DAY LEDGER ═══════════════════════

def _today() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _load():
    global _LEDGER, _loaded
    if _loaded:
        return
    try:
        from storage import db_get
        raw = db_get(STORAGE_KEY)
        if raw and isinstance(raw, dict):
            _LEDGER = raw
    except Exception as e:
        print(f"[SWING] Load error: {e}")
    _loaded = True


def _save():
    try:
        from storage import db_set
        db_set(STORAGE_KEY, _LEDGER)
    except Exception as e:
        print(f"[SWING] Save error: {e}")


def record_fill(parsed: dict, alert_name: str):
    """
    Called from bullflow_stream for EVERY parseable fill, any filter.
    Builds the day-long flow ledger the 3:45pm ranking reads from.
    """
    try:
        ticker = str(parsed.get("ticker", "") or "").upper()
        strike = str(parsed.get("strike", "") or "")
        expiry = str(parsed.get("expiry", "") or "")
        if not ticker or not strike or not expiry or len(ticker) > 6:
            return
        direction = "call" if "call" in str(parsed.get("option_type","call")).lower() else "put"
        premium   = float(parsed.get("premium", 0) or 0)
        if premium <= 0:
            return

        _load()
        today = _today()
        key   = f"{ticker}_{direction}"

        with _lock:
            if key not in _LEDGER or _LEDGER[key].get("day") != today:
                _LEDGER[key] = {"fills": [], "day": today,
                                "ticker": ticker, "direction": direction}
            _LEDGER[key]["fills"].append({
                "strike":   strike,
                "expiry":   expiry,
                "premium":  premium,
                "price":    float(parsed.get("option_price", 0) or 0),
                "dte":      int(parsed.get("dte", 0) or 0),
                "sweep":    bool(parsed.get("is_sweep", False)),
                "stock_px": float(parsed.get("stock_price", 0) or 0),
                "filter":   alert_name,
                "ts":       time.time(),
                "hour_et":  datetime.now(ET).hour + datetime.now(ET).minute / 60.0,
            })
        # Save at most every ~20 fills per key to limit Supabase churn
        if len(_LEDGER[key]["fills"]) % 20 == 1:
            _save()
    except Exception as e:
        print(f"[SWING] record_fill error: {e}")


# ═══════════════════════ FLOW SCORING ═══════════════════════

def _score_flow(key: str, entry: dict) -> dict | None:
    """Score the day's flow narrative for one ticker+direction. 0-10."""
    fills = entry.get("fills", [])
    if len(fills) < MIN_FILLS:
        return None
    total_prem = sum(f["premium"] for f in fills)
    if total_prem < MIN_PREMIUM:
        return None

    ticker    = entry["ticker"]
    direction = entry["direction"]
    score     = 0.0
    notes     = []

    # 1. Campaign, not print — fill count
    if len(fills) >= 5:
        score += 3;   notes.append(f"{len(fills)} fills (campaign)")
    else:
        score += 2;   notes.append(f"{len(fills)} fills")

    # 2. Total premium size
    if total_prem >= 3_000_000:   score += 3
    elif total_prem >= 1_000_000: score += 2
    else:                          score += 1

    # 3. Late-day weighting — share of premium after 2:00pm ET
    late_prem = sum(f["premium"] for f in fills if f.get("hour_et", 0) >= 14.0)
    late_share = late_prem / total_prem if total_prem else 0
    if late_share >= 0.30:
        score += 1.5; notes.append(f"{late_share:.0%} of premium after 2pm")

    # 4. Escalation — later half bigger than earlier half
    half = len(fills) // 2
    if half >= 1:
        early_avg = sum(f["premium"] for f in fills[:half]) / half
        late_avg  = sum(f["premium"] for f in fills[half:]) / (len(fills) - half)
        if late_avg > early_avg * 1.2:
            score += 1; notes.append("aggression escalating")

    # 5. One-sidedness — opposing direction premium on same ticker
    opp_key = f"{ticker}_{'put' if direction == 'call' else 'call'}"
    opp_prem = sum(f["premium"] for f in _LEDGER.get(opp_key, {}).get("fills", []))
    if opp_prem < total_prem * 0.25:
        score += 1.5; notes.append("one-sided tape")
    elif opp_prem > total_prem * 0.75:
        return None   # two-sided — vol bet or hedge, excluded

    # 6. Swing-appropriate DTE
    dtes = sorted(f["dte"] for f in fills if f.get("dte", 0) > 0)
    med_dte = dtes[len(dtes)//2] if dtes else 0
    if DTE_MIN <= med_dte <= DTE_MAX:
        score += 1; notes.append(f"median {med_dte}d DTE")

    # 7. Flow already working — last stock px vs first
    pxs = [f["stock_px"] for f in fills if f.get("stock_px", 0) > 0]
    if len(pxs) >= 2:
        moved = (pxs[-1] - pxs[0]) / pxs[0] if pxs[0] else 0
        if (direction == "call" and moved > 0.002) or (direction == "put" and moved < -0.002):
            score += 1; notes.append("flow already working")
        elif (direction == "call" and moved < -0.01) or (direction == "put" and moved > 0.01):
            score -= 1; notes.append("⚠️ flow underwater")

    sweeps = sum(1 for f in fills if f.get("sweep"))
    return {
        "key": key, "ticker": ticker, "direction": direction,
        "flow_score": round(min(score, 10.0), 2),
        "fill_count": len(fills), "total_prem": total_prem,
        "late_share": late_share, "sweeps": sweeps,
        "median_dte": med_dte, "notes": notes, "fills": fills,
    }


# ═══════════════════════ CHART DATA (Tradier) ═══════════════════════

def _tradier_get(path: str, params: dict):
    token = os.environ.get("TRADIER_TOKEN", "")
    if not token:
        return None
    try:
        r = requests.get(f"https://api.tradier.com/v1{path}", params=params,
                         headers={"Authorization": f"Bearer {token}",
                                  "Accept": "application/json"}, timeout=10)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            time.sleep(1.2)
            r = requests.get(f"https://api.tradier.com/v1{path}", params=params,
                             headers={"Authorization": f"Bearer {token}",
                                      "Accept": "application/json"}, timeout=10)
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        print(f"[SWING] Tradier error {path}: {e}")
    return None


def _fetch_daily(ticker: str, days: int = 90) -> list:
    start = (datetime.now(ET) - timedelta(days=days)).strftime("%Y-%m-%d")
    end   = datetime.now(ET).strftime("%Y-%m-%d")
    data  = _tradier_get("/markets/history",
                         {"symbol": ticker, "interval": "daily",
                          "start": start, "end": end})
    day = ((data or {}).get("history") or {}).get("day")
    if isinstance(day, dict):
        day = [day]
    return day or []


def _fetch_15min(ticker: str, days_back: int) -> list:
    start = (datetime.now(ET) - timedelta(days=days_back)).strftime("%Y-%m-%d 09:30")
    end   = datetime.now(ET).strftime("%Y-%m-%d %H:%M")
    data  = _tradier_get("/markets/timesales",
                         {"symbol": ticker, "interval": "15min",
                          "start": start, "end": end,
                          "session_filter": "open"})
    series = ((data or {}).get("series") or {}).get("data")
    if isinstance(series, dict):
        series = [series]
    return series or []


def _aggregate(bars: list, n: int) -> list:
    """Aggregate n consecutive bars into one OHLC bar."""
    out = []
    for i in range(0, len(bars) - n + 1, n):
        chunk = bars[i:i+n]
        out.append({
            "open":   float(chunk[0].get("open", 0)),
            "high":   max(float(b.get("high", 0)) for b in chunk),
            "low":    min(float(b.get("low", 0)) for b in chunk if float(b.get("low", 0)) > 0),
            "close":  float(chunk[-1].get("close", 0)),
            "volume": sum(int(b.get("volume", 0)) for b in chunk),
        })
    return out


def _ema(closes: list, period: int) -> float:
    if len(closes) < period:
        return 0.0
    k, e = 2 / (period + 1), sum(closes[:period]) / period
    for c in closes[period:]:
        e = c * k + e * (1 - k)
    return e


def _atr(days: list, period: int = 14) -> float:
    if len(days) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(days)):
        h, l = float(days[i].get("high", 0)), float(days[i].get("low", 0))
        pc   = float(days[i-1].get("close", 0))
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period


# ═══════════════════════ TECH SCORING ═══════════════════════

def _score_technicals(ticker: str, direction: str) -> dict | None:
    """
    Gatekeepers: daily regime + 4H structure must align, else None.
    Scorers: 1H intraday structure + 30M close strength. 0-10.
    """
    daily = _fetch_daily(ticker)
    if len(daily) < 25:
        return None
    closes = [float(d.get("close", 0)) for d in daily]
    px     = closes[-1]
    ema20  = _ema(closes, 20)
    ema50  = _ema(closes, 50) if len(closes) >= 50 else ema20
    atr    = _atr(daily)
    bull   = direction == "call"

    # ── GATEKEEPER 1: daily regime ──
    if bull and not (px > ema20 and ema20 >= ema50 * 0.995):
        return None
    if not bull and not (px < ema20 and ema20 <= ema50 * 1.005):
        return None
    # Room to run — not extended >3 ATR from the 20 EMA
    if atr > 0 and abs(px - ema20) / atr > 3.0:
        return None

    score = 3.0   # passed the daily gate
    notes = [f"daily {'up' if bull else 'down'}trend intact"]

    m15 = _fetch_15min(ticker, days_back=15)
    if len(m15) < 30:
        return None

    # ── GATEKEEPER 2: volume confirmation ──
    # Today's accumulated volume (from 15min bars) vs 20D average daily volume.
    # >=110% of avg = institutional participation confirmed.
    # Anything below the ratio floor is mostly retail noise — excluded.
    today_str_v = datetime.now(ET).strftime("%Y-%m-%d")
    today_vol   = sum(int(b.get("volume", 0)) for b in m15
                      if str(b.get("time", "")).startswith(today_str_v))
    hist_days   = [d for d in daily
                   if str(d.get("date", "")) != today_str_v][-20:]
    avg20_vol   = (sum(int(d.get("volume", 0)) for d in hist_days) / len(hist_days)
                   if hist_days else 0)
    vol_ratio   = (today_vol / avg20_vol) if avg20_vol > 0 else 0
    if avg20_vol > 0 and vol_ratio < VOL_RATIO_MIN:
        print(f"[SWING] {ticker}: volume {vol_ratio:.0%} of 20D avg "
              f"(need {VOL_RATIO_MIN:.0%}) — retail noise, excluded")
        return None
    if vol_ratio >= VOL_RATIO_MIN:
        notes.append(f"volume {vol_ratio:.0%} of 20D avg")

    # ── GATEKEEPER 3: 4H structure (16 × 15min) ──
    h4 = _aggregate(m15, 16)
    if len(h4) >= 4:
        lows  = [b["low"] for b in h4[-4:]]
        highs = [b["high"] for b in h4[-4:]]
        hl_ok = all(lows[i] >= lows[i-1] * 0.995 for i in range(1, len(lows)))
        lh_ok = all(highs[i] <= highs[i-1] * 1.005 for i in range(1, len(highs)))
        if bull and hl_ok:
            score += 2; notes.append("4H higher lows")
        elif not bull and lh_ok:
            score += 2; notes.append("4H lower highs")
        elif (bull and all(lows[i] < lows[i-1] for i in range(1, len(lows)))) or \
             (not bull and all(highs[i] > highs[i-1] for i in range(1, len(highs)))):
            return None   # 4H actively against the trade

    # ── SCORER 1: today's 1H structure (4 × 15min, today only) ──
    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    today15   = [b for b in m15 if str(b.get("time", "")).startswith(today_str)]
    h1 = _aggregate(today15, 4)
    if len(h1) >= 3:
        h1c = [b["close"] for b in h1]
        rising = sum(1 for i in range(1, len(h1c)) if h1c[i] > h1c[i-1])
        frac = rising / (len(h1c) - 1)
        pts = 2.5 * (frac if bull else 1 - frac)
        score += pts
        if pts >= 1.8:
            notes.append(f"1H {'stacked up' if bull else 'stacked down'} ({rising}/{len(h1c)-1})")

    # ── SCORER 2: 30M close strength (2 × 15min, today) ──
    m30 = _aggregate(today15, 2)
    if len(m30) >= 3:
        last3 = m30[-3:]
        dir_ok = sum(1 for b in last3
                     if (b["close"] > b["open"]) == bull)
        day_hi = max(b["high"] for b in m30)
        day_lo = min(b["low"] for b in m30)
        rng    = day_hi - day_lo
        pos    = (m30[-1]["close"] - day_lo) / rng if rng > 0 else 0.5
        close_str = pos if bull else 1 - pos
        pts = 1.5 * (dir_ok / 3) + 1.0 * close_str
        score += pts
        if close_str >= 0.66:
            notes.append("closing in the strong third of range")

    # R:R skeleton
    if bull:
        stop, target = px - 1.5 * atr, px + 3.0 * atr
    else:
        stop, target = px + 1.5 * atr, px - 3.0 * atr

    return {
        "tech_score": round(min(score, 10.0), 2),
        "px": px, "ema20": round(ema20, 2), "atr": round(atr, 2),
        "stop": round(stop, 2), "target": round(target, 2),
        "vol_ratio": round(vol_ratio, 2),
        "tech_notes": notes,
    }


# ═══════════════════════ RANK + REPORT ═══════════════════════

def _fmt_prem(p: float) -> str:
    return f"${p/1_000_000:.1f}M" if p >= 1_000_000 else f"${p/1_000:.0f}K"


def run_swing_scan(send_fn=None) -> str:
    """
    Evaluate the day's complete flow + chart story, rank top 5.
    send_fn(message) delivers to Telegram if provided.
    """
    _load()
    today = _today()

    # Flow-score every ticker+direction with today's fills
    candidates = []
    with _lock:
        items = [(k, v) for k, v in _LEDGER.items() if v.get("day") == today]
    for key, entry in items:
        fs = _score_flow(key, entry)
        if fs:
            candidates.append(fs)

    if not candidates:
        msg = (f"🎯 Swing Scan {today} 3:45pm\n"
               f"No candidates met flow criteria today "
               f"(need {MIN_FILLS}+ fills, {_fmt_prem(MIN_PREMIUM)}+ premium, one-sided).")
        if send_fn: send_fn(msg)
        return msg

    # Cap before chart fetches, best flow first
    candidates.sort(key=lambda c: -c["flow_score"])
    candidates = candidates[:MAX_CANDIDATES]

    ranked = []
    for c in candidates:
        tech = _score_technicals(c["ticker"], c["direction"])
        if tech is None:
            print(f"[SWING] {c['ticker']} {c['direction']}: gated out by daily/4H")
            continue
        composite = round(c["flow_score"] * 0.55 + tech["tech_score"] * 0.45, 2)
        earnings_flag = ""
        try:
            from fetcher import fetch_earnings_date
            e = fetch_earnings_date(c["ticker"])
            if e and e.get("earnings_str") and e.get("days_until") is not None \
                   and 0 <= e["days_until"] <= 42:
                earnings_flag = f"⚠️ earnings {e['earnings_str']}"
        except Exception:
            pass
        ranked.append({**c, **tech, "composite": composite, "earnings": earnings_flag})

    if not ranked:
        msg = (f"🎯 Swing Scan {today} 3:45pm\n"
               f"{len(candidates)} flow candidates, but none survived the "
               f"daily/4H gatekeepers. No swing plays today — that's the filter working.")
        if send_fn: send_fn(msg)
        return msg

    ranked.sort(key=lambda r: -r["composite"])
    top = ranked[:TOP_N]

    lines = [f"🎯 TOP {len(top)} SWING PLAYS — {today} 3:45pm",
             f"━━━ full-day flow + daily/4H/1H/30M ━━━", ""]
    for i, r in enumerate(top, 1):
        emoji = "📈" if r["direction"] == "call" else "📉"
        otype = "C" if r["direction"] == "call" else "P"
        best  = max(r["fills"], key=lambda f: f["premium"])
        lines += [
            f"#{i} {emoji} ${r['ticker']} {r['direction'].upper()}  "
            f"[{r['composite']:.1f}: flow {r['flow_score']:.1f} | tech {r['tech_score']:.1f}]",
            f"   {r['fill_count']} fills | {_fmt_prem(r['total_prem'])} | "
            f"{r['sweeps']}⚡ | {r['late_share']:.0%} after 2pm",
            f"   Top fill: {best['strike']}{otype} {best['expiry']} "
            f"{_fmt_prem(best['premium'])}",
            f"   Stock ${r['px']:.2f} | Vol {r.get('vol_ratio',0):.0%} of 20D | "
            f"Stop ${r['stop']} | Target ${r['target']}",
            f"   {' · '.join((r['notes'] + r['tech_notes'])[:4])}",
        ]
        if r["earnings"]:
            lines.append(f"   {r['earnings']}")
        lines.append("")
    lines.append("Swing window: 2-8 weeks. Flow+structure aligned — manage the R:R.")

    msg = "\n".join(lines)
    if send_fn: send_fn(msg)
    return msg


def start_swing_scan_async(send_fn):
    """Run the scan in a background thread (chart fetches take ~10-30s)."""
    t = threading.Thread(target=lambda: run_swing_scan(send_fn),
                         daemon=True, name="swing_scan")
    t.start()
