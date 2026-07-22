"""
targeted_swing_rating.py — 3:30pm swing-worthiness rating for the day's
Targeted_Strikes_Expiry alerts.

Scores each alerted run 0-100 on whether it's worth HOLDING as a swing,
which is a different question from whether the flow was interesting. Most
targeted-strike alerts fire late in the session on 0-3 DTE contracts; those
are scalps, not swings, and the DTE gate below reflects that deliberately.

Scoring (0-100) — TECHNICALS-DOMINANT:

These alerts are structurally 0-5 DTE, so DTE is context, not a gate. What
decides whether one is worth holding is the CHART: is the 30M structure
behind the trade, and is there room to the next level? Technicals carry 65
of the 100 points.

  30M chart structure    40   trend (EMA9/21 stack), momentum, position in
                              range, and distance to the next 30M pivot
  Higher-timeframe trend 25   daily/4H regime agreement
  Flow conviction        12   run length, premium, sweep share
  Premium skew            8   ticker put/call premium vs alert direction
  GEX positioning         7   room to the next wall
  Dark pool               5   real off-exchange prints
  (DTE is reported for context and only blocks 0DTE overnight holds)

Gatekeepers (score forced to 0, reason recorded):
  • 30M trend strongly opposes the trade direction
  • Daily/4H regime disagrees (swing_scanner._score_technicals returns None)

Config:
  TARGETED_SWING_MIN_DTE   = 5     minimum DTE to be swing-eligible
  TARGETED_SWING_MIN_SCORE = 55    minimum score to be reported as a candidate
  TARGETED_SWING_TOP_N     = 5     max candidates listed
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

MIN_DTE   = int(os.environ.get("TARGETED_SWING_MIN_DTE", "5"))
MIN_SCORE = float(os.environ.get("TARGETED_SWING_MIN_SCORE", "55"))
TOP_N     = int(os.environ.get("TARGETED_SWING_TOP_N", "5"))


def _fmt_prem(p: float) -> str:
    return f"${p/1_000_000:.1f}M" if p >= 1_000_000 else f"${p/1_000:.0f}K"


# ────────────────────────── component scorers ──────────────────────────

def _score_dte(dte: int) -> tuple:
    """
    Context only — NOT a gate. Targeted_Strikes_Expiry is structurally a
    0-5 DTE signal, so gating on DTE would reject the entire population.
    0DTE still gets flagged because it cannot be held overnight at all.
    """
    if dte <= 0:
        return 0.0, "0DTE — cannot hold overnight"
    if dte == 1:
        return 1.0, "1DTE — overnight hold only"
    if dte <= 3:
        return 2.0, f"{dte}DTE"
    return 3.0, f"{dte}DTE"


def _score_flow(run: dict) -> tuple:
    """0-20 from run length, premium committed, and sweep share."""
    pts, notes = 0.0, []
    count = run.get("count", 0)
    prem  = run.get("total_prem", 0)
    sweeps = run.get("sweeps", 0)

    # Run length — more consecutive fills = more deliberate accumulation.
    # Weighted lightly on purpose: in backtests, run LENGTH was the weakest
    # predictor of outcome (the largest run of the sample was also its worst
    # performer), so size of the sequence must not dominate the score.
    if count >= 8:
        pts += 4; notes.append(f"{count}x consecutive")
    elif count >= 6:
        pts += 3; notes.append(f"{count}x consecutive")
    else:
        pts += 2

    # Premium committed.
    if prem >= 1_000_000:
        pts += 4; notes.append(f"{_fmt_prem(prem)} committed")
    elif prem >= 400_000:
        pts += 3; notes.append(f"{_fmt_prem(prem)} committed")
    elif prem >= 150_000:
        pts += 1.5
    else:
        pts += 0.5

    # Sweep share — urgency.
    share = (sweeps / count) if count else 0
    if share >= 0.5:
        pts += 2; notes.append(f"{sweeps}/{count} sweeps")
    elif share > 0:
        pts += 1
    return min(pts, 12.0), notes


def _score_premium_skew(ticker: str, direction: str) -> tuple:
    """0-10. Does the ticker's targeted put/call premium back this direction?"""
    try:
        from ticker_premium_tracker import get_snapshot
        s = get_snapshot(ticker)
    except Exception:
        return 0.0, []
    if not s or s.get("total", 0) <= 0:
        return 0.0, []
    aligned_pct = s["call_pct"] if direction == "call" else s["put_pct"]
    if aligned_pct >= 70:
        return 8.0, [f"flow {aligned_pct:.0f}% {direction}-side"]
    if aligned_pct >= 55:
        return 5.0, [f"flow leans {direction} ({aligned_pct:.0f}%)"]
    if aligned_pct >= 45:
        return 3.0, []
    return 0.0, [f"⚠️ flow is {100-aligned_pct:.0f}% the OTHER way"]


