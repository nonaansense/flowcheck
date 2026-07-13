"""
preset_sweep.py — Parameter sweep for TP1 / TP2 / trailing-stop.

Streams a date range ONCE, collects every qualifying preset alert and its
option price path, then re-runs the 2-contract leg simulation across a grid
of TP1 / TP2 / trail-offset combinations — WITHOUT re-fetching any data
(option bars are cached by preset_backtest). Results go to an Excel ranked
by total P/L so you can see which parameters actually had an edge.

The point: your live settings are one cell in that grid. The sweep shows you
whether the neighbours are better, and by how much.

Telegram: /preset_sweep YYYY-MM-DD YYYY-MM-DD

Grid (override with env vars, comma-separated):
  SWEEP_TP1_PCTS   = 0.50,0.75,1.01,1.25,1.50
  SWEEP_TP2_PCTS   = 1.00,1.50,2.01,2.50
  SWEEP_TRAIL_PCTS = 0.30,0.40,0.50,0.60,0.75
"""
import os, re, threading
from datetime import datetime

MAX_RANGE_DAYS = 31


def _pcts(env_name: str, default: str) -> list:
    raw = os.environ.get(env_name, default)
    out = []
    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        try:
            out.append(float(p))
        except Exception:
            continue
    return out


def _grid() -> tuple:
    tp1s   = _pcts("SWEEP_TP1_PCTS",   "0.50,0.75,1.01,1.25,1.50")
    tp2s   = _pcts("SWEEP_TP2_PCTS",   "1.00,1.50,2.01,2.50")
    trails = _pcts("SWEEP_TRAIL_PCTS", "0.30,0.40,0.50,0.60,0.75")
    return tp1s, tp2s, trails


def _score_combo(trades: list, tp1_pct: float, tp2_pct: float,
                 trail_pct: float) -> dict:
    """
    Re-simulate every collected trade with one parameter combination.
    `trades` are dicts of {entry, flow_price, window} — the price path is
    already fetched, so this is pure arithmetic.

    Scored against the ENTIRE pooled trade set (all dates in the range), and
    also broken out per-day so a combo that only wins because of one outlier
    day can be spotted.
    """
    from preset_backtest import _run_legs
    import bullflow_presets as bp

    total_usd = 0.0
    pcts, wins, t1_hits, t2_hits = [], 0, 0, 0
    by_day: dict = {}

    for t in trades:
        entry  = t["entry"]
        window = t["window"]
        if entry <= 0 or not window:
            continue
        t1 = bp._floor_cent(entry * (1 + tp1_pct))
        t2 = bp._floor_cent(entry * (1 + tp2_pct))
        offset = round(t["flow_price"] * trail_pct, 2)
        if offset <= 0:
            continue

        res = _run_legs(window, entry, offset, t1, t2, use_trail=True)
        total_usd += res["pnl_usd"]
        pcts.append(res["pnl_pct"])
        by_day[t["date"]] = round(by_day.get(t["date"], 0.0) + res["pnl_usd"], 2)
        if res["pnl_usd"] > 0:
            wins += 1
        if res["leg1_reason"] == "TP1":
            t1_hits += 1
        if res["leg2_reason"] == "TP2":
            t2_hits += 1

    n = len(pcts)
    day_vals   = list(by_day.values())
    green_days = sum(1 for v in day_vals if v > 0)
    best_day   = max(day_vals) if day_vals else 0.0
    # How much of the total came from the single best day? A combo whose edge
    # is one lucky day is fragile, however good the headline number looks.
    concentration = round(best_day / total_usd * 100, 1) if total_usd > 0 else 0.0

    return {
        "tp1_pct":   round(tp1_pct * 100, 0),
        "tp2_pct":   round(tp2_pct * 100, 0),
        "trail_pct": round(trail_pct * 100, 0),
        "trades":    n,
        "total_usd": round(total_usd, 2),
        "avg_pct":   round(sum(pcts) / n, 1) if n else 0.0,
        "win_rate":  round(wins / n * 100, 1) if n else 0.0,
        "t1_rate":   round(t1_hits / n * 100, 1) if n else 0.0,
        "t2_rate":   round(t2_hits / n * 100, 1) if n else 0.0,
        "days":          len(day_vals),
        "green_days":    green_days,
        "green_day_pct": round(green_days / len(day_vals) * 100, 1) if day_vals else 0.0,
        "best_day_usd":  round(best_day, 2),
        "worst_day_usd": round(min(day_vals), 2) if day_vals else 0.0,
        "concentration": concentration,
        "by_day":        by_day,
    }


