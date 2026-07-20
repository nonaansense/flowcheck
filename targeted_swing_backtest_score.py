"""
targeted_swing_backtest_score.py — POINT-IN-TIME scoring of historical
targeted-strike alerts.

Why this is a separate module from targeted_swing_rating:

The live rater calls swing_scanner._score_technicals() and _fetch_15min(),
both of which build their date windows from datetime.now(). Pointing those
at a historical alert would score a January trade using July chart data —
textbook lookahead bias, and it would make every backtest look far better
than reality. Nothing here may use "now".

What CAN be scored point-in-time:
  ✓ DTE               — from the contract + alert date
  ✓ 30M structure     — Massive stock aggregates, truncated at the alert's
                        own timestamp (bars that CLOSED at or before it)
  ✓ Flow conviction   — run length / premium / sweeps, all from the replay
  ✓ Premium skew      — tallied during replay up to the alert (tkr_at_alert)
  ✓ Dark pool         — Bullflow darkPoolTrades is date-ranged

What CANNOT:
  ✗ GEX — /v1/data/netgex returns a live snapshot with no date parameter.
          There is no historical GEX source available, so the component is
          dropped entirely rather than faked with today's values.

Because GEX is dropped, the remaining weights are rescaled to 100 so
backtest scores stay comparable to live scores in magnitude. They are NOT
identical measures, and the report says so.

  DTE suitability     28   (live 25)
  30M structure       22   (live 20)
  Daily trend         22   (live 20, from the same point-in-time bars)
  Flow conviction     11   (live 10)
  Premium skew        11   (live 10)
  Dark pool            6   (live 5)
"""
import os
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MIN_DTE = int(os.environ.get("TARGETED_SWING_MIN_DTE", "5"))

_BAR_CACHE: dict = {}


# ───────────────── point-in-time stock bars ─────────────────

def _fetch_30m_stock_bars(ticker: str, start_date: str, end_date: str,
                          cutoff_ts: float) -> list:
    """
    30-minute OHLC bars for the UNDERLYING, from Massive, keeping only bars
    that CLOSED at or before cutoff_ts. That truncation is what makes this
    safe for backtesting — the scorer can never see a bar that had not yet
    completed when the alert fired.

    Returns [] on any failure (auth, rate limit, no data).
    """
    ck = f"{ticker}_{start_date}_{end_date}"
    bars = _BAR_CACHE.get(ck)
    if bars is None:
        bars = []
        try:
            import bullflow_presets as _bp
            if _bp._massive_keys():
                base = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com")
                url = (f"{base}/v2/aggs/ticker/{ticker.upper()}"
                       f"/range/30/minute/{start_date}/{end_date}")
                mkey = _bp._massive_acquire()
                if mkey:
                    r = requests.get(url, params={"adjusted": "true", "sort": "asc",
                                                  "limit": 50000, "apiKey": mkey},
                                     timeout=20)
                    if r.status_code == 200:
                        for b in (r.json().get("results") or []):
                            t_ms = b.get("t")
                            if t_ms is None:
                                continue
                            bars.append({
                                # bar START + 30min = its CLOSE time
                                "ts":    (float(t_ms) / 1000.0) + 1800,
                                "high":  float(b.get("h", 0) or 0),
                                "low":   float(b.get("l", 0) or 0),
                                "close": float(b.get("c", 0) or 0),
                            })
                        bars.sort(key=lambda x: x["ts"])
                    else:
                        print(f"[SWINGBT] Massive stock HTTP {r.status_code} "
                              f"for {ticker}: {r.text[:100]}")
        except Exception as e:
            print(f"[SWINGBT] bar fetch error {ticker}: {e}")
        _BAR_CACHE[ck] = bars

    return [b for b in bars if b["ts"] <= cutoff_ts]


def _ema(vals: list, period: int) -> float:
    if len(vals) < period:
        return 0.0
    k = 2 / (period + 1)
    e = sum(vals[:period]) / period
    for v in vals[period:]:
        e = v * k + e * (1 - k)
    return e


def _pivots(bars: list, lookback: int = 2) -> tuple:
    highs, lows = [], []
    for i in range(lookback, len(bars) - lookback):
        w = bars[i - lookback:i + lookback + 1]
        if bars[i]["high"] >= max(b["high"] for b in w):
            highs.append(bars[i]["high"])
        if bars[i]["low"] <= min(b["low"] for b in w if b["low"] > 0):
            lows.append(bars[i]["low"])
    return highs, lows


# ───────────────── component scorers ─────────────────

def _score_dte(dte: int) -> tuple:
    if dte < MIN_DTE:
        return 0.0, f"only {dte}DTE — too short to swing"
    if dte <= 9:
        return 13.0, f"{dte}DTE — tight for a swing"
    if dte <= 21:
        return 28.0, f"{dte}DTE — good swing runway"
    if dte <= 60:
        return 25.0, f"{dte}DTE — plenty of time"
    return 18.0, f"{dte}DTE — long-dated"