def _score_gex(ticker: str, direction: str, spot: float) -> tuple:
    """0-10. Room to run toward the next GEX wall in the trade's direction."""
    try:
        from gex_monitor import _get_gex
        gex = _get_gex(ticker)
    except Exception:
        return 0.0, []
    if not gex or not spot:
        return 0.0, []
    try:
        strikes = gex.get("strikes") or []
        if not strikes:
            return 0.0, []
        if direction == "call":
            walls = [s for s in strikes
                     if float(s.get("strike", 0)) > spot
                     and float(s.get("gex", 0)) > 0]
            nearest = min(walls, key=lambda s: float(s["strike"])) if walls else None
        else:
            walls = [s for s in strikes
                     if float(s.get("strike", 0)) < spot
                     and float(s.get("gex", 0)) > 0]
            nearest = max(walls, key=lambda s: float(s["strike"])) if walls else None
        if not nearest:
            return 6.0, ["no GEX wall blocking"]
        dist_pct = abs(float(nearest["strike"]) - spot) / spot * 100
        if dist_pct >= 3:
            return 7.0, [f"{dist_pct:.1f}% of room to GEX wall"]
        if dist_pct >= 1.5:
            return 4.0, [f"{dist_pct:.1f}% to GEX wall"]
        return 1.0, [f"⚠️ GEX wall only {dist_pct:.1f}% away"]
    except Exception:
        return 0.0, []


def _pivots(bars: list, lookback: int = 2) -> tuple:
    """
    Swing highs / swing lows on the given bars. A pivot high is a bar whose
    high exceeds the `lookback` bars on both sides (and vice versa) — the
    standard fractal definition. Returns (highs, lows) as price lists.
    """
    highs, lows = [], []
    for i in range(lookback, len(bars) - lookback):
        h = bars[i]["high"]
        l = bars[i]["low"]
        window = bars[i - lookback:i + lookback + 1]
        if h >= max(b["high"] for b in window):
            highs.append(h)
        if l <= min(b["low"] for b in window if b["low"] > 0):
            lows.append(l)
    return highs, lows


def _rsi(closes: list, period: int = 14) -> float:
    """Wilder RSI on the given closes. 0.0 if not enough data."""
    if len(closes) < period + 1:
        return 0.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def _levels(bars: list, lookback: int = 3, cluster_pct: float = 0.4) -> tuple:
    """
    Support/resistance LEVELS, not raw pivots.

    A 2-bar fractal on 30M data marks ~16 pivots per 10 sessions, so the
    "nearest" one is almost always within a fraction of a percent — which
    made any "room to the next level" test fire on ~5% of alerts and be
    useless. Two corrections:

      1. Wider lookback (3) so minor wiggles don't qualify.
      2. Cluster pivots within cluster_pct of each other into one level and
         count touches. A level touched once is noise; touched 2+ times it is
         real structure.

    Returns (resistance_levels, support_levels), each a list of
    (price, touch_count) sorted by price.
    """
    raw_hi, raw_lo = [], []
    for i in range(lookback, len(bars) - lookback):
        w = bars[i - lookback:i + lookback + 1]
        if bars[i]["high"] >= max(b["high"] for b in w):
            raw_hi.append(bars[i]["high"])
        if bars[i]["low"] <= min(b["low"] for b in w if b["low"] > 0):
            raw_lo.append(bars[i]["low"])

    def cluster(vals):
        if not vals:
            return []
        vals = sorted(vals)
        out, cur = [], [vals[0]]
        for v in vals[1:]:
            if abs(v - cur[-1]) / cur[-1] * 100 <= cluster_pct:
                cur.append(v)
            else:
                out.append((sum(cur) / len(cur), len(cur)))
                cur = [v]
        out.append((sum(cur) / len(cur), len(cur)))
        return out

    return cluster(raw_hi), cluster(raw_lo)