def _collect_trades(days: list, send, bot, chat) -> list:
    """
    Stream each day once, and for every qualifying alert grab the option's
    post-fill price path. Returns the raw material the sweep re-scores.
    """
    from preset_backtest import (collect_day, _expiry_to_iso, _fetch_option_daily,
                                 _fetch_option_intraday, INTRADAY_FILL)
    from datetime import timedelta

    trades = []
    for i, date in enumerate(days, 1):
        try:
            day = collect_day(date)
        except Exception as e:
            print(f"[SWEEP] {date}: {e}")
            continue

        for a in day.get("alerts", []):
            entry = float(a.get("entry_price") or 0)
            flow  = float(a.get("price") or 0)
            if entry <= 0 or flow <= 0:
                continue
            occ = (f"O:{a['ticker']}"
                   f"{_occ_tail(a)}")
            exp_iso = _expiry_to_iso(a.get("expiry", ""))
            if not exp_iso:
                continue

            # Rebuild the same bar path the single-day backtest uses
            path, day0, used_intraday = [], [], False
            epoch = a.get("timestamp")
            if INTRADAY_FILL and epoch:
                raw = _fetch_option_intraday(occ, date)
                try:
                    ae = float(epoch)
                    day0 = [b for b in raw if b["ts"] >= ae]
                except Exception:
                    day0 = []
                if day0:
                    used_intraday = True
                    path.extend(day0)
            start = date
            if used_intraday:
                try:
                    d0 = datetime.strptime(date, "%Y-%m-%d").date()
                    start = (d0 + timedelta(days=1)).strftime("%Y-%m-%d")
                except Exception:
                    start = date
            path.extend(_fetch_option_daily(occ, start, exp_iso))
            if not path:
                continue

            # Fill index (same rule as the sim)
            fill_idx = None
            for j, b in enumerate(path):
                if b["low"] > 0 and b["low"] <= entry:
                    fill_idx = j
                    break
            if fill_idx is None:
                continue   # never filled — contributes nothing to any combo

            trades.append({
                "date":       date,
                "ticker":     a["ticker"],
                "entry":      entry,
                "flow_price": flow,
                "window":     path[fill_idx:],
            })

        if i % 5 == 0 and i < len(days):
            send(f"… {i}/{len(days)} days streamed ({len(trades)} filled trades)",
                 bot, chat)

    return trades


def _occ_tail(a: dict) -> str:
    """Rebuild the OCC tail (YYMMDD + C/P + strike) from a result dict."""
    try:
        mm, dd, yy = a["expiry"].split("/")
        cp = "C" if a["direction"] == "call" else "P"
        strike_int = int(round(float(a["strike"]) * 1000))
        return f"{yy}{mm}{dd}{cp}{strike_int:08d}"
    except Exception:
        return ""