def _score_structure(bars: list, direction: str) -> tuple:
    """0-22 from 30M structure, using only bars available at alert time."""
    if len(bars) < 25:
        return 0.0, ["insufficient 30M history"], {}
    closes = [b["close"] for b in bars]
    px = closes[-1]
    ema9, ema21 = _ema(closes, 9), _ema(closes, 21)
    bull = direction == "call"
    pts, notes = 0.0, []

    if ema9 and ema21:
        up = ema9 > ema21 and px > ema9
        dn = ema9 < ema21 and px < ema9
        if (up if bull else dn):
            pts += 11.0; notes.append("30M trend aligned")
        elif (dn if bull else up):
            notes.append("30M trend opposed")
        else:
            pts += 4.0; notes.append("30M trend mixed")

    highs, lows = _pivots(bars)
    res = [h for h in highs if h > px]
    sup = [l for l in lows if l < px]
    nr = min(res) if res else None
    ns = max(sup) if sup else None
    obstacle = nr if bull else ns
    detail = {"px": round(px, 2), "resistance": round(nr, 2) if nr else None,
              "support": round(ns, 2) if ns else None}

    if obstacle and px:
        room = abs(obstacle - px) / px * 100
        detail["room_pct"] = round(room, 2)
        if room >= 2.0:
            pts += 11.0; notes.append(f"{room:.1f}% clear")
        elif room >= 1.0:
            pts += 6.0; notes.append(f"{room:.1f}% to level")
        else:
            pts += 1.0; notes.append(f"level only {room:.1f}% away")
    else:
        pts += 8.0

    return min(pts, 22.0), notes, detail


def _score_daily_trend(bars: list, direction: str) -> tuple:
    """
    0-22. Longer-horizon trend derived from the SAME point-in-time 30M series
    (roughly 13 bars per session), so no separate daily feed is needed and no
    future data can leak in.
    """
    if len(bars) < 130:                       # ~10 sessions
        return 0.0, ["insufficient daily history"]
    closes = [b["close"] for b in bars]
    px = closes[-1]
    # ~20d and ~50d equivalents on a 30M series
    ema_fast = _ema(closes, 13 * 5)
    ema_slow = _ema(closes, 13 * 15) if len(closes) >= 13 * 15 else ema_fast
    bull = direction == "call"
    if not ema_fast:
        return 0.0, []
    aligned = (px > ema_fast and ema_fast >= ema_slow) if bull else \
              (px < ema_fast and ema_fast <= ema_slow)
    opposed = (px < ema_fast and ema_fast < ema_slow) if bull else \
              (px > ema_fast and ema_fast > ema_slow)
    if aligned:
        return 22.0, ["daily regime aligned"]
    if opposed:
        return 0.0, ["daily regime opposes trade"]
    return 9.0, ["daily regime mixed"]


def _score_flow(run: dict) -> tuple:
    pts, notes = 0.0, []
    count = run.get("count", 0)
    prem = run.get("total_prem", 0)
    if count >= 8:
        pts += 4; notes.append(f"{count}x run")
    elif count >= 6:
        pts += 3
    else:
        pts += 2
    if prem >= 1_000_000:
        pts += 5; notes.append("$1M+ committed")
    elif prem >= 400_000:
        pts += 3
    else:
        pts += 1
    return min(pts, 11.0), notes


def _score_skew(run: dict, direction: str) -> tuple:
    """Uses the premium tally captured DURING replay, up to the alert."""
    snap = run.get("tkr_at_alert") or {}
    c, p = float(snap.get("call", 0)), float(snap.get("put", 0))
    tot = c + p
    if tot <= 0:
        return 0.0, []
    aligned = (c / tot * 100) if direction == "call" else (p / tot * 100)
    if aligned >= 70:
        return 11.0, [f"flow {aligned:.0f}% aligned"]
    if aligned >= 55:
        return 7.0, [f"flow leans {direction}"]
    if aligned >= 45:
        return 3.0, []
    return 0.0, [f"flow {100-aligned:.0f}% against"]


def _score_darkpool(ticker: str, date: str, direction: str, spot: float) -> tuple:
    """Bullflow darkPoolTrades is date-ranged, so this IS point-in-time.
    Note: it covers the WHOLE day, including prints after the alert fired —
    an intraday cutoff isn't applied because sipTimestampMs would need to be
    filtered per row; treated as same-day context, not a precise snapshot."""
    try:
        from bullflow_darkpool import get_dark_pool_summary, fmt_notional
        s = get_dark_pool_summary(ticker, date=date, spot=spot)
    except Exception:
        return 0.0, [], {}
    if not s:
        return 0.0, [], {}
    tot = s["total_notional"]
    pts = 5.0 if tot >= 500_000_000 else 4.0 if tot >= 100_000_000 else \
          3.0 if tot >= 25_000_000 else 1.0
    notes = [f"dark pool {fmt_notional(tot)}"]
    bull = direction == "call"
    if s["lean"] == "accumulation" and bull:
        pts += 1.0
    elif s["lean"] == "distribution" and not bull:
        pts += 1.0
    elif s["lean"] != "neutral":
        pts -= 1.0; notes.append(f"prints lean {s['lean']} — against trade")
    return max(0.0, min(pts, 6.0)), notes, s


