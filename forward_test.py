"""
forward_test.py — pre-registered forward validation of a backtest hypothesis.

Why pre-registration matters:

The "trend AND RSI, hold for the tail" rule was selected by searching ~56
segment/target combinations on 230 historical alerts. Its 95% interval
barely spans zero. That makes it the best CANDIDATE, not a validated edge —
the usual way people lose money is treating the two as the same thing.

This module locks the hypothesis in writing BEFORE any live data arrives:
the entry criteria, the expected hit rate, the break-even hit rate, and the
kill criterion. Once registered, those numbers cannot be edited by the
report — so the goalposts cannot move after a drawdown.

Live alerts matching the criteria are recorded automatically. Outcomes are
filled in later from Bullflow /v1/data/peakReturn (peak % since the trade
timestamp), which is exactly the metric the backtest used.

Commands:
  /forward_register   lock in the hypothesis (once)
  /forward_status     running results vs expectation
  /forward_update     fetch outcomes for pending candidates
"""
import os
import json
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
REG_KEY = "forward_test_registration"
REC_KEY = "forward_test_records"
BASE = "https://api.bullflow.io"


def _db_get(key):
    try:
        from storage import db_get
        raw = db_get(key)
        if not raw:
            return None
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        print(f"[FWDTEST] read error {key}: {e}")
        return None


def _db_set(key, obj) -> bool:
    try:
        from storage import db_set
        return bool(db_set(key, json.dumps(obj)))
    except Exception as e:
        print(f"[FWDTEST] write error {key}: {e}")
        return False


# ───────────────────── registration ─────────────────────

def register(target: float = 500.0, expected_hit: float = 21.0,
             breakeven_hit: float = 16.0, kill_after: int = 40,
             kill_below: float = 12.0) -> list:
    """
    Lock the hypothesis. Refuses to overwrite an existing registration —
    re-registering after seeing results is exactly the failure mode this
    guards against.
    """
    existing = _db_get(REG_KEY)
    if existing:
        return ["⚠️ A hypothesis is already registered "
                f"({existing.get('registered_at', '?')[:10]}).",
                "Re-registering after seeing results would defeat the purpose.",
                "Use /forward_status to see progress, or clear it deliberately."]

    reg = {
        "registered_at": datetime.now(ET).isoformat(),
        "criteria": "30M trend aligned AND RSI favorable, at alert time",
        "rule": f"hold for the tail; count a win at +{target:.0f}% peak",
        "target": target,
        "expected_hit_pct": expected_hit,
        "breakeven_hit_pct": breakeven_hit,
        "kill_after_n": kill_after,
        "kill_below_pct": kill_below,
        "source": "230 backtested alerts, Jan-Jul 2026; in-sample 95% CI "
                  "-0.7% to +130.3% per trade",
    }
    if not _db_set(REG_KEY, reg):
        return ["❌ Could not save registration."]
    return [
        "📌 HYPOTHESIS REGISTERED",
        f"   criteria: {reg['criteria']}",
        f"   rule: {reg['rule']}",
        f"   expected hit rate: {expected_hit:.0f}%  "
        f"(break-even {breakeven_hit:.0f}%)",
        f"   KILL: if after {kill_after} trades the hit rate is below "
        f"{kill_below:.0f}%, abandon this.",
        "",
        "Matching live alerts will now be recorded automatically.",
        "This registration is locked — results cannot change it.",
    ]


def is_registered() -> bool:
    return _db_get(REG_KEY) is not None


# ───────────────────── live capture ─────────────────────

def record_if_matches(result: dict) -> bool:
    """
    Called when a targeted alert fires. Records it if it meets the registered
    criteria. Returns True if recorded.

    Deliberately cheap on failure: any error just skips the record rather
    than interfering with the alert itself.
    """
    if os.environ.get("FORWARD_TEST_ENABLED", "true").lower() \
            not in ("true", "1", "yes", "on"):
        return False
    reg = _db_get(REG_KEY)
    if not reg:
        return False
    try:
        from targeted_swing_rating import rate_run
        fills = result.get("fills", [])
        if not fills:
            return False
        run = {
            "ticker": result["ticker"], "direction": result.get("direction", "call"),
            "strike": result.get("strike", ""), "expiry": result.get("expiry", ""),
            "count": result.get("count", 0),
            "total_prem": result.get("total_prem", 0),
            "sweeps": sum(1 for f in fills if f.get("sweep")),
            "dte": fills[-1].get("dte", 0),
            "stock_px": fills[-1].get("stock_px", 0),
            "fills": fills,
        }
        r = rate_run(run)
        m30 = r.get("m30") or {}
        notes = " ".join(r.get("notes") or [])
        trend_ok = "30M trend aligned" in notes
        rsi = m30.get("rsi", 0) or 0
        bull = run["direction"] == "call"
        rsi_ok = bool(rsi) and ((50 <= rsi <= 70) if bull else (30 <= rsi <= 50))
        if not (trend_ok and rsi_ok):
            return False

        trig = fills[-1]
        recs = _db_get(REC_KEY) or []
        key = (f"{datetime.now(ET).strftime('%Y-%m-%d')}_{run['ticker']}"
               f"_{run['strike']}_{run['expiry']}_{run['direction']}")
        if any(x.get("k") == key for x in recs):
            return False          # one record per contract per day
        recs.append({
            "k": key,
            "date": datetime.now(ET).strftime("%Y-%m-%d"),
            "time": datetime.now(ET).strftime("%H:%M:%S"),
            "ticker": run["ticker"], "direction": run["direction"],
            "strike": run["strike"], "expiry": run["expiry"],
            "occ": trig.get("occ", ""),
            "entry": float(trig.get("price", 0) or 0),
            "ts": float(trig.get("ts", 0) or 0),
            "score": r.get("score", 0),
            "rsi": round(rsi, 1),
            "peak_pct": None,          # filled by update_outcomes()
        })
        _db_set(REC_KEY, recs)
        print(f"[FWDTEST] recorded {run['ticker']} {run['strike']}"
              f"{'C' if bull else 'P'} — matches registered criteria")
        return True
    except Exception as e:
        print(f"[FWDTEST] record error: {e}")
        return False


