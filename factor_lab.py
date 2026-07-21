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
import math
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


def _usable(alerts: list) -> list:
    """Alerts with both pricing and recorded factors."""
    return [a for a in alerts
            if a.get("pricing") and a.get("factors") and not a.get("swing_dq")]


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
        return [f"⚠️ Only {len(usable)} usable alerts (need ≥{MIN_N*2}).",
                "Run a wider date range, and check that option pricing is",
                "working — alerts without pricing can't be scored."]

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

    lines.append("── everything else (informational only) ──")
    for r in weak[:8]:
        flag = []
        if r["underpowered"]:  flag.append("underpowered")
        if not r["screened"]:  flag.append(f"not significant in-sample (p={r['in_p']:.3f})"
                                           if r["in_p"] is not None else "no in-sample data")
        elif not r["holds"]:   flag.append(f"FAILED out-of-sample (p={r['out_p']:.3f})"
                                           if r["out_p"] is not None else "no out-of-sample data")
        lines.append(f"  {r['label']}: {r['diff']:+.0f}pp "
                     f"(n={r['n_with']}/{r['n_without']}) — {', '.join(flag)}")

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