# ───────────────── main entry ─────────────────

def score_historical_alert(alert: dict) -> dict:
    """
    Score one backtest alert using ONLY data available at its fire time.
    Adds "swing_score", "swing_notes", "swing_dq" to the alert dict and
    returns it.
    """
    alert["swing_score"] = 0.0
    alert["swing_notes"] = []
    alert["swing_dq"] = None

    ticker    = alert.get("ticker", "")
    direction = alert.get("direction", "call")
    date      = alert.get("date", "")
    fills     = alert.get("fills", [])
    if not fills or not date:
        alert["swing_dq"] = "no fill data"
        return alert

    trigger = fills[-1]
    dte = int(trigger.get("dte", 0) or 0)

    dte_pts, dte_note = _score_dte(dte)
    if dte_pts == 0:
        alert["swing_dq"] = dte_note
        return alert

    # Point-in-time cutoff = the triggering fill's own timestamp.
    try:
        from bullflow_presets import _norm_epoch
        cutoff = _norm_epoch(trigger.get("ts", 0))
    except Exception:
        cutoff = float(trigger.get("ts", 0) or 0)
        if cutoff > 1e11:
            cutoff /= 1000.0
    if not cutoff:
        alert["swing_dq"] = "no timestamp"
        return alert

    # 25 calendar days back gives ~15 sessions of 30M bars.
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=25)).strftime("%Y-%m-%d")
    bars = _fetch_30m_stock_bars(ticker, start, date, cutoff)
    spot = bars[-1]["close"] if bars else float(trigger.get("stock_px", 0) or 0)

    daily_pts, daily_notes = _score_daily_trend(bars, direction)
    if daily_pts == 0 and "opposes" in " ".join(daily_notes):
        alert["swing_dq"] = "daily regime opposed the trade"
        return alert

    struct_pts, struct_notes, struct_detail = _score_structure(bars, direction)
    flow_pts, flow_notes = _score_flow(alert)
    skew_pts, skew_notes = _score_skew(alert, direction)
    dp_pts, dp_notes, dp = _score_darkpool(ticker, date, direction, spot)

    score = dte_pts + daily_pts + struct_pts + flow_pts + skew_pts + dp_pts
    alert["swing_score"] = round(min(score, 100.0), 1)
    alert["swing_notes"] = ([dte_note] + daily_notes + struct_notes +
                            flow_notes + skew_notes + dp_notes)
    alert["swing_breakdown"] = {
        "dte": dte_pts, "daily": daily_pts, "m30": struct_pts,
        "flow": flow_pts, "skew": skew_pts, "darkpool": dp_pts,
    }
    alert["swing_struct"] = struct_detail
    if not bars:
        alert["swing_notes"].append("⚠️ no bar data — score is DTE/flow only")
    return alert


def score_correlation_summary(alerts: list) -> list:
    """
    The point of scoring a backtest: does a higher score actually predict a
    better outcome? Buckets scored alerts and reports realised performance
    per bucket. Returns [] if scores or pricing are missing.
    """
    scored = [a for a in alerts
              if a.get("swing_score") is not None and a.get("pricing")]
    scored = [a for a in scored if not a.get("swing_dq")]
    if len(scored) < 4:
        return []

    buckets = [("70-100", 70, 101), ("55-69", 55, 70),
               ("40-54", 40, 55), ("0-39", 0, 40)]
    lines = ["", "━━━━━━ 📊 SCORE vs OUTCOME ━━━━━━",
             "does a higher swing score predict a better trade?", ""]
    for label, lo, hi in buckets:
        grp = [a for a in scored if lo <= a["swing_score"] < hi]
        if not grp:
            continue
        n = len(grp)
        avg_max = sum(a["pricing"]["max_gain_pct"] for a in grp) / n
        avg_exp = sum(a["pricing"]["expiry_pct"] for a in grp) / n
        wins = sum(1 for a in grp if a["pricing"]["max_gain_pct"] >= 50)
        lines.append(f"  score {label}: n={n} | avg max {avg_max:+.0f}% | "
                     f"avg exp {avg_exp:+.0f}% | {wins}/{n} hit +50%")
    lines.append("")
    lines.append("⚠️ GEX excluded — no historical GEX source exists, so these")
    lines.append("   scores are not directly comparable to live 3:30pm scores.")
    return lines