def _nearest_level(levels: list, px: float, above: bool, min_touches: int = 2):
    """Nearest clustered level in the given direction with enough touches."""
    cands = [(p, t) for p, t in levels
             if (p > px if above else p < px) and t >= min_touches]
    if not cands:
        # Fall back to single-touch levels rather than claiming clear air.
        cands = [(p, t) for p, t in levels if (p > px if above else p < px)]
    if not cands:
        return None, 0
    return min(cands, key=lambda x: x[0]) if above else max(cands, key=lambda x: x[0])


def _score_30m_structure(ticker: str, direction: str) -> tuple:
    """
    0-40 from the 30-MINUTE chart. This is the heart of the rating: these
    contracts are 0-5 DTE, so the intraday chart — not the calendar — decides
    whether the trade has room to work.

      Trend      (0-12): EMA9 vs EMA21 stack + price position
      Momentum   (0-8) : RSI direction and headroom (not already exhausted)
      Range pos  (0-6) : where price sits in the recent 30M range
      Levels     (0-14): distance to the next 30M pivot in the trade's way

    Returns (points, notes, detail_dict). A strongly opposed trend returns
    the sentinel -1 so the caller can disqualify.
    """
    try:
        from swing_scanner import _fetch_15min, _aggregate, _ema
        raw = _fetch_15min(ticker, 10)
        if len(raw) < 30:
            return 0.0, [], {}
        bars = _aggregate(raw, 2)          # 15min x2 = 30M
        if len(bars) < 25:
            return 0.0, [], {}
    except Exception as e:
        print(f"[SWINGRATE] 30M fetch error {ticker}: {e}")
        return 0.0, [], {}

    closes = [b["close"] for b in bars]
    px     = closes[-1]
    ema9   = _ema(closes, 9)
    ema21  = _ema(closes, 21)
    bull   = direction == "call"
    pts, notes = 0.0, []

    # ── Trend (0-12) ──
    opposed = False
    if ema9 and ema21:
        up   = ema9 > ema21 and px > ema9
        dn   = ema9 < ema21 and px < ema9
        if (up if bull else dn):
            pts += 12.0
            notes.append(f"30M trend aligned (EMA9 {'>' if bull else '<'} EMA21)")
        elif (dn if bull else up):
            opposed = True
            notes.append("⚠️ 30M trend strongly opposes the trade")
        else:
            pts += 5.0
            notes.append("30M trend mixed/flat")

    # ── Momentum (0-8) — RSI direction plus room left to run ──
    rsi = _rsi(closes)
    rsi_prev = _rsi(closes[:-3]) if len(closes) > 20 else rsi
    if rsi:
        rising = rsi > rsi_prev
        if bull:
            if 50 <= rsi <= 70 and rising:
                pts += 8.0; notes.append(f"RSI {rsi:.0f} rising — momentum with room")
            elif rsi > 70:
                pts += 3.0; notes.append(f"⚠️ RSI {rsi:.0f} overbought")
            elif rsi >= 45:
                pts += 5.0; notes.append(f"RSI {rsi:.0f}")
            else:
                pts += 1.0; notes.append(f"RSI {rsi:.0f} weak")
        else:
            if 30 <= rsi <= 50 and not rising:
                pts += 8.0; notes.append(f"RSI {rsi:.0f} falling — momentum with room")
            elif rsi < 30:
                pts += 3.0; notes.append(f"⚠️ RSI {rsi:.0f} oversold")
            elif rsi <= 55:
                pts += 5.0; notes.append(f"RSI {rsi:.0f}")
            else:
                pts += 1.0; notes.append(f"RSI {rsi:.0f} against")

    # ── Position in recent 30M range (0-6) ──
    recent = bars[-26:]                     # ~2 sessions
    hi = max(b["high"] for b in recent)
    lo = min(b["low"] for b in recent if b["low"] > 0)
    rng_pos = ((px - lo) / (hi - lo) * 100) if hi > lo else 50.0
    # Calls want strength but not the absolute top; puts the mirror.
    score_pos = rng_pos if bull else (100 - rng_pos)
    if 55 <= score_pos <= 85:
        pts += 6.0; notes.append(f"breaking out of 2-day range ({score_pos:.0f}%)")
    elif score_pos > 85:
        pts += 3.0; notes.append(f"extended in range ({score_pos:.0f}%)")
    elif score_pos >= 40:
        pts += 4.0
    else:
        pts += 1.0; notes.append(f"weak position in range ({score_pos:.0f}%)")

    # ── Levels (0-14) — the obstacle in the trade's path ──
    res_lv, sup_lv = _levels(bars)
    _nr, _nr_t = _nearest_level(res_lv, px, above=True)
    _ns, _ns_t = _nearest_level(sup_lv, px, above=False)
    nearest_res, nearest_sup = _nr, _ns

    detail = {
        "px": round(px, 2), "rsi": round(rsi, 1),
        "range_pos": round(rng_pos, 1),
        "ema9": round(ema9, 2), "ema21": round(ema21, 2),
        "resistance": round(nearest_res, 2) if nearest_res else None,
        "support":    round(nearest_sup, 2) if nearest_sup else None,
    }

    obstacle = nearest_res if bull else nearest_sup
    if obstacle and px:
        room_pct = abs(obstacle - px) / px * 100
        detail["room_pct"] = round(room_pct, 2)
        if room_pct >= 2.0:
            pts += 14.0
            notes.append(f"{room_pct:.1f}% clear to 30M "
                         f"{'resistance' if bull else 'support'} ${obstacle:.2f}")
        elif room_pct >= 1.0:
            pts += 9.0
            notes.append(f"{room_pct:.1f}% to 30M "
                         f"{'resistance' if bull else 'support'} ${obstacle:.2f}")
        elif room_pct >= 0.5:
            pts += 4.0
            notes.append(f"only {room_pct:.1f}% to 30M "
                         f"{'resistance' if bull else 'support'} ${obstacle:.2f}")
        else:
            notes.append(f"⚠️ 30M {'resistance' if bull else 'support'} "
                         f"${obstacle:.2f} right here ({room_pct:.1f}%)")
    else:
        pts += 11.0
        notes.append(f"clear air — no 30M {'resistance' if bull else 'support'} above"
                     if bull else "clear air — no 30M support below")

    backstop = nearest_sup if bull else nearest_res
    if backstop:
        notes.append(f"30M {'support' if bull else 'resistance'} ${backstop:.2f}")

    if opposed:
        return -1.0, notes, detail          # sentinel: caller disqualifies
    return min(pts, 40.0), notes, detail


