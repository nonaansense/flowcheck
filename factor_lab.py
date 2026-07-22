"""
factor_lab.py — single-factor A/B testing for targeted-strike alerts.

Why this exists instead of tuning the composite score:

A 7-component weighted score needs far more data to validate than a
0-5 DTE alert stream produces. One factor at a time is answerable with the
sample sizes you actually have. This module asks one question per factor:

    "When factor X is present, do these alerts perform better?"

and answers it with an effect size, a confidence interval, and a p-value —
not a vibe.

Three safeguards against fooling yourself, which is the default outcome of
adaptive alpha search:

1. MULTIPLE TESTING. Testing 13 factors at p<0.05 means ~1 false positive
   per run by chance alone. A Bonferroni-corrected threshold is reported
   alongside the raw p-value; only the corrected column should move you.

2. WALK-FORWARD. Alerts are split chronologically. A factor is only
   credible if it holds in the OUT-OF-SAMPLE half it was never measured on.
   In-sample-only results are labelled as such and should be ignored.

3. MINIMUM SAMPLE. Anything under MIN_N per side is reported but flagged
   "underpowered" — the difference is not distinguishable from noise.

Outcome metric is win-at-peak (max gain >= target %), not return at expiry.
These are 0-5 DTE contracts; expiry return is dominated by theta and tells
you about decay, not about whether the setup worked.

Telegram: /factor_lab YYYY-MM-DD YYYY-MM-DD
"""
import os
import json
import math
import threading
from datetime import datetime

MIN_N = int(os.environ.get("FACTOR_LAB_MIN_N", "20"))
WIN_TARGET = float(os.environ.get("TARGETED_STRIKES_WIN_TARGET", "50"))

FACTOR_LABELS = {
    "30m_trend_aligned": "30M trend aligned with trade",
    "daily_aligned":     "Daily regime aligned",
    "room_2pct_plus":    "2%+ room to next 30M level",
    "rsi_favorable":     "RSI in favorable zone",
    "rsi_extended":      "RSI extended (overbought/oversold)",
    "flow_skew_aligned": "Ticker flow 60%+ same side",
    "big_premium_500k":  "Run premium >= $500K",
    "long_run_6plus":    "Run length >= 6",
    "sweep_heavy":       "50%+ of fills were sweeps",
    "early_session":     "Touched pre-cutoff flow",
    "is_call":           "Call (vs put)",
    "dte_2plus":         "DTE >= 2",
    "darkpool_aligned":  "Dark pool lean agrees",
}


# ───────────────────── statistics ─────────────────────

def _usable(alerts: list) -> list:
    """
    Alerts that can be analysed: they have a realised outcome (pricing) and
    recorded factors.

    Deliberately does NOT exclude alerts flagged swing_dq. A disqualification
    means "don't trade this", not "bad data" — and the reasons for DQ (daily
    regime opposed, etc.) are themselves factors under test. Filtering them
    out would remove the contrast group and make those factors unmeasurable.
    """
    return [a for a in alerts if a.get("pricing") and a.get("factors")]


def _drop_reasons(alerts: list) -> list:
    """Explain what got excluded, so an empty sample is diagnosable."""
    total = len(alerts)
    no_price = sum(1 for a in alerts if not a.get("pricing"))
    no_fact  = sum(1 for a in alerts if a.get("pricing") and not a.get("factors"))
    usable   = total - no_price - no_fact
    lines = [f"   {total} scored alerts → {usable} usable"]
    if no_price:
        lines.append(f"   • {no_price} dropped: no option pricing (Massive)")
    if no_fact:
        lines.append(f"   • {no_fact} dropped: no factors recorded")
    dq = sum(1 for a in alerts if a.get("swing_dq"))
    if dq:
        lines.append(f"   ({dq} flagged untradeable — KEPT for analysis)")
    return lines


POOL_KEY = "factor_lab_pool"
POOL_MAX = int(os.environ.get("FACTOR_LAB_POOL_MAX", "3000"))

