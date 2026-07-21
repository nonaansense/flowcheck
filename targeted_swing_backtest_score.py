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

  30M structure       44   trend, RSI, distance to next pivot
  Daily trend         28   from the same point-in-time bars
  Flow conviction     13   run length + premium
  Premium skew         9   ticker put/call vs direction
  DTE context          3   0-5 DTE is the norm here, so context only
  Dark pool            3   real off-exchange prints, cut off at alert time
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


def _rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    g=[max(closes[i]-closes[i-1],0.0) for i in range(1,len(closes))]
    l=[max(closes[i-1]-closes[i],0.0) for i in range(1,len(closes))]
    ag=sum(g[:period])/period; al=sum(l[:period])/period
    for i in range(period,len(g)):
        ag=(ag*(period-1)+g[i])/period; al=(al*(period-1)+l[i])/period
    if al==0: return 100.0
    return 100.0-(100.0/(1.0+ag/al))


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
    """Context only — these alerts are structurally 0-5 DTE, so gating on
    DTE would reject the entire population."""
    if dte <= 0:
        return 0.0, "0DTE"
    if dte == 1:
        return 1.0, "1DTE"
    if dte <= 3:
        return 2.0, f"{dte}DTE"
    return 3.0, f"{dte}DTE"


def _score_structure(bars: list, direction: str) -> tuple:
    """0-44 from 30M structure — the dominant component. Uses only bars
    available at alert time."""
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
            pts += 14.0; notes.append("30M trend aligned")
        elif (dn if bull else up):
            notes.append("30M trend opposed")
        else:
            pts += 6.0; notes.append("30M trend mixed")

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
            pts += 16.0; notes.append(f"{room:.1f}% clear")
        elif room >= 1.0:
            pts += 10.0; notes.append(f"{room:.1f}% to level")
        else:
            pts += 3.0; notes.append(f"level only {room:.1f}% away")
    else:
        pts += 13.0

    # RSI + range position add the remaining technical depth.
    rsi = _rsi(closes)
    if rsi:
        good = (50 <= rsi <= 70) if bull else (30 <= rsi <= 50)
        ext  = (rsi > 70) if bull else (rsi < 30)
        if good:
            pts += 14.0; notes.append(f"RSI {rsi:.0f} with room")
        elif ext:
            pts += 5.0; notes.append(f"RSI {rsi:.0f} extended")
        else:
            pts += 8.0; notes.append(f"RSI {rsi:.0f}")
        detail["rsi"] = round(rsi, 1)

    return min(pts, 44.0), notes, detail


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
        return 28.0, ["daily regime aligned"]
    if opposed:
        return 0.0, ["daily regime opposes trade"]
    return 12.0, ["daily regime mixed"]


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
    return min(pts, 13.0), notes


def _score_skew(run: dict, direction: str) -> tuple:
    """Uses the premium tally captured DURING replay, up to the alert."""
    snap = run.get("tkr_at_alert") or {}
    c, p = float(snap.get("call", 0)), float(snap.get("put", 0))
    tot = c + p
    if tot <= 0:
        return 0.0, []
    aligned = (c / tot * 100) if direction == "call" else (p / tot * 100)
    if aligned >= 70:
        return 9.0, [f"flow {aligned:.0f}% aligned"]
    if aligned >= 55:
        return 6.0, [f"flow leans {direction}"]
    if aligned >= 45:
        return 3.0, []
    return 0.0, [f"flow {100-aligned:.0f}% against"]


def _score_darkpool(ticker: str, date: str, direction: str, spot: float,
                    cutoff_ts: float = 0.0) -> tuple:
    """
    Bullflow darkPoolTrades is date-ranged AND each row carries
    sipTimestampMs, so passing cutoff_ts restricts the aggregate to prints
    that had already happened when the alert fired. Without that cutoff this
    component would be forward-looking.
    """
    try:
        from bullflow_darkpool import get_dark_pool_summary, fmt_notional
        s = get_dark_pool_summary(ticker, date=date, spot=spot,
                                  cutoff_ts=cutoff_ts)
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
    return max(0.0, min(pts, 3.0)), notes, s


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
    # NOTE: a daily-regime conflict marks the alert as untradeable, but we do
    # NOT return here. "daily_aligned" is one of the factors under test, so
    # dropping opposed alerts would delete the contrast group and make the
    # factor unmeasurable. Score it, record the factors, flag it at the end.
    daily_opposed = (daily_pts == 0 and "opposes" in " ".join(daily_notes))

    struct_pts, struct_notes, struct_detail = _score_structure(bars, direction)
    flow_pts, flow_notes = _score_flow(alert)
    skew_pts, skew_notes = _score_skew(alert, direction)
    dp_pts, dp_notes, dp = _score_darkpool(ticker, date, direction, spot,
                                           cutoff_ts=cutoff)

    score = dte_pts + daily_pts + struct_pts + flow_pts + skew_pts + dp_pts
    alert["swing_score"] = round(min(score, 100.0), 1)
    alert["swing_notes"] = ([dte_note] + daily_notes + struct_notes +
                            flow_notes + skew_notes + dp_notes)
    alert["swing_breakdown"] = {
        "dte": dte_pts, "daily": daily_pts, "m30": struct_pts,
        "flow": flow_pts, "skew": skew_pts, "darkpool": dp_pts,
    }
    alert["swing_struct"] = struct_detail

    # Raw point-in-time FACTORS for single-factor analysis. Each is a plain
    # boolean recorded as of the alert, so factor_lab can A/B them without
    # re-deriving anything (and without touching post-alert data).
    snap = alert.get("tkr_at_alert") or {}
    tot_pc = float(snap.get("call", 0)) + float(snap.get("put", 0))
    aligned_pct = 0.0
    if tot_pc > 0:
        aligned_pct = (float(snap.get("call", 0)) / tot_pc * 100) if direction == "call" \
                      else (float(snap.get("put", 0)) / tot_pc * 100)
    rsi = struct_detail.get("rsi", 0) or 0
    bull = direction == "call"
    alert["factors"] = {
        "30m_trend_aligned":  "30M trend aligned" in " ".join(struct_notes),
        "daily_aligned":      daily_pts >= 28,
        "room_2pct_plus":     (struct_detail.get("room_pct") or 0) >= 2.0,
        "rsi_favorable":      bool(rsi) and ((50 <= rsi <= 70) if bull else (30 <= rsi <= 50)),
        "rsi_extended":       bool(rsi) and ((rsi > 70) if bull else (rsi < 30)),
        "flow_skew_aligned":  aligned_pct >= 60,
        "big_premium_500k":   float(alert.get("total_prem", 0)) >= 500_000,
        "long_run_6plus":     int(alert.get("count", 0)) >= 6,
        "sweep_heavy":        (sum(1 for f in fills if f.get("sweep")) / len(fills)) >= 0.5 if fills else False,
        "early_session":      bool(alert.get("early")),
        "is_call":            bull,
        "dte_2plus":          dte >= 2,
        "darkpool_aligned":   (dp or {}).get("lean") == ("accumulation" if bull else "distribution"),
    }
    if not bars:
        alert["swing_notes"].append("⚠️ no bar data — score is DTE/flow only")
    if daily_opposed:
        # Untradeable, but retained for factor analysis.
        alert["swing_dq"] = "daily regime opposed the trade"
        alert["swing_score"] = 0.0
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