def _score_darkpool(ticker: str, direction: str, spot: float) -> tuple:
    """
    0-5 from REAL dark pool prints (Bullflow /v1/data/darkPoolTrades).

    Scores institutional participation by notional, then adjusts for whether
    print prices lean above or below spot in the trade's favour. TRF prints
    carry no buy/sell flag, so the lean is a soft tiebreaker — it can add or
    remove a point, never carry the score on its own.
    """
    try:
        from bullflow_darkpool import get_dark_pool_summary, fmt_notional
        s = get_dark_pool_summary(ticker, spot=spot)
    except Exception as e:
        print(f"[SWINGRATE] dark pool error {ticker}: {e}")
        return 0.0, [], {}
    if not s:
        return 0.0, [], {}

    total = s["total_notional"]
    if total >= 500_000_000:
        pts = 4.0
    elif total >= 100_000_000:
        pts = 3.0
    elif total >= 25_000_000:
        pts = 2.0
    else:
        pts = 1.0

    notes = [f"dark pool {fmt_notional(total)} / {s['print_count']} prints"]

    bull = direction == "call"
    if s["lean"] == "accumulation" and bull:
        pts += 1.0; notes.append("🟢 prints lean accumulation (above spot)")
    elif s["lean"] == "distribution" and not bull:
        pts += 1.0; notes.append("🔴 prints lean distribution (below spot)")
    elif s["lean"] != "neutral":
        pts -= 1.0; notes.append(f"⚠️ prints lean {s['lean']} — against this trade")

    return max(0.0, min(pts, 5.0)), notes, s


