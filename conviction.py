"""
conviction.py - Multi-factor conviction scoring for FlowCheck.
Measures convergence of independent systems on the same trade.
Score 5-6/6 = worth taking. 2-3/6 = noise.
"""
import os, time, json
from zoneinfo import ZoneInfo
from datetime import datetime

FACTORS = ["flow","gex","technical","macro","repeat","xsource"]


def score_conviction(data: dict, trade: dict, result: dict,
                     flow_history: list = None) -> dict:
    ticker  = trade.get("ticker","").upper()
    scores  = {}
    notes   = {}

    # 1. Flow score >= 6.5/7
    fs = float(result.get("final_score", 0) or 0)
    scores["flow"] = fs >= 6.5
    notes["flow"]  = f"{fs}/7"

    # 2. GEX entry aligned
    ge = data.get("_gex_entry_score","")
    scores["gex"] = ge == "GOOD"
    notes["gex"]  = ge or "N/A"

    # 3. Technical MODERATE+ confirmed in last 4h
    try:
        from storage import db_get as _dg
        ts = float(_dg(f"tech_confirmed_{ticker.lower()}") or 0)
        scores["technical"] = ts > 0 and (time.time() - ts) < 14400
        notes["technical"]  = (f"{int((time.time()-ts)/60)}min ago"
                                if scores["technical"] else "not yet")
    except:
        scores["technical"] = False
        notes["technical"]  = "N/A"

    # 4. Macro: VIX calm + SPY direction matches
    vix    = float(data.get("vix", 20) or 20)
    trend  = str(data.get("spy_trend","") or "")
    is_call = "put" not in (trade.get("option_type","call") or "call").lower()
    if is_call:
        macro_ok = vix < 22 and trend in ("uptrend","flat")
    else:
        macro_ok = vix < 22 and trend in ("downtrend","flat")
    scores["macro"] = macro_ok
    notes["macro"]  = f"VIX {vix:.1f} | SPY {trend or 'unknown'}"

    # 5. Repeat flow: same ticker 2+ times in 7 days
    repeat = 0
    if flow_history:
        cutoff = time.time() - 7 * 86400
        cur_id = data.get("analysis_id","")
        for h in flow_history:
            if (h.get("ticker","").upper() == ticker
                    and float(h.get("timestamp",0) or h.get("time",0) or 0) > cutoff
                    and h.get("id","") != cur_id):
                repeat += 1
    scores["repeat"] = repeat >= 1
    notes["repeat"]  = f"{repeat} prior in 7d" if repeat else "first occurrence"

    # 6. Cross-source: both FlowGod + Bullflow in last 24h
    try:
        from storage import db_get as _dg2
        raw   = _dg2(f"xsource_{ticker.lower()}") or "{}"
        xdata = json.loads(raw)
        day   = time.time() - 86400
        has_fg = xdata.get("flowgod", 0) > day
        has_bf = xdata.get("bullflow", 0) > day
        scores["xsource"] = has_fg and has_bf
        notes["xsource"]  = "FlowGod + Bullflow" if scores["xsource"] else "single source"
    except:
        scores["xsource"] = False
        notes["xsource"]  = "N/A"

    total = sum(1 for v in scores.values() if v)
    if total >= 5:   label = "ELITE"
    elif total >= 4: label = "HIGH"
    elif total >= 3: label = "MODERATE"
    elif total >= 2: label = "LOW"
    else:            label = "SKIP"

    return {"total": total, "out_of": 6, "label": label,
            "scores": scores, "notes": notes}


def format_conviction(conv: dict) -> str:
    t, o, lbl = conv["total"], conv["out_of"], conv["label"]
    sc, nt = conv["scores"], conv["notes"]
    emoji = {"ELITE":"🔥","HIGH":"💎","MODERATE":"✅","LOW":"⚠️","SKIP":"❌"}
    lines = [f"📊 CONVICTION: {t}/{o} {emoji.get(lbl,'')} {lbl}"]
    keys  = [("flow","Flow score"),("gex","GEX entry"),
             ("technical","Technical"),("macro","Macro"),
             ("repeat","Repeat flow"),("xsource","Cross-source")]
    for k, label in keys:
        lines.append(f"  {'✅' if sc.get(k) else '❌'} {label}: {nt.get(k,'')}")
    return "\n".join(lines)


def update_xsource(ticker: str, source: str):
    """Call this when a flow alert fires to track cross-source."""
    from storage import db_get as _dg, db_set as _ds
    try:
        raw   = _dg(f"xsource_{ticker.lower()}") or "{}"
        xdata = json.loads(raw)
        xdata[source.lower()] = time.time()
        _ds(f"xsource_{ticker.lower()}", json.dumps(xdata))
    except: pass


def store_tech_confirmation(ticker: str):
    """Call this when technical scanner fires MODERATE+ on a watchlist ticker."""
    from storage import db_set as _ds
    try: _ds(f"tech_confirmed_{ticker.lower()}", str(time.time()))
    except: pass