# save_to_pool is read-modify-write against Supabase. Several range backtests
# can run at once (each is its own thread), and without serialisation their
# writes clobber each other — thread A and B both read N records, both write
# N+their own, and whichever finishes last silently erases the other's month.
_POOL_LOCK = threading.Lock()


def _pool_record(a: dict) -> dict:
    """Minimal record — only what the analysis needs, so the pool stays small."""
    pr = a.get("pricing") or {}
    return {
        "k":   f"{a.get('date','')}_{a.get('ticker','')}_{a.get('strike','')}"
               f"_{a.get('expiry','')}_{a.get('direction','')}",
        "d":   a.get("date", ""),
        "t":   a.get("ticker", ""),
        "dir": a.get("direction", ""),
        "f":   a.get("factors") or {},
        "mg":  round(float(pr.get("max_gain_pct", 0)), 1),
        "ex":  round(float(pr.get("expiry_pct", 0)), 1),
        "sc":  a.get("swing_score") or 0,
    }


def save_to_pool(alerts: list) -> int:
    """
    Append this run's scored alerts to the persistent pool.

    Bullflow caps a replay range at 31 days, so a statistically useful sample
    can only be built by running several months and accumulating. Records are
    deduped by date+contract, so re-running a month overwrites rather than
    double-counts.

    Returns the pool size after saving.
    """
    usable = _usable(alerts)
    if not usable:
        return 0
    # Hold the lock across the WHOLE read-modify-write, not just the write —
    # re-reading inside the lock is what makes concurrent runs safe.
    with _POOL_LOCK:
        try:
            from storage import db_get, db_set
            # db_get returns a STRING — the repo convention is json dumps/loads.
            # Passing a raw list meant the isinstance check below failed on
            # every read and the pool silently reset to empty each run.
            raw = db_get(POOL_KEY)
            pool = []
            if raw:
                try:
                    pool = json.loads(raw) if isinstance(raw, str) else raw
                except Exception as pe:
                    print(f"[FACTORLAB] pool parse error: {pe}")
                    pool = []
            if not isinstance(pool, list):
                pool = []
            by_key = {r.get("k"): r for r in pool if isinstance(r, dict)}
            for a in usable:
                r = _pool_record(a)
                by_key[r["k"]] = r
            merged = sorted(by_key.values(), key=lambda r: r.get("d", ""))
            if len(merged) > POOL_MAX:
                merged = merged[-POOL_MAX:]      # keep the most recent
            db_set(POOL_KEY, json.dumps(merged))
            print(f"[FACTORLAB] pool now {len(merged)} alerts "
                  f"(+{len(usable)} from this run)")
            return len(merged)
        except Exception as e:
            print(f"[FACTORLAB] pool save error: {e}")
            return 0


def load_pool() -> list:
    """Pooled records re-inflated into the shape analyze_factor expects."""
    try:
        from storage import db_get
        raw = db_get(POOL_KEY)
        if not raw:
            return []
        pool = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(pool, list):
            return []
    except Exception as e:
        print(f"[FACTORLAB] pool load error: {e}")
        return []
    out = []
    for r in pool:
        if not isinstance(r, dict) or not r.get("f"):
            continue
        out.append({
            "date": r.get("d", ""), "time": "", "ticker": r.get("t", ""),
            "direction": r.get("dir", ""), "factors": r.get("f", {}),
            "pricing": {"max_gain_pct": r.get("mg", 0),
                        "expiry_pct": r.get("ex", 0)},
            "swing_dq": None, "swing_score": r.get("sc", 0),
        })
    return out


def clear_pool() -> bool:
    """Wipe the pool. Use when it was filled from runs with broken data
    (e.g. dead Massive key -> every technical factor recorded as False)."""
    with _POOL_LOCK:
        try:
            from storage import db_set
            db_set(POOL_KEY, json.dumps([]))
            print("[FACTORLAB] pool cleared")
            return True
        except Exception as e:
            print(f"[FACTORLAB] pool clear error: {e}")
            return False