def rate_run(run: dict) -> dict:
    """Score one alerted run 0-100 for swing-worthiness."""
    ticker    = run["ticker"]
    direction = run["direction"]
    spot      = run.get("stock_px", 0)

    result = {**run, "score": 0.0, "notes": [], "disqualified": None,
              "tech": None}

    # DTE is context only now — these alerts are structurally 0-5 DTE, so
    # gating on it would reject the whole population. Only a 0DTE contract
    # gets called out, since it cannot be held past the close.
    dte_pts, dte_note = _score_dte(run.get("dte", 0))

    # Gate 1 — higher-timeframe regime. _score_technicals returns None when
    # the daily/4H trend disagrees with the direction.
    try:
        from swing_scanner import _score_technicals
        tech = _score_technicals(ticker, direction)
    except Exception as e:
        print(f"[SWINGRATE] technicals error {ticker}: {e}")
        tech = None
    if not tech:
        result["disqualified"] = "daily/4H trend disagrees with direction"
        return result
    result["tech"] = tech

    # Gate 2 — the 30M chart itself. For a 0-5 DTE hold this matters more
    # than the daily, so a strongly opposed intraday trend is fatal.
    m30_pts, m30_notes, m30_detail = _score_30m_structure(ticker, direction)
    result["m30"] = m30_detail
    if m30_pts < 0:
        result["disqualified"] = "30M trend opposes the trade"
        result["notes"] = m30_notes
        return result

    notes = [dte_note] + list(tech.get("tech_notes", []))
    tech_pts = float(tech.get("tech_score", 0)) / 10.0 * 25.0
    score = dte_pts + tech_pts + m30_pts
    notes += m30_notes

    flow_pts, flow_notes = _score_flow(run);                score += flow_pts; notes += flow_notes
    skew_pts, skew_notes = _score_premium_skew(ticker, direction); score += skew_pts; notes += skew_notes
    gex_pts,  gex_notes  = _score_gex(ticker, direction, spot);    score += gex_pts;  notes += gex_notes
    vol_pts, vol_notes, dp = _score_darkpool(ticker, direction, spot)
    score += vol_pts; notes += vol_notes
    result["darkpool"] = dp

    result["score"] = round(min(score, 100.0), 1)
    result["notes"] = notes
    result["breakdown"] = {
        "dte": dte_pts, "tech": round(tech_pts, 1), "m30": m30_pts,
        "flow": flow_pts, "skew": skew_pts, "gex": gex_pts, "vol": vol_pts,
    }
    # Flag partial data — a score built on 3 of 7 inputs shouldn't read as
    # confidently as one built on all of them.
    missing = [k for k, v in (("30M chart", m30_pts), ("GEX", gex_pts),
                              ("dark pool", vol_pts), ("flow skew", skew_pts))
               if v == 0]
    result["missing_inputs"] = missing
    if len(missing) >= 3:
        result["notes"] = notes + [f"⚠️ limited data — no {', '.join(missing)}"]
    return result