def _build_workbook(rows: list, trades: list, start: str, end: str) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "Sweep"
    hdr = ["TP1 %", "TP2 %", "Trail %", "Trades", "Total P/L $",
           "Avg P/L %", "Win Rate %", "TP1 Hit %", "TP2 Hit %",
           "Days", "Green Days", "Green Day %", "Best Day $", "Worst Day $",
           "Best-Day Concentration %"]
    ws.append(hdr)
    hf, hfill, ctr = (Font(bold=True, color="FFFFFF"),
                      PatternFill("solid", fgColor="1F3864"),
                      Alignment(horizontal="center"))
    for c in range(1, len(hdr) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = hf; cell.fill = hfill; cell.alignment = ctr

    ranked = sorted(rows, key=lambda x: -x["total_usd"])
    for r in ranked:
        ws.append([r["tp1_pct"], r["tp2_pct"], r["trail_pct"], r["trades"],
                   r["total_usd"], r["avg_pct"], r["win_rate"],
                   r["t1_rate"], r["t2_rate"],
                   r["days"], r["green_days"], r["green_day_pct"],
                   r["best_day_usd"], r["worst_day_usd"], r["concentration"]])

    # Highlight the best row
    if rows:
        for c in range(1, len(hdr) + 1):
            ws.cell(row=2, column=c).font = Font(bold=True)
    for col, w in zip("ABCDEFGHIJKLMNO", (8, 8, 9, 8, 13, 11, 11, 11, 11, 7, 11, 12, 12, 12, 22)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"

    # Per-day P/L for the top 3 combos — is the edge consistent or one lucky day?
    if ranked:
        ws_d = wb.create_sheet("Top3 Daily")
        top3 = ranked[:3]
        ws_d.append(["Date"] + [f"TP1 {r['tp1_pct']:.0f}/TP2 {r['tp2_pct']:.0f}/TR {r['trail_pct']:.0f}"
                                for r in top3])
        for c in range(1, len(top3) + 2):
            cell = ws_d.cell(row=1, column=c)
            cell.font = hf; cell.fill = hfill; cell.alignment = ctr
        all_dates = sorted({d for r in top3 for d in r["by_day"]})
        for d in all_dates:
            ws_d.append([d] + [r["by_day"].get(d, 0.0) for r in top3])
        ws_d.append([])
        ws_d.append(["TOTAL"] + [r["total_usd"] for r in top3])
        for c in range(1, len(top3) + 2):
            ws_d.cell(row=ws_d.max_row, column=c).font = Font(bold=True)
        for col, w in zip("ABCD", (12, 24, 24, 24)):
            ws_d.column_dimensions[col].width = w
        ws_d.freeze_panes = "A2"

    # Trades tab: the fills the sweep ran on
    ws2 = wb.create_sheet("Trades")
    ws2.append(["Date", "Ticker", "Entry", "Flow Price", "Bars"])
    for c in range(1, 6):
        cell = ws2.cell(row=1, column=c)
        cell.font = hf; cell.fill = hfill; cell.alignment = ctr
    for t in trades:
        ws2.append([t["date"], t["ticker"], t["entry"], t["flow_price"], len(t["window"])])
    for col, w in zip("ABCDE", (12, 9, 9, 11, 7)):
        ws2.column_dimensions[col].width = w

    os.makedirs("/tmp", exist_ok=True)
    path = f"/tmp/preset_sweep_{start}_to_{end}.xlsx"
    wb.save(path)
    return path


def _run_sweep_thread(start: str, end: str, bot: str, chat: str):
    from sms import send_telegram, send_telegram_document
    from preset_backtest_range import _weekdays_in_range

    days = _weekdays_in_range(start, end)
    if not days:
        send_telegram(f"No weekdays in {start} → {end}.", bot, chat)
        return

    tp1s, tp2s, trails = _grid()
    combos = len(tp1s) * len(tp2s) * len(trails)

    send_telegram(
        f"🧪 Parameter sweep: {start} → {end}\n"
        f"{len(days)} trading days | {combos} combinations\n"
        f"TP1: {[f'{int(p*100)}%' for p in tp1s]}\n"
        f"TP2: {[f'{int(p*100)}%' for p in tp2s]}\n"
        f"Trail: {[f'{int(p*100)}%' for p in trails]}\n"
        f"Streaming days first (~{len(days)*6} min), then scoring.",
        bot, chat)

    trades = _collect_trades(days, send_telegram, bot, chat)
    if not trades:
        send_telegram("No filled trades in that range — nothing to sweep.", bot, chat)
        return

    send_telegram(f"📊 {len(trades)} filled trades collected. Scoring {combos} combos…",
                  bot, chat)

    rows = []
    for t1 in tp1s:
        for t2 in tp2s:
            if t2 <= t1:
                continue          # TP2 must be beyond TP1
            for tr in trails:
                rows.append(_score_combo(trades, t1, t2, tr))

    if not rows:
        send_telegram("No valid combinations (TP2 must exceed TP1).", bot, chat)
        return

    best = max(rows, key=lambda x: x["total_usd"])
    cur  = next((r for r in rows
                 if r["tp1_pct"] == 101 and r["tp2_pct"] == 201 and r["trail_pct"] == 75),
                None)

    msg = [f"🧪 Sweep complete: {start} → {end}",
           f"{len(trades)} filled trades | {len(rows)} combos",
           "",
           f"🥇 BEST: TP1 {best['tp1_pct']:.0f}% / TP2 {best['tp2_pct']:.0f}% / "
           f"Trail {best['trail_pct']:.0f}%",
           f"   ${best['total_usd']:+,.0f} | {best['win_rate']:.0f}% win | "
           f"TP1 hit {best['t1_rate']:.0f}% | TP2 hit {best['t2_rate']:.0f}%"]
    if cur:
        msg += ["",
                f"📍 YOUR CURRENT (101/201/75): ${cur['total_usd']:+,.0f} | "
                f"{cur['win_rate']:.0f}% win | TP2 hit {cur['t2_rate']:.0f}%",
                f"   Delta vs best: ${best['total_usd'] - cur['total_usd']:+,.0f}"]
    msg += ["",
            f"   Consistency: green on {best['green_days']}/{best['days']} days "
            f"({best['green_day_pct']:.0f}%)",
            f"   Best day ${best['best_day_usd']:+,.0f} = {best['concentration']:.0f}% of total",
            "",
            "⚠️ If one day is most of the total, the edge is fragile.",
            "Full grid + Top3 daily breakdown in the Excel."]
    send_telegram("\n".join(msg), bot, chat)

    try:
        path = _build_workbook(rows, trades, start, end)
    except Exception as e:
        send_telegram(f"❌ Excel build error: {e}", bot, chat)
        return

    send_telegram_document(path, bot, chat,
                           caption=f"Parameter sweep {start} → {end}: "
                                   f"{len(rows)} combos over {len(trades)} trades.")
    try:
        os.remove(path)
    except Exception:
        pass


def start_sweep(start: str, end: str, bot: str, chat: str):
    """Validate and launch. Returns (ok, message)."""
    if not (re.match(r'^\d{4}-\d{2}-\d{2}$', start) and re.match(r'^\d{4}-\d{2}-\d{2}$', end)):
        return False, "Both dates must be YYYY-MM-DD. e.g. /preset_sweep 2026-06-01 2026-06-30"
    try:
        d0 = datetime.strptime(start, "%Y-%m-%d").date()
        d1 = datetime.strptime(end,   "%Y-%m-%d").date()
    except Exception:
        return False, "Invalid date(s)."
    if d1 < d0:
        return False, "End date is before start date."
    if (d1 - d0).days + 1 > MAX_RANGE_DAYS:
        return False, f"Range too large. Max {MAX_RANGE_DAYS} days."

    threading.Thread(target=_run_sweep_thread, args=(start, end, bot, chat),
                     daemon=True, name=f"preset_sweep_{start}").start()
    return True, ""