def pool_health() -> list:
    """
    Warn if the pool looks like it was built without technical data. If the
    Massive key was dead during a run, every 30M/daily factor lands as False
    and the analysis is worthless — that's silent unless it's checked.
    """
    pool = load_pool()
    if not pool:
        return []
    tech = ["30m_trend_aligned", "daily_aligned", "room_2pct_plus", "rsi_favorable"]
    n_tech_true = sum(1 for p in pool
                      if any(p["factors"].get(t) for t in tech))
    pct = n_tech_true / len(pool) * 100
    if pct < 10:
        return ["", f"⚠️ Only {pct:.0f}% of pooled alerts have ANY technical factor set.",
                "   That usually means option/stock bar data was unavailable",
                "   (dead Massive key) when these months were backtested.",
                "   Fix the key, then /factor_pool clear and re-run the months."]
    return []


def pool_summary() -> list:
    """One-line status of the accumulated pool."""
    pool = load_pool()
    if not pool:
        return ["Factor pool is empty — run /targeted_backtest_range first."]
    dates = sorted({p["date"] for p in pool if p.get("date")})
    months = sorted({d[:7] for d in dates})
    return ([f"📦 Factor pool: {len(pool)} alerts across {len(months)} month(s)",
             f"   {dates[0]} → {dates[-1]}",
             f"   months: {', '.join(months)}"] + pool_health())


def _min_detectable_effect(n1: int, n2: int, base_rate: float,
                           alpha: float, power: float = 0.80) -> float:
    """
    Smallest difference in win rate (percentage points) this sample could
    detect at the given alpha and power.

    Uses the standard two-proportion approximation:
        MDE ≈ (z_alpha/2 + z_beta) * sqrt(p(1-p) * (1/n1 + 1/n2))

    This is the number that tells you whether "not significant" means "no
    edge" or simply "not enough data". At n≈145 split 13 ways with a
    Bonferroni correction, only very large effects are visible — so a null
    result carries almost no information about small, realistic edges.
    """
    if n1 < 2 or n2 < 2:
        return 100.0
    z_a = {0.05: 1.960, 0.01: 2.576, 0.0038: 2.895,
           0.005: 2.807, 0.001: 3.291}.get(round(alpha, 4), 2.9)
    z_b = 0.842                                   # 80% power
    p = min(max(base_rate, 0.05), 0.95)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (z_a + z_b) * se * 100