def build_swing_report() -> str:
    """3:30pm report ranking today's targeted alerts by swing-worthiness."""
    try:
        from targeted_strikes_tracker import get_todays_alerted_runs
        runs = get_todays_alerted_runs()
    except Exception as e:
        print(f"[SWINGRATE] could not load runs: {e}")
        return ""
    if not runs:
        return ""

    rated = [rate_run(r) for r in runs]
    eligible = sorted([r for r in rated if not r["disqualified"]],
                      key=lambda r: -r["score"])
    candidates = [r for r in eligible if r["score"] >= MIN_SCORE][:TOP_N]
    dq = [r for r in rated if r["disqualified"]]

    now_s = datetime.now(ET).strftime("%-I:%M %p")
    lines = [
        f"🎯 SWING RATING — {now_s} ET",
        f"━━━ today's targeted-strike alerts, scored for HOLDING ━━━",
        "",
    ]

    if not candidates:
        lines.append(f"No candidates scored ≥{MIN_SCORE:.0f}.")
        if eligible:
            b = eligible[0]
            o = "C" if b["direction"] == "call" else "P"
            lines.append(f"Best was ${b['ticker']} {b['strike']}{o} at {b['score']:.0f}.")
    else:
        for i, r in enumerate(candidates, 1):
            o = "C" if r["direction"] == "call" else "P"
            emoji = "📈" if r["direction"] == "call" else "📉"
            grade = "🟢 STRONG" if r["score"] >= 75 else "🟡 MODERATE"
            lines.append(
                f"{i}. {grade} {r['score']:.0f}/100  {emoji} ${r['ticker']} "
                f"{r['strike']}{o} {r['expiry']}  ({r['dte']}DTE)"
            )
            lines.append(
                f"   {r['count']}x run | {_fmt_prem(r['total_prem'])} | "
                f"stock ${r.get('stock_px', 0):.2f}"
            )
            t = r.get("tech") or {}
            if t.get("stop") and t.get("target"):
                lines.append(f"   stop ${t['stop']:.2f} | target ${t['target']:.2f} "
                             f"| ATR ${t.get('atr', 0):.2f}")
            m = r.get("m30") or {}
            if m.get("support") or m.get("resistance"):
                lines.append(
                    f"   30M: sup ${m.get('support', 0) or 0:.2f} / "
                    f"res ${m.get('resistance', 0) or 0:.2f} "
                    f"| EMA9 ${m.get('ema9', 0):.2f} EMA21 ${m.get('ema21', 0):.2f}")
            dp = r.get("darkpool") or {}
            if dp:
                try:
                    from bullflow_darkpool import format_summary
                    for dl in format_summary(dp):
                        lines.append(f"   {dl.strip()}")
                except Exception:
                    pass
            for n in r["notes"][:4]:
                lines.append(f"   • {n}")
            lines.append("")

    if dq:
        lines.append(f"━━━ {len(dq)} not swing-eligible ━━━")
        short = sum(1 for r in dq if "too short" in (r["disqualified"] or ""))
        trend = len(dq) - short
        if short:
            lines.append(f"   {short} too short-dated (<{MIN_DTE}DTE)")
        if trend:
            lines.append(f"   {trend} trend disagreed with direction")

    return "\n".join(lines)


def send_swing_report(send_fn=None) -> None:
    """Scheduler entry point."""
    msg = build_swing_report()
    if not msg:
        print("[SWINGRATE] no targeted alerts today — nothing to rate")
        return
    try:
        if send_fn:
            send_fn(msg)
        else:
            from sms import send_telegram
            bot  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat = (os.environ.get("TELEGRAM_TRADE_CHAT_ID", "") or
                    os.environ.get("TELEGRAM_CHAT_ID", ""))
            if bot and chat:
                send_telegram(msg, bot, chat)
        print("[SWINGRATE] swing rating sent")
    except Exception as e:
        print(f"[SWINGRATE] send error: {e}")
