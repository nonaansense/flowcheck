"""
targeted_swing_rating.py — 3:30pm swing-worthiness rating for the day's
Targeted_Strikes_Expiry alerts.

Scores each alerted run 0-100 on whether it's worth HOLDING as a swing,
which is a different question from whether the flow was interesting. Most
targeted-strike alerts fire late in the session on 0-3 DTE contracts; those
are scalps, not swings, and the DTE gate below reflects that deliberately.

Scoring (0-100):
  DTE suitability        25   swing needs time; 0-2 DTE is disqualifying
  Daily/4H alignment     20   reuses swing_scanner._score_technicals, which
                              also GATEKEEPS on daily + 4H regime (None =
                              disqualified — the trend disagrees)
  30M chart structure    20   30-minute trend (EMA9/21 stack) + distance to
                              the next 30M support/resistance pivot
  Flow conviction        10   run length, premium, sweep share
  Premium skew           10   does the ticker's put/call premium agree with
                              the alert's direction?
  GEX positioning        10   room to the next wall in the trade's direction
  Dark pool               5   real off-exchange prints (Bullflow API)

Gatekeepers (score forced to 0, reason recorded):
  • DTE < TARGETED_SWING_MIN_DTE (default 5)
  • Daily/4H regime disagrees with the alert direction

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
    """0-25. Swings need runway; weeklies expiring in days do not qualify."""
    if dte < MIN_DTE:
        return 0.0, f"only {dte}DTE — too short to swing"
    if dte <= 9:
        return 12.0, f"{dte}DTE — tight for a swing"
    if dte <= 21:
        return 25.0, f"{dte}DTE — good swing runway"
    if dte <= 60:
        return 22.0, f"{dte}DTE — plenty of time"
    return 16.0, f"{dte}DTE — long-dated, slower to move"


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
    return min(pts, 10.0), notes


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
        return 10.0, [f"flow {aligned_pct:.0f}% {direction}-side"]
    if aligned_pct >= 55:
        return 6.0, [f"flow leans {direction} ({aligned_pct:.0f}%)"]
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
            return 8.0, ["no GEX wall blocking"]
        dist_pct = abs(float(nearest["strike"]) - spot) / spot * 100
        if dist_pct >= 3:
            return 10.0, [f"{dist_pct:.1f}% of room to GEX wall"]
        if dist_pct >= 1.5:
            return 6.0, [f"{dist_pct:.1f}% to GEX wall"]
        return 2.0, [f"⚠️ GEX wall only {dist_pct:.1f}% away"]
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


def _score_30m_structure(ticker: str, direction: str) -> tuple:
    """
    0-20 from the 30-MINUTE chart: overall trend + where price sits relative
    to support/resistance.

    Trend (0-10): EMA9 vs EMA21 stack on 30M closes, plus price above/below.
    Levels (0-10): distance to the next 30M resistance (for calls) or support
    (for puts). Buying straight into overhead resistance is the single most
    common way a good-looking flow setup stalls, so it's scored explicitly.

    Returns (points, notes, detail_dict).
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

    # ── Trend on 30M (0-10) ──
    if ema9 and ema21:
        stacked_up   = ema9 > ema21 and px > ema9
        stacked_down = ema9 < ema21 and px < ema9
        aligned      = stacked_up if bull else stacked_down
        opposed      = stacked_down if bull else stacked_up
        if aligned:
            pts += 10.0
            notes.append(f"30M trend aligned (EMA9 {'>' if bull else '<'} EMA21, "
                         f"price {'above' if bull else 'below'})")
        elif opposed:
            notes.append("⚠️ 30M trend opposes the trade")
        else:
            pts += 4.0
            notes.append("30M trend mixed/flat")

    # ── Support & resistance from 30M pivots (0-10) ──
    highs, lows = _pivots(bars)
    res = [h for h in highs if h > px]
    sup = [l for l in lows  if l < px]
    nearest_res = min(res) if res else None
    nearest_sup = max(sup) if sup else None

    detail = {
        "px": round(px, 2),
        "ema9": round(ema9, 2), "ema21": round(ema21, 2),
        "resistance": round(nearest_res, 2) if nearest_res else None,
        "support":    round(nearest_sup, 2) if nearest_sup else None,
    }

    # For a call, the obstacle is overhead resistance; for a put, support below.
    obstacle = nearest_res if bull else nearest_sup
    if obstacle and px:
        room_pct = abs(obstacle - px) / px * 100
        detail["room_pct"] = round(room_pct, 2)
        if room_pct >= 2.0:
            pts += 10.0
            notes.append(f"{room_pct:.1f}% clear to 30M "
                         f"{'resistance' if bull else 'support'} ${obstacle:.2f}")
        elif room_pct >= 1.0:
            pts += 6.0
            notes.append(f"{room_pct:.1f}% to 30M "
                         f"{'resistance' if bull else 'support'} ${obstacle:.2f}")
        else:
            pts += 1.0
            notes.append(f"⚠️ 30M {'resistance' if bull else 'support'} "
                         f"${obstacle:.2f} only {room_pct:.1f}% away")
    else:
        pts += 8.0
        notes.append(f"no 30M {'resistance' if bull else 'support'} in range")

    # Note where the protective level sits — useful for stop placement.
    backstop = nearest_sup if bull else nearest_res
    if backstop:
        notes.append(f"30M {'support' if bull else 'resistance'} ${backstop:.2f}")

    return min(pts, 20.0), notes, detail


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

    # Gate 1 — DTE.
    dte_pts, dte_note = _score_dte(run.get("dte", 0))
    if dte_pts == 0:
        result["disqualified"] = dte_note
        return result

    # Gate 2 — technical regime. _score_technicals returns None when the
    # daily/4H trend disagrees with the direction; that's a hard stop for a
    # swing regardless of how strong the flow looked.
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

    notes = [dte_note] + list(tech.get("tech_notes", []))
    tech_pts = float(tech.get("tech_score", 0)) / 10.0 * 20.0
    score = dte_pts + tech_pts

    m30_pts, m30_notes, m30_detail = _score_30m_structure(ticker, direction)
    score += m30_pts; notes += m30_notes
    result["m30"] = m30_detail

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