def _power_note(usable: list, in_s: list, n_tests: int) -> list:
    """Explain what this sample size can and cannot resolve."""
    n = len(usable)
    wins = sum(1 for a in usable if _won(a))
    base = wins / n if n else 0.35
    # Typical split: roughly half the alerts carry a given factor.
    half_in = max(len(in_s) // 2, 1)
    mde_in = _min_detectable_effect(half_in, half_in, base, 0.05)
    bonf = 0.05 / max(n_tests, 1)
    mde_full = _min_detectable_effect(n // 2, n // 2, base, bonf)

    # How many alerts would be needed to see a realistic 15pp edge?
    need = n
    while need < 5000:
        if _min_detectable_effect(need // 2, need // 2, base, bonf) <= 15.0:
            break
        need += 25

    lines = [
        "",
        "📐 WHAT THIS SAMPLE CAN RESOLVE",
        f"   base win rate: {base*100:.0f}%  ({wins}/{n} hit +{WIN_TARGET:.0f}%)",
        f"   in-sample screening can detect ≥{mde_in:.0f}pp effects",
        f"   corrected full-sample can detect ≥{mde_full:.0f}pp effects",
    ]
    if need < 5000:
        lines.append(f"   to detect a realistic 15pp edge you'd need ~{need} alerts")
    else:
        lines.append("   a 15pp edge is not reachable at any practical sample size here")
    lines.append("   → 'not significant' at this n mostly means UNDERPOWERED,")
    lines.append("     not 'no edge exists'.")
    return lines


def pool_stats() -> list:
    """
    Full performance report computed from ALREADY-STORED pool records.
    No replay, no API calls — everything here comes from data collected
    during previous backtest runs.

    Covers win rates at several targets, outcome distribution, direction
    split, month-by-month breakdown (the decay check), and score-vs-outcome
    buckets.
    """
    pool = load_pool()
    if not pool:
        return ["Factor pool is empty — nothing to analyse."]

    n = len(pool)
    gains = [float(p["pricing"]["max_gain_pct"]) for p in pool]
    exps  = [float(p["pricing"]["expiry_pct"])   for p in pool]

    def med(v):
        s = sorted(v); k = len(s)
        return 0.0 if not s else (s[k // 2] if k % 2 else (s[k//2 - 1] + s[k//2]) / 2)

    avg = lambda v: (sum(v) / len(v)) if v else 0.0
    hit = lambda t: sum(1 for g in gains if g >= t)

    dates = sorted({p["date"] for p in pool if p.get("date")})
    lines = [
        "📊 POOL PERFORMANCE (from stored data — no re-run)",
        f"━━━ {n} alerts | {dates[0] if dates else '?'} → "
        f"{dates[-1] if dates else '?'} ━━━",
        "",
        "🎯 WIN RATE BY TARGET (max gain reached at any point)",
    ]
    for t in (25, 50, 100, 200):
        h = hit(t)
        lines.append(f"   +{t}%: {h}/{n} ({h/n*100:.0f}%)")

    win_exp = sum(1 for e in exps if e > 0)
    lines += [
        "",
        "📈 OUTCOME DISTRIBUTION",
        f"   avg max gain:  {avg(gains):+.0f}%   median {med(gains):+.0f}%",
        f"   avg at expiry: {avg(exps):+.0f}%   median {med(exps):+.0f}%",
        f"   green at expiry: {win_exp}/{n} ({win_exp/n*100:.0f}%)",
    ]

    # Direction split.
    calls = [p for p in pool if p.get("direction") == "call"]
    puts  = [p for p in pool if p.get("direction") == "put"]
    if calls and puts:
        cg = [p["pricing"]["max_gain_pct"] for p in calls]
        pg = [p["pricing"]["max_gain_pct"] for p in puts]
        cw = sum(1 for g in cg if g >= WIN_TARGET)
        pw = sum(1 for g in pg if g >= WIN_TARGET)
        lines += [
            "",
            "📞 BY DIRECTION",
            f"   calls: {len(calls)} | {cw} hit +{WIN_TARGET:.0f}% "
            f"({cw/len(calls)*100:.0f}%) | avg max {avg(cg):+.0f}%",
            f"   puts:  {len(puts)} | {pw} hit +{WIN_TARGET:.0f}% "
            f"({pw/len(puts)*100:.0f}%) | avg max {avg(pg):+.0f}%",
        ]

    # Month by month — this is the decay / regime check.
    months = {}
    for p in pool:
        months.setdefault(p["date"][:7], []).append(p)
    if len(months) > 1:
        lines += ["", "📅 BY MONTH (is the edge stable or decaying?)"]
        for m in sorted(months):
            g = [x["pricing"]["max_gain_pct"] for x in months[m]]
            w = sum(1 for x in g if x >= WIN_TARGET)
            lines.append(f"   {m}: n={len(g):3d} | {w:2d} hit +{WIN_TARGET:.0f}% "
                         f"({w/len(g)*100:3.0f}%) | avg max {avg(g):+5.0f}%")

    # Score vs outcome — does the composite rating predict anything?
    scored = [p for p in pool if p.get("swing_score")]
    if len(scored) >= 8:
        lines += ["", "🎯 SWING SCORE vs OUTCOME"]
        for label, lo, hi in [("70-100", 70, 101), ("55-69", 55, 70),
                              ("40-54", 40, 55), ("0-39", 0, 40)]:
            grp = [p for p in scored if lo <= p["swing_score"] < hi]
            if not grp:
                continue
            g = [x["pricing"]["max_gain_pct"] for x in grp]
            w = sum(1 for x in g if x >= WIN_TARGET)
            lines.append(f"   {label}: n={len(g):3d} | {w:2d} hit +{WIN_TARGET:.0f}% "
                         f"({w/len(g)*100:3.0f}%) | avg max {avg(g):+5.0f}%")
        lines.append("   (monotonic = the score works; flat = it doesn't)")

    # Per-factor win rates, sorted — descriptive, no significance claimed.
    lines += ["", "🔍 WIN RATE BY FACTOR (descriptive only — see /factor_lab)"]
    rows = []
    for f, label in FACTOR_LABELS.items():
        wf = [p for p in pool if p["factors"].get(f)]
        wo = [p for p in pool if not p["factors"].get(f)]
        if len(wf) < 5 or len(wo) < 5:
            continue
        r1 = sum(1 for p in wf if p["pricing"]["max_gain_pct"] >= WIN_TARGET) / len(wf) * 100
        r2 = sum(1 for p in wo if p["pricing"]["max_gain_pct"] >= WIN_TARGET) / len(wo) * 100
        rows.append((r1 - r2, label, r1, r2, len(wf), len(wo)))
    rows.sort(reverse=True)
    for d, label, r1, r2, n1, n2 in rows[:10]:
        lines.append(f"   {d:+5.0f}pp  {label}: {r1:.0f}% (n={n1}) vs {r2:.0f}% (n={n2})")

    lines += ["", "⚠️ Descriptive only. Differences here are NOT tested for",
              "   significance — run /factor_lab for that."]
    return lines


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _two_proportion_test(s1: int, n1: int, s2: int, n2: int) -> dict:
    """
    Two-proportion z-test. s=successes, n=sample size.
    Returns rate1, rate2, diff, z, p (two-tailed), and a 95% CI on the diff.
    """
    if n1 == 0 or n2 == 0:
        return {}
    p1, p2 = s1 / n1, s2 / n2
    diff = p1 - p2
    pool = (s1 + s2) / (n1 + n2)
    se_pool = math.sqrt(pool * (1 - pool) * (1 / n1 + 1 / n2))
    z = (diff / se_pool) if se_pool > 0 else 0.0
    p = 2 * (1 - _norm_cdf(abs(z)))
    # CI uses unpooled SE (standard for a difference estimate)
    se_un = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    return {"rate_with": p1 * 100, "rate_without": p2 * 100,
            "diff": diff * 100, "z": z, "p": p,
            "ci_lo": (diff - 1.96 * se_un) * 100,
            "ci_hi": (diff + 1.96 * se_un) * 100}


def _won(alert: dict) -> bool:
    pr = alert.get("pricing") or {}
    return float(pr.get("max_gain_pct", 0)) >= WIN_TARGET


# ───────────────────── analysis ─────────────────────

def analyze_factor(alerts: list, factor: str) -> dict:
    with_f = [a for a in alerts if a["factors"].get(factor)]
    without = [a for a in alerts if not a["factors"].get(factor)]
    if not with_f or not without:
        return {}
    res = _two_proportion_test(sum(1 for a in with_f if _won(a)), len(with_f),
                               sum(1 for a in without if _won(a)), len(without))
    if not res:
        return {}
    avg = lambda g: (sum(a["pricing"]["max_gain_pct"] for a in g) / len(g)) if g else 0.0
    res.update({
        "factor": factor, "label": FACTOR_LABELS.get(factor, factor),
        "n_with": len(with_f), "n_without": len(without),
        "avg_max_with": avg(with_f), "avg_max_without": avg(without),
        "underpowered": len(with_f) < MIN_N or len(without) < MIN_N,
    })
    return res


def _split_chronologically(alerts: list, frac: float = 0.6) -> tuple:
    """Oldest `frac` = in-sample, remainder = out-of-sample."""
    ordered = sorted(alerts, key=lambda a: (a.get("date", ""), a.get("time", "")))
    cut = int(len(ordered) * frac)
    return ordered[:cut], ordered[cut:]


def run_factor_lab(alerts: list) -> list:
    """
    Full report. Every factor is measured in-sample, then re-measured on the
    held-out remainder. Only factors that survive BOTH — and the multiple-
    testing correction — are worth acting on.
    """
    usable = _usable(alerts)
    if len(usable) < MIN_N * 2:
        return ([f"⚠️ Only {len(usable)} usable alerts (need ≥{MIN_N*2})."]
                + _drop_reasons(alerts)
                + ["", "If most were dropped for pricing, Massive isn't"
                       " returning bars — check the logs for HTTP 401/429."])

    in_s, out_s = _split_chronologically(usable)
    factors = [f for f in FACTOR_LABELS if any(f in a["factors"] for a in usable)]
    n_tests = len(factors)

    # PROPER WALK-FORWARD:
    #   1. Screen on in-sample only (loose threshold — this is discovery).
    #   2. Re-test survivors on the held-out half with a FRESH significance
    #      test. That out-of-sample p is the decision statistic.
    #   3. Bonferroni-correct by the number of candidates CARRIED FORWARD,
    #      not the number originally screened.
    # Computing p on the combined sample and then checking sign agreement —
    # which is what this did first — produced false positives on ~2 of 3
    # pure-noise datasets. Sign agreement alone is not evidence.
    results = []
    for f in factors:
        r_all = analyze_factor(usable, f)
        if not r_all:
            continue
        r_in  = analyze_factor(in_s, f)
        r_out = analyze_factor(out_s, f)
        r_all["in_diff"]  = r_in.get("diff")  if r_in  else None
        r_all["in_p"]     = r_in.get("p")     if r_in  else None
        r_all["out_diff"] = r_out.get("diff") if r_out else None
        r_all["out_p"]    = r_out.get("p")    if r_out else None
        r_all["out_n"]    = (r_out.get("n_with", 0) + r_out.get("n_without", 0)) if r_out else 0
        r_all["screened"] = (r_all["in_p"] is not None and r_all["in_p"] < 0.05
                             and r_all["in_diff"] is not None
                             and abs(r_all["in_diff"]) >= 5.0)
        results.append(r_all)

    # Correction applies to candidates that passed screening.
    candidates = [r for r in results if r["screened"]]
    bonf = 0.05 / max(len(candidates), 1)
    for r in results:
        r["holds"] = bool(
            r["screened"] and r["out_p"] is not None and r["out_diff"] is not None
            and r["out_p"] < bonf
            and r["in_diff"] * r["out_diff"] > 0
            and abs(r["out_diff"]) >= 5.0
        )

    results.sort(key=lambda r: -abs(r["diff"]))

    dates = sorted({a.get("date", "") for a in usable if a.get("date")})
    lines = [
        "🔬 FACTOR LAB — single-factor A/B",
        f"━━━ {len(usable)} alerts | {dates[0] if dates else '?'} → "
        f"{dates[-1] if dates else '?'} ━━━",
        f"outcome = max gain ≥ {WIN_TARGET:.0f}%  |  {n_tests} factors tested",
        f"in-sample {len(in_s)} / out-of-sample {len(out_s)}",
        "",
        f"screened in-sample (p<0.05, ≥5pp) → {len(candidates)} candidate(s)",
        f"out-of-sample threshold: p < {bonf:.4f} (Bonferroni on candidates)",
        "",
    ]

    lines += _power_note(usable, in_s, n_tests)
    lines.append("")

    strong = [r for r in results if r["holds"] and not r["underpowered"]]
    weak   = [r for r in results if r not in strong]

    if strong:
        lines.append("✅ SURVIVES CORRECTION + HOLDS OUT-OF-SAMPLE")
        for r in strong:
            lines += _fmt(r)
        lines.append("")
    else:
        lines.append("❌ No factor survived both correction and out-of-sample.")
        lines.append("   Nothing here is worth trading on yet.")
        lines.append("")

    near = [r for r in results
            if r.get("screened") and not r["holds"]
            and r.get("out_diff") is not None and r.get("in_diff") is not None
            and r["in_diff"] * r["out_diff"] > 0 and abs(r["out_diff"]) >= 5.0]
    if near:
        lines.append("🟡 CONSISTENT ACROSS BOTH HALVES, but not yet significant")
        for r in near:
            lines.append(f"  {r['label']}: {r['in_diff']:+.0f}pp in-sample, "
                         f"{r['out_diff']:+.0f}pp out-of-sample "
                         f"(p={r['out_p']:.3f}, need <{bonf:.4f})")
        lines += ["   These are the ones to watch. Same direction and a",
                  "   material effect in BOTH halves is what a real edge looks",
                  "   like early — but it is also what noise looks like, so",
                  "   more data is the only way to tell.", ""]

    lines.append("── everything else (informational only) ──")
    for r in weak[:8]:
        flag = []
        if r["underpowered"]:  flag.append("underpowered")
        if not r["screened"]:  flag.append(f"not significant in-sample (p={r['in_p']:.3f})"
                                           if r["in_p"] is not None else "no in-sample data")
        elif not r["holds"]:
            # Show the out-of-sample EFFECT alongside the p. "+18pp, p=0.08"
            # is an underpowered but consistent result; "+2pp, p=0.08" is
            # nothing. Reporting only the p makes those look identical.
            if r["out_p"] is not None and r["out_diff"] is not None:
                consistent = r["in_diff"] * r["out_diff"] > 0 and abs(r["out_diff"]) >= 5
                tag = "CONSISTENT but underpowered" if consistent else "FAILED"
                flag.append(f"{tag} out-of-sample: {r['out_diff']:+.0f}pp, "
                            f"p={r['out_p']:.3f}")
            else:
                flag.append("no out-of-sample data")
        # Show the IN-SAMPLE diff next to the in-sample p — pairing a
        # full-sample effect size with an in-sample p-value reads as a
        # contradiction (e.g. "+22pp, p=0.62") and invites bad conclusions.
        d = r["in_diff"] if r.get("in_diff") is not None else r["diff"]
        lines.append(f"  {r['label']}: {d:+.0f}pp in-sample "
                     f"(full {r['diff']:+.0f}pp, n={r['n_with']}/{r['n_without']}) "
                     f"— {', '.join(flag)}")

    pos = sum(1 for r in results if r["diff"] > 0)
    tot = len(results)
    if tot >= 8:
        lines += ["",
                  f"🧭 Aggregate lean: {pos}/{tot} factors point positive.",
                  "   Under pure noise this should be about half. A strong",
                  "   skew hints at something real but is NOT evidence on its",
                  "   own — the factors overlap heavily (a call in an uptrend",
                  "   trips several at once), so they are not independent."]

    lines += [
        "",
        "⚠️ A factor is only actionable if it survives the corrected p AND",
        "   holds out-of-sample. Anything else is very likely noise — do not",
        "   retune the score against it.",
    ]
    return lines


def _fmt(r: dict) -> list:
    return [
        f"  📊 {r['label']}",
        f"     with:    {r['rate_with']:.0f}% win (n={r['n_with']}, "
        f"avg max {r['avg_max_with']:+.0f}%)",
        f"     without: {r['rate_without']:.0f}% win (n={r['n_without']}, "
        f"avg max {r['avg_max_without']:+.0f}%)",
        f"     edge: {r['diff']:+.0f}pp  95%CI [{r['ci_lo']:+.0f}, {r['ci_hi']:+.0f}]  "
        f"p={r['p']:.4f}",
        f"     in-sample:     {r['in_diff']:+.0f}pp (p={r['in_p']:.4f})",
        f"     OUT-OF-SAMPLE: {r['out_diff']:+.0f}pp (n={r['out_n']}, p={r['out_p']:.4f}) ✓",
    ]