# ───────────────────── outcomes ─────────────────────

def update_outcomes() -> list:
    """
    Fill in peak returns for pending records via Bullflow /v1/data/peakReturn
    (60 req/min). Only touches records that don't yet have an outcome.
    """
    recs = _db_get(REC_KEY) or []
    pending = [r for r in recs if r.get("peak_pct") is None
               and r.get("occ") and r.get("entry", 0) > 0]
    if not pending:
        return ["Nothing pending — all recorded trades already have outcomes."]
    key = os.environ.get("BULLFLOW_API_KEY", "").strip()
    if not key:
        return ["BULLFLOW_API_KEY not set — cannot fetch outcomes."]

    done = fail = 0
    for r in pending[:50]:
        try:
            resp = requests.get(f"{BASE}/v1/data/peakReturn",
                                params={"key": key, "sym": r["occ"],
                                        "old_price": r["entry"],
                                        "trade_timestamp": int(r["ts"])},
                                timeout=20)
            if resp.status_code == 200:
                body = resp.json() or {}
                pk = body.get("peakPercentReturnSinceTimestamp")
                if pk is not None:
                    r["peak_pct"] = round(float(pk), 1)
                    done += 1
                else:
                    fail += 1
            else:
                print(f"[FWDTEST] peakReturn HTTP {resp.status_code} "
                      f"for {r['occ']}: {resp.text[:100]}")
                fail += 1
            time.sleep(1.1)                # stay under 60/min
        except Exception as e:
            print(f"[FWDTEST] peakReturn error {r.get('occ')}: {e}")
            fail += 1
    _db_set(REC_KEY, recs)
    return [f"Updated {done} outcome(s), {fail} failed, "
            f"{len(pending) - done - fail} still pending."]


# ───────────────────── reporting ─────────────────────

def status() -> list:
    reg = _db_get(REG_KEY)
    if not reg:
        return ["No hypothesis registered. Run /forward_register first."]
    recs = _db_get(REC_KEY) or []
    settled = [r for r in recs if r.get("peak_pct") is not None]
    pending = len(recs) - len(settled)

    target = float(reg.get("target", 500))
    exp_hit = float(reg.get("expected_hit_pct", 21))
    be_hit = float(reg.get("breakeven_hit_pct", 16))
    kill_n = int(reg.get("kill_after_n", 40))
    kill_p = float(reg.get("kill_below_pct", 12))

    lines = [
        "📊 FORWARD TEST STATUS",
        f"━━━ registered {reg.get('registered_at', '?')[:10]} ━━━",
        f"   criteria: {reg.get('criteria')}",
        f"   win = peak ≥ +{target:.0f}%",
        "",
        f"recorded: {len(recs)}  |  settled: {len(settled)}  |  "
        f"pending: {pending}",
    ]
    if not settled:
        lines += ["", "No settled outcomes yet. Run /forward_update after",
                  "positions have had time to play out."]
        return lines

    wins = sum(1 for r in settled if r["peak_pct"] >= target)
    hit = wins / len(settled) * 100
    avg_peak = sum(r["peak_pct"] for r in settled) / len(settled)
    lines += [
        "",
        f"   hit rate: {wins}/{len(settled)} ({hit:.0f}%)",
        f"   expected: {exp_hit:.0f}%   break-even: {be_hit:.0f}%",
        f"   avg peak: {avg_peak:+.0f}%",
        "",
    ]

    if len(settled) < kill_n:
        lines.append(f"⏳ {kill_n - len(settled)} more trades before the kill "
                     f"criterion applies.")
        lines.append("   Do not judge it yet — this n is far too small.")
    elif hit < kill_p:
        lines += [f"🛑 KILL CRITERION HIT — {hit:.0f}% is below the "
                  f"{kill_p:.0f}% you pre-committed to.",
                  "   The hypothesis failed forward validation. Abandon it."]
    elif hit < be_hit:
        lines.append(f"⚠️ Below break-even ({be_hit:.0f}%) but above the kill "
                     f"line. Marginal.")
    else:
        lines.append(f"✅ Running above break-even. Still forward data — keep "
                     f"going before sizing up.")

    recent = sorted(settled, key=lambda r: (r["date"], r["time"]))[-5:]
    lines += ["", "recent:"]
    for r in recent:
        o = "C" if r["direction"] == "call" else "P"
        mark = "✅" if r["peak_pct"] >= target else "  "
        lines.append(f"  {mark} {r['date']} ${r['ticker']} {r['strike']}{o} "
                     f"peak {r['peak_pct']:+.0f}%")
    return lines
